"""Deterministic warehouse seeder for the query-latency benchmark tier.

Fills ClickHouse with a preprod-scale synthetic dataset under the
``PERF_NETWORK`` namespace: ``transactions`` rows with realistic fee/value
distributions and a small input/output fan each (child rows in
``transaction_inputs`` / ``transaction_outputs``), plus engine-shaped
``tx_class_scores`` rows for a configured fraction of them. Volumes and
ratios come from ``config/performance.yaml`` (``query_latency.seed``) via
``perf.config``.

Idempotence: every row is a pure function of ``DATASET_VERSION``, the row
index, and a day-anchored time window, so a rerun regenerates identical rows
and the ReplacingMergeTree tables collapse them to one row per key instead of
doubling. That also makes the seeder safe to interrupt: per-statement inserts
are atomic, and rerunning re-covers whatever a partial run already wrote.

Runnable as a module:

    cd backend && uv run python -m perf.seed [--transactions N]

or programmatically via :func:`ensure_seeded`, which seeds only when the
namespace holds fewer rows than the configured target.
"""

import argparse
import hashlib
import math
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

from app.analysis.normalise import BAND_HIGH_THRESHOLD, BAND_MODERATE_MAX, score_to_band
from app.analysis.scorer_config import composite_corroboration_config
from app.db import clickhouse, clickhouse_scores
from app.db.clickhouse_scores import _CLASS_COLS
from perf import PERF_NETWORK
from perf import config as perf_config

# Hash-derivation namespace + dataset generation. Every identifier (tx hash,
# block hash, address, parent ref) derives from these, so bumping the version
# produces a brand-new dataset instead of deduplicating against the old one;
# bump only when the generator's content rules change incompatibly.
_SEED_NAMESPACE = "perfseed"
DATASET_VERSION = "v1"

# Transactions generated per insert round-trip. A 10k-tx chunk carries roughly
# 10k tx rows + ~30k input rows + ~45k output rows + ~9k score rows: tens of
# MB of Python tuples, which bounds peak memory while keeping per-INSERT
# round-trip overhead well amortized.
CHUNK_TRANSACTIONS = 10_000

# scored_ratio / alert_ratio membership is decided by index arithmetic
# (i % grid < slots) instead of RNG draws so that (a) expected row counts are
# exactly computable in O(1) for the ensure_seeded resume check and (b)
# membership is stable across chunk boundaries and reruns by construction.
# 1000 slots quantize the configured ratios to 0.1%, far finer than the
# realism they encode.
RATIO_GRID = 1000

# Per-tx I/O fan: most preprod txs are 1-2 input / 2 output wallet payments;
# batching and DEX interactions push the tail to a handful. This range is the
# seed-workload contract documented for the query_latency benchmarks.
MIN_INPUTS_PER_TX = 1
MAX_INPUTS_PER_TX = 5
MIN_OUTPUTS_PER_TX = 1
MAX_OUTPUTS_PER_TX = 8

# Fee distribution: lognormal around the typical Cardano payment fee. The
# floor is the protocol min-fee constant "a" (155381 lovelace on
# mainnet/preprod protocol parameters), which no valid tx can undercut; the
# median/sigma put the bulk in the observed 0.17-0.25 ADA range with a
# plausible script-heavy tail.
FEE_FLOOR_LOVELACE = 155_381
FEE_MEDIAN_LOVELACE = 180_000
FEE_SIGMA = 0.35

# Output value distribution: heavy-tailed lognormal. A ~10 ADA median with
# sigma 2.0 spans dust-sized transfers through occasional whale moves
# (p99 around 1000 ADA), the spread the dashboard aggregates actually face.
VALUE_MEDIAN_LOVELACE = 10_000_000
VALUE_SIGMA = 2.0

# Per-output floor: the ledger min-UTxO order of magnitude (~1 ADA), so every
# seeded output stays individually plausible after the value split.
MIN_OUTPUT_LOVELACE = 1_000_000

# Distinct synthetic addresses. Finite reuse (about 20 txs per address at the
# default 100k volume) gives the address_transactions lookup table its
# realistic many-rows-per-address shape instead of a degenerate all-unique
# key space.
ADDRESS_POOL_SIZE = 5_000

# 50 hex chars after the addr_test1 HRP yield a 60-char address: inside the
# length band of real Shelley bech32 addresses and the API's ADDRESS_RE, so
# seeded addresses behave like real ones anywhere they surface.
_ADDRESS_SUFFIX_HEX_CHARS = 50

# analyzed_at lag behind block time, in seconds: the engine polls every few
# seconds and scores in batches, so a fixed one-minute lag is a realistic
# stand-in that keeps analyzed_at fully deterministic.
SCORING_LAG_SECONDS = 60

# Engaged classes per scored tx: real engine output typically has one dominant
# class with zero to two secondary gates open; every other class stays at the
# -1 "gate closed" sentinel (tx_class_scores schema convention, filled in by
# insert_class_scores for absent keys).
MIN_ENGAGED_CLASSES = 1
MAX_ENGAGED_CLASSES = 3

# Scores are 0-100 per the detection spec; the top of the alert-band draw.
SCORE_SCALE_MAX = 100.0

# The engine rounds persisted scores to 2 decimal places; mirror it so seeded
# rows are indistinguishable in precision from real engine output.
_SCORE_DECIMALS = 2

# Sub-scores are normalised [0, 1] quantities; 4 decimals matches the
# precision the normalisation framework meaningfully carries.
_NORMALISED_DECIMALS = 4

# Seeded block heights start well above zero so they read as plausible
# mid-chain values rather than genesis-adjacent ones.
BLOCK_HEIGHT_BASE = 1_000_000

# analysis_version written to seeded score rows, so synthetic rows remain
# distinguishable from real engine passes in the ReplacingMergeTree history.
SEED_ANALYSIS_VERSION = f"perf-seed-{DATASET_VERSION}"

# A class corroborates when it scores at or above this threshold: the same
# knob the engine reads (config/detection.yaml composite_corroboration), so
# seeded corroboration_count matches what a real re-score would produce.
_CORROBORATION_THRESHOLD = float(composite_corroboration_config()["corroboration_threshold"])

# Naive-UTC Unix epoch, for deriving slots from block time (see _generate_tx).
_UNIX_EPOCH = datetime(1970, 1, 1)

# Column lists mirror app.db.clickhouse.insert_transactions_batch so seeded
# rows are shaped exactly like ingester output (tx_size_bytes is left at its
# column default, as it is for non-CBOR ingestion).
_TX_INSERT = """
    INSERT INTO transactions (
        tx_hash, network, slot, block_height, block_hash, block_index, timestamp, fee, deposit,
        input_count, output_count, total_input_value, total_output_value,
        addresses, metadata, raw_data, raw_data_truncated, script_valid, ingestion_timestamp
    ) VALUES
"""
_INPUTS_INSERT = """
    INSERT INTO transaction_inputs (
        tx_hash, network, input_index, input_tx_hash, input_index_in_tx,
        address, amount, assets, is_reference, is_collateral,
        is_unspent_attempt, ingestion_timestamp
    ) VALUES
"""
_OUTPUTS_INSERT = """
    INSERT INTO transaction_outputs (
        tx_hash, network, output_index, address, amount, assets, is_collateral, ingestion_timestamp
    ) VALUES
"""


def _hex64(*parts: object) -> str:
    """64-hex-char identifier derived from the dataset namespace and parts."""
    material = ":".join((_SEED_NAMESPACE, DATASET_VERSION, *map(str, parts)))
    return hashlib.sha256(material.encode()).hexdigest()


def _address(k: int) -> str:
    return "addr_test1" + _hex64("addr", k)[:_ADDRESS_SUFFIX_HEX_CHARS]


def dataset_anchor() -> datetime:
    """Deterministic end of the seeded time window: today's UTC midnight, naive.

    Anchoring to a day boundary (instead of now()) makes every rerun within
    the same day generate byte-identical rows, so ReplacingMergeTree collapses
    them instead of accumulating near-duplicates that differ only in
    timestamps; the now()-relative dashboard queries still fully overlap the
    window. Naive because clickhouse_driver expects naive UTC datetimes for
    DateTime columns (same convention as the live_db tier).
    """
    now = datetime.now(UTC)
    return datetime(now.year, now.month, now.day)


def _ratio_slots(ratio: float) -> int:
    """Quantize a [0, 1] ratio onto the RATIO_GRID membership slots."""
    return round(ratio * RATIO_GRID)


def _alert_slots(scored_slots: int, alert_ratio: float) -> int:
    """Alert slots within the scored slots (alert_ratio is a fraction OF
    scored rows). Never quantized to zero while the ratio is positive: the
    timeseries benchmark needs alert rows to scan, and a sub-slot ratio
    rounding to none would silently benchmark an empty result."""
    slots = round(scored_slots * alert_ratio)
    if alert_ratio > 0 and scored_slots > 0:
        slots = max(slots, 1)
    return min(slots, scored_slots)


def expected_scored_count(n_transactions: int, scored_ratio: float) -> int:
    """Exact number of seeded txs that carry a score row, computable in O(1)
    thanks to index-arithmetic membership (see RATIO_GRID)."""
    return _expected_members(n_transactions, _ratio_slots(scored_ratio))


def _expected_members(n_transactions: int, slots: int) -> int:
    """Count of indices i < n with (i % RATIO_GRID) < slots."""
    full_cycles, remainder = divmod(n_transactions, RATIO_GRID)
    return full_cycles * slots + min(remainder, slots)


def _split_amount(total: int, parts: int, rng: random.Random, floor: int) -> list[int]:
    """Split ``total`` into ``parts`` amounts of at least ``floor`` each,
    weighted randomly; the last part absorbs integer-rounding remainder so the
    parts sum to ``total`` exactly. Caller guarantees total >= parts * floor."""
    weights = [rng.random() for _ in range(parts)]
    # `or 1.0`: guards the astronomically unlikely all-zero draw; any positive
    # denominator keeps the split valid (all remainder goes to the last part).
    total_weight = sum(weights) or 1.0
    spread = total - parts * floor
    amounts = [floor + int(spread * w / total_weight) for w in weights]
    amounts[-1] += total - sum(amounts)
    return amounts


@dataclass(frozen=True)
class _SeedPlan:
    """Derived, fully deterministic quantities shared by every generated row."""

    transactions: int
    span_seconds: int
    window_start: datetime
    txs_per_block: int
    n_blocks: int
    scored_slots: int
    alert_slots: int
    address_pool: tuple[str, ...]


def _plan(cfg: perf_config.PerformanceConfig, transactions: int | None) -> _SeedPlan:
    seed_cfg = cfg.query_latency.seed
    n = transactions if transactions is not None else seed_cfg.transactions
    anchor = dataset_anchor()
    span_seconds = int(timedelta(days=seed_cfg.span_days).total_seconds())
    # Blocks are shaped like the ingestion replay workload (busy preprod
    # blocks); reusing that knob keeps the two synthetic datasets consistent.
    txs_per_block = cfg.ingestion.txs_per_block
    scored_slots = _ratio_slots(seed_cfg.scored_ratio)
    return _SeedPlan(
        transactions=n,
        span_seconds=span_seconds,
        window_start=anchor - timedelta(seconds=span_seconds),
        txs_per_block=txs_per_block,
        n_blocks=math.ceil(n / txs_per_block),
        scored_slots=scored_slots,
        alert_slots=_alert_slots(scored_slots, seed_cfg.alert_ratio),
        address_pool=tuple(_address(k) for k in range(ADDRESS_POOL_SIZE)),
    )


def _generate_score(
    i: int,
    rng: random.Random,
    tx_hash: str,
    timestamp: datetime,
    plan: _SeedPlan,
) -> dict[str, Any] | None:
    """Engine-shaped tx_class_scores row for index ``i``, or None when the
    index falls outside the scored membership slots."""
    slot_pos = i % RATIO_GRID
    if slot_pos >= plan.scored_slots:
        return None
    is_alert = slot_pos < plan.alert_slots

    engaged = rng.sample(_CLASS_COLS, rng.randint(MIN_ENGAGED_CLASSES, MAX_ENGAGED_CLASSES))
    dominant = engaged[0]
    if is_alert:
        # Alert rows land in the High/Critical bands, i.e. at or above the
        # High threshold, matching the dashboard's alerting-band predicate.
        dominant_raw = rng.uniform(BAND_HIGH_THRESHOLD, SCORE_SCALE_MAX)
    else:
        # Non-alert scored rows top out at the Moderate band's ceiling so
        # they can never cross into the alerting bands.
        dominant_raw = rng.uniform(0.0, BAND_MODERATE_MAX)

    class_scores: dict[str, float] = {dominant: round(dominant_raw, _SCORE_DECIMALS)}
    for cls in engaged[1:]:
        # Secondary engaged classes score at or below the dominant one, so
        # max_class stays the dominant class by construction.
        class_scores[cls] = round(rng.uniform(0.0, dominant_raw), _SCORE_DECIMALS)

    max_score = class_scores[dominant]
    corroborating = sorted(c for c, v in class_scores.items() if v >= _CORROBORATION_THRESHOLD)
    return {
        "tx_hash": tx_hash,
        "network": PERF_NETWORK,
        # Only engaged classes are present: insert_class_scores fills every
        # absent class with the -1 "gate closed" sentinel per the
        # tx_class_scores schema convention.
        **class_scores,
        "max_score": max_score,
        "max_class": dominant,
        "risk_band": score_to_band(max_score),
        "sub_scores": {
            cls: {"seeded": round(v / SCORE_SCALE_MAX, _NORMALISED_DECIMALS)}
            for cls, v in class_scores.items()
        },
        "evidence": {dominant: {"reasons": ["perf-seed synthetic row"], "seed_index": i}},
        "corroboration_count": len(corroborating),
        "corroborating_classes": ",".join(corroborating),
        "analysis_version": SEED_ANALYSIS_VERSION,
        "analyzed_at": timestamp + timedelta(seconds=SCORING_LAG_SECONDS),
    }


def _generate_tx(
    i: int,
    plan: _SeedPlan,
    ingestion_timestamp: datetime,
) -> tuple[tuple, list[tuple], list[tuple], dict[str, Any] | None]:
    """All rows for seeded transaction ``i``: the transactions tuple, its
    input/output child tuples, and the optional score row. Pure function of
    (DATASET_VERSION, i, plan), which is what makes reruns dedup."""
    # Per-index RNG: membership in a chunk never changes the draw sequence,
    # so interrupted runs and different chunk sizes regenerate identical rows.
    rng = random.Random(f"{_SEED_NAMESPACE}:{DATASET_VERSION}:{i}")
    tx_hash = _hex64("tx", i)

    block_ordinal = i // plan.txs_per_block
    # Chain time advances block by block and all of a block's txs share its
    # timestamp, exactly as ingested chain data does (timestamp derives from
    # the block slot). Integer arithmetic keeps the value drift-free.
    offset_seconds = plan.span_seconds * (block_ordinal + 1) // plan.n_blocks
    timestamp = plan.window_start + timedelta(seconds=offset_seconds)
    # Preprod slots advance one per second; seconds-since-epoch of the UTC
    # block time is a deterministic stand-in with the same granularity.
    slot = int((timestamp - _UNIX_EPOCH).total_seconds())
    block_height = BLOCK_HEIGHT_BASE + block_ordinal
    block_hash = _hex64("block", block_ordinal)
    block_index = i % plan.txs_per_block

    fee = max(FEE_FLOOR_LOVELACE, int(rng.lognormvariate(math.log(FEE_MEDIAN_LOVELACE), FEE_SIGMA)))
    n_inputs = rng.randint(MIN_INPUTS_PER_TX, MAX_INPUTS_PER_TX)
    n_outputs = rng.randint(MIN_OUTPUTS_PER_TX, MAX_OUTPUTS_PER_TX)

    value_draw = int(rng.lognormvariate(math.log(VALUE_MEDIAN_LOVELACE), VALUE_SIGMA))
    total_output_value = max(value_draw, n_outputs * MIN_OUTPUT_LOVELACE)
    # Value conservation: inputs cover the outputs plus the fee, exactly as a
    # balanced on-chain transaction does (no deposit on seeded payments).
    total_input_value = total_output_value + fee

    input_addresses = [plan.address_pool[rng.randrange(ADDRESS_POOL_SIZE)] for _ in range(n_inputs)]
    output_addresses = [
        plan.address_pool[rng.randrange(ADDRESS_POOL_SIZE)] for _ in range(n_outputs)
    ]
    input_amounts = _split_amount(total_input_value, n_inputs, rng, floor=1)
    output_amounts = _split_amount(total_output_value, n_outputs, rng, floor=MIN_OUTPUT_LOVELACE)

    input_rows = [
        (
            tx_hash,
            PERF_NETWORK,
            j,
            # Parents resolve outside the seeded window (like spends of
            # pre-window UTxOs); deterministic so reruns dedup.
            _hex64("parent", i, j),
            rng.randint(0, MAX_OUTPUTS_PER_TX - 1),
            input_addresses[j],
            input_amounts[j],
            "",  # assets: none of the benchmarked queries parse them
            0,  # is_reference
            0,  # is_collateral
            0,  # is_unspent_attempt
            ingestion_timestamp,
        )
        for j in range(n_inputs)
    ]
    output_rows = [
        (
            tx_hash,
            PERF_NETWORK,
            j,
            output_addresses[j],
            output_amounts[j],
            "",  # assets
            0,  # is_collateral
            ingestion_timestamp,
        )
        for j in range(n_outputs)
    ]
    tx_row = (
        tx_hash,
        PERF_NETWORK,
        slot,
        block_height,
        block_hash,
        block_index,
        timestamp,
        fee,
        None,  # deposit: seeded payments carry no certificate deposits
        n_inputs,
        n_outputs,
        total_input_value,
        total_output_value,
        # Unique input+output addresses, the same shape the ingester stores
        # (feeds the address_transactions materialized view).
        list(dict.fromkeys(input_addresses + output_addresses)),
        "",  # metadata
        # raw_data stays empty: it is columnar and none of the benchmarked
        # queries read it, while storing 100k synthetic JSON payloads would
        # slow seeding without changing any measured query's cost.
        "",
        0,  # raw_data_truncated: the payload is genuinely absent, not sliced
        1,  # script_valid
        ingestion_timestamp,
    )
    score_row = _generate_score(i, rng, tx_hash, timestamp, plan)
    return tx_row, input_rows, output_rows, score_row


def dataset_counts(ch_module: ModuleType) -> dict[str, int]:
    """Live row counts for the seeded namespace.

    Distinct-by-key counts (not bare count()) so re-inserted rows that
    background merges have not collapsed yet do not read as extra volume.
    """
    params = {"network": PERF_NETWORK}

    def one(sql: str) -> int:
        return int(ch_module._execute_query(sql, params)[0][0])

    return {
        "transactions": one(
            "SELECT countDistinct(tx_hash) FROM transactions WHERE network = %(network)s"
        ),
        "transaction_inputs": one(
            "SELECT countDistinct((tx_hash, input_index)) FROM transaction_inputs "
            "WHERE network = %(network)s"
        ),
        "transaction_outputs": one(
            "SELECT countDistinct((tx_hash, output_index)) FROM transaction_outputs "
            "WHERE network = %(network)s"
        ),
        "tx_class_scores": one(
            "SELECT countDistinct(tx_hash) FROM tx_class_scores WHERE network = %(network)s"
        ),
        # The alerting-band predicate mirrors get_alert_timeseries.
        "alert_scores": one(
            "SELECT countDistinct(tx_hash) FROM tx_class_scores "
            f"WHERE network = %(network)s AND lower(risk_band) IN ({clickhouse_scores.ALERT_BANDS_SQL})"
        ),
    }


def seed_dataset(
    ch_module: ModuleType,
    cfg: perf_config.PerformanceConfig,
    transactions: int | None = None,
) -> dict[str, int]:
    """Generate and insert the full dataset, chunked; returns live counts.

    ``ch_module`` is the connected ``app.db.clickhouse`` module (the perf
    tier's ``ch`` fixture, or the module itself from the CLI).
    """
    plan = _plan(cfg, transactions)
    # The ingester stamps one ingestion_timestamp per batch; using the window
    # anchor keeps the ReplacingMergeTree version column deterministic within
    # a day, so same-day reruns are byte-identical and later-day reruns
    # cleanly supersede the previous rows per key.
    ingestion_timestamp = dataset_anchor()
    client = ch_module._get_client()

    started = time.perf_counter()
    total_chunks = math.ceil(plan.transactions / CHUNK_TRANSACTIONS)
    inserted = {"transactions": 0, "inputs": 0, "outputs": 0, "scores": 0}
    for chunk_index, start in enumerate(range(0, plan.transactions, CHUNK_TRANSACTIONS), start=1):
        stop = min(start + CHUNK_TRANSACTIONS, plan.transactions)
        tx_rows: list[tuple] = []
        input_rows: list[tuple] = []
        output_rows: list[tuple] = []
        score_rows: list[dict[str, Any]] = []
        for i in range(start, stop):
            tx_row, tx_inputs, tx_outputs, score_row = _generate_tx(i, plan, ingestion_timestamp)
            tx_rows.append(tx_row)
            input_rows.extend(tx_inputs)
            output_rows.extend(tx_outputs)
            if score_row is not None:
                score_rows.append(score_row)

        client.execute(_TX_INSERT, tx_rows)
        client.execute(_INPUTS_INSERT, input_rows)
        client.execute(_OUTPUTS_INSERT, output_rows)
        clickhouse_scores.insert_class_scores(score_rows)

        inserted["transactions"] += len(tx_rows)
        inserted["inputs"] += len(input_rows)
        inserted["outputs"] += len(output_rows)
        inserted["scores"] += len(score_rows)
        elapsed = time.perf_counter() - started
        print(
            f"[perf.seed] chunk {chunk_index}/{total_chunks}: "
            f"+{len(tx_rows)} txs, +{len(input_rows)} inputs, "
            f"+{len(output_rows)} outputs, +{len(score_rows)} scores "
            f"({elapsed:.1f}s elapsed)",
            flush=True,
        )

    counts = dataset_counts(ch_module)
    elapsed = time.perf_counter() - started
    summary = ", ".join(f"{name}={value}" for name, value in counts.items())
    print(f"[perf.seed] done in {elapsed:.1f}s; live counts: {summary}", flush=True)
    return counts


def ensure_seeded(ch_module: ModuleType, cfg: perf_config.PerformanceConfig) -> dict[str, int]:
    """Seed only if the namespace is below the configured volume; return counts.

    The check is exact on both gating tables: distinct transactions against
    the target volume, and distinct score rows against the O(1)-computable
    expected scored count (covers a run interrupted between a chunk's
    transactions insert and its scores insert). ``>=`` rather than ``==``
    because a previous larger seed run satisfies the benchmark's needs too.
    """
    seed_cfg = cfg.query_latency.seed
    counts = dataset_counts(ch_module)
    expected_scored = expected_scored_count(seed_cfg.transactions, seed_cfg.scored_ratio)
    if (
        counts["transactions"] >= seed_cfg.transactions
        and counts["tx_class_scores"] >= expected_scored
    ):
        summary = ", ".join(f"{name}={value}" for name, value in counts.items())
        print(f"[perf.seed] already seeded, skipping (live counts: {summary})", flush=True)
        return counts
    return seed_dataset(ch_module, cfg)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m perf.seed",
        description=(
            "Idempotently seed ClickHouse with the query-latency benchmark "
            f"dataset under network '{PERF_NETWORK}' "
            "(volumes from config/performance.yaml query_latency.seed)."
        ),
    )
    parser.add_argument(
        "--transactions",
        type=int,
        default=None,
        help="Override query_latency.seed.transactions from config/performance.yaml",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate and re-insert even if the warehouse already holds the target volume",
    )
    args = parser.parse_args(argv)
    cfg = perf_config.load()

    clickhouse.init_client()
    clickhouse.execute_schema()
    try:
        # Default path is the same already-seeded gate the benchmark fixture
        # uses, so a rerun (CI re-runs, operator habit) costs five count
        # queries instead of regenerating ~1M rows for ClickHouse to merge
        # away. An explicit volume override changes the target that gate
        # compares against, so overridden runs seed unconditionally.
        if args.force or args.transactions is not None:
            seed_dataset(clickhouse, cfg, transactions=args.transactions)
        else:
            ensure_seeded(clickhouse, cfg)
    finally:
        clickhouse.close_client()


if __name__ == "__main__":
    main()
