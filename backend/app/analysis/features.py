"""Extended feature extraction from raw Ogmios transaction data.

Extracts UTxO-level and transaction-level features beyond what the ingestion
parser captures: CBOR byte sizes, datum information, redeemer counts, execution
units, and minting details.  These features feed the 9 attack-class scoring
pipelines defined in the Polimi detection spec.

All functions accept a raw_data dict (the original Ogmios JSON payload,
already stored per transaction) and return structured tuples ready for
ClickHouse insertion.
"""

import json
import logging
import math
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from app.analysis.normalise import EPSILON

logger = logging.getLogger(__name__)

# Lazy-load cbor2 to avoid import errors when the package is not installed
# (e.g. during lightweight testing of unrelated modules).
_cbor2 = None


def get_cbor2():
    global _cbor2
    if _cbor2 is None:
        import cbor2
        _cbor2 = cbor2
    return _cbor2


# ---------------------------------------------------------------------------
# Address classification
# ---------------------------------------------------------------------------

def has_spend_redeemer(raw_data: Dict[str, Any]) -> bool:
    """True if the tx has at least one spend-purpose redeemer (Plutus input).

    Native scripts never carry redeemers: they are declarative ledger predicates
    (signatures, timelocks, n-of-m clauses) evaluated without Plutus execution.
    By contract (Cardano ledger: ``dom txrdmrs ≡ᵉ scriptRdrptrs``), every
    Plutus-script input must have exactly one ``spend`` redeemer; therefore
    "no spend redeemer" implies "all script inputs are native".

    This is the structural test the ``multiple_sat`` gate uses to skip
    native-script multisig wallets, which are immune to multiple-satisfaction
    by construction (no validator code to short-circuit).

    Caveat: a True return does not localise the Plutus inputs to a specific
    script address. In a mixed tx (Plutus mint or spend on script A combined
    with native-script consolidation on script B), this returns True even
    though script B's inputs are still native. Callers needing per-script
    granularity must inspect redeemer pointers (``spend:N`` keys or
    ``validator.index`` fields) and map them to input indices.
    """
    redeemers = raw_data.get("redeemers")
    if not redeemers:
        return False
    if isinstance(redeemers, dict):
        # Ogmios v5 keys are "spend:N" / "mint:N" / "publish:N" / "withdraw:N".
        return any(str(k).startswith("spend") for k in redeemers.keys())
    if isinstance(redeemers, list):
        for r in redeemers:
            if not isinstance(r, dict):
                continue
            tag = r.get("validator")
            if isinstance(tag, dict) and tag.get("purpose") == "spend":
                return True
            if r.get("purpose") == "spend":
                return True
    return False


# CIP-19 Shelley address types whose PAYMENT credential is a script hash.
# The header high nibble encodes the type; the first Bech32 data character
# follows from it (shared by mainnet and testnet since the network bit does
# not reach it):
#   type 1 -> 'z' (script payment + stake key)
#   type 3 -> 'x' (script payment + script stake)
#   type 5 -> '2' (script payment + pointer stake, deprecated but valid)
#   type 7 -> 'w' (script payment, no stake / enterprise)
# Type 2 ('y') is payment-KEY + script-stake and is deliberately excluded:
# the spending credential is a key, so script-targeted attacks do not apply.
SCRIPT_ADDRESS_PREFIXES = (
    "addr1w", "addr1z", "addr1x", "addr12",
    "addr_test1w", "addr_test1z", "addr_test1x", "addr_test12",
)


def is_script_address(address: str) -> bool:
    """Detect whether a Cardano address is a script (validator) address.

    Matches every CIP-19 address type whose payment credential is a script
    hash (see ``SCRIPT_ADDRESS_PREFIXES``). Byron-era addresses (Ae2...,
    DdzFF...) are never script addresses.
    """
    if not address:
        return False
    return address.lower().startswith(SCRIPT_ADDRESS_PREFIXES)


# ---------------------------------------------------------------------------
# UTxO-level feature extraction
# ---------------------------------------------------------------------------

def extract_lovelace(value: Any) -> int:
    """Lovelace out of an Ogmios value dict, handling both schema versions.

    Ogmios v5 emits ``{"lovelace": N, <policy>: {...}}`` at the top level.
    Ogmios v6 nests ADA: ``{"ada": {"lovelace": N}, <policy>: {...}}``.
    A bare int is also accepted for callers that flatten the value upstream.
    Returns ``0`` on anything unrecognised so callers can default safely.
    """
    if isinstance(value, dict):
        # Same defensive contract as the bare path: a malformed quantity in
        # untrusted chain data must degrade to 0 (tx still ingested and
        # scored), never abort the parse and skip the tx (recall-first).
        try:
            ada = value.get("ada")
            if isinstance(ada, dict):
                return int(ada.get("lovelace", 0) or 0)
            return int(value.get("lovelace", 0) or 0)
        except (TypeError, ValueError):
            return 0
    if value:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


def estimate_utxo_total_bytes(
    address: str, value_cbor: int, datum_bytes: int, script_ref: Any
) -> int:
    """Approximate the on-chain UTxO size in bytes: address + value CBOR + datum +
    script-ref bytes. Single-sourced so the stored large_datum feature and the
    score-time recompute cannot drift."""
    address_bytes = len(address.encode()) if address else 0
    script_bytes = len(json.dumps(script_ref).encode()) if script_ref else 0
    return address_bytes + value_cbor + datum_bytes + script_bytes


def datum_ratio_of(datum_bytes: int, utxo_total: int) -> float:
    """``datum_bytes / utxo_total`` with the shared EPSILON guard; 0.0 when the
    UTxO has no bytes. The guard must match between the stored feature and the
    score-time recompute (the large_datum separator anchors are calibrated on
    this exact ratio)."""
    return datum_bytes / (utxo_total + EPSILON) if utxo_total > 0 else 0.0


def extract_fee(tx_data: Any) -> int:
    """Transaction fee in lovelace, handling both Ogmios schema versions.

    The fee field carries the same shape as a value's ADA component
    (v6 ``{"ada": {"lovelace": N}}``, v5 ``{"lovelace": N}``, or a bare
    number), so this delegates to :func:`extract_lovelace`. The two
    previously hand-rolled copies of this branching (block parser and
    mempool collision capture) are the exact duplication class that let
    the v6 mempool-resolution bug ship; one shared reader removes it.
    """
    if not isinstance(tx_data, dict):
        return 0
    return extract_lovelace(tx_data.get("fee", {}))


def flatten_assets(value: Any) -> Dict[str, int]:
    """Flatten an Ogmios value dict's native assets to ``{"policy.name": qty}``.

    Skips the ``ada`` (v6) and ``lovelace`` (v5) components; nested
    ``{policy: {name: qty}}`` bundles flatten to dotted keys, and already-
    flat entries (legacy v5 paths) pass through. This is the storage shape
    used by ``transaction_inputs.assets`` / resolved-input caches; the
    block parser and the mempool UTxO resolver previously kept
    character-identical copies of this loop.
    """
    assets: Dict[str, int] = {}
    if not isinstance(value, dict):
        return assets

    def _qty(raw: Any) -> int:
        # Same defensive contract as extract_lovelace: a malformed quantity in
        # untrusted chain data degrades to 0 with the asset key PRESERVED, so
        # the asset's presence still shows. It must never raise here, because
        # this builds transaction_inputs.assets — an exception would drop the
        # whole transaction from the warehouse and create a detection blind
        # spot (recall-first).
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    for key, val in value.items():
        if key in ("lovelace", "ada"):
            continue
        if isinstance(val, dict):
            for asset_name, qty in val.items():
                assets[f"{key}.{asset_name}"] = _qty(qty)
        else:
            assets[key] = _qty(val)
    return assets


def iter_assets(val: Any):
    """Yield ``((policy_id, asset_name), qty)`` pairs from an Ogmios value dict.

    Skips the lovelace component. Handles both v5 (`{"lovelace": N, policy: {asset: qty}}`)
    and v6 (`{"ada": {"lovelace": N}, policy: {asset: qty}}`) shapes.
    Entries whose quantity does not parse as an integer are skipped, and
    flat (non-dict) policy entries are ignored: callers that must also
    honour legacy flat entries use :func:`flatten_assets` instead.
    """
    if not isinstance(val, dict):
        return
    for policy, inner in val.items():
        if policy in ("ada", "lovelace"):
            continue
        if not isinstance(inner, dict):
            continue
        for asset_name, qty in inner.items():
            try:
                yield (policy, asset_name), int(qty)
            except (TypeError, ValueError):
                continue


def extract_ttl(tx_data: Any) -> int:
    """Transaction TTL (the slot after which the tx is invalid), handling both
    Ogmios schema versions.

    Ogmios v6 emits ``validityInterval.invalidAfter``; v5 emits ``timeToLive``.
    Returns ``0`` when absent or unparseable so callers can default safely.
    """
    if not isinstance(tx_data, dict):
        return 0
    vi = tx_data.get("validityInterval")
    if isinstance(vi, dict) and vi.get("invalidAfter") is not None:
        try:
            return int(vi["invalidAfter"])
        except (TypeError, ValueError):
            return 0
    try:
        return int(tx_data.get("timeToLive", 0) or 0)
    except (TypeError, ValueError):
        return 0


def decode_hex_asset_name(hex_name: str) -> str:
    """Decode a hex-encoded asset name to UTF-8, falling back to the raw string.

    Ogmios emits native-asset names as hex. The fallback keeps the contract
    total (callers always get a string back); a pure-hex fallback contains no
    ``.`` so it can never satisfy URL/domain matching downstream.
    """
    try:
        return bytes.fromhex(hex_name).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return hex_name


def _estimate_value_cbor_bytes(value: Dict[str, Any]) -> int:
    """Estimate the CBOR byte size of an Ogmios output value dict.

    Ogmios represents the value as {"lovelace": N, "policyId": {"assetName": qty}}.
    We re-encode a simplified structure to CBOR to approximate the on-chain size.
    """
    try:
        cbor2 = get_cbor2()
        # Build a structure similar to the on-chain CBOR encoding:
        # - If only lovelace: just an integer
        # - If multi-asset: (lovelace, {policy_bytes: {asset_bytes: qty}})
        # Handle both Ogmios v5 {"lovelace": N, ...} and v6 {"ada": {"lovelace": N}, ...}
        ada_obj = value.get("ada")
        if isinstance(ada_obj, dict):
            lovelace = ada_obj.get("lovelace", 0)
        else:
            lovelace = value.get("lovelace", 0)
        assets = {}
        for key, val in value.items():
            if key in ("lovelace", "ada"):
                continue
            if isinstance(val, dict):
                assets[key.encode()] = {
                    aname.encode(): int(qty) for aname, qty in val.items()
                }
            else:
                assets[key.encode()] = int(val)

        if not assets:
            return len(cbor2.dumps(int(lovelace)))
        return len(cbor2.dumps([int(lovelace), assets]))
    except Exception:
        # Fallback: rough estimate based on JSON size
        return len(json.dumps(value).encode())



def _nonzero_qty(q: Any) -> bool:
    """Return True if the asset quantity is non-zero (positive or negative)."""
    try:
        return int(q) != 0
    except (TypeError, ValueError):
        return False


def count_assets(value: Dict[str, Any]) -> Tuple[int, int]:
    """Count unique policy IDs and live asset classes in a value dict.

    Zero-quantity entries are skipped: they appear in mint/burn payloads
    where a policy is listed with qty=0 (burn-only). Counting them inflates
    ``unique_token_count`` and causes false positives in token_dust scoring
    on wallet sweeps of previously held assets.

    Returns ``(unique_policy_count, unique_token_count)``.
    """
    policies = set()
    token_count = 0
    for key, val in value.items():
        if key in ("lovelace", "ada"):
            continue
        if isinstance(val, dict):
            live = sum(1 for q in val.values() if _nonzero_qty(q))
            if live:
                policies.add(key)
                token_count += live
        elif _nonzero_qty(val):
            policies.add(key)
            token_count += 1
    return len(policies), token_count


def _extract_datum_info(
    output: Dict[str, Any], datums: Optional[Dict[str, Any]] = None,
) -> Tuple[int, int]:
    """Extract datum presence flag and byte size from an Ogmios output.

    Returns (datum_present, datum_bytes).
    datum_present: 0=none, 1=datum_hash, 2=inline_datum

    ``datums`` is the transaction's witness datum map (hash -> preimage), when
    the ingested payload carries it. A datum-hash-only output whose preimage is
    supplied in the same transaction can then be sized from that preimage
    instead of being reported as 0 bytes (which let a bloat-by-hash attack
    slip the byte gates). Absent the map, the size stays unknown (needs a
    standalone datum indexer).
    """
    # Inline datum (Babbage era)
    datum = output.get("datum")
    if datum is not None:
        if isinstance(datum, str):
            # Hex-encoded CBOR datum
            return 2, len(datum) // 2
        if isinstance(datum, dict):
            encoded = json.dumps(datum).encode()
            return 2, len(encoded)
        return 2, 0

    # Datum hash only
    datum_hash = output.get("datumHash")
    if datum_hash:
        if datums and datum_hash in datums:
            preimage = datums[datum_hash]
            if isinstance(preimage, str):
                return 1, len(preimage) // 2
            if isinstance(preimage, dict):
                return 1, len(json.dumps(preimage).encode())
        return 1, 0  # hash present but preimage not carried in this tx

    return 0, 0


def _object_datum_byte_leaves(datum: Any) -> List[bytes]:
    """Decoded byte-string leaves of an Ogmios Plutus-Data-JSON datum object.

    Walks the ``{"bytes": hex}`` / ``{"list": [...]}`` / ``{"map": [...]}`` /
    ``{"constructor": n, "fields": [...]}`` shape iteratively (no recursion, so
    a maliciously deep object datum cannot blow the stack) and returns each
    ByteArray leaf's raw bytes. Used so the entropy / leaf-concentration bloat
    discriminators can also assess an OBJECT-shaped inline datum, not only the
    hex-string form (an object datum otherwise scored as "not assessable" and
    bypassed the content gate).
    """
    leaves: List[bytes] = []
    stack: List[Any] = [datum]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            b = node.get("bytes")
            if isinstance(b, str):
                try:
                    leaves.append(bytes.fromhex(b))
                except ValueError:
                    pass
            for key in ("list", "fields"):
                v = node.get(key)
                if isinstance(v, list):
                    stack.extend(v)
            m = node.get("map")
            if isinstance(m, list):
                for entry in m:
                    if isinstance(entry, dict):
                        stack.append(entry.get("k"))
                        stack.append(entry.get("v"))
            # Generic (non-Plutus-JSON) dict: descend into all values.
            if not any(k in node for k in ("bytes", "list", "fields", "map")):
                stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return leaves


# Maximum possible Shannon entropy for byte data (log2(256)); used as the
# "not assessable / not padding" default so the bloat check never fires on a
# datum we cannot measure.
_MAX_BYTE_ENTROPY_BITS = 8.0


def datum_shannon_entropy_bits(output: Dict[str, Any]) -> float:
    """Shannon entropy (bits/byte) of an inline datum's raw bytes.

    A datum-bloat DoS pads the datum with repetitive, low-information bytes to
    inflate its size cheaply, which yields near-zero entropy (observed CTF
    bloat: ~0.3-1.5 bits/byte). A legitimate large datum carries structured
    contract state with entropy near the 8-bit ceiling (~7 bits/byte observed).
    Absolute datum size cannot separate the two (a real ~7KB attack overlaps a
    benign ~7KB contract), so entropy is the discriminator.

    Returns ``_MAX_BYTE_ENTROPY_BITS`` (treated as "not padding") when there is
    no hex inline datum to assess (object datum, datum-hash-only, or absent),
    so the bloat check only fires on a measured low-entropy hex datum.

    Limitation: an adaptive attacker could pad with random (high-entropy) bytes
    to evade this; per-script size baselines / recurrence are the complementary
    defence (deferred, see large_datum recurrence stub).
    """
    datum = output.get("datum")
    if isinstance(datum, dict):
        # Object-shaped inline datum: measure entropy over its decoded
        # ByteArray leaves so object-form padding is assessable too. No byte
        # leaves -> not assessable (return the not-padding default).
        raw = b"".join(_object_datum_byte_leaves(datum))
        if not raw:
            return _MAX_BYTE_ENTROPY_BITS
    elif isinstance(datum, str) and len(datum) >= 2:
        try:
            raw = bytes.fromhex(datum)
        except ValueError:
            return _MAX_BYTE_ENTROPY_BITS
    else:
        return _MAX_BYTE_ENTROPY_BITS
    n = len(raw)
    if n == 0:
        return _MAX_BYTE_ENTROPY_BITS
    counts = Counter(raw)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _max_primitive_leaf_bytes(obj: Any) -> int:
    """Largest single primitive leaf (bytes/text/int) in a decoded PlutusData tree.

    Walks iteratively (an explicit stack, not recursion, so a maliciously deep
    datum cannot blow the Python stack). cbor2 represents a Plutus Constr as a
    CBORTag whose ``.value`` holds the fields; lists/maps are containers.
    """
    best = 0
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, (bytes, bytearray)):
            best = max(best, len(o))
        elif isinstance(o, str):
            best = max(best, len(o.encode()))
        elif isinstance(o, bool):
            continue  # bool is an int subclass; contributes nothing meaningful
        elif isinstance(o, int):
            best = max(best, (o.bit_length() + 7) // 8 or 1)
        elif isinstance(o, (list, tuple)):
            stack.extend(o)
        elif isinstance(o, dict):
            for k, v in o.items():
                stack.append(k)
                stack.append(v)
        elif hasattr(o, "value"):  # cbor2 CBORTag (Plutus Constr / wrapped value)
            stack.append(o.value)
    return best


def datum_leaf_concentration(output: Dict[str, Any]) -> float:
    """Fraction of total datum bytes held by the single largest CBOR leaf.

    A datum-bloat attack concentrates its bytes in one oversized primitive leaf
    (a giant ByteArray), giving a ratio near 1.0; legitimate large state is
    bounded heterogeneous nesting that spreads bytes across many small leaves,
    giving a ratio near 0 (observed ~0.005). Unlike entropy, this is a
    STRUCTURAL signal: padding the leaf with random (high-entropy) bytes does not
    lower it, so it catches the high-entropy single-leaf bloat that the entropy
    gate misses.

    Returns 0.0 ("not concentrated / not assessable") when there is no hex inline
    datum, the hex is malformed, or the CBOR cannot be decoded, so the bloat
    check only fires on a measured high concentration and degrades safely (the
    entropy gate and size backstop still apply) when cbor2 is unavailable.
    """
    datum = output.get("datum")
    if isinstance(datum, dict):
        # Object-shaped inline datum: concentration over its decoded ByteArray
        # leaves (largest leaf / total leaf bytes), mirroring the hex path so
        # single-leaf padding in object form is caught too.
        leaves = _object_datum_byte_leaves(datum)
        total = sum(len(b) for b in leaves)
        if total == 0:
            return 0.0
        return max(len(b) for b in leaves) / total
    if not isinstance(datum, str) or len(datum) < 2:
        return 0.0
    try:
        raw = bytes.fromhex(datum)
    except ValueError:
        return 0.0
    n = len(raw)
    if n == 0:
        return 0.0
    try:
        obj = get_cbor2().loads(raw)
    except Exception:
        return 0.0
    return _max_primitive_leaf_bytes(obj) / n


def extract_utxo_features(
    tx_hash: str,
    network: str,
    raw_data: Dict[str, Any],
) -> List[tuple]:
    """Extract UTxO-level features from raw Ogmios transaction data.

    Returns a list of tuples ready for insert_utxo_features().
    """
    outputs = raw_data.get("outputs", [])
    datums = raw_data.get("datums")  # tx witness datum map (hash -> preimage)
    rows = []
    for idx, out in enumerate(outputs):
        address = out.get("address", "")
        value = out.get("value", {})
        if not isinstance(value, dict):
            value = {"lovelace": int(value) if value else 0}

        ada_obj = value.get("ada")
        if isinstance(ada_obj, dict):
            ada_amount = ada_obj.get("lovelace", 0)
        else:
            ada_amount = value.get("lovelace", 0)
        value_cbor = _estimate_value_cbor_bytes(value)
        policy_count, token_count = count_assets(value)
        datum_flag, datum_bytes = _extract_datum_info(out, datums)

        utxo_total = estimate_utxo_total_bytes(
            address, value_cbor, datum_bytes, out.get("script")
        )
        datum_ratio = datum_ratio_of(datum_bytes, utxo_total)

        rows.append((
            tx_hash,
            network,
            idx,
            address,
            1 if is_script_address(address) else 0,
            int(ada_amount),
            value_cbor,
            policy_count,
            token_count,
            datum_flag,
            datum_bytes,
            round(datum_ratio, 4),
            utxo_total,
        ))
    return rows


# ---------------------------------------------------------------------------
# Transaction-level script feature extraction
# ---------------------------------------------------------------------------

def extract_tx_script_features(
    tx_hash: str,
    network: str,
    raw_data: Dict[str, Any],
) -> Optional[tuple]:
    """Extract transaction-level script execution features.

    Returns a single tuple ready for insert_tx_script_features(),
    or None if no script features are present (simple payment tx).
    """
    redeemers = raw_data.get("redeemers")
    mint = raw_data.get("mint")

    # Count spending inputs (non-reference, non-collateral)
    inputs = raw_data.get("inputs", [])
    spending_inputs = len(inputs)

    redeemers_count = 0
    exunits_mem = 0
    exunits_cpu = 0

    if redeemers:
        if isinstance(redeemers, list):
            redeemers_count = len(redeemers)
            for r in redeemers:
                budget = r.get("executionUnits", r.get("budget", {}))
                exunits_mem += int(budget.get("memory", 0))
                exunits_cpu += int(budget.get("cpu", budget.get("steps", 0)))
        elif isinstance(redeemers, dict):
            # Ogmios v5 uses a dict keyed by "spend:N", "mint:N", etc.
            redeemers_count = len(redeemers)
            for key, r in redeemers.items():
                budget = r.get("executionUnits", r.get("budget", {}))
                exunits_mem += int(budget.get("memory", 0))
                exunits_cpu += int(budget.get("cpu", budget.get("steps", 0)))

    mint_policy_count = 0
    mint_entries_json = ""
    if mint:
        if isinstance(mint, dict):
            mint_policy_count = len(mint)
            entries = []
            for policy_id, assets in mint.items():
                if isinstance(assets, dict):
                    for asset_name, qty in assets.items():
                        entries.append({
                            "policy_id": policy_id,
                            "asset_name": asset_name,
                            "quantity": int(qty),
                        })
            mint_entries_json = json.dumps(entries) if entries else ""

    # Only store a row if there's script activity worth recording
    if redeemers_count == 0 and mint_policy_count == 0:
        return None

    return (
        tx_hash,
        network,
        redeemers_count,
        spending_inputs,
        exunits_mem,
        exunits_cpu,
        mint_policy_count,
        mint_entries_json,
    )
