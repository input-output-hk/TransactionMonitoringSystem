"""Optional pre-deployment history backfill for watched contracts.

The host TMS syncs tip-forward, so a watched contract's activity from before
the deployment never reaches the host tables. When ``HISTORY_SOURCE`` is set
(host_ch deployments only; see the startup guards), every onboarded contract
automatically backfills up to its per-contract cap from a secondary source
before its first fit:

- ``blockfrost`` downloads the history into the ENGINE's own raw tables (the
  hybrid repo unions them into every read; see storage/clickhouse/hybrid.py);
- ``kupo`` triggers the HOST's own full-fidelity ``POST /api/v1/backfill``
  (rows land in the host tables, so plain host-backed reads pick them up), in
  trigger-and-continue style: the host job can run for many minutes and the
  sidecar has a single job worker, so nothing here ever waits on it.

Everything in this module is deliberately NON-FATAL to the fit: a deferred or
rate-limited backfill returns a status, never raises, because the fit can and
should proceed on the host's tip-forward data alone. The shared flavor shell
(``_HistoryFlavor.run``) enforces this at the boundary itself — any unexpected
exception (a ClickHouse hiccup in the boundary aggregates, a non-JSON host
reply) degrades to ``deferred`` — so a backfill bug can never fail the
onboarding job that carries it. Resume is cursor-driven: ``run()`` is cheap
when there is nothing to do (one cursor read), so the online classify tick
calls it every pass until the history completes.

THE IMMUTABILITY BOUNDARY (the invariant everything else rests on): backfilled
rows are only persisted strictly BELOW ``least(target's earliest host slot,
host tip - safety window)``. The host's chain-rollback purge never touches the
engine's raw tables, so a backfilled row near the tip could become a fork
ghost that re-enters every fit; bounding the backfill to slots no rollback can
reach makes the local rows immutable by construction, and (as a corollary)
disjoint from the host rows — which is what the hybrid repo's reads and the
publish filter assume.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from app.config import Settings
from app.ingest.ingester import ProgressFn, ingest
from app.models import AssetRecord, TxRecord, UtxoRecord
from app.sources.base import ChainSource, SourceNotFound, TargetMeta
from app.sources.host_ch.source import payment_is_script
from app.storage.clickhouse import ClickHouseRepo
from app.storage.clickhouse.base import connect

logger = logging.getLogger(__name__)

# Cardano's security parameter: the protocol cannot reorganize blocks deeper
# than this many blocks (Ouroboros Praos common-prefix guarantee). Cited by
# name (not inlined) below since both rollback-safety constants derive from it.
CARDANO_SECURITY_PARAMETER_K = 2_160

# Cardano's stability window 3k/f = 3 * 2160 / 0.05 = 129,600 slots (~36h on
# mainnet, where 1 slot = 1s): 3k/f slots is the settlement bound within which
# k blocks are guaranteed to have been produced. History at or below
# (tip - this window) is immutable, so a backfilled row there can never be
# orphaned by a rollback.
ROLLBACK_SAFETY_SLOTS = 129_600
# The block-height twin of the slot bound above: 3k = 6,480 blocks is the same
# ~36h wall-clock at the active-slot rate f = 0.05 (one block per ~20 slots).
# It must match the slot bound's TIME SPAN, not k alone: a shorter height
# bound would let the bounded walk fetch transactions whose slots sit above
# the slot floor, burning the per-contract cap on rows the slot guard drops.
ROLLBACK_SAFETY_BLOCKS = 3 * CARDANO_SECURITY_PARAMETER_K

# The history flavor's OWN cursor tag: distinct from BlockfrostSource.name
# ("blockfrost", the PRIMARY chain source's tag). A bounded history walk and an
# unbounded CHAIN_SOURCE=blockfrost primary walk would otherwise share the
# identical tag and cursor-string shape, so switching CHAIN_SOURCE from
# blockfrost-primary to host_ch+HISTORY_SOURCE=blockfrost on an un-wiped volume
# could let a leftover unbounded cursor satisfy the skip-fast check below and
# silently skip the bounded backfill (docs/operations.md warns operators to
# wipe first; this tag makes the two cursor spaces disjoint regardless).
_BLOCKFROST_HISTORY_SOURCE_TAG = "blockfrost_history"

HistoryStatus = Literal["completed", "rate_limited", "deferred", "pending", "skipped"]


@dataclass(slots=True)
class HistoryResult:
    """Outcome of one history-backfill attempt.

    ``completed``   the history is fully persisted (or verifiably done);
    ``rate_limited`` the walk stopped on provider quota, cursor saved — the
                     next classify tick resumes it;
    ``pending``     a host-side job was triggered and is (or may still be)
                    running; the next classify tick checks on it;
    ``deferred``    could not run now (no host tip yet, host API unreachable,
                    policy target); retried on later ticks;
    ``skipped``     nothing to do (already complete, or the window is full so
                    downloaded history would never be read).
    """

    status: HistoryStatus
    txs_ingested: int = 0
    note: str = ""


@dataclass(slots=True)
class HostBoundary:
    """The per-target immutability boundary plus the host-side facts the
    pre-flight checks need (computed in one client session)."""

    floor_slot: int
    floor_height: int
    host_tx_count: int


class HistoryBackfill(Protocol):
    """One history-backfill flavor. ``run`` must be idempotent, cheap when
    there is nothing to do, and must never raise (statuses, not exceptions:
    a failed backfill must not fail the onboarding fit)."""

    async def run(
        self, *, target: str, target_type: str, max_txs: int, progress: ProgressFn
    ) -> HistoryResult: ...


def get_history_backfill(settings: Settings) -> HistoryBackfill | None:
    """The configured flavor, or None when history is disabled. Mirrors
    ``sources/factory.py``'s lazy-import discipline."""
    if not settings.history_enabled:
        return None
    if settings.history_source == "blockfrost":
        return BlockfrostHistory(settings)
    if settings.history_source == "kupo":
        return KupoHistory(settings)
    return None


def history_cap(contract_row: dict[str, Any] | None, settings: Settings) -> int:
    """The contract's effective history depth: its persisted per-contract cap
    (``requested_max_txs``), falling back to the configured default when the
    row carries none — 0 or absent, which is what host-backed feed onboarding
    leaves. The single definition of this resolution: the pipeline stage, the
    online resume gate and the API detail read must all agree on it, or the
    completeness checks drift from the walk they gate. The ceiling clamp is
    applied later by the flavor's ``run()``, so callers pass this straight
    through as ``max_txs``."""
    return int((contract_row or {}).get("requested_max_txs") or 0) or settings.history_max_txs


def host_history_boundary(settings: Settings, target: str) -> HostBoundary | None:
    """Compute the target's immutability boundary from the host tables.

    ``floor = least(target's earliest host slot/height, host tip - safety)``:
    the first term makes local and host rows disjoint, the second keeps the
    boundary out of rollback range even when the target's earliest host row is
    minutes old (a freshly-watched contract) or absent (host-unknown target,
    where the tip-minus-safety term stands alone). ``minIf(x, x > 0)`` skips
    the zero-slot rows the host's address_transactions MV stores for NULL-slot
    transactions (a plain min() would be poisoned to 0 by a single one).

    Returns None (defer) only when the host tip itself cannot be established
    (no host rows for the network yet, or the deployment is younger than the
    safety window so nothing is safely immutable). Not cached: it runs once
    per backfill attempt (rare) and legitimately moves as the host ingests;
    a stale cache would be a correctness bug for the immutability invariant.
    """
    client = connect(settings)
    try:
        host = settings.host_clickhouse_db
        params = {"net": settings.cardano_network, "tgt": target}
        tip_rows = client.query(
            f"SELECT ifNull(max(slot), 0), ifNull(max(block_height), 0) "
            f"FROM {host}.transactions WHERE network = {{net:String}}",
            parameters=params,
        ).result_rows
        tip_slot = int(tip_rows[0][0]) if tip_rows else 0
        tip_height = int(tip_rows[0][1]) if tip_rows else 0
        if tip_slot <= ROLLBACK_SAFETY_SLOTS or tip_height <= ROLLBACK_SAFETY_BLOCKS:
            return None  # no tip (or genesis-adjacent test chain): nothing provably immutable

        floor_rows = client.query(
            f"SELECT ifNull(minIf(slot, slot > 0), 0), uniqExact(tx_hash) "
            f"FROM {host}.address_transactions "
            f"WHERE network = {{net:String}} AND address = {{tgt:String}}",
            parameters=params,
        ).result_rows
        target_floor_slot = int(floor_rows[0][0]) if floor_rows else 0
        host_tx_count = int(floor_rows[0][1]) if floor_rows else 0
        height_rows = client.query(
            f"SELECT ifNull(minIf(block_height, block_height > 0), 0) "
            f"FROM {host}.transactions "
            f"WHERE network = {{net:String}} AND tx_hash IN ("
            f"  SELECT tx_hash FROM {host}.address_transactions"
            f"  WHERE network = {{net:String}} AND address = {{tgt:String}}"
            f")",
            parameters=params,
        ).result_rows
        target_floor_height = int(height_rows[0][0]) if height_rows else 0
        if target_floor_height == 0 and target_floor_slot > 0:
            # The height join can come back empty even though the slot term is
            # genuinely known: CH_RETENTION_DAYS_TRANSACTIONS and
            # CH_RETENTION_DAYS_IO (address_transactions) are independently
            # configurable, and the common direction is address_transactions
            # outliving transactions. Falling back to the loose tip-safety
            # height here (as if the target had no known history at all) would
            # let to_block drift far looser than floor_slot, burning the whole
            # per-contract cap on near-tip rows _SlotCappedRepo then drops.
            # Estimate from the tip's own observed height/slot ratio instead,
            # anchored to the slot term we DO trust.
            target_floor_height = round(target_floor_slot * tip_height / tip_slot)

        safety_slot = tip_slot - ROLLBACK_SAFETY_SLOTS
        safety_height = tip_height - ROLLBACK_SAFETY_BLOCKS
        floor_slot = min(target_floor_slot, safety_slot) if target_floor_slot else safety_slot
        floor_height = (
            min(target_floor_height, safety_height) if target_floor_height else safety_height
        )
        if floor_slot <= 0 or floor_height <= 0:
            return None
        return HostBoundary(
            floor_slot=floor_slot, floor_height=floor_height, host_tx_count=host_tx_count
        )
    finally:
        client.close()


class _SlotCappedRepo:
    """Delegating proxy over the inserting repo that drops any row at or above
    the boundary slot (and its utxo/asset rows). Belt-and-braces: the primary
    bound is the ``to_block`` discovery limit, but the boundary can legitimately
    move between the aggregate and the walk, and the disjointness invariant that
    publish filtering and rollback safety rest on must hold unconditionally.
    Relies on the ingester's flush order (transactions before utxos/assets) to
    know the dropped hashes before the dependent rows arrive; the dropped set is
    bounded by the per-contract cap."""

    def __init__(self, repo: ClickHouseRepo, floor_slot: int, progress: ProgressFn) -> None:
        self._repo = repo
        self._floor = floor_slot
        self._progress = progress
        self._dropped: set[str] = set()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repo, name)

    def insert_transactions(self, rows: list[TxRecord]) -> None:
        kept = []
        for r in rows:
            if int(r.slot) >= self._floor:
                self._dropped.add(str(r.tx_hash))
            else:
                kept.append(r)
        if len(kept) != len(rows):
            self._progress(
                f"dropped {len(rows) - len(kept)} tx(s) at/above the immutability "
                f"boundary (slot {self._floor})"
            )
        self._repo.insert_transactions(kept)

    def insert_utxos(self, rows: list[UtxoRecord]) -> None:
        self._repo.insert_utxos([r for r in rows if str(r.tx_hash) not in self._dropped])

    def insert_assets(self, rows: list[AssetRecord]) -> None:
        self._repo.insert_assets([r for r in rows if str(r.tx_hash) not in self._dropped])


# Returned by both flavors when the immutability boundary cannot be computed
# yet; one definition so the two retry paths cannot drift apart.
_BOUNDARY_DEFERRED_NOTE = (
    "host tip not established (or younger than the rollback safety window); retrying on later ticks"
)


class _HistoryFlavor:
    """Shared shell of a history-backfill flavor.

    The pieces every flavor must get right live here once: the address-only
    guard (there is no host policy index to backfill from), the per-contract
    ceiling clamp, and the never-raise enforcement — an unexpected exception
    anywhere in a flavor (cursor read, boundary aggregates, host-API parsing,
    repo construction) must degrade to a deferred attempt, never fail the
    onboarding/classify job that carries the stage. Subclasses implement only
    the flavor-specific ``_run`` and receive the already-clamped ``cap``."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def run(
        self, *, target: str, target_type: str, max_txs: int, progress: ProgressFn
    ) -> HistoryResult:
        if target_type != "address":
            return HistoryResult(
                "deferred", 0, "history backfill is address-only (no host policy index)"
            )
        try:
            return await self._run(
                target=target,
                target_type=target_type,
                cap=min(int(max_txs), self._settings.history_max_txs_ceiling),
                progress=progress,
            )
        except Exception:
            logger.exception("history backfill failed for %s", target[:24])
            return HistoryResult("deferred", 0, "history source error; see server logs")

    async def _run(
        self, *, target: str, target_type: str, cap: int, progress: ProgressFn
    ) -> HistoryResult:
        raise NotImplementedError


class BlockfrostHistory(_HistoryFlavor):
    """Blockfrost flavor: download the most recent N txs strictly below the
    boundary into the engine's own raw tables, via a directly-constructed base
    ``ClickHouseRepo`` — the request/worker repo is host-backed with no-op
    writes by design, and this is the one path that must write."""

    async def _run(
        self, *, target: str, target_type: str, cap: int, progress: ProgressFn
    ) -> HistoryResult:
        repo = ClickHouseRepo(self._settings)
        try:
            # Skip-fast FIRST: the common already-complete case must cost one
            # cursor read (the classify tick calls this every 30s). ``done`` is
            # deliberately NOT required: the ingester's max_reached leaves the
            # cursor done=0 with txs_seen == cap (so a RAISED cap can resume
            # from the stored page cursor), and that state is complete at the
            # CURRENT cap. A raised cap (txs_seen < cap) intentionally falls
            # through and resumes/re-walks wider — the resume and
            # recent-restart sizing invariants in the ingester handle it.
            cur = repo.get_cursor(target)
            if (
                cur
                and cur.get("source") == _BLOCKFROST_HISTORY_SOURCE_TAG
                and int(cur.get("txs_seen") or 0) >= cap
            ):
                return HistoryResult(
                    "skipped", int(cur["txs_seen"]), "history complete at this cap"
                )

            boundary = host_history_boundary(self._settings, target)
            if boundary is None:
                return HistoryResult("deferred", 0, _BOUNDARY_DEFERRED_NOTE)
            # Window-full pre-flight: once the host rows ALONE fill the rolling
            # window, downloaded history would be evicted from every read —
            # don't spend provider quota on permanently invisible rows. Fires
            # only at host_tx_count >= window (below that, history still
            # occupies the window's tail and is read by every fit). Marked done
            # at the current cap so later ticks skip-fast (the host count only
            # grows, so the window never frees up); a raised per-contract cap
            # falls through the guard and re-evaluates. A raised
            # CLUSTERING_WINDOW_TXS alone does NOT re-open a settled marker —
            # accepted: the host rows that filled the old window still fill the
            # new one's head, and the cap is the operator's re-open lever.
            window = int(self._settings.clustering_window_txs)
            if window > 0 and boundary.host_tx_count >= window:
                self._mark_done(repo, target, target_type, cap)
                return HistoryResult(
                    "skipped", 0, "window full; history would be evicted from every read"
                )

            # Shortfall gate (opt-in): a target the host already has a substantial
            # recent sample for needs no pre-deployment padding — the recent rows
            # anchor the fit on their own — so skip and spend no provider quota.
            # Distinct from the window-full skip above (a hard eviction limit);
            # this is a lower policy threshold. host_tx_count only grows, so mark
            # done for skip-fast like the window-full path — and, like it, the
            # marker is sticky: lowering the threshold later won't re-open a
            # settled contract (a raised per-contract cap does). 0 disables.
            min_host = int(self._settings.history_min_host_txs)
            if min_host > 0 and boundary.host_tx_count >= min_host:
                self._mark_done(repo, target, target_type, cap)
                return HistoryResult(
                    "skipped",
                    0,
                    f"host already has {boundary.host_tx_count} txs "
                    f"(>= {min_host}); backfill not needed",
                )

            progress(
                f"history boundary: slot < {boundary.floor_slot} "
                f"(block <= {boundary.floor_height - 1}), cap {cap}"
            )
            from app.blockfrost.source import BlockfrostSource

            capped = _SlotCappedRepo(repo, boundary.floor_slot, progress)
            try:
                async with BlockfrostSource(self._settings) as source:
                    # Instance-level override: ingest() tags the cursor with
                    # source.name, and this walk's cursor must never collide
                    # with a primary CHAIN_SOURCE=blockfrost walk's (see
                    # _BLOCKFROST_HISTORY_SOURCE_TAG above).
                    source.name = _BLOCKFROST_HISTORY_SOURCE_TAG
                    result = await ingest(
                        repo=capped,  # type: ignore[arg-type]  # delegating proxy over Repo
                        source=source,
                        address=target,
                        max_txs=cap,
                        recent=True,
                        # to_block is inclusive; the boundary block itself holds
                        # the target's earliest HOST row, so stop one below it.
                        to_block=str(boundary.floor_height - 1),
                        progress=progress,
                    )
            except SourceNotFound:
                # The address has no history upstream (or none below the bound):
                # nothing to backfill is a completed outcome, not an error. The
                # ingester wrote no cursor (discovery raised before any page),
                # so mark done here or every later tick would re-ask upstream.
                # (Any other exception is the run() wrapper's business.)
                self._mark_done(repo, target, target_type, cap)
                return HistoryResult("completed", 0, "no pre-deployment history upstream")

            if result.status == "rate_limited":
                # Cursor already persisted by the ingester; the next classify
                # tick resumes the walk.
                return HistoryResult(
                    "rate_limited", result.txs_ingested, "provider quota hit; will resume"
                )
            return HistoryResult("completed", result.txs_ingested)
        finally:
            repo.close()

    @staticmethod
    def _mark_done(repo: ClickHouseRepo, target: str, target_type: str, cap: int) -> None:
        """Record a terminal no-work outcome so later ticks skip-fast. txs_seen
        is set to the CAP (not 0): the skip guard is ``txs_seen >= cap``, and a
        later raised cap should re-open the question while the same cap stays
        settled."""
        repo.upsert_cursor(
            target,
            target_type,
            cursor="",
            last_tx_hash="",
            txs_seen=cap,
            done=True,
            source=_BLOCKFROST_HISTORY_SOURCE_TAG,
        )


# Cap on kupo trigger attempts per contract-and-cap: a host that keeps failing
# or returning degraded scans must not be re-driven through a full Kupo/Ogmios
# chain scan every 30s classify tick forever. Three attempts ride out a
# transient failure; a persistent one gives up loudly (WARN + a gave-up marker
# settled at the cap, surfaced as history_status "failed"; a raised cap
# re-opens the question with a fresh trigger budget).
_KUPO_MAX_TRIGGERS = 3

# Marker-cursor encoding for the kupo flavor: the cursor column is free-form
# (source-owned), so it carries the trigger-attempt counter, plus a gave-up
# flag when the attempt budget was exhausted (so the settled marker is
# distinguishable from a genuinely landed backfill in history_status), or a
# no-job flag when the last attempt failed before the host ever created a job
# (so the next tick re-triggers instead of polling a job that never started).
_KUPO_ATTEMPTS_PREFIX = "attempts:"
_KUPO_GAVE_UP_SUFFIX = ";gave_up"
_KUPO_NO_JOB_SUFFIX = ";no_job"


def _kupo_attempts(cur: dict[str, Any] | None) -> int:
    raw = (cur or {}).get("cursor") or ""
    if isinstance(raw, str) and raw.startswith(_KUPO_ATTEMPTS_PREFIX):
        # The counter is the first ';'-separated segment after the prefix (a
        # gave-up/no-job flag, when present, rides behind it).
        try:
            return int(raw[len(_KUPO_ATTEMPTS_PREFIX) :].split(";", 1)[0])
        except ValueError:
            return 0
    return 0


def _kupo_gave_up(cur: dict[str, Any] | None) -> bool:
    raw = (cur or {}).get("cursor") or ""
    return isinstance(raw, str) and raw.endswith(_KUPO_GAVE_UP_SUFFIX)


def _kupo_has_no_job(cur: dict[str, Any] | None) -> bool:
    """Whether the marker's last attempt failed before the host ever created a
    job (network-unreachable, 503, auth failure, or an unexpected status).
    Distinguishes that state from a genuinely outstanding host job (also
    ``done=False``): the former must re-trigger next tick, the latter must
    poll ``_check_host_job`` — polling a job that was never created would
    either 404 (harmless, but a wasted round trip) or, on the SAME class of
    host-unreachable/auth failure, return early as "pending" and skip the
    give-up counter check entirely, defeating ``_KUPO_MAX_TRIGGERS``."""
    raw = (cur or {}).get("cursor") or ""
    return isinstance(raw, str) and raw.endswith(_KUPO_NO_JOB_SUFFIX)


class KupoHistory(_HistoryFlavor):
    """Kupo flavor: trigger the HOST's own backfill and get out of the way.

    Trigger-and-continue: the host job can run up to an hour and the sidecar
    has ONE job worker, so polling inline would stall every other contract.
    Completion is tracked with an ``ingest_cursor`` marker row (source="kupo",
    no local raw writes — the rows land in the HOST tables): ``run()`` checks
    the host job status on later ticks and flips the marker when the host
    reports OUR bounded job done. Foreign jobs (an operator's manual latest-N
    backfill for the same address, recognizable by a missing
    ``created_before_slot``) are never adopted. Failures and degraded scans
    re-trigger (idempotent on the host's ReplacingMergeTree), bounded by
    ``_KUPO_MAX_TRIGGERS``; a lost host job (in-memory store, host restart →
    404) re-POSTs the same way."""

    async def _run(
        self, *, target: str, target_type: str, cap: int, progress: ProgressFn
    ) -> HistoryResult:
        repo = ClickHouseRepo(self._settings)
        try:
            cur = repo.get_cursor(target)
            attempts = 0
            if cur and cur.get("source") == "kupo":
                attempts = _kupo_attempts(cur)
                # Same raised-cap semantics as the blockfrost flavor: done at a
                # smaller txs_seen than the (new) cap falls through and re-runs.
                if cur.get("done") and int(cur.get("txs_seen") or 0) >= cap:
                    if _kupo_gave_up(cur):
                        return HistoryResult(
                            "skipped",
                            0,
                            "gave up after earlier host failures; raise the cap to retry",
                        )
                    return HistoryResult(
                        "skipped",
                        int(cur.get("txs_seen") or 0),
                        "history already backfilled via host",
                    )
                if cur.get("done"):
                    # Falling past the skip guard on a done marker means the cap
                    # was raised: the question re-opens with a FRESH trigger
                    # budget. The stored counter belongs to the settled attempt
                    # (gave-up or landed alike) — keeping it would let the
                    # attempts wall below re-close the re-open on the same tick.
                    attempts = 0
                elif not _kupo_has_no_job(cur):
                    # A trigger is outstanding: ask the host how it went.
                    checked = await self._check_host_job(repo, target, target_type, cap)
                    if checked is not None:
                        return checked
                    # failed / degraded / foreign / forgotten → re-trigger below.
                # else: the last attempt failed before a job existed (no_job) —
                # skip straight to the attempts-wall/re-trigger below rather than
                # polling a job that was never created.

            if attempts >= _KUPO_MAX_TRIGGERS:
                logger.warning(
                    "kupo history for %s gave up after %d trigger attempts; "
                    "see the host's backfill logs",
                    target[:24],
                    attempts,
                )
                # Settle at the cap so later ticks skip-fast, flagged gave-up so
                # history_status reports "failed" rather than a landed backfill;
                # a raised cap re-opens it (txs_seen < new cap, budget reset).
                self._mark(
                    repo,
                    target,
                    target_type,
                    done=True,
                    txs_seen=cap,
                    attempts=attempts,
                    gave_up=True,
                )
                return HistoryResult(
                    "skipped", 0, f"giving up after {attempts} host backfill attempts"
                )

            boundary = host_history_boundary(self._settings, target)
            if boundary is None:
                return HistoryResult("deferred", 0, _BOUNDARY_DEFERRED_NOTE)
            return await self._trigger(
                repo, target, target_type, cap, boundary, progress, attempts=attempts
            )
        finally:
            repo.close()

    async def _trigger(
        self,
        repo: ClickHouseRepo,
        target: str,
        target_type: str,
        cap: int,
        boundary: HostBoundary,
        progress: ProgressFn,
        *,
        attempts: int,
    ) -> HistoryResult:
        try:
            async with self._host_client() as client:
                resp = await client.post(
                    "/api/v1/backfill",
                    json={
                        "address": target,
                        "max_txs": cap,
                        "created_before_slot": boundary.floor_slot,
                    },
                )
        except httpx.HTTPError:
            logger.warning("host backfill API unreachable for %s", target[:24])
            self._mark(
                repo,
                target,
                target_type,
                done=False,
                txs_seen=0,
                attempts=attempts + 1,
                no_job=True,
            )
            return HistoryResult("deferred", 0, "host API unreachable; retrying on later ticks")
        if resp.status_code == 202:
            # Only a 202 counts as an attempt: OUR job is now running.
            self._mark(repo, target, target_type, done=False, txs_seen=0, attempts=attempts + 1)
            progress(f"host backfill triggered (boundary slot {boundary.floor_slot})")
            return HistoryResult(
                "pending", 0, "host backfill running; rows appear as the job lands them"
            )
        if resp.status_code == 409:
            # A FOREIGN run (an operator's manual backfill) holds the
            # same-address slot. Not OUR failure (nothing to give up on) — do
            # not adopt it and do not write a marker: the next tick simply
            # retries the trigger once it finishes.
            return HistoryResult(
                "pending", 0, "another backfill for this address is running; will retry"
            )
        # Every path below is a failure to even start OUR job: mark it as a
        # spent (no-job) attempt so a persistent misconfiguration eventually
        # crosses _KUPO_MAX_TRIGGERS and surfaces as history_status "failed"
        # (see _kupo_has_no_job) instead of retrying silently forever.
        if resp.status_code == 503:
            self._mark(
                repo,
                target,
                target_type,
                done=False,
                txs_seen=0,
                attempts=attempts + 1,
                no_job=True,
            )
            return HistoryResult("deferred", 0, "host KUPO_URL not configured")
        # Any other status is a misconfiguration the startup guards cannot
        # catch (they verify the key is SET, not that it is valid), and the
        # deferred retry loop would otherwise churn on it invisibly forever —
        # so it must be loud in the logs, auth failures by name.
        if resp.status_code in (401, 403):
            logger.warning(
                "host backfill API rejected the trigger for %s (HTTP %d); "
                "check HOST_API_KEY against the host's API keys",
                target[:24],
                resp.status_code,
            )
            self._mark(
                repo,
                target,
                target_type,
                done=False,
                txs_seen=0,
                attempts=attempts + 1,
                no_job=True,
            )
            return HistoryResult("deferred", 0, f"host API auth failed (HTTP {resp.status_code})")
        logger.warning(
            "host backfill trigger for %s returned unexpected HTTP %d",
            target[:24],
            resp.status_code,
        )
        self._mark(
            repo, target, target_type, done=False, txs_seen=0, attempts=attempts + 1, no_job=True
        )
        return HistoryResult("deferred", 0, f"host backfill returned HTTP {resp.status_code}")

    async def _check_host_job(
        self, repo: ClickHouseRepo, target: str, target_type: str, cap: int
    ) -> HistoryResult | None:
        """The outstanding trigger's status, or None to re-trigger.

        None covers: the host forgot the job (404 after a restart, in-memory
        store), the job failed, the finished job is FOREIGN (an operator's
        manual latest-N backfill carries no ``created_before_slot``; adopting
        its result would freeze our bounded history as complete), or it
        finished DEGRADED (``result.complete`` false: blocks were skipped, so
        the idempotent re-run is the retry path). The re-trigger loop is
        bounded by ``_KUPO_MAX_TRIGGERS`` in ``run``."""
        try:
            async with self._host_client() as client:
                resp = await client.get(f"/api/v1/backfill/{target}")
        except httpx.HTTPError:
            return HistoryResult("pending", 0, "host API unreachable; will re-check")
        if resp.status_code == 404:
            return None  # host restarted (in-memory job store): re-POST
        if resp.status_code != 200:
            return HistoryResult("pending", 0, f"host status returned HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            # A 200 that is not JSON (a proxy error page, say) is a transient
            # infrastructure answer, not a job verdict: keep waiting.
            return HistoryResult("pending", 0, "host status returned non-JSON; will re-check")
        status = body.get("status")
        if status == "running":
            return HistoryResult("pending", 0, "host backfill still running")
        if status == "done":
            if body.get("created_before_slot") is None:
                return None  # foreign (operator) job: run ours instead
            result = body.get("result") or {}
            if not result.get("complete", False):
                logger.warning(
                    "kupo history for %s finished degraded (%s); re-triggering",
                    target[:24],
                    result.get("degraded_reason") or "no reason reported",
                )
                return None
            ingested = int(result.get("txs_ingested") or 0)
            # Settle txs_seen at the CAP, not the ingested count: fewer rows
            # than requested means the bounded history is exhausted, which is
            # complete at this cap (a raised cap still re-opens it).
            self._mark(
                repo,
                target,
                target_type,
                done=True,
                txs_seen=cap,
                attempts=_kupo_attempts(repo.get_cursor(target)),
            )
            return HistoryResult("completed", ingested, "host backfill landed")
        return None  # failed → re-trigger

    def _host_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._settings.host_api_url.rstrip("/"),
            headers={"X-API-Key": self._settings.host_api_key},
            timeout=self._settings.host_api_timeout_seconds,
        )

    @staticmethod
    def _mark(
        repo: ClickHouseRepo,
        target: str,
        target_type: str,
        *,
        done: bool,
        txs_seen: int,
        attempts: int,
        gave_up: bool = False,
        no_job: bool = False,
    ) -> None:
        # Mutually exclusive: gave_up settles the marker terminally (done=True),
        # no_job marks a failed-to-start attempt on an outstanding (done=False)
        # marker. A caller never has a reason to pass both.
        suffix = _KUPO_GAVE_UP_SUFFIX if gave_up else (_KUPO_NO_JOB_SUFFIX if no_job else "")
        repo.upsert_cursor(
            target,
            target_type,
            cursor=f"{_KUPO_ATTEMPTS_PREFIX}{attempts}{suffix}",
            last_tx_hash="",
            txs_seen=txs_seen,
            done=done,
            source="kupo",
        )


# The cursor sources the history markers may carry; anything else (a primary
# CHAIN_SOURCE cursor on a reused database, say) is not a history marker.
_HISTORY_SOURCES = (_BLOCKFROST_HISTORY_SOURCE_TAG, "kupo")


def _marker_complete(cur: dict[str, Any] | None, cap: int) -> bool:
    """Whether the stored marker says there is nothing left to do at this cap:
    explicitly done, or an at-cap walk. The second arm exists because the
    ingester's max_reached leaves done=0 with the page cursor intact (so a
    RAISED cap can resume mid-walk) — at the CURRENT cap that state is
    complete, and treating it as outstanding would re-run the boundary
    aggregates every classify tick forever."""
    if not cur or cur.get("source") not in _HISTORY_SOURCES:
        return False
    return bool(cur.get("done")) or int(cur.get("txs_seen") or 0) >= cap


def _read_marker(settings: Settings, target: str) -> dict[str, Any] | None:
    """One cursor read on a short-lived base repo — cheap enough for the 30s
    classify tick that drives resume (host-backed request/worker repos no-op
    cursor reads, so a dedicated base repo is the only way to see the marker)."""
    repo = ClickHouseRepo(settings)
    try:
        return repo.get_cursor(target)
    finally:
        repo.close()


def history_status(settings: Settings, target: str, cap: int) -> str:
    """Operator-facing state of the target's history backfill, derived from the
    cursor marker at read time (no schema change): "none" when the feature is
    disabled or no attempt has been marked yet, "in_progress" while a walk or a
    host-side job is outstanding, "complete" once done at ``cap``, "failed"
    when the kupo flavor exhausted its trigger budget and gave up (raising the
    per-contract cap re-opens it)."""
    if not settings.history_enabled:
        return "none"
    cur = _read_marker(settings, target)
    if cur is None or cur.get("source") not in _HISTORY_SOURCES:
        return "none"
    if _kupo_gave_up(cur):
        return "failed"
    return "complete" if _marker_complete(cur, cap) else "in_progress"


def history_incomplete(settings: Settings, target: str, cap: int) -> bool:
    """Whether the target's history backfill still has work outstanding at this
    cap (no marker at all counts: deferred attempts write no cursor and must
    retry). The gave-up marker counts as settled — the classify tick must not
    retry it; only a raised cap re-opens it."""
    return not _marker_complete(_read_marker(settings, target), cap)


async def resolve_metadata(
    source: ChainSource, settings: Settings, target: str, target_type: str
) -> TargetMeta:
    """The pipeline's metadata call, with a history-aware fallback.

    ``HostChainSource.metadata`` raises ``SourceNotFound`` for a target the
    host has no rows for — which, with a history source configured, is exactly
    the target history exists to serve. The blockfrost flavor answers with the
    provider's real metadata (balance/tokens); the kupo flavor synthesizes it
    locally the way host_ch does (the trigger-and-continue backfill cannot
    produce host rows synchronously — the pending-retry loop finishes the job).
    """
    try:
        return await source.metadata(target, target_type)
    except SourceNotFound:
        if not settings.history_enabled or target_type != "address":
            raise
        if settings.history_source == "blockfrost":
            from app.blockfrost.source import BlockfrostSource

            async with BlockfrostSource(settings) as bf:
                # A genuinely unknown address re-raises SourceNotFound here,
                # which is the right answer for both sides.
                return await bf.metadata(target, target_type)
        # kupo: unlike blockfrost above, there is no synchronous existence
        # check available here — the sidecar never queries the chain directly
        # in this flavor (that capability lives on the HOST side; this call
        # only trigger-and-continues the host's own backfill). A typo'd or
        # never-existed address therefore onboards the same as a real one and
        # simply never accumulates history — a dead watchlist entry, not a
        # security or recall issue (it can never be flagged, since it has no
        # transactions to score).
        return {
            "exists": True,
            "is_script": payment_is_script(target),
            "script_type": "",
            "balance_lovelace": 0,
            "asset_count": 0,
            "sample_tokens": "[]",
        }
