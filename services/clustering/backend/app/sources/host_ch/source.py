"""``HostChainSource`` — the ChainSource the engine uses as the TMS clustering
sidecar.

The host TMS has already ingested the chain into ``HOST_CLICKHOUSE_DB``
(``tms_analytics``); this source therefore never downloads a transaction. It
provides:

- ``metadata`` for contract onboarding, read from the host's
  ``address_transactions`` (existence) and the address header (script-ness).
- ``tx_hash_pages`` for discovery, paging the watched address's hashes from
  ``address_transactions`` by slot (used by the scored-watermark feed; the
  canonical fit runs with ``reprocess=True`` and never discovers here).

``fetch_tx`` raises: in the integrated deployment the engine never fetches an
individual transaction (the feature reads come from the host tables via
``HostBackedRepo``, and inserts are no-ops), so a call here is a wiring bug, not
a normal path. v1 supports address/script targets only (the host indexes by
address; policy targets are rejected).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Any

from app.config import Settings
from app.registry.bech32 import _decode_address_bytes
from app.sources.base import (
    ChainSource,
    DiscoveryMode,
    DiscoveryWindow,
    NormalizedTx,
    SourceError,
    SourceNotFound,
    TargetMeta,
)
from app.storage.clickhouse.base import connect

# Shelley address header high-nibble values whose PAYMENT credential is a script
# (base-script/script, pointer-script, enterprise-script): types 1, 3, 5, 7.
# (Even types carry a key payment credential.) See CIP-19 / the bech32 module.
_SCRIPT_PAYMENT_TYPES = frozenset({1, 3, 5, 7})

# Discovery page size: tx_hashes per yielded page. Bounds the result set per
# round-trip the same way any page-based adapter's page size does.
_PAGE = 1000

# Chars of the target address echoed in error messages: enough to recognise the
# address in a client-facing log without dumping the full ~100-char bech32.
_TARGET_PREVIEW = 24

# ClickHouse min()/max() over an empty set return the DateTime zero value
# (1970-01-01), not NULL. Cardano genesis is 2017, so any real floor is well
# after this; a floor in this year therefore means "no rows for the network".
_EPOCH_YEAR = 1970


def _payment_is_script(address: str) -> bool:
    raw = _decode_address_bytes(address)
    if not raw:
        return False
    return (raw[0] >> 4) in _SCRIPT_PAYMENT_TYPES


class HostChainSource:
    """ChainSource backed by the host TMS's ClickHouse (read-only, no download)."""

    # Data is already in the host tables (read via HostBackedRepo); onboarding
    # reads features directly and must never discover+download (fetch_tx is a
    # hard error here). The pipeline keys off this to skip the download path.
    host_backed = True

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._host_db = settings.host_clickhouse_db
        self._network = settings.cardano_network
        self._client: Any = None

    async def __aenter__(self) -> ChainSource:
        self._client = connect(self._settings)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    async def metadata(self, target: str, target_type: str) -> TargetMeta:
        if target_type != "address":
            raise SourceNotFound(
                "host_ch v1 supports address/script targets only "
                "(the host indexes transactions by address, not by policy id)",
                client_safe=True,
            )
        rows = self._client.query(
            f"SELECT count() FROM {self._host_db}.address_transactions "
            "WHERE network = {net:String} AND address = {tgt:String}",
            parameters={"net": self._network, "tgt": target},
        ).result_rows
        count = int(rows[0][0]) if rows else 0
        if count == 0:
            raise SourceNotFound(self._no_txs_message(target), client_safe=True)
        # Identity is read from the address header; balance/token enrichment is
        # left to the host's own views (display-only here, not needed to fit).
        return {
            "exists": True,
            "is_script": _payment_is_script(target),
            "script_type": "",
            "balance_lovelace": 0,
            "asset_count": 0,
            "sample_tokens": "[]",
        }

    def _no_txs_message(self, target: str) -> str:
        """Explain a zero-row onboarding without misleading the client.

        A script that exists on-chain but has no rows here is rarely "not on
        chain": far more often this instance simply has not synced back to the
        address's last activity (a fresh deployment syncs tip-forward, no
        historical backfill) or those rows aged out of retention. Report how far
        back the instance has indexed this network so the operator can tell
        "before our data begins" apart from "genuinely never active"."""
        floor = self._network_data_floor()
        head = (
            f"address {target[:_TARGET_PREVIEW]}… has no transactions in this "
            f"instance's {self._network} data"
        )
        if floor is None:
            return f"{head}; no {self._network} transactions have been indexed yet"
        return (
            f"{head}; this instance has indexed {self._network} activity only back "
            f"to {floor:%Y-%m-%d} (the address was likely last active before then, "
            "or its rows aged out of retention)"
        )

    def _network_data_floor(self) -> datetime | None:
        """Earliest transaction time this instance has indexed for the network,
        or None when it holds none. Runs only on the zero-row error path, so the
        full-partition ``min(timestamp)`` scan is off the hot path."""
        rows = self._client.query(
            f"SELECT min(timestamp) FROM {self._host_db}.address_transactions "
            "WHERE network = {net:String}",
            parameters={"net": self._network},
        ).result_rows
        earliest = rows[0][0] if rows else None
        if earliest is None or earliest.year <= _EPOCH_YEAR:
            return None
        return earliest

    async def tx_hash_pages(
        self,
        *,
        address: str | None,
        policy_id: str | None,
        cursor: str | None,
        mode: DiscoveryMode,
        max_items: int | None,
        from_block: str | None,
        to_block: str | None,
        window: DiscoveryWindow,
        progress: Callable[[str], None],
    ) -> AsyncIterator[tuple[str, list[str]]]:
        """Page the watched address's tx_hashes from the host's
        ``address_transactions`` by ascending slot. The cursor is ``slot:<n>``;
        ``mode='tip'`` re-covers from the cursor's slot (idempotent: the host
        rows are append-only and the engine classifies each hash once)."""
        if not address:
            raise SourceNotFound(
                "host_ch discovery requires an address target", client_safe=True
            )
        from_slot = 0
        if cursor and mode != "restart" and cursor.startswith("slot:"):
            from_slot = int(cursor.split(":", 1)[1])
        seen = 0
        while True:
            rows = self._client.query(
                f"""
                SELECT max_slot AS slot, tx_hash FROM (
                    -- Alias the aggregate to a NAME DISTINCT from its source
                    -- column (`slot`): aliasing `max(slot) AS slot` shadows the
                    -- source column and trips ClickHouse Code 184 on 26.x.
                    SELECT tx_hash, max(slot) AS max_slot
                    FROM {self._host_db}.address_transactions
                    WHERE network = {{net:String}} AND address = {{addr:String}}
                      AND slot >= {{from_slot:UInt64}}
                    GROUP BY tx_hash
                ) ORDER BY max_slot, tx_hash LIMIT {{lim:UInt32}}
                """,
                parameters={
                    "net": self._network,
                    "addr": address,
                    "from_slot": from_slot,
                    "lim": _PAGE,
                },
            ).result_rows
            if not rows:
                return
            hashes = [str(r[1]) for r in rows]
            max_slot = int(rows[-1][0])
            seen += len(hashes)
            progress(f"discovered {seen} tx hashes (slot {max_slot})")
            # Advance past this page; +1 slot avoids re-yielding the boundary's
            # already-emitted hashes (a tx is uniquely the max-slot row here).
            yield f"slot:{max_slot}", hashes
            if max_items is not None and seen >= max_items:
                return
            if len(hashes) < _PAGE:
                return
            from_slot = max_slot + 1

    async def fetch_tx(self, target: str, target_type: str, tx_hash: str) -> NormalizedTx:
        raise SourceError(
            "host_ch does not fetch individual transactions: in the integrated "
            "sidecar the engine reads features from the host tables via "
            "HostBackedRepo and runs with reprocess=True / direct classify. A "
            "call here means the download path was taken unintentionally."
        )
