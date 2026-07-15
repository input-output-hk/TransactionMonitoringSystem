"""Kupo HTTP client: the address→transaction index the base node lacks.

A cardano-node (via Ogmios) can stream blocks, resolve a UTxO by reference, and
watch the mempool, but it cannot answer "which transactions touched this
address" : that needs an index. Kupo (github.com/CardanoSolutions/kupo),
co-located with the node, maintains exactly that index.

This client asks Kupo only for *where* an address's transactions live on-chain
(their block points), newest-first. It deliberately does NOT trust Kupo for the
transaction bodies: the address backfill (see ``address_backfill.py``) re-fetches
those blocks through Ogmios chain-sync and the canonical ``ogmios_parser`` so a
backfilled row is byte-for-byte identical to a live-synced one (fee, size,
redeemers, chain-time), preserving detection fidelity.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import settings


@dataclass(frozen=True, slots=True)
class TxPoint:
    """A transaction and the block that contains it, as Kupo reports it. A
    transaction lives in exactly one block, so ``(slot, header_hash)`` is a
    stable coordinate for re-fetching it via chain-sync."""

    tx_hash: str
    slot: int
    header_hash: str


@dataclass(frozen=True, slots=True)
class ChainPoint:
    """A chain intersection point (Ogmios ``findIntersection`` shape is
    ``{"slot": ..., "id": header_hash}``)."""

    slot: int
    header_hash: str


class KupoError(RuntimeError):
    """A Kupo request failed (network, HTTP status, or malformed body)."""


class KupoUnavailable(KupoError):
    """Kupo is not configured (``KUPO_URL`` empty). Callers treat this as
    'backfill unavailable', distinct from a transient request failure."""


def _point_fields(point: dict | None) -> tuple[int, str] | None:
    """``(slot, header_hash)`` from a Kupo ``created_at``/``spent_at``/checkpoint
    object, or None when either field is absent (a partial point is unusable)."""
    if not point:
        return None
    slot = point.get("slot_no")
    header_hash = point.get("header_hash")
    if slot is None or not header_hash:
        return None
    return int(slot), header_hash


class KupoClient:
    """Async client over Kupo's HTTP API. One short-lived ``httpx`` request per
    call; Kupo is co-located with the node so latency is LAN-local."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout_seconds: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        url = (base_url if base_url is not None else settings.KUPO_URL).rstrip("/")
        if not url:
            raise KupoUnavailable("KUPO_URL is not configured; address backfill is unavailable")
        self._base = url
        self._timeout = httpx.Timeout(
            timeout_seconds if timeout_seconds is not None else settings.KUPO_TIMEOUT_SECONDS
        )
        # Injected only by tests (httpx.MockTransport); None uses the real network.
        self._transport = transport

    async def _get_json(self, path: str, params: dict[str, str] | None = None) -> object:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(
                    f"{self._base}{path}",
                    params=params,
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError as exc:
            raise KupoError(f"Kupo request to {path} failed: {exc}") from exc

    async def health(self) -> dict:
        """Kupo's health/status JSON (``connection_status``,
        ``most_recent_checkpoint``, ``most_recent_node_tip``, ...)."""
        data = await self._get_json("/health")
        if not isinstance(data, dict):
            raise KupoError("Kupo /health did not return an object")
        return data

    async def address_tx_points(
        self, address: str, *, max_txs: int | None = None
    ) -> list[TxPoint]:
        """The block points of the transactions touching ``address``, newest
        first, capped at ``max_txs`` distinct transactions.

        Both sides of the address's activity are included: the match's own
        ``transaction_id`` (the tx that *created* an output at the address) and
        ``spent_at.transaction_id`` (the tx that later *spent* it). Their union is
        the address's full transaction set : exactly what ``address_transactions``
        records. A transaction occupies one block, so the first point seen per
        hash is canonical and later duplicates are ignored.
        """
        matches = await self._get_json(
            f"/matches/{address}", params={"order": "most_recent_first"}
        )
        if not isinstance(matches, list):
            raise KupoError(f"Kupo /matches/{address} did not return a list")

        by_hash: dict[str, TxPoint] = {}
        for match in matches:
            if not isinstance(match, dict):
                continue
            self._collect(by_hash, match.get("transaction_id"), match.get("created_at"))
            spent = match.get("spent_at")
            if isinstance(spent, dict):
                self._collect(by_hash, spent.get("transaction_id"), spent)

        # Sort by slot descending; break ties on tx_hash so the ordering (and the
        # max_txs cut) is deterministic for a given set of matches.
        points = sorted(by_hash.values(), key=lambda p: (p.slot, p.tx_hash), reverse=True)
        if max_txs is not None:
            points = points[:max_txs]
        return points

    @staticmethod
    def _collect(acc: dict[str, TxPoint], tx_hash: object, point: dict | None) -> None:
        if not isinstance(tx_hash, str) or not tx_hash:
            return
        fields = _point_fields(point)
        if fields is None:
            return
        slot, header_hash = fields
        acc.setdefault(tx_hash, TxPoint(tx_hash, slot, header_hash))

    async def ancestor_point(self, before_slot: int) -> ChainPoint | None:
        """A chain point strictly before ``before_slot`` to intersect at, so a
        forward chain-sync walk *re-covers* the block at ``before_slot`` (an
        Ogmios ``findIntersection`` positions the read head AT the point, so the
        target block is only delivered when the intersection is its ancestor).

        Kupo's ``/checkpoints/{slot}`` returns the nearest checkpoint at or before
        the slot; asking for ``before_slot - 1`` yields one strictly earlier.
        Returns None when Kupo has no checkpoint that far back (the caller then
        falls back to origin)."""
        if before_slot <= 0:
            return None
        data = await self._get_json(f"/checkpoints/{before_slot - 1}")
        if not isinstance(data, dict):
            return None
        fields = _point_fields(data)
        if fields is None:
            return None
        slot, header_hash = fields
        return ChainPoint(slot, header_hash)
