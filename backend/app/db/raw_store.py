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
  M1/Preprod  → local filesystem (this module, zero new service)
  Production  → MinIO: replace _write_sync / read_raw with boto3 S3 calls
  Mainnet     → Cloudflare R2 / Backblaze B2 / AWS S3 (same boto3 client)
"""

import asyncio
import gzip
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
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


