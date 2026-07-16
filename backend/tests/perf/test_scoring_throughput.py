"""Scoring-engine throughput benchmark: pure compute, zero I/O.

Times the exact per-transaction call the analysis engine's ``run_once`` loop
makes (``_score_transaction`` over the scorer list built by
``_build_scorers``) and asserts the measured throughput against
``perf_config.scoring.min_throughput_tps``.

The workload is a deterministic synthetic batch whose mix mirrors production
traffic: mostly plain transfers (the all-gates-closed fast path) plus enough
of every attack-class shape that all 9 scorers do real scoring work instead
of short-circuiting on their gates. Rows are built in the same pre-parsed
form ``run_once`` hands to ``_score_transaction`` (metadata and raw_data as
dicts, enrichment keys injected), so the timed region is exactly the
production compute path with ClickHouse baseline lookups stubbed in memory.
"""

import random
import statistics
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.analysis import external
from app.analysis.engine import _CLASS_NAMES, _build_scorers, _score_transaction
from app.analysis.features import extract_lovelace
from app.analysis.scorers import circular as circular_scorer
from app.analysis.scorers import large_datum as large_datum_scorer
from app.analysis.scorers import sandwich as sandwich_scorer
from app.analysis.scorers import token_dust as token_dust_scorer
from app.config import settings
from perf import MS_PER_SECOND, PERF_NETWORK, POLICY_ID_HEX_CHARS, WORKLOAD_SEED, p95, results
from perf.config import load

# Chain-position bases at recent-preprod orders of magnitude, offset per tx,
# so slot/height arithmetic inside scorers sees realistic values.
_BASE_SLOT = 90_000_000
_BASE_BLOCK_HEIGHT = 3_600_000
_BASE_TIMESTAMP = datetime(2026, 7, 1, tzinfo=UTC)

# CIP-19 testnet address prefixes: payment-key ('q') vs script-payment ('w');
# the 'w' form is what features.SCRIPT_ADDRESS_PREFIXES matches as a script.
_WALLET_ADDR_PREFIX = "addr_test1q"
_SCRIPT_ADDR_PREFIX = "addr_test1w"

# 64 bits of random address suffix: unique within the batch; scorers inspect
# only the CIP-19 prefix, so full bech32 validity is not required.
_ADDR_SUFFIX_BITS = 64
# A Cardano transaction id is a blake2b-256 digest: 32 bytes.
_TX_HASH_BITS = 256
# Base-16 packs 4 bits per character: converts bit widths to hex widths.
_BITS_PER_HEX_CHAR = 4

# Typical Cardano fee span: the ~0.17 ADA protocol floor up to a heavy
# script transaction, keeping the fee feature inside its normal distribution.
_FEE_LOVELACE_RANGE = (170_000, 900_000)
# Wallet transfers from min-UTxO scale (~1.2 ADA) to whale-sized moves, so
# value-driven code paths see varied magnitudes rather than one bucket.
_PAYMENT_LOVELACE_RANGE = (1_200_000, 2_000_000_000)
# Plain payments carry a handful of inputs/outputs (recipient plus change).
_PLAIN_COUNT_RANGE = (1, 4)
# High fan-in/fan-out shape (airdrops, exchange batching): enough entries
# that per-output loops dominate per-tx overhead for this profile.
_FAN_COUNT_RANGE = (10, 40)

# Large-value shape: tens to hundreds of thousands of ADA at a script, the
# magnitude the large_value class exists to catch.
_LARGE_VALUE_LOVELACE_RANGE = (50_000_000_000, 500_000_000_000)
# Wide native-asset quantity spread so the quantity-digits axis exercises
# real normalisation instead of hitting one fixed digit count.
_TOKEN_QTY_RANGE = (1_000_000, 1_000_000_000_000)

# Min-UTxO-scale lovelace riding on value-bloat outputs: dust bundles carry
# just enough ADA to satisfy the ledger deposit.
_DUST_LOVELACE_RANGE = (1_200_000, 2_500_000)
# Names per policy in the dust bundle: a few policies each carrying several
# names mirrors observed bloat mints better than one policy with all names.
_DUST_ASSETS_PER_POLICY = 5

# One inline datum exactly at the configured per-output bloat floor: crosses
# the large_datum gate while keeping the synthetic batch memory-light.
_BLOAT_DATUM_BYTES = int(large_datum_scorer._MIN_DATUM_BYTES)

# Same-script fan-in for the double-satisfaction drain: 2 is the definitional
# gate minimum; 5 covers the multi-UTxO variants in the CTF corpus.
_MSAT_INPUT_COUNT_RANGE = (2, 5)
# Per-input script UTxO sizes for the drain shape (a few to ~80 ADA each).
_MSAT_INPUT_LOVELACE_RANGE = (5_000_000, 80_000_000)
# Plutus execution budgets at real spend-validator magnitudes, well under
# the ledger per-tx cap, so exunits normalisation sees plausible numbers.
_EXUNITS_CPU_RANGE = (50_000_000, 500_000_000)
_EXUNITS_MEMORY_RANGE = (200_000, 5_000_000)
# Spend-redeemer payload width in hex chars: short constructor-style CBOR,
# distinct per input so the uniform-sweep guard does not suppress the score.
_REDEEMER_HEX_CHARS = 16

# Lure text hitting two curated phishing URL patterns (cardano-airdrop,
# claim-ada) on a real public-suffix TLD so the PSL filter keeps the URL.
_PHISHING_LURE = (
    "Congratulations! Claim your ADA reward at https://cardano-airdrop-{n}.claim-ada.com"
)
# CIP-20 message metadata label, the tx-level carrier the phishing gate scans.
_CIP20_LABEL = "674"

# Scam-token mint supplies span meme-token magnitudes.
_MINT_QTY_RANGE = (1_000, 1_000_000_000)
# Token names to counterfeit: the curated seed registry itself, so the
# similarity axis computes an exact-name match against a mismatched policy.
_FAKE_TOKEN_NAMES = tuple(sorted(external._SEED_TOKENS))

# Mempool collision shapes: sub-second snipes through slow accidental
# collisions, 1-3 shared inputs, and a spread of attacker win histories.
_COLLISION_DELTA_MS_RANGE = (50.0, 5_000.0)
_COLLISION_SHARED_INPUTS_RANGE = (1, 3)
_COLLISION_WIN_COUNT_RANGE = (0, 12)
# Observed outcome labels from config/detection.yaml outcome_scores, mixing
# confirmed thefts with pending/ambiguous races.
_COLLISION_OUTCOMES = ("TX1_FAILS_UTXO_SPENT", "TX_B_CONFIRMED", "BOTH_PENDING", "TX2_WINS")
# Transaction validity windows (slots) for the collision pair's TTL features.
_TTL_SLOTS_RANGE = (300, 900)

# Sandwich victim shapes: adverse fills below the 1.0 baseline rate, small
# but real price impact, and profits above the suppression floor so the
# scorer runs its full path instead of returning an early no-finding.
_SWAP_RATE_VICTIM_RANGE = (0.80, 0.98)
_SWAP_RATE_BASELINE = 1.0
_PRICE_IMPACT_RANGE = (0.005, 0.05)
_SANDWICH_COUNT_RANGE = (0, 6)
_SANDWICH_PROFIT_RANGE = (
    sandwich_scorer._MIN_PROFIT_LOVELACE,
    sandwich_scorer._MIN_PROFIT_LOVELACE * 20,
)

# Circular-transfer shapes: high amount similarity (the layering signal) and
# a net loss consistent with fee-only loss. The loss multiplier stays at or
# below FEE_TOLERANCE_STRICT, comfortably inside the gate's tolerance, so
# every cycle row engages rather than being rejected as organic flow.
_AMOUNT_SIMILARITY_RANGE = (0.70, 0.98)
_NET_LOSS_FEE_MULTIPLIER_RANGE = (0.5, circular_scorer.FEE_TOLERANCE_STRICT)
_CYCLE_RECURRENCE_RANGE = (0, 5)
_RECIPIENT_ENTROPY_RANGE = (0.1, 0.9)
_TEMPORAL_CONCENTRATION_RANGE = (0.1, 0.9)
_INTER_HOP_DELTA_SLOTS_RANGE = (1.0, 50.0)

# Boolean enrichment flags (change-address sharing, attacker linkage, round
# amounts) split the batch evenly so both branches of each scorer's flag
# handling do real work in every run.
_FLAG_PROBABILITY = 0.5


def _hex_of_bits(rng: random.Random, bits: int) -> str:
    return f"{rng.getrandbits(bits):0{bits // _BITS_PER_HEX_CHAR}x}"


def _wallet_addr(rng: random.Random) -> str:
    return _WALLET_ADDR_PREFIX + _hex_of_bits(rng, _ADDR_SUFFIX_BITS)


def _script_addr(rng: random.Random) -> str:
    return _SCRIPT_ADDR_PREFIX + _hex_of_bits(rng, _ADDR_SUFFIX_BITS)


def _policy_id(rng: random.Random) -> str:
    return _hex_of_bits(rng, POLICY_ID_HEX_CHARS * _BITS_PER_HEX_CHAR)


def _lovelace_output(rng: random.Random, amount: int) -> dict[str, Any]:
    return {"address": _wallet_addr(rng), "value": {"lovelace": amount}}


def _base_row(
    rng: random.Random,
    idx: int,
    raw_data: dict[str, Any],
    *,
    metadata: dict[str, Any] | None = None,
    collision: dict[str, Any] | None = None,
    cycle: dict[str, Any] | None = None,
    sandwich: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One transaction row in the pre-parsed shape run_once feeds the scorer
    loop: metadata/raw_data already dicts, enrichment keys already injected."""
    inputs = raw_data.get("inputs") or []
    outputs = raw_data.get("outputs") or []
    addresses = [
        entry["address"]
        for entry in (*inputs, *outputs)
        if isinstance(entry, dict) and entry.get("address")
    ]
    return {
        "tx_hash": _hex_of_bits(rng, _TX_HASH_BITS),
        "network": PERF_NETWORK,
        "fee": rng.randint(*_FEE_LOVELACE_RANGE),
        "input_count": len(inputs),
        "output_count": len(outputs),
        "total_output_value": sum(extract_lovelace(o.get("value")) for o in outputs),
        "metadata": metadata,
        "addresses": addresses,
        "raw_data": raw_data,
        "slot": _BASE_SLOT + idx,
        "block_height": _BASE_BLOCK_HEIGHT + idx,
        "timestamp": _BASE_TIMESTAMP + timedelta(seconds=idx),
        "collision": collision,
        "cycle": cycle,
        "sandwich": sandwich,
    }


def _plain_payment(rng: random.Random, idx: int) -> dict[str, Any]:
    """Ordinary wallet-to-wallet transfer: every gate stays closed."""
    raw_data = {
        "inputs": [
            _lovelace_output(rng, rng.randint(*_PAYMENT_LOVELACE_RANGE))
            for _ in range(rng.randint(*_PLAIN_COUNT_RANGE))
        ],
        "outputs": [
            _lovelace_output(rng, rng.randint(*_PAYMENT_LOVELACE_RANGE))
            for _ in range(rng.randint(*_PLAIN_COUNT_RANGE))
        ],
    }
    return _base_row(rng, idx, raw_data)


def _fan_out_transfer(rng: random.Random, idx: int) -> dict[str, Any]:
    """High fan-in/fan-out wallet batch: gates closed, big per-output loops."""
    raw_data = {
        "inputs": [
            _lovelace_output(rng, rng.randint(*_PAYMENT_LOVELACE_RANGE))
            for _ in range(rng.randint(*_FAN_COUNT_RANGE))
        ],
        "outputs": [
            _lovelace_output(rng, rng.randint(*_PAYMENT_LOVELACE_RANGE))
            for _ in range(rng.randint(*_FAN_COUNT_RANGE))
        ],
    }
    return _base_row(rng, idx, raw_data)


def _large_value_script_tx(rng: random.Random, idx: int) -> dict[str, Any]:
    """Whale-sized deposit at a script with a single native asset."""
    value = {
        "lovelace": rng.randint(*_LARGE_VALUE_LOVELACE_RANGE),
        _policy_id(rng): {"544f4b454e": rng.randint(*_TOKEN_QTY_RANGE)},
    }
    raw_data = {
        "inputs": [_lovelace_output(rng, rng.randint(*_PAYMENT_LOVELACE_RANGE))],
        "outputs": [
            {"address": _script_addr(rng), "value": value},
            _lovelace_output(rng, rng.randint(*_PAYMENT_LOVELACE_RANGE)),
        ],
    }
    return _base_row(rng, idx, raw_data)


def _token_dust_bundle(rng: random.Random, idx: int) -> dict[str, Any]:
    """Value-bloat DoS shape: a script output stuffed with distinct assets."""
    pair_count = int(token_dust_scorer._DOS_ASSET_MIN)
    value: dict[str, Any] = {"lovelace": rng.randint(*_DUST_LOVELACE_RANGE)}
    remaining = pair_count
    while remaining > 0:
        names = min(remaining, _DUST_ASSETS_PER_POLICY)
        value[_policy_id(rng)] = {_hex_of_bits(rng, _ADDR_SUFFIX_BITS): 1 for _ in range(names)}
        remaining -= names
    raw_data = {
        "inputs": [_lovelace_output(rng, rng.randint(*_PAYMENT_LOVELACE_RANGE))],
        "outputs": [{"address": _script_addr(rng), "value": value}],
    }
    return _base_row(rng, idx, raw_data)


def _large_datum_bloat(rng: random.Random, idx: int) -> dict[str, Any]:
    """Script output carrying an inline low-entropy padding datum at the
    configured bloat floor (hex string: two chars encode one byte)."""
    raw_data = {
        "inputs": [_lovelace_output(rng, rng.randint(*_PAYMENT_LOVELACE_RANGE))],
        "outputs": [
            {
                "address": _script_addr(rng),
                "value": {"lovelace": rng.randint(*_DUST_LOVELACE_RANGE)},
                "datum": "00" * _BLOAT_DATUM_BYTES,
            }
        ],
    }
    return _base_row(rng, idx, raw_data)


def _multiple_sat_drain(rng: random.Random, idx: int) -> dict[str, Any]:
    """Double-satisfaction drain: several UTxOs at one script spent with
    distinct spend redeemers, all value leaving to a wallet."""
    script = _script_addr(rng)
    n_inputs = rng.randint(*_MSAT_INPUT_COUNT_RANGE)
    inputs = [
        {"address": script, "value": {"lovelace": rng.randint(*_MSAT_INPUT_LOVELACE_RANGE)}}
        for _ in range(n_inputs)
    ]
    total_in = sum(extract_lovelace(inp["value"]) for inp in inputs)
    redeemers = {
        f"spend:{i}": {
            "redeemer": _hex_of_bits(rng, _REDEEMER_HEX_CHARS * _BITS_PER_HEX_CHAR),
            "executionUnits": {
                "memory": rng.randint(*_EXUNITS_MEMORY_RANGE),
                "cpu": rng.randint(*_EXUNITS_CPU_RANGE),
            },
        }
        for i in range(n_inputs)
    }
    raw_data = {
        "inputs": inputs,
        "outputs": [_lovelace_output(rng, total_in)],
        "redeemers": redeemers,
    }
    return _base_row(rng, idx, raw_data)


def _phishing_metadata_tx(rng: random.Random, idx: int) -> dict[str, Any]:
    """Payment carrying a CIP-20 lure message with a phishing URL."""
    raw_data = {
        "inputs": [_lovelace_output(rng, rng.randint(*_PAYMENT_LOVELACE_RANGE))],
        "outputs": [_lovelace_output(rng, rng.randint(*_PAYMENT_LOVELACE_RANGE))],
    }
    metadata = {_CIP20_LABEL: _PHISHING_LURE.format(n=idx)}
    return _base_row(rng, idx, raw_data, metadata=metadata)


def _fake_token_mint(rng: random.Random, idx: int) -> dict[str, Any]:
    """Counterfeit mint: a registry token name minted under a foreign policy."""
    name_hex = rng.choice(_FAKE_TOKEN_NAMES).encode().hex()
    qty = rng.randint(*_MINT_QTY_RANGE)
    policy = _policy_id(rng)
    raw_data = {
        "inputs": [_lovelace_output(rng, rng.randint(*_PAYMENT_LOVELACE_RANGE))],
        "outputs": [
            {
                "address": _wallet_addr(rng),
                "value": {"lovelace": rng.randint(*_DUST_LOVELACE_RANGE), policy: {name_hex: qty}},
            }
        ],
        "mint": {policy: {name_hex: qty}},
    }
    return _base_row(rng, idx, raw_data)


def _front_running_victim(rng: random.Random, idx: int) -> dict[str, Any]:
    """Mempool collision pair member (collision enrichment stubbed in-memory)."""
    fee = rng.randint(*_FEE_LOVELACE_RANGE)
    collision = {
        "counterpart_tx": _hex_of_bits(rng, _TX_HASH_BITS),
        "shared_inputs": rng.randint(*_COLLISION_SHARED_INPUTS_RANGE),
        "delta_ms": rng.uniform(*_COLLISION_DELTA_MS_RANGE),
        "outcome": rng.choice(_COLLISION_OUTCOMES),
        "counterpart_fee": rng.randint(*_FEE_LOVELACE_RANGE),
        "counterpart_ttl": rng.randint(*_TTL_SLOTS_RANGE),
        "shares_change_address": rng.random() < _FLAG_PROBABILITY,
        "attacker_win_count": rng.randint(*_COLLISION_WIN_COUNT_RANGE),
    }
    raw_data = {
        "inputs": [_lovelace_output(rng, rng.randint(*_PAYMENT_LOVELACE_RANGE))],
        "outputs": [_lovelace_output(rng, rng.randint(*_PAYMENT_LOVELACE_RANGE))],
        "timeToLive": rng.randint(*_TTL_SLOTS_RANGE),
    }
    row = _base_row(rng, idx, raw_data, collision=collision)
    row["fee"] = fee
    return row


def _sandwich_victim(rng: random.Random, idx: int) -> dict[str, Any]:
    """DEX swap flagged as a sandwich victim (enrichment stubbed in-memory)."""
    sandwich = {
        "tx_a": _hex_of_bits(rng, _TX_HASH_BITS),
        "tx_b": _hex_of_bits(rng, _TX_HASH_BITS),
        "pool_id": _policy_id(rng),
        "asset_pair": "ADA/" + rng.choice(_FAKE_TOKEN_NAMES),
        "attacker_linked": rng.random() < _FLAG_PROBABILITY,
        "swap_rate_victim": rng.uniform(*_SWAP_RATE_VICTIM_RANGE),
        "swap_rate_baseline": _SWAP_RATE_BASELINE,
        "price_impact_a": rng.uniform(*_PRICE_IMPACT_RANGE),
        "profit_b": rng.randint(*_SANDWICH_PROFIT_RANGE),
        "attacker_sandwich_count": rng.randint(*_SANDWICH_COUNT_RANGE),
        "slot_span": rng.randint(1, sandwich_scorer.W_SLOTS),
    }
    raw_data = {
        "inputs": [_lovelace_output(rng, rng.randint(*_PAYMENT_LOVELACE_RANGE))],
        "outputs": [_lovelace_output(rng, rng.randint(*_PAYMENT_LOVELACE_RANGE))],
    }
    return _base_row(rng, idx, raw_data, sandwich=sandwich)


def _circular_cycle_tx(rng: random.Random, idx: int) -> dict[str, Any]:
    """Member of a fee-consistent transfer cycle (enrichment stubbed)."""
    length = rng.randint(circular_scorer._MIN_LEN, circular_scorer._MAX_LEN)
    net_loss = length * circular_scorer._PER_HOP_FEE * rng.uniform(*_NET_LOSS_FEE_MULTIPLIER_RANGE)
    cycle = {
        "cycle_length": length,
        "addresses": [_wallet_addr(rng) for _ in range(length)],
        "amount_similarity": rng.uniform(*_AMOUNT_SIMILARITY_RANGE),
        "net_loss_ratio": net_loss,
        "recurrence_count": rng.randint(*_CYCLE_RECURRENCE_RANGE),
        "recipient_entropy": rng.uniform(*_RECIPIENT_ENTROPY_RANGE),
        "round_amount_flag": rng.random() < _FLAG_PROBABILITY,
        "temporal_concentration": rng.uniform(*_TEMPORAL_CONCENTRATION_RANGE),
        "mean_inter_hop_delta_slots": rng.uniform(*_INTER_HOP_DELTA_SLOTS_RANGE),
        "origin_cluster": "perfcluster" + _hex_of_bits(rng, _ADDR_SUFFIX_BITS),
    }
    raw_data = {
        "inputs": [_lovelace_output(rng, rng.randint(*_PAYMENT_LOVELACE_RANGE))],
        "outputs": [_lovelace_output(rng, rng.randint(*_PAYMENT_LOVELACE_RANGE))],
    }
    return _base_row(rng, idx, raw_data, cycle=cycle)


_RowBuilder = Callable[[random.Random, int], dict[str, Any]]

# Batch mix, applied round-robin: weighted toward plain transfers (the
# dominant chain traffic, exercising the all-gates-closed fast path) while
# every scorer family recurs often enough that the measurement includes real
# scoring work. Weights are parts of the 20-slot cycle below.
_PROFILE_MIX: tuple[tuple[_RowBuilder, int], ...] = (
    (_plain_payment, 7),
    (_fan_out_transfer, 2),
    (_large_value_script_tx, 2),
    (_token_dust_bundle, 2),
    (_large_datum_bloat, 1),
    (_multiple_sat_drain, 1),
    (_phishing_metadata_tx, 1),
    (_fake_token_mint, 1),
    (_front_running_victim, 1),
    (_sandwich_victim, 1),
    (_circular_cycle_tx, 1),
)


def _build_batch(batch_size: int) -> list[dict[str, Any]]:
    """Deterministic synthetic batch: the profile schedule cycles the weighted
    mix and a single seeded RNG fills in every field, so two runs of the same
    config produce identical rows."""
    rng = random.Random(WORKLOAD_SEED)
    schedule = [builder for builder, weight in _PROFILE_MIX for _ in range(weight)]
    return [schedule[i % len(schedule)](rng, i) for i in range(batch_size)]


@pytest.fixture
def pure_compute_scoring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the zero-I/O seam for the benchmark.

    The perf tier deliberately drops the suite-wide baseline mock (storage
    benchmarks need the real ClickHouse), so this benchmark stubs
    ``clickhouse.get_baseline`` itself: a plain in-memory function returning
    None, which sends every scorer to its bootstrap anchors. That matches a
    fresh deployment and is the same seam the hermetic suite mocks, keeping
    the timed region pure compute with no client, socket, or cache involved.

    All scorer flags are pinned on so the measurement covers the documented
    9-scorer workload regardless of local .env toggles, and testnet mode
    populates the fake_token registry for the non-mainnet perf network (the
    registry serves the in-memory seed list; it never fetches inline).
    """
    for name in _CLASS_NAMES:
        monkeypatch.setattr(settings, f"SCORER_{name.upper()}_ENABLED", True)
    monkeypatch.setattr(settings, "FAKE_TOKEN_TESTNET_MODE", True)
    monkeypatch.setattr("app.db.clickhouse.get_baseline", lambda *args, **kwargs: None)


def test_scoring_throughput_meets_budget(pure_compute_scoring):
    cfg = load().scoring
    scorers = _build_scorers()
    assert {s.name for s in scorers} == set(_CLASS_NAMES), (
        "benchmark expects all 9 scorers enabled; the budget was set for the full pipeline"
    )

    batch = _build_batch(cfg.batch_size)

    # Warmup pass: pulls lazy one-time costs (config loads, regex compiles,
    # registry cache fill) out of the timed window, and doubles as the
    # workload-validity check that every class actually scores somewhere in
    # the batch instead of short-circuiting on its gate.
    engaged: dict[str, int] = dict.fromkeys(_CLASS_NAMES, 0)
    for row in batch:
        result = _score_transaction(row, scorers)
        for cls in _CLASS_NAMES:
            if result[cls] >= 0:
                engaged[cls] += 1
    assert all(engaged[cls] > 0 for cls in _CLASS_NAMES), (
        f"synthetic mix left some classes unscored (engaged={engaged}); "
        "the throughput number would not cover the full compute path"
    )

    # Timed passes: one wall-clock measurement per full batch; the median
    # decides pass/fail so a single GC pause or CPU-frequency dip cannot.
    batch_times: list[float] = []
    for _ in range(cfg.iterations):
        started = time.perf_counter()
        for row in batch:
            _score_transaction(row, scorers)
        batch_times.append(time.perf_counter() - started)

    median_batch_s = statistics.median(batch_times)
    tps_median = cfg.batch_size / median_batch_s
    tps_min = cfg.batch_size / max(batch_times)
    tps_max = cfg.batch_size / min(batch_times)
    # Mean per-tx latency is the judged batch time spread over the batch:
    # derived, so it can never disagree with the recorded throughput.
    latency_mean_ms = median_batch_s / cfg.batch_size * MS_PER_SECOND

    # Instrumented pass, outside the timed window, solely for the tail
    # statistic: p95 needs per-tx wall times, and instrumenting the timed
    # passes would fold perf_counter overhead into the throughput number.
    per_tx_s: list[float] = []
    for row in batch:
        started = time.perf_counter()
        _score_transaction(row, scorers)
        per_tx_s.append(time.perf_counter() - started)
    latency_p95_ms = p95(per_tx_s) * MS_PER_SECOND

    checks = [results.check("tps_median", tps_median, ">=", cfg.min_throughput_tps)]
    # Recorded BEFORE the assert: a failed run must still leave an artifact
    # for the performance report, judged on the same checks.
    artifact = results.record(
        "scoring_throughput",
        metrics={
            "tps_median": tps_median,
            "tps_min": tps_min,
            "tps_max": tps_max,
            "batch_size": cfg.batch_size,
            "iterations": cfg.iterations,
            "batch_seconds_median": median_batch_s,
            "latency_mean_ms": latency_mean_ms,
            "latency_p95_ms": latency_p95_ms,
            "engaged_txs_per_class": engaged,
            "workload_seed": WORKLOAD_SEED,
        },
        checks=checks,
    )
    assert all(c["passed"] for c in checks), (
        f"scoring throughput regression: median {tps_median:.0f} tx/s is below the "
        f"{cfg.min_throughput_tps} tx/s budget (artifact: {artifact})"
    )
