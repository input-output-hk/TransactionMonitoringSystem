"""Local filesystem raw transaction store.

Writes full Ogmios JSON for each transaction as gzip-compressed files.
Path: {RAW_STORE_PATH}/{prefix}/{network}/{YYYYMMDD}/{tx_hash[:2]}/{tx_hash}.json.gz

The 2-hex-char shard directory limits each leaf to ~11,700 files at Mainnet
scale (3M txs/day ÷ 256 buckets).  It also distributes S3/MinIO PUTs across
index partitions, avoiding hot-prefix throttling.

prefix values:
  confirmed/  — transactions confirmed on-chain (chain sync path)
  mempool/    — transactions first seen in mempool (mempool monitor path)

Write-once: existing files are skipped (safe on ingestion replay after restart).
Async via dedicated 2-worker thread pool — event loop is never blocked.

Upgrade path:
  Preprod     : local filesystem (this module, zero new service)
  Production  : MinIO, replace _write_sync / read_raw with boto3 S3 calls
  Mainnet     : Cloudflare R2 / Backblaze B2 / AWS S3 (same boto3 client)
"""

import asyncio
import gzip
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from app.config import settings

logger = logging.getLogger(__name__)

_PREFIX_CONFIRMED = "confirmed"
_PREFIX_MEMPOOL = "mempool"

_executor: Optional[ThreadPoolExecutor] = None


def init_store():
    """Create base directory and start the write executor."""
    global _executor
    os.makedirs(settings.RAW_STORE_PATH, exist_ok=True)
    _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="raw_store")
    logger.info(f"Raw store initialized at {settings.RAW_STORE_PATH}")


def shutdown_executor():
    """Shut down the thread pool gracefully."""
    global _executor
    if _executor:
        _executor.shutdown(wait=True)
        _executor = None


def _build_path(prefix: str, network: str, tx_hash: str, date: datetime) -> str:
    """Return the full file path for a raw transaction blob.

    Path: {RAW_STORE_PATH}/{prefix}/{network}/{YYYYMMDD}/{shard}/{tx_hash}.json.gz

    The shard directory is the first 2 hex characters of tx_hash (256 buckets).
    At Mainnet scale (~3M txs/day) this limits each leaf directory to ~11,700
    files — well within ext4/XFS/APFS performance bounds.  For S3/MinIO the
    shard prefix distributes PUTs across multiple index partitions, avoiding
    the hot-prefix throttling that occurs when millions of keys share a prefix.
    """
    # Validate tx_hash to prevent path traversal (Ogmios data is untrusted)
    if not tx_hash or not all(c in "0123456789abcdef" for c in tx_hash):
        logger.warning(f"Invalid tx_hash for raw store: {tx_hash[:20]!r}")
        return ""
    day_dir = date.strftime("%Y%m%d")
    shard = tx_hash[:2]  # 256 uniform buckets (tx_hash is SHA-256 derived)
    dir_path = os.path.join(settings.RAW_STORE_PATH, prefix, network, day_dir, shard)
    return os.path.join(dir_path, f"{tx_hash}.json.gz")


def _write_sync(prefix: str, network: str, tx_hash: str,
                data: Dict[str, Any], ts: datetime):
    """Atomic gzip write via temp-file + rename. Runs in the thread pool.

    Writes to a .tmp sibling first, then os.replace() atomically renames it to
    the final path.  This prevents a corrupt partial file surviving a crash:
    - If the process dies before os.replace(), only the .tmp file is left and
      the final path does not exist, so the write is retried on the next replay.
    - If os.replace() completes, the file is a valid gzip on every reader.
    os.replace() is guaranteed atomic on POSIX (rename(2) syscall).
    """
    path = _build_path(prefix, network, tx_hash, ts)
    if not path:
        return
    if os.path.exists(path):
        return  # write-once: skip on ingestion replay after restart
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    try:
        with gzip.open(tmp_path, "wt", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_path, path)  # atomic on POSIX
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


async def _write_async(prefix: str, network: str, tx_hash: str,
                       data: Dict[str, Any], ts: datetime):
    """Non-blocking write: submits _write_sync to the thread pool."""
    if not settings.RAW_STORE_ENABLED or _executor is None:
        return
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_executor, _write_sync, prefix, network, tx_hash, data, ts)


async def write_confirmed(network: str, tx_hash: str,
                          raw_data: Dict[str, Any], ts: datetime):
    """Write a confirmed transaction's full Ogmios payload."""
    await _write_async(_PREFIX_CONFIRMED, network, tx_hash, raw_data, ts)


async def write_mempool(network: str, tx_hash: str,
                        tx_data: Dict[str, Any], ts: datetime):
    """Write a mempool-observed transaction's full Ogmios payload."""
    await _write_async(_PREFIX_MEMPOOL, network, tx_hash, tx_data, ts)


def prune_old_days(retention_days: int) -> int:
    """Delete day directories older than the retention window (opt-in).

    Layout {prefix}/{network}/{YYYYMMDD}/... makes retention a directory
    walk: whole days are removed at once. Returns the number of day
    directories removed.

    Refuses to prune while RAW_DATA_MAX_BYTES > 0: with capped ClickHouse
    payloads the raw store is the ONLY full copy of large transactions and
    the engine's raw_data fallback depends on it; pruning would make those
    transactions permanently unscorable at full fidelity.
    """
    if retention_days <= 0:
        return 0
    from app.config import settings as _settings
    if _settings.RAW_DATA_MAX_BYTES > 0:
        logger.warning(
            "Raw-store retention skipped: RAW_DATA_MAX_BYTES > 0 makes the "
            "raw store load-bearing for the engine's raw_data fallback. "
            "Set RAW_DATA_MAX_BYTES=0 (full payloads in ClickHouse) before "
            "enabling RAW_STORE_RETENTION_DAYS."
        )
        return 0
    import shutil
    from datetime import timezone as _tz
    cutoff = int(
        (datetime.now(_tz.utc) - timedelta(days=retention_days)).strftime("%Y%m%d")
    )
    removed = 0
    for prefix in (_PREFIX_CONFIRMED, _PREFIX_MEMPOOL):
        prefix_dir = os.path.join(settings.RAW_STORE_PATH, prefix)
        if not os.path.isdir(prefix_dir):
            continue
        for network in os.listdir(prefix_dir):
            net_dir = os.path.join(prefix_dir, network)
            if not os.path.isdir(net_dir):
                continue
            for day in os.listdir(net_dir):
                if not (len(day) == 8 and day.isdigit() and int(day) < cutoff):
                    continue
                try:
                    shutil.rmtree(os.path.join(net_dir, day))
                    removed += 1
                except OSError as e:
                    logger.warning(f"Raw-store prune failed for {network}/{day}: {e}")
    if removed:
        logger.info(f"Raw-store retention: removed {removed} day directories")
    return removed


def read_confirmed(network: str, tx_hash: str, ts: datetime) -> Optional[Dict[str, Any]]:
    """Read back a transaction's full Ogmios payload from the raw store.

    ``ts`` is the transaction row's ``timestamp``: the ingester passes the
    same ``now`` to both the ClickHouse row and ``write_confirmed``, so the
    day directory is derivable. Probe order covers clock-edge skew and
    mempool-only observation:

      1. ``confirmed/{YYYYMMDD(ts)}``
      2. ``confirmed/{YYYYMMDD(ts +/- 1 day)}`` (midnight-boundary writes)
      3. ``mempool/`` same three days (tx observed in mempool, confirmed
         payload write failed)

    Returns the parsed dict, or None when no blob is found or parseable.
    Synchronous by design: the analysis engine calls it from the ClickHouse
    executor thread, never from the event loop.
    """
    candidates = []
    for prefix in (_PREFIX_CONFIRMED, _PREFIX_MEMPOOL):
        for day_offset in (0, -1, 1):
            candidates.append(
                _build_path(
                    prefix, network, tx_hash,
                    ts + timedelta(days=day_offset),
                )
            )
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError, EOFError) as e:
            logger.warning(f"Raw store read failed for {path}: {e}")
    return None


