"""Large Datum attack scorer (Class 3).

Detects UTxOs at script addresses with abnormally large inline datums (or
resolvable datum hashes).  The bloat originates from the datum component
exclusively: the Value field remains normal (only ADA or small standard assets).

The key structural separator from Classes 1-2 is datum_ratio: the fraction
of total UTxO bytes occupied by the datum.  Values above 0.60 are strong
indicators of datum-bloat rather than general value-field bloat.

Scoring is per-UTxO; the transaction score is the max across all outputs.

Sub-scores (Polimi Section 4.3.3; weights live in config/detection.yaml
under large_datum.weights and are quoted here only for orientation):
  datum_bytes    (0.50): absolute byte size, per-script baseline
  datum_ratio    (0.35): datum_bytes / utxo_total_bytes, fixed anchors
  value_cbor_inv (0.05): inverted; lean Value field = datum-bloat signature
  recurrence     (0.10): repeated bloated-datum deposits (stubbed to 0 in
                         code until entity clustering ships)
"""

import logging
from typing import Any

from app.analysis import features as feat_mod
from app.analysis.normalise import BAND_MODERATE_MAX, normalise, normalise_inverted
from app.analysis.scorer_config import (
    anchor as _anchor,
)
from app.analysis.scorer_config import (
    fraction_of_limit as _fraction_of_limit,
)
from app.analysis.scorer_config import (
    get as _get_cfg,
)
from app.analysis.scorer_config import (
    load_network_map as _load_network_map,
)
from app.analysis.scorer_config import (
    resolved_or_bootstrap as _resolve,
)
from app.analysis.scorers.base import (
    BaseScorer,
    ScorerResult,
    finalise_score,
    reduce_to_best,
)
from app.analysis.scorers.multiple_sat import _payment_credential

logger = logging.getLogger(__name__)

_CFG = _get_cfg("large_datum")
_W = _CFG["weights"]
_FIXED = _CFG["fixed_anchors"]
_BOOT = _CFG["bootstrap_anchors"]
_REASON_T = float(_CFG["reason_threshold"])
_MIN_DATUM_BYTES = int(_CFG["gate"]["min_datum_bytes"])
# A datum only counts as bloat when its byte entropy is at or below this floor.
# Padding attacks are low-entropy (repetitive filler); legitimate large datums
# carry high-entropy structured state. Size alone cannot separate them, so this
# content check is what suppresses benign large datums without losing a real
# (size-overlapping) bloat attack. See features.datum_shannon_entropy_bits.
_BLOAT_ENTROPY_MAX = float(_CFG["gate"]["bloat_entropy_max"])
# A datum is bloat when one CBOR leaf holds at least this fraction of its bytes
# (single-leaf padding). Structural, so it catches high-entropy random padding
# that the entropy gate misses; legitimate nested datums sit far below it.
_LEAF_CONCENTRATION_MAX = float(_CFG["gate"]["leaf_concentration_max"])
# Absolute-size backstop: a datum at or above this many bytes is flagged
# regardless of entropy, because it approaches the point where a consuming tx
# can no longer fit under maxTxSize. Robust against a high-entropy (random)
# padding attack that evades the entropy gate, and NOT downgraded for having
# cleared the content branches: an attacker writes those bytes, so a benign
# reading of them vouches for nothing. Derived from the tx-size limit.
_SIZE_BACKSTOP = _fraction_of_limit(_CFG["gate"]["size_backstop_fraction"], "max_tx_size_bytes")
_AGGREGATE_ENGAGEMENT_MIN = int(_CFG["aggregate_engagement_min"])
# Observability flag for datumHash-only outputs at script addresses. The
# referenced datum cannot be sized without an indexer, so a bloat-by-hash
# attack is invisible to the byte gates; when enabled, the scorer engages
# and records datum_hash_only_count (score stays -1: never alerts).
_FLAG_DATUM_HASH_ONLY = bool(_CFG["gate"]["flag_datum_hash_only"])
# Known legitimate large-state contracts, network-scoped prefixes. A script here
# holds a datum near the size backstop as its normal operation, so a
# backstop-only finding on it is capped out of the alerting bands (see the cap in
# _score_utxo). Identity, not shape, is what earns that: any depositor can build
# a datum of any shape at a victim script, so shape cannot distinguish the two,
# but only the contract's own operator controls its address.
_LARGE_STATE_ALLOWLIST: dict[str, tuple[str, ...]] = _load_network_map(
    _CFG.get("large_state_allowlist_prefixes"),
    scorer="large_datum",
    field="large_state_allowlist_prefixes",
)


def _datum_hash_only_addresses(outputs, datums=None) -> list:
    """Script-output addresses whose datum is a hash reference (flag 1) that is
    still unsizable, i.e. the preimage is NOT carried in this tx's witness
    ``datums`` map. A hash whose preimage IS present is sized normally and
    handled by the byte gates, so it is excluded here."""
    return [
        out.get("address", "")
        for out in outputs
        if isinstance(out, dict)
        and feat_mod.is_script_address(out.get("address", ""))
        and feat_mod._extract_datum_info(out, datums)[0] == 1
        and feat_mod._extract_datum_info(out, datums)[1] == 0
    ]


# Which gate condition admitted a datum. The distinction is recorded as a reason
# so an analyst can see whether the datum's CONTENT corroborated the size, but it
# does NOT decide the band: see the allowlist cap in _score_utxo for the only
# thing that does.
_TRIGGER_CONTENT = "content"
_TRIGGER_SIZE_BACKSTOP = "size_backstop"
# Backstop admission where the content could not be measured at all. Kept
# distinct from _TRIGGER_SIZE_BACKSTOP because "content says benign" and
# "content unreadable" must never be treated alike: the attacker writes those
# bytes (see features.datum_content_assessable).
_TRIGGER_SIZE_BACKSTOP_UNASSESSED = "size_backstop_unassessed"


def _bloat_trigger(
    output: dict[str, Any],
    datum_bytes: int,
    datums: dict[str, Any] | None = None,
) -> str | None:
    """Which gate admits this datum as a bloat-DoS candidate, or None.

    Triggers:
      - content gate: a large datum (``>= _MIN_DATUM_BYTES``) that is either
        low-entropy padding (``<= _BLOAT_ENTROPY_MAX``) OR structurally
        concentrated in one CBOR leaf (``>= _LEAF_CONCENTRATION_MAX``). The
        concentration branch catches single-leaf padding even when the padding
        bytes are high-entropy. Returns ``_TRIGGER_CONTENT``.
      - absolute backstop: ``datum_bytes >= _SIZE_BACKSTOP`` admits a datum
        whose content did NOT trip either branch above, because it nears the
        point where a consuming tx can no longer fit under maxTxSize. Returns
        ``_TRIGGER_SIZE_BACKSTOP`` when the content was actually measured and
        ``_TRIGGER_SIZE_BACKSTOP_UNASSESSED`` when it could not be.

    The content gate is evaluated first so that a datum large enough to trip
    both is reported as content-triggered: an oversized LOW-entropy datum is a
    bloat attack twice over.
    """
    if datum_bytes >= _MIN_DATUM_BYTES and (
        feat_mod.datum_shannon_entropy_bits(output, datums) <= _BLOAT_ENTROPY_MAX
        or feat_mod.datum_leaf_concentration(output, datums) >= _LEAF_CONCENTRATION_MAX
    ):
        return _TRIGGER_CONTENT
    if datum_bytes >= _SIZE_BACKSTOP:
        if feat_mod.datum_content_assessable(output, datums):
            return _TRIGGER_SIZE_BACKSTOP
        return _TRIGGER_SIZE_BACKSTOP_UNASSESSED
    return None


def _is_large_state_allowlisted(script_addr: str, network: str) -> bool:
    """True when this script is a known legitimate large-state contract.

    Prefix + network semantics are identical to ``multiple_sat`` and
    ``token_dust``: see :func:`app.analysis.scorer_config.load_network_map`.
    """
    return any(script_addr.startswith(p) for p in _LARGE_STATE_ALLOWLIST.get(network, ()))


def _per_script_datum_bytes(outputs, datums=None):
    """Return ``{payment_credential: total_datum_bytes}`` across script
    outputs.

    Aggregation is keyed by payment credential so the multi-output
    bloat shape ("N inflated outputs at the SAME contract") aggregates
    correctly across stake-credential variants of the same script, and
    does NOT aggregate across distinct contracts (where the carry-
    forward DoS mechanism does not apply). Falls back gracefully when
    ``_payment_credential`` cannot bech32-decode the address: the raw
    address is used as the key, so two outputs at the same raw address
    still group together.
    """
    by_script: dict[str, int] = {}
    for out in outputs:
        addr = out.get("address", "")
        if not feat_mod.is_script_address(addr):
            continue
        datum_flag, datum_bytes = feat_mod._extract_datum_info(out, datums)
        if datum_flag == 0:
            continue
        key = _payment_credential(addr)
        by_script[key] = by_script.get(key, 0) + datum_bytes
    return by_script


class LargeDatumScorer(BaseScorer):
    name = "large_datum"

    def gate(self, features: dict[str, Any]) -> bool:
        """Engage scoring when either a single script datum exceeds the
        per-output floor (canonical DoS shape) OR the sum of datum bytes
        AT THE SAME SCRIPT crosses ``aggregate_engagement_min``
        (observability path for multi-output bloat).

        The per-output predicate is what produces an alert: only when
        one UTxO's datum saturates do downstream users have to copy
        bloated state. The one exception is a script listed in
        ``large_state_allowlist_prefixes``, whose backstop-only findings are
        capped below the alerting bands (see ``_score_utxo``); the shipped
        config lists none. The aggregate predicate engages the scorer but
        does NOT contribute to ``max_score`` or band; it exists so the
        ``max_script_datum_bytes`` sub-score reaches storage when an
        attacker splits a bloat payload across N outputs of the same
        contract, each of size ``< min_datum_bytes``. Per-script
        aggregation prevents benign cross-contract DeFi composition
        (e.g. DEX-A 3.5KB state + DEX-B 3.5KB state) from engaging.
        """
        raw_data = features.get("raw_data")
        if not raw_data or not isinstance(raw_data, dict):
            return False
        outputs = raw_data.get("outputs", [])
        datums = raw_data.get("datums")  # tx witness datum map (hash -> preimage)
        for out in outputs:
            addr = out.get("address", "")
            if not feat_mod.is_script_address(addr):
                continue
            _, datum_bytes = feat_mod._extract_datum_info(out, datums)
            if _bloat_trigger(out, datum_bytes, datums) is not None:
                return True
        per_script = _per_script_datum_bytes(outputs, datums)
        if any(v >= _AGGREGATE_ENGAGEMENT_MIN for v in per_script.values()):
            return True
        # Observability path: a datum-hash-only output reports 0 bytes
        # (_extract_datum_info cannot size the referenced datum without an
        # indexer), so a bloat-by-hash attack is invisible to the byte gates
        # above. Engage so score() records datum_hash_only_count; the result
        # stays a no-finding (-1) and never alerts.
        if _FLAG_DATUM_HASH_ONLY and _datum_hash_only_addresses(outputs, datums):
            return True
        return False

    def score(self, features: dict[str, Any]) -> ScorerResult:
        raw_data = features.get("raw_data", {})
        network = features.get("network", "")
        outputs = raw_data.get("outputs", [])
        datums = raw_data.get("datums")  # tx witness datum map (hash -> preimage)

        # Per-script aggregate datum bytes. The largest same-script
        # aggregate is the observability metric: it identifies a single
        # contract under bloat pressure, not a tx-wide sum across
        # unrelated scripts. The per-output predicate below still drives
        # scoring; this is for analyst queries only.
        per_script = _per_script_datum_bytes(outputs, datums)
        max_script_datum_bytes = max(per_script.values(), default=0)

        candidates = []

        for out in outputs:
            addr = out.get("address", "")
            if not feat_mod.is_script_address(addr):
                continue
            datum_flag, datum_bytes = feat_mod._extract_datum_info(out, datums)
            if datum_flag == 0:
                continue
            trigger = _bloat_trigger(out, datum_bytes, datums)
            if trigger is None:
                continue

            candidates.append(
                self._score_utxo(
                    out,
                    addr,
                    datum_bytes,
                    datum_flag,
                    network,
                    max_script_datum_bytes,
                    trigger=trigger,
                )
            )

        best = reduce_to_best(candidates)
        # Deliberately keyed on sub_scores (not score) to preserve the
        # historical contract: only a winner that produced a sub-score
        # breakdown counts as a per-output finding.
        if best.sub_scores:
            return best

        # Aggregate-only / hash-only engagement path: gate fired because the
        # per-script aggregate crossed `aggregate_engagement_min` or a
        # datum-hash-only script output was present, but no single output
        # passed the per-output threshold. Surface the observability metrics
        # while returning score=-1 so the engine does NOT select
        # `large_datum` as `max_class` (-1 is filtered out by
        # `applicable = {k: v ... if v >= 0}`); writing -1 to the column
        # matches the existing "scorer didn't produce a finding" convention.
        hash_only_addrs = _datum_hash_only_addresses(outputs) if _FLAG_DATUM_HASH_ONLY else []
        return ScorerResult.no_finding(
            sub_scores={
                "max_script_datum_bytes": float(max_script_datum_bytes),
                "datum_hash_only_count": float(len(hash_only_addrs)),
            },
            evidence=({"datum_hash_only_addresses": hash_only_addrs} if hash_only_addrs else {}),
        )

    def _score_utxo(
        self,
        output: dict,
        address: str,
        datum_bytes: int,
        datum_flag: int,
        network: str,
        max_script_datum_bytes: int,
        trigger: str,
    ) -> ScorerResult:
        value = output.get("value", {})
        if not isinstance(value, dict):
            value = {"lovelace": 0}

        value_cbor = feat_mod._estimate_value_cbor_bytes(value)
        utxo_total = feat_mod.estimate_utxo_total_bytes(
            address, value_cbor, datum_bytes, output.get("script")
        )
        datum_ratio = feat_mod.datum_ratio_of(datum_bytes, utxo_total)

        # datum_bytes: per-script baseline
        p50_db, p99_db, bl1 = _resolve(
            "datum_bytes",
            "per_script",
            address,
            network,
            _BOOT,
            "datum_bytes",
        )
        # value_cbor_bytes: per-script baseline (for inversion)
        p50_cb, p99_cb, _ = _resolve(
            "value_cbor_bytes",
            "per_script",
            address,
            network,
            _BOOT,
            "value_cbor_bytes",
        )
        p50_r, p99_r = _anchor(_FIXED, "datum_ratio")
        bl_source = bl1

        # Sub-scores
        s_datum = normalise(datum_bytes, p50=p50_db, p99=p99_db)
        s_ratio = normalise(datum_ratio, p50=p50_r, p99=p99_r)
        s_value_inv = normalise_inverted(value_cbor, p50=p50_cb, p99=p99_cb)
        # Blind spot: recurrence/steady-state suppression (a contract that
        # emits the same datum size every block is benign protocol traffic, a
        # spiky novel datum is the attack) needs entity clustering, deferred to
        # mainnet. Its weight contributes 0 until then.
        s_recurrence = 0.0

        raw = (
            float(_W["datum_bytes"]) * s_datum
            + float(_W["datum_ratio"]) * s_ratio
            + float(_W["value_cbor_inv"]) * s_value_inv
            + float(_W["recurrence"]) * s_recurrence
        )
        final = finalise_score(raw)

        reasons = []
        if s_datum > _REASON_T:
            reasons.append("large_datum_bytes")
        if s_ratio > _REASON_T:
            reasons.append("high_datum_ratio")
        if s_value_inv > _REASON_T:
            reasons.append("lean_value_field")

        # Record whether the datum's CONTENT corroborated its size. This is
        # evidence for the analyst, not an input to the band: a backstop-only
        # finding keeps its full score.
        if trigger == _TRIGGER_SIZE_BACKSTOP:
            reasons.append("size_backstop_only")
        elif trigger == _TRIGGER_SIZE_BACKSTOP_UNASSESSED:
            reasons.append("size_backstop_content_unreadable")

        # Known-legitimate large-state contract: cap out of the alerting bands.
        # A contract whose normal state IS a near-backstop datum re-spends that
        # UTxO on every transition, which falsifies the backstop's "this UTxO can
        # no longer be spent" premise. Both driving axes then saturate for
        # definitional rather than adversarial reasons: a datum-heavy UTxO is
        # nearly all datum, and the ADA it holds is the min-ADA that datum size
        # forces. With no per-script baseline yet (BASELINE_MIN_SAMPLES) and
        # recurrence stubbed to 0, nothing in the score separates that from an
        # attack, so the suppression has to come from outside the score.
        #
        # No such contract is known yet, on any network, so the shipped
        # allowlist is empty and this branch is inert. Note in particular that
        # the mainnet 2026-07-26 burst is NOT an instance: its datum measures
        # entropy 0.2749 and leaf concentration 0.9662, so it is
        # content-triggered and never reaches this cap. Its volume is bounded in
        # the delivery path instead (app.notifications.grouping).
        #
        # It is keyed on the SCRIPT, never on the datum's shape. Shape cannot
        # carry it: an attacker chooses the bytes freely, so every "benign
        # shape" is reproducible at a victim script (13 KB of random 32-byte
        # leaves reads as legitimate registry state on both content axes), and
        # a datum delivered by hash or encoded past cbor2's nesting limit is
        # not measurable at all. The contract's address is the one thing only
        # its operator controls.
        #
        # Gated on _TRIGGER_SIZE_BACKSTOP alone, which already carries the
        # content requirement: _bloat_trigger returns the _UNASSESSED variant
        # when the datum could not be read, so an allowlisted script that starts
        # serving unreadable datums re-alerts. Re-testing assessability here
        # would be a second source of truth for the same question, and a second
        # CBOR parse of a 12 KB payload.
        if trigger == _TRIGGER_SIZE_BACKSTOP and _is_large_state_allowlisted(address, network):
            final = min(final, BAND_MODERATE_MAX)
            reasons.append("large_state_allowlisted")

        datum_type = "inline" if datum_flag == 2 else "hash"
        lovelace = feat_mod.extract_lovelace(value)

        return ScorerResult(
            score=final,
            sub_scores={
                "datum_bytes": round(s_datum, 4),
                "datum_ratio": round(s_ratio, 4),
                "value_cbor_bytes_inverted": round(s_value_inv, 4),
                "sender_recurrence": round(s_recurrence, 4),
                "max_script_datum_bytes": float(max_script_datum_bytes),
            },
            reasons=reasons,
            baseline_source=bl_source,
            evidence={
                "datum_bytes_raw": int(datum_bytes),
                "utxo_total_bytes": int(utxo_total),
                "datum_type": datum_type,
                "datum_utxo_ratio": round(datum_ratio, 4),
                "target_script_address": address,
                "value_cbor_bytes_raw": int(value_cbor),
                "lovelace_amount": lovelace,
                # Which gate admitted this datum, in EVIDENCE rather than only
                # in `reasons`, because evidence is the part that reaches
                # ClickHouse (tx_class_scores has sub_scores + evidence columns
                # and no reasons column). Triaging backstop-only findings, which
                # is how `large_state_allowlist_prefixes` entries get sourced,
                # needs this to be queryable.
                "bloat_trigger": trigger,
            },
        )
