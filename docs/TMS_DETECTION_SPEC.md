# TMS Detection Specification

This document defines 9 attack classes that the TMS must detect on the Cardano blockchain. For each attack: what it is, what on-chain data to extract, how to score it, and what the TMS Forge test tool produces so you can validate your detection pipeline.

All scoring uses the continuous 0-100 risk score framework from PolimiDocs (weighted averages of percentile-normalised sub-features, per-script/per-policy baselines, fallback to global baselines when < 200 historical samples).

**Score interpretation bands:**

| Score | Risk | Action |
|-------|------|--------|
| 0-30 | Informational | No action, scored-but-not-alerting baseline (renamed from "Low" 2026-06; legacy values still parsed) |
| 31-59 | Moderate | Flagged for periodic review |
| 60-79 | High | Queued for analyst review |
| 80-100 | Critical | Immediate alert |


## Common Extraction Schema

Every transaction and UTxO observed (confirmed or mempool) should have these features extracted:

### UTxO-Level Features

| Feature | Type | Description |
|---------|------|-------------|
| `address_type` | categorical | Script address vs payment key address; staking credential present or not |
| `lovelace_amount` | integer | ADA locked in the UTxO (in lovelace) |
| `multiasset_count` | integer | Total distinct native asset entries (all policy IDs + token names) |
| `unique_policy_count` | integer | Distinct minting policies in the UTxO's value |
| `unique_tokenname_count` | integer | Distinct asset classes regardless of quantity |
| `value_cbor_bytes` | integer | Byte size of the UTxO's `Value` field in CBOR encoding |
| `datum_present` | ternary | None / datum hash / inline datum |
| `datum_bytes` | integer | Byte size of the datum (inline or resolved from indexer) |
| `utxo_total_bytes` | integer | Full byte footprint: address + value + datum + reference script |

### Transaction-Level Features

| Feature | Type | Description |
|---------|------|-------------|
| `n_inputs` | integer | Total inputs consumed |
| `n_outputs` | integer | Total outputs produced |
| `n_script_inputs` | integer | Inputs from script addresses |
| `n_inputs_same_script` | integer | Inputs sharing the same script hash |
| `n_outputs_to_same_script` | integer | Outputs directed back to the same script |
| `script_hashes_involved` | set | Distinct validator hashes referenced |
| `metadata_present` | boolean | Whether TX carries metadata |
| `metadata_bytes` | integer | Total byte size of metadata |
| `metadata_labels` | list | Numeric keys in the metadata map (674, 721, etc.) |
| `mint_present` | boolean | Whether TX includes a minting action |
| `mint_asset_count` | integer | Distinct assets minted or burned |
| `mint_policy_count` | integer | Distinct policies in the minting action |
| `redeemers_count` | integer | Redeemers in the witness set |
| `exunits_total` | struct | Aggregated execution units (memory + CPU) |
| `mempool_first_seen` | timestamp | When TX was first observed in mempool |
| `block_inclusion_time` | timestamp | Slot and wall-clock time of on-chain confirmation |

### Contextual Features

| Feature | Type | Description |
|---------|------|-------------|
| `address_cluster` | set | Addresses likely controlled by the same entity (UTxO-graph heuristics) |
| `common_change_address` | boolean/address | Same change address across multiple TXs |
| `sender_recurrence` | float | Frequency of this address/cluster as TX submitter in recent window |
| `per_script_p95` / `per_script_p99` | float | Historical percentiles for a feature on a specific script |
| `per_policy_p95` / `per_policy_p99` | float | Historical percentiles for a feature on a specific policy |


## Attack 1: Token Dust

### Definition
A UTxO value size spam attack. The attacker creates a single UTxO carrying hundreds of unique tokens with different PolicyIDs and/or TokenNames, bloating its CBOR representation toward the 16 KB protocol limit. When placed at a script (validator) address, it can make that UTxO unspendable, effectively freezing funds locked at that script.

### Gate Condition
- `address_type == SCRIPT` (attack is only meaningful at script addresses)
- `unique_assetclass_count >= min_token_count` (default 2; tunable via `token_dust.gate.min_token_count`)
- **DoS engagement discriminator**: a bundle enters scoring only when it is a plausible value-bloat DoS: either `>= dos_asset_min` distinct `(policy, name)` pairs (default 15) OR its serialized Value CBOR reaches `dos_value_cbor_fraction` (0.20) of `max_value_size_bytes` (5000, so 1000 bytes). The byte branch makes the gate robust to the long-asset-name evasion (few pairs, long names yield high CBOR). Normal protocol multi-asset UTxOs (2-6 pairs, <=0.5 KB Value CBOR) sit under both thresholds and produce no finding at all.

The minimum-bundle gate is part of the attack definition: a CBOR-bloat attack requires multiple distinct `(policy, name)` entries to inflate the Value field. A single-asset UTxO is bounded in size regardless of quantity and is not in scope for this scorer.

**Moderate cap below `dos_asset_min`**: an output whose bundle has fewer than `dos_asset_min` distinct pairs is capped at the top of Moderate (`BAND_HIGH_THRESHOLD - 1`) even when its three primary sub-scores saturate. A small bundle cannot meaningfully bloat the 16 KB tx-CBOR limit, so the band reflects that structural fact while the saturated sub-scores still record severity. The composite `script_value_bloat_dos` reason and a High+ band require at least `dos_asset_min` pairs.

### Detection Features

| Feature | Role | Weight |
|---------|------|--------|
| `value_cbor_bytes` | Primary: byte footprint of the Value field | 0.35 |
| `unique_assetclass_count` | Primary: high count across many policies = dust signature | 0.35 |
| `lovelace_amount` | Secondary (inverted): low ADA relative to asset count = classical dust | 0.15 |
| `sender_recurrence` | Contextual: repeated dust deposits from same cluster | 0.15 |

### Scoring

```
score_token_dust(utxo):
    if utxo.address_type != SCRIPT: return 0
    baselines = resolve_baselines(utxo.script_hash)

    s_bytes      = normalise(utxo.value_cbor_bytes, baselines)
    s_assets     = normalise(utxo.unique_assetclass_count, baselines)
    s_ada        = 1 - normalise(utxo.lovelace_amount, baselines)  # inverted
    s_recurrence = normalise(utxo.sender_recurrence, baselines)

    score = 0.35 * s_bytes + 0.35 * s_assets + 0.15 * s_ada + 0.15 * s_recurrence
    return clip(score, 0, 1) * 100
```

### False Positive Mitigation
- **Legitimate multi-asset bundles**: marketplace contracts, asset vaults, batch settlement validators routinely handle many assets. Per-script baseline normalisation handles this (their p99 is already high).
- **Per-script allowlist**: known batch-handling contracts bypass scoring or get adjusted weights. The policy-side allowlist (`allowlist_policies`, network-scoped) suppresses bundles made ENTIRELY of verified collection policies: an attacker cannot mint under those policy ids, and smuggling dust alongside them un-allowlists the bundle. Address-prefix allowlisting of permissionless marketplace scripts is deliberately avoided (it would suppress real dust deposits to them).
- **Established-collection cap** (`established_collection`): when every policy in the bundle was first seen at least `min_policy_age_slots` ago (per the `asset_policy_first_seen` lookup, populated inline at ingestion) and nothing in the bundle is minted in this tx, the score is capped at the top of Moderate, never suppressed. The threat model mints worthless names and fires; established collections moving in bulk (staking-vault rollovers, NFT AMM pools, marketplace bulk listings) produce the same mechanical shape from months-old policies. Every degraded-data path fails open to no cap (unknown slot, missing lookup row, lookup error), and the policy-level mint check keeps an old open policy minting fresh junk uncapped. Documented residual: a pre-aged junk policy lands top-of-Moderate instead of High; the finding stays recorded, reported, and corroboration-eligible.
- **Novelty gate**: if all sub-scores < 0.5, suppress the final score.

### Reason Flags
Primary reasons (per sub-score, fired when above `reason_threshold`, default 0.5): `high_value_cbor_bytes`, `many_distinct_assets`, `low_lovelace_amount`. `established_collection_cap` marks a Moderate-capped established-collection bundle.

Composite reason `script_value_bloat_dos`: emitted when all three primary signals saturate at the same script-address output. The shape is the canonical value-bloat denial-of-service signature (many unique native tokens locked at a contract UTxO with minimal ADA cushion, forcing every future spender to carry them forward and pushing min-UTxO and tx-size limits). The class column stays `token_dust` because the underlying observable is correctly "many tokens, low ADA, large CBOR"; the reason flag lets analysts distinguish "bloat a contract" from "spray dust at random addresses" without a schema migration. The score conveys severity; the reason conveys shape.

### What TMS Forge Produces
- Single TX minting `token_count` (1-200) unique tokens named `DUST000`, `DUST001`, etc.
- Distributed across `policy_count` (1-10) distinct PolicyIDs (each using `ScriptAll([ScriptPubkey(vkh), InvalidBefore(nonce)])` for uniqueness)
- Sent to a `ScriptAll([])` always-succeeds script address (default) to satisfy the gate condition
- Look for: high `multiasset_count`, high `unique_policy_count`, high `value_cbor_bytes`, `address_type == SCRIPT`


## Attack 2: Large Value

### Definition
Instead of many different tokens, the attacker creates a UTxO with a single AssetClass at an astronomically large quantity. Large CBOR integer encoding inflates the UTxO byte size. Same end goal as Token Dust (make the UTxO unspendable at a validator).

### Gate Conditions
- `address_type == SCRIPT`
- `unique_assetclass_count <= 2` (distinguishes from Token Dust)

### Detection Features

| Feature | Role | Weight |
|---------|------|--------|
| `quantity_digits` | Primary: decimal digits in the quantity (proxy for CBOR cost). **Per-policy baseline.** | 0.55 |
| `value_cbor_bytes` | Primary: high despite low asset count = defining anomaly. **Per-script baseline.** | 0.20 |
| `lovelace_amount` | Secondary (inverted): minimal ADA = instrumental deposit | 0.15 |
| `sender_recurrence` | Contextual | 0.10 |

Weights were retuned from the Polimi starting points (0.40/0.35/0.10/0.15): a simple single-token UTxO has naturally low `value_cbor_bytes` regardless of quantity, so the byte axis only fires for multi-asset outputs; the heavier `quantity_digits` weight lets a max-quantity (near int64) attack reach High/Critical without the CBOR signal. Tunables live in `config/detection.yaml` under `large_value.weights`.

### Scoring

```
score_large_value(utxo):
    if utxo.address_type != SCRIPT: return 0
    if utxo.unique_assetclass_count > 2: return 0  # route to Token Dust instead

    s_digits     = normalise(utxo.quantity_digits, per_policy_baselines)
    s_bytes      = normalise(utxo.value_cbor_bytes, per_script_baselines)
    s_ada        = 1 - normalise(utxo.lovelace_amount, per_script_baselines)
    s_recurrence = normalise(utxo.sender_recurrence, per_script_baselines)

    score = 0.55 * s_digits + 0.20 * s_bytes + 0.15 * s_ada + 0.10 * s_recurrence
    return clip(score, 0, 1) * 100
```

### What TMS Forge Produces
- Single TX minting 1 token `BIGVAL` with `quantity` up to 10^18
- Sent to the always-succeeds script address
- Look for: `unique_assetclass_count == 1`, extremely high `quantity_digits`, high `value_cbor_bytes`


## Attack 3: Large Datum

### Definition
The attacker attaches an oversized inline datum to a UTxO output. The Value field remains normal; all bloat is in the datum. When consuming this UTxO, the transaction may exceed the protocol's TX size limit.

### Gate Conditions
- `address_type == SCRIPT`
- `datum_present == INLINE` (or resolvable hash) and `datum_bytes != null`
- **Bloat discriminator** (`_is_bloat_datum`): a script output's datum is a candidate only when it is either
  - at or above the absolute size backstop (`size_backstop_fraction` * `max_tx_size_bytes` = 0.75 * 16384 = 12288 bytes), flagged regardless of content because it nears the point a consuming tx cannot fit under `maxTxSize`; or
  - large (`>= min_datum_bytes`, default 6000) AND low-information, where low-information means byte-entropy `<= bloat_entropy_max` (default 4.0 bits/byte: padding attacks observe ~0.3-1.5, legitimate structured state ~7) OR a single CBOR leaf holds `>= leaf_concentration_max` (default 0.5) of the datum bytes (catches high-entropy single-leaf padding the entropy gate misses).

  Absolute size alone cannot separate the two populations: an observed CTF bloat attack carries a 7.3 KB datum, overlapping a benign contract's ~6.9 KB datum. Content entropy and leaf concentration are the discriminators that removed the false positives, which is why the scoring axes below keep their original size-based shape.
- **Aggregate engagement** (observability only): when no single output passes the per-output predicate but the SUM of datum bytes across outputs at the same payment credential reaches `aggregate_engagement_min` (12000), the scorer engages and records `max_script_datum_bytes` in `sub_scores` but returns `score=-1` (no alert, not selected as `max_class`). This surfaces the multi-output split-payload shape for analyst queries without firing.
- **DatumHash-only observability** (`gate.flag_datum_hash_only`, default true): a script output that carries only a `datumHash` (the datum body is off-chain, so its size cannot be measured without an indexer) engages the gate and records `datum_hash_only_count` plus `datum_hash_only_addresses` in evidence, returning `score=-1` (no alert). Without this, hash-only bloat carriers were invisible to the scorer; the gate condition `datum_present == INLINE (or resolvable hash)` previously did not surface the unresolvable-hash case at all.

### Detection Features

| Feature | Role | Weight |
|---------|------|--------|
| `datum_bytes` | Primary: absolute byte size of the datum. Per-script baseline. | 0.50 |
| `datum_ratio` | Derived primary: `datum_bytes / utxo_total_bytes`. **Fixed anchors: p50=0.70, p99=0.97.** A confirmed bloat datum occupies almost the whole UTxO. | 0.35 |
| `value_cbor_bytes` | Separation signal (inverted): expected to be normal, distinguishes from Token Dust | 0.05 |
| `sender_recurrence` | Contextual (stubbed to 0 pending entity clustering) | 0.10 |

### Scoring

The gate (the bloat discriminator above) is what removes false positives, so the score axes keep their original size-based shape: a confirmed bloat datum is both large (`datum_bytes`) and occupies most of the UTxO (`datum_ratio`), both saturating toward Critical.

```
score_large_datum(utxo):
    if utxo.address_type != SCRIPT: return 0
    if utxo.datum_present == NONE or utxo.datum_bytes == null: return 0
    if not is_bloat_datum(utxo): return -1  # gate (entropy / leaf-conc / size backstop)

    datum_ratio = utxo.datum_bytes / (utxo.utxo_total_bytes + EPSILON)

    s_datum      = normalise(utxo.datum_bytes, per_script_baselines)
    s_ratio      = normalise(datum_ratio, p50=0.70, p99=0.97)  # fixed anchors
    s_value_inv  = 1 - normalise(utxo.value_cbor_bytes, per_script_baselines)
    s_recurrence = 0.0  # stubbed: entity clustering deferred

    score = 0.50 * s_datum + 0.35 * s_ratio + 0.05 * s_value_inv + 0.10 * s_recurrence
    return clip(score, 0, 1) * 100
```

### What TMS Forge Produces
- Single TX with `RawPlutusData(CBORTag(121, [bytes]))` as inline datum
- Payload size = `datum_size_bytes - 4` (accounts for CBOR overhead)
- Payload is repeated `\xde\xad` bytes (no semantic content, should trigger malformed datum heuristic)
- Sent to the always-succeeds script address
- Look for: high `datum_bytes`, high `datum_ratio`, normal `value_cbor_bytes`, `datum_present == INLINE`


## Attack 4: Multiple Satisfaction

### Definition
A spending contract validates outputs for its input without checking that each input is independently satisfied. This lets an attacker consume N UTxOs from the same script in one TX while satisfying the spending condition only once, extracting N times the authorized value.

### Gate Condition
- `n_inputs_same_script >= 2` AND
- transaction carries at least one `spend`-purpose redeemer (excludes native-script multisig / timelock consolidations, which the ledger evaluates as declarative predicates per-input and which are immune to multiple-satisfaction by construction)

### Detection Features

| Feature | Role | Weight |
|---------|------|--------|
| `s_extraction` = max(`net_value_out_of_script`, `n_assets_out_of_script`) | Primary: value extracted from the script, measured along two complementary axes (lovelace and distinct native-asset `(policy, name)` pairs) and combined via `max()` so either dimension can carry the signal. Per-script baseline on each axis. | 0.42 |
| `exunits_per_script_input` | Corroborating (inverted): low execution units per script input is anomalous given multiple script inputs. Per-script baseline. | 0.28 |
| `n_inputs_same_script` | Primary structural: severity gradation above the gate threshold; draining 10 UTxOs is more severe than 2. Per-script baseline. | 0.16 |
| `sender_recurrence` | Contextual: repeated attempts against the same script suggest systematic exploitation. Per-script baseline. | 0.14 |

`redeemer_input_ratio` is deliberately excluded. The Cardano ledger enforces `dom txrdmrs ≡ᵉ scriptRdrptrs`, so the ratio is structurally 1.0 for every valid on-chain transaction. The Multiple Satisfaction vulnerability is semantic (inside the validator) and is not observable through redeemer counts.

### Key Derived Features
- `net_value_out_of_script = sum(script_input_lovelace) - sum(script_output_lovelace)`
- `n_assets_out_of_script` = count of distinct `(policy_id, asset_name)` pairs with strictly positive net flow out of the script address. Pair-count, not unit-count: 50 fungible-token units of one asset count as 1, the same as a single NFT.
- `exunits_per_script_input = exunits_total.cpu / n_inputs_same_script`

### Grouping Basis: Payment Credential

"Same script" means same **payment credential** (script hash, CIP-19 28-byte
Blake2b-224 hash), not same full address. Two UTxOs at the same validator
deployed under different stake credentials live at distinct Shelley addresses
but share a payment credential. Grouping on full address misses the canonical
purchase-offer double-satisfaction shape, where the attacker spends offers
from multiple stake-cred variants of one script in a single transaction.
Per-script baselines and the allowlist remain keyed by full address; the
group representative is the first input's address in the group.

### Value-Agnostic Extraction

Real-world double-satisfaction targets two distinct asset classes: lovelace (DeFi vaults, escrow contracts) and native assets (NFT marketplaces, token-locking contracts). The canonical NFT-marketplace case drains native assets while the script's lovelace position is flat: min-UTxO ADA enters and the same min-UTxO ADA leaves. A lovelace-only extraction signal is invariant in that case and produces no detection.

The scorer therefore computes both axes independently against per-script baselines and combines them via `max()`. Either dimension is a sufficient signal of value extraction; combining via `max()` rather than a weighted sum prevents one neutral axis from diluting the other. The extraction sub-score reaches its ceiling whenever either axis does.

### Scoring

```
score_multiple_satisfaction(tx):
    if tx.n_inputs_same_script < 2: return 0
    if not has_spend_redeemer(tx):  return 0

    net_value_out      = compute_net_value_out_of_script(tx)
    n_assets_out       = compute_n_assets_out_of_script(tx)
    exunits_per_input  = tx.exunits_total.cpu / (tx.n_inputs_same_script + EPSILON)

    # Value axis (net_value, n_assets): per-script baseline, then bootstrap
    # anchor on miss -- NEVER the global tier (see "Per-Script-Only Value
    # Baselines"). Per-script anchors are widened by `per_script_extraction_headroom`.
    s_extraction_lov    = normalise(net_value_out, per_script_value_baseline, headroom)
    s_extraction_assets = normalise(n_assets_out,  per_script_value_baseline, headroom)
    s_extraction        = max(s_extraction_lov, s_extraction_assets)
    # exunits / n_inputs / recurrence: per_script -> global -> bootstrap (absolute)
    s_exunits_inv       = 1 - normalise(exunits_per_input, baselines)
    s_inputs            = normalise(tx.n_inputs_same_script, baselines)
    s_recurrence        = normalise(tx.sender_recurrence,  baselines)

    score = 0.42 * s_extraction + 0.28 * s_exunits_inv + 0.16 * s_inputs + 0.14 * s_recurrence
    score = clip(score, 0, 1) * 100

    # Un-widened extraction for the floor gate only (headroom must not weaken
    # the high-confidence path): s_extraction_floor = max over both axes at raw p99.
    uniform_sweep = is_uniform_sweep(tx)   # many inputs, identical spend redeemers, no script return

    # Lazy-validator band floor (see below). Requires real extraction
    # (s_extraction_floor > lazy_validator_extraction_min) and not a sweep.
    floor_applies = (not allowlisted and not uniform_sweep
                     and s_exunits_inv > lazy_validator_threshold
                     and s_extraction_floor > lazy_validator_extraction_min)
    if floor_applies:
        score = max(score, lazy_validator_floor)
    if uniform_sweep:
        score = min(score, BAND_MODERATE_MAX)   # sweep classification stands even under allowlist reweight

    # Suppression: a benign multi-input spend that is NOT double-satisfaction
    # (owner sweep, or value returned to the script = state continuation) is
    # dropped to no-finding (-1). Gated on `not floor_applies` so a floored
    # lazy-validator exploit and the CTF-01 marketplace case (uniform=False,
    # value_returned=0) are never suppressed.
    # Escape hatch: both benign shapes are attacker-reachable (return 1
    # lovelace to force the state-continuation arm; an identical-redeemer
    # full drain matches the sweep fingerprint), so when the un-widened
    # extraction floor signal reaches suppression_escape.extraction_floor_min
    # the finding is kept and banded exactly Moderate (floored and capped).
    escape = (suppression_escape.enabled and not allowlisted
              and s_extraction_floor >= suppression_escape.extraction_floor_min)
    if not floor_applies and (uniform_sweep or value_returned_to_script > 0):
        if not escape:
            return no_finding
        score = min(score, BAND_MODERATE_MAX)   # reason: extraction_escape_moderate_cap

    return score
```

### Lazy-Validator Band Floor

When the gate has fired AND `s_exunits_inv` saturates above `lazy_validator_threshold` (default 0.8, the validator did near-zero CPU per input), the final score is floored to `lazy_validator_floor` (default 60.0, the High band threshold). The weighted average is biased toward value extraction, so a low-value but structurally unambiguous exploit (multiple inputs, gate satisfied, validator clearly skipping per-input work) can produce a Moderate score; the floor surfaces these to operators on signal strength rather than dollar impact. The mechanism is the inverse of `front_running.high_band_cap`, which caps the score when structural confirmation is weak.

Allowlisted scripts are exempt: legitimate batch-processing contracts often run minimal per-input CPU by design (the validator runs once and amortises across all batched orders), so the lazy-validator fingerprint is part of their normal operation.

The floor additionally requires `s_extraction_floor > lazy_validator_extraction_min` (the un-widened extraction, so the per-script headroom cannot weaken this high-confidence path) and `not uniform_sweep`. Double-satisfaction by definition needs value to leave the script: a state-machine contract that consumes its own UTxOs and writes state back has `s_extraction_floor = 0` and is not floored even when execution is cheap.

### Per-Script-Only Value Baselines

The value-extraction axis (`net_value_out_of_script`, `n_assets_out_of_script`) resolves per-script then drops straight to the bootstrap anchor, **never the global tier**. The global distribution of value/assets leaving a script is dominated by legitimate high-volume asset-movers (DEX/marketplace batchers), so a global baseline would learn "extracting 2+ assets is normal" and de-sensitise detection on the rare/novel scripts where one-shot double-satisfaction exploits live (the CTF-01 anchor extracts 2 assets on a 3-tx script). `per_script -> bootstrap` keeps established contracts judged against their own norm while rare scripts stay on the conservative default. This applies only to the value axis: `exunits_per_script_input` feeds the inverted lazy-validator signal where "lazy" means near-zero CPU in absolute terms, so it stays on the absolute bootstrap (a per-script exunits baseline would make a heavy-work contract look maximally lazy against its own median and spuriously floor it).

**Per-script extraction headroom**: the extraction features are discrete and low-cardinality, so a per-script p99 often sits ~1 above p50 (e.g. `n_assets` p50=2, p99=3) and normalise() would saturate on the contract's common upper-normal value. When a per-script baseline is in use, the upper anchor is widened to `p50 + (p99 - p50) * per_script_extraction_headroom` (default 3.0) so only extraction well above the contract's own normal range scores. Bootstrap/global anchors are used unchanged, keeping rare/novel scripts on the conservative floor (CTF-01 recall preserved).

### False Positive: Legitimate UTxO Batching
- DEX batch settlement, staking reward consolidation, multi-position liquidation, and prediction-market resolution all have elevated `n_inputs_same_script` and large `net_value_out_of_script` as normal behaviour.
- **Per-script value baselines (no global tier)** judge extraction against the contract's own history, not a batcher-dominated global distribution. See above.
- **Uniform-sweep guard**: a tx whose fingerprint is "owner sweeping their own script UTxOs" (>= `min_inputs` script inputs, identical spend redeemers, no value returned to the same script) is a UTxO consolidation, not double-satisfaction. The lazy-validator floor is suppressed and the score is capped at the top of Moderate. Each leg (uniform-redeemer, no-return, min-inputs) is independently config-gated under `uniform_sweep_guard`. Real double-satisfaction has asymmetric satisfaction arguments and writes the satisfying value to a distinct address shape that the no-return predicate rejects.
- **State-continuation suppression**: when the floor does not apply and either the sweep guard fires or any lovelace is returned to the script (state continuation, not extraction), the finding is dropped to no-finding (`score=-1`). Gated on `not floor_applies` so the CTF-01 marketplace double-sat (uniform=False, value_returned=0, Moderate) is unaffected.
- **Extraction-magnitude escape hatch** (`suppression_escape`): both suppression shapes are attacker-reachable (returning 1 lovelace to the script forces the state-continuation arm; a large identical-redeemer full drain matches the sweep fingerprint), so when the un-widened extraction floor signal reaches `extraction_floor_min` (default 0.10, compared with `>=`) the finding is kept and banded exactly Moderate: capped at the top of the band (reason `extraction_escape_moderate_cap`) and floored at the bottom, so a deliberately un-silenced finding can never land in Informational. The floor signal is computed against the RESOLVED baselines, after the p99 cap and the anchor-relative p50 bound, without the per-script headroom widening. The threshold sits about 2x above the worst observed benign signal (a 12-input owner sweep at ~0.053; state machines ~0) and about 1.6x below the worst capped-poisoned attack minimum: the worst poisoned per-script `n_assets` pair resolves to (p50 = 0.5 from `per_script_p50_cap_spread_fraction`, p99 = 10 from the 5x p99 cap), so a 2-NFT drain normalises to (2 - 0.5) / (10 - 0.5), about 0.158. A single-NFT drain on the bootstrap anchor lands at ~0.49999975 because `normalise()` adds EPSILON to its denominator, which is why the old 0.5 strict-greater threshold silenced it. Recall-first: a Moderate false positive on a large owner-sweep is strictly better than a silenced real drain.
- Per-script allowlist of known batch-processing / resolution contracts **reduces** the `s_extraction` weight (redistributed proportionally to `s_inputs` and `s_recurrence`) rather than bypassing the scorer. This preserves the structural signals while suppressing the economic-magnitude signal for contracts where large extraction is legitimate.
- The spend-redeemer gate condition excludes native-script multisig wallets, which evaluate as declarative ledger predicates per-input and are immune to multiple-satisfaction by construction.
- **Net value linearity check**: spec-defined corroboration on the coefficient of variation of per-input extracted values, on the roadmap.

### What TMS Forge Produces
- **Setup TX**: creates `utxo_count` (2-10) outputs at a `ScriptAll([ScriptPubkey(vkh)])` address, each carrying `ada_per_utxo` ADA
- **Exploit TX**: consumes all script UTxOs (filtered by setup TX ID) in a single transaction, sends everything back to the sender as change
- Look for: `n_inputs_same_script >= 2`, all inputs share the same script hash, large `net_value_out_of_script`


## Attack 5: Front-Running

### Definition
A malicious actor observes a pending TX in the mempool and races to spend the same UTxO(s) first. On Cardano, this relies on propagation advantage (network co-location) rather than fee auctions. The victim's TX fails with "UTxO already spent".

### Gate Condition
- Two transactions must share at least one input (UTxO input collision)

### Detection Features

| Feature | Role | Weight |
|---------|------|--------|
| `collision_outcome` | Primary: later-seen tx confirmed = front-run signal. Fixed mapping: `TX_B_CONFIRMED=1.0, BOTH_PENDING=0.5, TX_A_CONFIRMED=0.0`. TX_B is the later-seen tx; TX_A is the earlier-seen tx. | 0.35 |
| `mempool_delta_ms` | Primary (reciprocal transform): `1 / (delta_ms + EPSILON)`. Small delta = automation-consistent. **Anchors: p50=1/2000, p99=1/200.** | 0.30 |
| `attacker_recurrence` | Primary: how often this submitter "wins" collisions in a rolling window. Per-cluster baseline. | 0.25 |
| Structural similarity | Corroborating: composite of `fee_similarity`, `ttl_similarity`, `common_change_address`. Average of 3 sub-signals. | 0.10 |

### Key Notes
- **Detection unit is a transaction PAIR**, not a single TX
- The reciprocal transform on `mempool_delta_ms` captures non-linearity: 50ms is qualitatively much more suspicious than 500ms
- **Minimum recurrence gate**: if `collision_win_count < 3` in 24h window, exclude from high-risk band regardless of raw score

### What TMS Forge Produces
- Builds two TXs spending the **same UTxO** before either is submitted (guarantees the collision)
- TX1 is submitted first and succeeds
- TX2 is submitted second and fails (expected: "UTxO already spent" error)
- Both TXs target the same recipient with the same amount
- Look for: shared input UTxO between TX1 and TX2, TX1 confirmed on-chain, TX2 rejected, small `mempool_delta_ms`


## Attack 6: Sandwich Attack

### Definition
A three-TX exploit targeting DEX swaps. The attacker places tx_A (buy) before the victim's swap and tx_B (sell) after it. tx_A inflates the price, the victim swaps at a worse rate, tx_B profits from the elevated price. The victim receives fewer tokens than expected.

### Gate Conditions (full DEX detection, future)
- All 3 TXs share the same `pool_id` and `asset_pair`
- tx_A and victim have the same `swap_direction`; tx_B has the opposite direction
- All 3 TXs fall within `W_SLOTS` window (recommended: 5 slots = ~25 seconds; expand to 20 for batching DEXes)

### Current Implementation: Structural Detection
The current implementation uses structural pattern detection without DEX redeemer parsing. It detects an attacker's two legs bracketing a victim's tx at the same **script address** within a `window_slots` (5) window. `swap_rate_delta` and `price_impact` are set to 0. Script addresses are filtered by Bech32 prefix (`addr1w`, `addr_test1w`, etc.). Three structural requirements were added to remove the arbitrage/batcher false-positive class that dominated this scorer:

1. **Temporal bracketing**: the attacker's legs must actually straddle the victim in `(slot, block_index)` order: closest leg before the victim is `tx_a`, closest after is `tx_b`. `block_index` (the tx's position within its block, already ingested) totally-orders transactions including within a single block, so genuine same-block sandwiches are confirmable, while co-occurrence (both legs before, or both after, the victim) is rejected. Bracketing is a necessary condition for a sandwich, so this gate is recall-safe by construction and adds same-block detection the slot-only logic lacked. The victim's position is resolved from the neighbour set, falling back to a direct point-query (`_tx_position`) when it sits outside the capped neighbour window.
2. **Non-script (wallet) attacker**: the 2-leg cluster's first-input address must be a payment-key (wallet) address, not a script. This excludes pool/batcher self-interaction, the dominant structural false positive.
3. **Net-ADA profit floor** (`min_profit_lovelace`, 200000): the attacker's net ADA across the front and back legs must exceed the floor or the candidate is suppressed entirely (`score=-1`, no finding). A sandwich that extracts no value is not an attack. Blind spot: profit taken in a non-ADA token nets ~0 ADA and is missed; that needs the deferred DEX datum / rate-impact confirmation.

### Detection Features

| Feature | Role | Weight |
|---------|------|--------|
| `attacker_link` | Primary: tx_A and tx_B share change address, collateral, or cluster. Boolean → `linked=1.0, unlinked=0.2`. | 0.30 |
| `swap_rate_delta` | Primary: `(victim_swap_rate - pool_median) / pool_median`. Negative = victim got fewer tokens. **Anchors: p50=0.0, p99=0.15.** | 0.30 |
| `price_impact_A` | Corroborating: estimated price movement caused by tx_A. Per-pool baseline. | 0.20 |
| `profit_B` | Corroborating: estimated profit of tx_B relative to tx_A's cost. Per-pool baseline. | 0.10 |
| `sender_recurrence` | Contextual: repeated sandwich patterns from same cluster. | 0.10 |

### Temporal Pattern Detection (what TMS Forge exercises)
Since TMS Forge cannot interact with live DEX pools, it simulates the **temporal and structural pattern** that the TMS should use as a first-pass filter before DEX-specific analysis:

1. **Temporal sequence**: tx_A → victim_tx → tx_B in rapid succession (within a few slots)
2. **Shared change address**: tx_A and tx_B share the same change address (same attacker)
3. **CIP-20 correlation metadata**: all TXs carry label 674 with `group:<uuid>` and `role:front_run|victim|back_run`
4. **Distinct wallets**: attacker and victim use different addresses (different HD derivation indices)

### What TMS Forge Produces
- **4 transactions** from 2 distinct wallets (attacker = HD index 0, victim = HD index 99):
  1. **Setup TX**: attacker funds victim address (amount + 2 ADA for fees)
  2. **tx_A (front-run)**: attacker self-send with CIP-20 `role:front_run, group:<uuid>`
  3. **victim_tx**: victim sends to attacker with CIP-20 `role:victim, group:<uuid>`
  4. **tx_B (back-run)**: attacker self-send with CIP-20 `role:back_run, group:<uuid>`
- All 4 share the same `group:<uuid>` in label 674 metadata
- tx_A and tx_B share the same change address (attacker's address)
- Victim address is different from attacker address
- Look for:
  - 3 TXs in rapid temporal sequence from correlated addresses
  - tx_A and tx_B share change address
  - Victim TX is sandwiched between tx_A and tx_B temporally
  - CIP-20 metadata label 674 with `SANDWICH_SIM`, `role:*`, `group:*` tags
  - **NOT triggered**: `pool_id`, `swap_rate_delta`, `price_impact` (no DEX interaction)


## Attack 7: Circular Transfers

### Definition
Value (ADA or native assets) travels through a sequence of addresses and returns to its origin within a bounded time window. Net economic displacement is near-zero (only fees lost). Used for: wash trading (fake volume), AML layering (obscure fund provenance), self-churn to confuse UTxO-graph clustering.

### Gate Conditions
- Cycle length k in [3..6] (`cycle.min_length` = 3). A 2-hop `A → script → A` is a
  deposit/withdraw round-trip, not circular layering, and was the dominant
  false positive; the floor is 3 hops.
- `net_loss_ratio` consistent with fee-only loss: `(amount_in - amount_out) / amount_in <= expected_fee_ratio * fee_tolerance_multiplier` (default 4.0)

### Detection Features

| Feature | Role | Weight |
|---------|------|--------|
| `amount_similarity` | Primary: `1 - (std_dev(hop_amounts) / mean(hop_amounts))`. Near 1.0 = same amount at every hop. **Fixed anchors: p50=0.70, p99=0.97.** | 0.30 |
| `cycle_recurrence` | Primary: how many times this cycle (or near-identical) appears in the time window. Per-cluster baseline. | 0.30 |
| `recipient_entropy` | Corroborating (inverted): Shannon entropy of destination addresses. Low = same nodes recycled. **Fixed anchors: p50=0.80, p99=0.30 (inverted).** | 0.20 |
| `round_amount_flag` + `temporal_concentration` | Auxiliary: round numbers + time clustering. | 0.10 |
| `inter_hop_delta_slots` | Contextual (reciprocal): very small delta = automation. **Anchors: p50=1/20, p99=1/2.** | 0.10 |

### Key Derived Features
- `net_loss_ratio = (amount_in - amount_out) / amount_in`: near fee-only ratio confirms circular intent
- `amount_similarity = 1 - CV(hop_amounts)`: near 1.0 = same value passed through each hop
- `cluster_self_return`: terminal address belongs to same cluster as origin

### Two Detection Levels
1. **Graph-level pipeline**: detect explicit cycles of length k in [3..6] in the transfer graph (rolling window). Score each cycle as a unit.
2. **Address-level pipeline** (lightweight fallback): detect 2-hop ping-pong (A → B → A) without full graph traversal. The detector still finds 2-hop round-trips, but they sit below the `min_length` = 3 gate and are not alerted: a 2-hop `A → script → A` is a deposit/withdraw round-trip, not circular layering, and was the dominant false positive.

A structural-only match (just `amount_similarity` + `cycle_recurrence`, with the corroborating axes near zero) is suppressed entirely to no-finding (`score = -1`): when the corroborating signals (entropy + auxiliary + speed) sum below `structural_corroboration_floor`, the cycle is structurally indistinguishable from benign DeFi composition (a pool swap or state machine) and carries no layering evidence, so a plain round-trip never reaches an alerting band on the two structural axes alone. Separately, a corroborated cycle that still loses more than the strict fee-only ratio (`fee_tolerance_strict`) is capped at the top of Moderate (`moderate_cap`).

### What TMS Forge Produces
- `hop_count` (3-8) separate TXs chaining ADA through HD-derived addresses: `addr_0 → addr_1 → addr_2 → ... → addr_0`
- Each hop sends the same `amount_ada` (high `amount_similarity`)
- All addresses derived from the same HD wallet (indices 0 through N-1)
- Inter-hop delay is minimal (just waiting for UTxO confirmation, typically ~20 seconds per hop)
- Net displacement after the cycle completes is near-zero (only fees lost)
- Look for:
  - Same amount at every hop (`amount_similarity` near 1.0)
  - Cycle closes back to origin address
  - `net_loss_ratio` consistent with fee-only loss
  - Low `inter_hop_delta_slots` (automated execution)
  - Same small set of addresses reused (`recipient_entropy` low)


## Attack 8: Fake Token Distribution

### Definition
The attacker mints tokens with a TokenName identical or visually similar (Unicode homoglyphs) to a legitimate token but under a different PolicyID. Since most interfaces display only TokenName (not PolicyID), users mistake the fake for the real asset. Often combined with CIP-25 metadata impersonation (copying name, ticker, image, description from the real token).

### Gate Conditions
- TX includes a minting action (`mint_present == true`)
- At least one minted token name has `tokenname_similarity >= 0.70` against a known legitimate token (tuned down from the Polimi starting point 0.80; `fake_token.similarity_threshold` in `config/detection.yaml`)
- `policy_id != legitimate_policy_id` for the matched token

### Detection: Two Sub-Pipelines

#### Sub-Pipeline 1: Identity Deception (weight = 0.60)

| Feature | Role | Sub-weight |
|---------|------|------------|
| `tokenname_similarity` | Levenshtein similarity after Unicode NFKC normalisation + confusable mapping. **Anchors: p50=0.80, p99=0.97.** | 0.40 |
| `unicode_suspicion_score` | Composite: homoglyph substitution + zero-width chars + mixed Unicode scripts. **Anchors: p50=0.0, p99=0.6.** | 0.35 |
| `cip25_similarity` | Aggregated CIP-25 metadata field similarity (name, ticker, image, description). **Anchors: p50=0.0, p99=0.80.** | 0.25 |

#### Sub-Pipeline 2: Distribution Pattern (weight = 0.40)

| Feature | Role | Sub-weight |
|---------|------|------------|
| `recipient_count` | Distinct addresses receiving the fake token. Per-policy baseline. | 0.40 |
| `mint_to_recipient_ratio` | Inverted: `minted_qty / recipient_count`. Low = wide distribution. Per-policy baseline. | 0.30 |
| `mint_policy_age` | Inverted (reciprocal): new policy = higher risk. **Anchors: p50=1/100000, p99=1/5000 slots.** | 0.20 |
| `sender_recurrence` | Contextual | 0.10 |

```
final_score = 0.60 * identity_deception_score + 0.40 * distribution_score
```

#### Critical-Asset Escalation

Impersonating a high-value asset is more dangerous than cloning a meme coin: a fake stablecoin redirects DeFi collateral that users trust as worth $1. The identity sub-pipeline weighs every impersonation equally on the name axis, so a plain exact-name clone of a stablecoin scores no higher than any other clone and its severity ends up driven by distribution alone. To correct this, when the matched legitimate token is on the curated `fake_token.critical_assets.names` list (stablecoins iUSD/DJED/SHEN by default), the identity score is multiplied by `fake_token.critical_assets.multiplier` (default 1.8) and clipped to 1.0. The multiplier is >= 1.0 and the clip keeps it from exceeding a clean full-identity score, so the adjustment is strictly monotonic: it only ever raises an impersonation's score, never lowers a detection. The matched asset's tier is recorded in evidence as `matched_token_criticality` (`critical` or `standard`).

### `tokenname_similarity` Implementation
Two-stage comparison:
1. **Normalise**: Unicode NFKC normalisation → strip zero-width chars → map confusable chars to canonical equivalents (Unicode Consortium confusables.txt)
2. **Compare**: Levenshtein similarity = `1 - (edit_distance / max(len(s1), len(s2)))`

**ASCII homoglyph fold** (`fake_token.ascii_homoglyphs_enabled`, default true): before the similarity comparison, the O/0 and I/l/1 lookalike groups are folded to a canonical member, so `1NDY` now matches `INDY` at 1.0. The `unicode_suspicion` axis receives the homoglyph bump only when the fold strictly increases similarity to the matched name (`_ascii_fold_increases_similarity`), so purely numeric names like `HOSKY2` or `MIN100` gain nothing from digits that are not standing in for letters.

### Homoglyph Characters Used by TMS Forge
The test tool uses these substitutions (defined in `_HOMOGLYPH_MAP`):

| Original | Replacement | Type |
|----------|-------------|------|
| `O` | `0` | ASCII lookalike |
| `I` | `l` | ASCII lookalike |
| `l` | `1` | ASCII lookalike |
| `a` | `а` (U+0430) | Cyrillic |
| `e` | `е` (U+0435) | Cyrillic |
| `o` | `о` (U+043E) | Cyrillic |
| `s` | `ѕ` (U+0455) | Cyrillic |
| `c` | `с` (U+0441) | Cyrillic |
| `p` | `р` (U+0440) | Cyrillic |
| `x` | `х` (U+0445) | Cyrillic |
| `y` | `у` (U+0443) | Cyrillic |

If no substitution fits within the 32-byte AssetName limit, a zero-width space (U+200B) is appended.

### What TMS Forge Produces
- Single TX minting N tokens (1 per recipient) with a homoglyph-substituted TokenName
- PolicyID is different from the legitimate token's policy (newly minted)
- Optional CIP-25 label 721 metadata with impersonated `name`, `ticker`, `image`, `description`
- Policy ID key in CIP-25 metadata uses `policy_id.payload.hex()` (hex string per CIP-25 spec)
- One output per recipient carrying 1 fake token
- Look for:
  - `tokenname_similarity` high against known legitimate tokens
  - `policy_id_mismatch` = true
  - Unicode anomalies in TokenName (Cyrillic chars, zero-width spaces)
  - CIP-25 metadata (label 721) with fields resembling a known legitimate token
  - Multiple outputs to distinct addresses (distribution pattern)


## Attack 9: Phishing via Metadata

### Definition
The attacker embeds malicious URLs, deceptive instructions, or social engineering messages in on-chain transaction metadata (CIP-20 label 674), CIP-25 NFT metadata (label 721), inline datums, or the native-asset name itself. Often delivered as mass airdrops, with small token or bare ADA sent to many recipients alongside the malicious payload.

### Gate Conditions
- At least one URL extracted from any of the three carriers below
- Sender not on the allowlist of known legitimate protocol addresses

### URL Carriers
1. **Tx-level metadata** under a relevant label (`phishing.metadata_labels`, default 674 and 721).
2. **Inline datums on outputs**: the Plutus-Data tree is walked and every UTF-8 decodable span is scanned. Catches CIP-68 reference-NFT phishing, where the payload lives in the datum rather than auxiliary data.
3. **Decoded native-asset names** from the mint map and output value bundles (`phishing.asset_name_carrier.enabled`). Catches the URL-named scam-token airdrop: the dominant in-the-wild Cardano scam mints a token literally named after the phishing domain (e.g. `claim-ada.xyz`) and mass-airdrops it to wallet addresses with no metadata and no datum, so it never enters the other two carriers. Names are hex-decoded to UTF-8 first; names that fail decoding fall back to raw hex, which contains no dot and can never match. Asset names also feed the social-engineering text scan (scam tokens are routinely named with urgency or brand bait). URLs found in asset names are recorded in evidence as `asset_name_urls` and raise the `url_in_asset_name` reason flag.

Bare-domain forms (`cardano-drop.io/claim`) are matched alongside scheme-prefixed URLs in every carrier, then validated against the Public Suffix List so bare-word matches like `3.14` do not survive. Mass distribution is scored by the delivery sub-pipeline below regardless of carrier (`recipient_count` counts distinct output addresses).

### Detection: Two Sub-Pipelines

#### Sub-Pipeline 1: Content Analysis (weight = 0.65)

| Feature | Role | Sub-weight |
|---------|------|------------|
| `social_engineering_score` | Keyword/pattern matching for urgency language, credential requests, impersonation. **Anchors: p50=0.0, p99=0.30** (saturates at 2 Tier-2 matches at `urgency_increment=0.15` each). Social text in on-chain metadata is the dominant practical signal: legitimate Cardano protocols rarely push urgency/reward language through chain metadata, so this carries the heaviest content sub-weight. | 0.40 |
| `url_blacklist_match` | Confidence-weighted match against threat intel feeds (OpenPhish, PhishTank). `1.0 = confirmed, 0.5 = newer feed, 0.0 = no match` | 0.30 |
| Domain suspicion composite | `0.50 * s_age + 0.50 * s_brand`. Domain age inverted (**anchors: p50=1/365, p99=1/7 days**). Brand similarity against known protocol domains (**anchors: p50=0.0, p99=0.85**). | 0.30 |

Two stacking bonuses are applied to the content sub-score when a URL and Tier-2 social-engineering text co-occur in the same metadata: `url_combo_bonus` (0.25) fires whenever any URL is present alongside Tier-2 text, and `phishing_tld_bonus` (0.15) stacks on top when the URL also lives in a phishing-prone TLD (`.xyz` / `.top` / `.click` / RFC 2606 reserved). Cardano protocols do not deploy in those TLDs, so the combination is near-specific to phishing.

#### Sub-Pipeline 2: Delivery Pattern (weight = 0.35)

| Feature | Role | Sub-weight |
|---------|------|------------|
| `recipient_count` | Distinct addresses receiving the TX/token. Per-cluster baseline. | 0.35 |
| `url_hash_recurrence` | How many TXs in the window share the same URL(s). Per-cluster baseline. | 0.25 |
| `targeting_score` | Net-new-holder delivery: fraction of (URL-named asset, recipient) pairs where the recipient did not already hold that exact asset in the tx's own inputs (or the asset is minted in this tx). Already in [0, 1], no anchors. A real airdrop is all net-new (1.0); a URL-named token riding in the sender's own change is 0.0. The spec's original recipient-to-protocol interaction graph remains deferred and would refine this signal. | 0.25 |
| `sender_recurrence` | Contextual | 0.15 |

**Self-change bonus gate**: when asset names are the SOLE URL carrier and every (URL-asset, recipient) pair is positively proven prior-held (pure self-change, nothing minted), the two stacking bonuses (`url_combo_bonus`, `phishing_tld_bonus`) are withheld; content and delivery scoring are untouched. Suppression needs positive proof per pair, so missing input values, missing addresses, or an absent inputs list all fail open toward detection. Matching is on the full output address, not the stake credential (a stake-cred match is attacker-forgeable by minting an address from the attacker's payment credential plus the victim's stake credential). Metadata- and datum-carried URLs never gate. Kill switch: `phishing.asset_name_carrier.require_delivery_for_bonuses`.

### Severity Classification
- **KNOWN_BAD**: `url_blacklist_match == 1.0` (confirmed by mature feed)
- **SUSPICIOUS_NEW_DOMAIN**: `content_score >= 0.60` driven by domain age + brand similarity
- **SOCIAL_ENGINEERING**: residual, deceptive text content without clearly suspicious URL

### `social_engineering_score` Implementation
Three tiers:
1. **Tier 1 (score = 1.0)**: credential requests such as "seed phrase", "recovery phrase", "private key", "enter your mnemonic". Also includes Voltaire-era governance phishing domains ("cardano-governance", "ada-governance", "governance-reward")
2. **Tier 2 (proportional)**: urgency language such as "limited time", "claim before", "expires", "act now", "only N remaining", "governance reward"
3. **Tier 3 (brand similarity)**: impersonation of known protocol names, wallet brands, foundation entities

### What TMS Forge Produces

**CIP-20 delivery mode:**
- TX with label 674 metadata: `{"msg": ["<message_text>", "<phishing_url>"]}`
- One output per recipient carrying ~2 ADA
- Look for: label 674 in `metadata_labels`, URL extraction from `msg` array

**CIP-25 delivery mode:**
- TX minting `CLAIM_REWARD` NFT (1 per recipient) with label 721 metadata:
  ```json
  {721: {"<policy_id_hex>": {"CLAIM_REWARD": {
      "name": "Claim Your Reward",
      "description": "<message_text>",
      "image": "<phishing_url>",
      "url": "<phishing_url>"
  }}}}
  ```
- One output per recipient carrying the NFT
- Look for: label 721 in `metadata_labels`, URL in `image` and `url` fields, social engineering in `name` and `description`


## Fixed Anchor Reference Table

All values are recommended starting points. Validate against production data.

| Attack Class | Feature | p50 Anchor | p99 Anchor | Notes |
|-------------|---------|------------|------------|-------|
| Large Datum | `datum_ratio` | 0.70 | 0.97 | Fraction of UTxO bytes from datum |
| Front-Running | `1 / mempool_delta_ms` | 1/2000 | 1/200 | 200ms = automation threshold |
| Front-Running | `fee_delta` | 500 | 5000 | Lovelace |
| Front-Running | `ttl_delta` | 10 | 100 | Slots |
| Sandwich | `swap_rate_delta` | 0.00 | 0.15 | Fractional deterioration |
| Circular | `amount_similarity` | 0.70 | 0.97 | 1 - CV of hop amounts |
| Circular | `recipient_entropy` (inv) | 0.80 | 0.30 | Shannon entropy, inverted |
| Circular | `1 / inter_hop_delta_slots` | 1/20 | 1/2 | Reciprocal of mean slot delta |
| Fake Token | `tokenname_similarity` | 0.80 | 0.97 | Post-normalisation Levenshtein |
| Fake Token | `unicode_suspicion` | 0.00 | 0.60 | Composite score |
| Fake Token | `cip25_similarity` | 0.00 | 0.80 | Metadata field similarity |
| Fake Token | `1 / policy_age_slots` | 1/100000 | 1/5000 | 5000 slots ≈ 2.7 hours |
| Phishing | `1 / domain_age_days` | 1/365 | 1/7 | 7 days = very new |
| Phishing | `brand_similarity` | 0.00 | 0.85 | Domain name Levenshtein |
| Phishing | `social_engineering` | 0.00 | 0.30 | Keyword/pattern composite; saturates at 2 Tier-2 matches |
| Phishing | `targeting_score` | 0.05 | 0.50 | Recipient overlap with protocol |


## Baseline Maintenance

| Baseline Type | Update Cadence | Rolling Window |
|--------------|----------------|----------------|
| Per-script feature baselines | Daily | 90 days |
| Per-policy feature baselines | Daily | 90 days |
| Global fallback baselines | Weekly | 180 days |
| Cluster recurrence baselines | Daily | 30 days |
| Pool swap rate baselines | Hourly | 7 days |

- Minimum 200 transactions per script/policy before per-entity baseline is valid
- Below threshold → fall back to global baselines (by script type)
- **Exception**: the Multiple Satisfaction value-extraction axis (`net_value_out_of_script`, `n_assets_out_of_script`) skips the global tier and falls back per-script → bootstrap, because a global value-extraction distribution is dominated by legitimate batchers and would de-sensitise the scorer (see Attack 4: Per-Script-Only Value Baselines).
- **Drift guard** (`baselines.drift`): direction-aware. A recompute that drifts beyond the threshold in a recall-harmful direction (p99 widening beyond `p99_threshold`, or p50 rising beyond `p50_threshold`, both default 0.50 relative) is HELD: the prior baseline stays active. For pure-normalise features, recall-safe drifts (p99 narrowing, p50 falling) always apply; holding them made a poisoned first baseline self-protecting, because every honest recompute that shrank it back was itself an over-threshold change. Features with at least one `normalise_inverted()` consumer (`ada_amount` feeds the token_dust and large_value inverted-ADA axes; `value_cbor_bytes` feeds large_datum's inverted value axis) hold BOTH directions: an attacker dumping low-ADA or small-CBOR outputs at a victim script drags those percentiles DOWN, and a lower window zeroes the inverted dust/value axes, so "falling is safe" does not hold there (see `INVERTED_CONSUMER_FEATURES` in `baselines.py`). Every drift event, held or applied, is logged to `baseline_drift_events` (with `axis` and `applied` columns) for analyst review; the held-drift warning names the specific axis or axes that caused the hold. First-ever baselines always pass. This is the anti-poisoning control: pre-training a per-script distribution to de-sensitise a scorer now requires moving it slowly under the threshold across multiple recomputes, leaving a trail.
- **Per-script p99 cap and p50 bound** (`baselines.per_script_p99_cap_multiplier`, default 5.0; `baselines.per_script_p50_cap_spread_fraction`, default 0.25): a learned baseline's p99 is capped at M times the scorer's bootstrap anchor p99 at resolution time. The resolved p99 is the normalisation saturation point, so without the cap a poisoned baseline could push it arbitrarily high and a real attack would normalise to ~0. The p50 is bounded too, at `anchor_p50 + K * (anchor_p99 - anchor_p50)`: `normalise()` subtracts p50 first, so a poisoned median de-sensitises an axis exactly like a widened tail. The bound is anchor-relative because the earlier cap-relative bound (`capped_p99 / (1 + min_spread_ratio)`, about 4.55x the anchor p99) left an in-bound median-poisoned pair enough room to zero a real drain below the suppression-escape floor. Worked for the `n_assets` axis (anchors p50=0, p99=2, M=5, K=0.25): the worst poisoned pair resolves to (0.5, 10) and a 2-NFT drain normalises to about 0.158, clearing the 0.10 escape floor with ~1.6x margin; the general form with anchor p50 = 0 is `(1 - K) / (M - K)`. An anchor p50 of 0 (count-like features) degrades naturally to `K * anchor_p99`, the bound is additionally kept below the p99 cap via `min_spread_ratio` so the capped pair always keeps a usable spread, and the combined bound can only ever lower a resolved p50 relative to the cap-relative rule (recall-positive). An established contract may legitimately run up to M times the protocol-grounded anchor, never beyond it.
- **Windows and determinism**: baseline percentile queries window on chain time (`transactions.timestamp`, not ingestion time, so backfills cannot collapse the window) and use `quantileExact` so recomputes are deterministic and drift signals are not jitter.


## Normalisation Formula

```
normalise(f, baselines) = clip((f - p50) / (p99 - p50 + EPSILON), 0, 1)
```

Where `p50` and `p99` are from the per-script/per-policy baseline (or fixed anchors for dimensionless features). `EPSILON = 1e-6` prevents division by zero.

## Score Composition

```
RiskScore(tx, class) = clip(sum(w_i * norm(f_i)) / sum(w_i), 0, 1) * 100
```

Each attack class produces an independent score. A single TX can score on multiple classes simultaneously. Output one score vector per TX, with top contributing features and normalised values for each non-zero score.

## Cross-Class Corroboration

The risk band is derived solely from the single highest class score (`max_score`), so a transaction that independently trips two different detectors is banded identically to one that trips only the strongest: the agreement between detectors is otherwise lost. To surface that agreement without changing alerting, each scored transaction also records two fields on `tx_class_scores`:

- `corroboration_count`: the number of distinct attack classes scoring at or above `composite_corroboration.corroboration_threshold` (default 40.0, in `config/detection.yaml`).
- `corroborating_classes`: the comma-separated names of those classes (e.g. `sandwich,token_dust`).

This is a flag only: it deliberately does not feed `max_score` or `risk_band`, so alerting volume is unchanged. The list endpoint exposes a `min_corroboration` filter for analyst triage:

```
GET /api/analysis/results?min_corroboration=2
```

returns only transactions where at least that many distinct classes corroborated (`corroboration_count >= 2`); `0` (the default) disables the filter. The intent is to pull multi-signal transactions where several independent detectors agree, regardless of the band the single highest score placed them in. The filter is API-only; there is no UI control.

## Implementation Notes

### Ogmios v6 Value Format
All scorers and feature extractors handle both Ogmios v5 (`{"lovelace": N, "policyId": {...}}`) and v6 (`{"ada": {"lovelace": N}, "policyId": {...}}`) value formats. The `"ada"` key is skipped when iterating native assets.

### Ogmios v6 TTL Format
`features.extract_ttl` handles both schemas for the transaction time-to-live: v6 exposes it as `validityInterval.invalidAfter`, v5 as a top-level `timeToLive`. It feeds the mempool-collision capture and the Front-Running structural axis (`ttl_similarity`). Before this was added, `ttl_similarity` was pinned at 1.0 on v6 nodes because the v5 key was absent, silently inflating the structural sub-signal.

### CIP-19 Script Address Prefixes
`features.is_script_address` recognises every Bech32 prefix whose payment part is a script hash: `addr1w`/`addr_test1w` (script + no stake), `addr1z`/`addr_test1z` (script + stake key), `addr1x`/`addr_test1x` (script + script), and `addr12`/`addr_test12` (script + pointer). DEX and sandwich detection share the same prefix tuple. Mainnet DeFi contracts that stake their own UTxOs (address type 3, `addr1x`) were previously misclassified as wallets by every script-gated scorer.

### Baseline Resolution Order
Scorers call `scorer_config.resolved_or_bootstrap()`, which wraps `normalise.resolve_baseline()`: a per-network dynamic baseline is tried first (per-script → global fallback within the same network). When no dynamic baseline is available, the scorer's bootstrap anchors from `config/detection.yaml` are substituted and the source is reported as `"bootstrap"`. Fixed anchors (declared in the same config file under `fixed_anchors`) are consulted directly by the scorer, not through this helper; they apply to dimensionless features like `datum_ratio` that never baseline against data. The effect: learned per-script baselines supersede bootstraps as each script accumulates ≥ `BASELINE_MIN_SAMPLES` transactions.

### Collision and Displacement Detection
Front-running on Cardano differs from Ethereum: a single node's mempool rejects a second transaction spending the same UTxO, so two competing transactions cannot coexist in the same mempool. The primary detection mechanism is therefore **displacement detection**: when a confirmed block contains a transaction that spends inputs claimed by a still-pending transaction, the system records a collision with the confirmed transaction as the winner.

Collisions are recorded in PostgreSQL `mempool_collisions` with `TX_A` (pending/displaced) and `TX_B` (confirmed/displacer) designation. The outcome is set at detection time: `TX_B_CONFIRMED`. For the rare case where two competing transactions are observed in the same mempool (e.g. via different relay paths), the original concurrent-pending collision detection also remains active, with outcome resolved when one confirms.

The Front-Running scorer maps `TX_B_CONFIRMED=1.0` (front-run signal) and `TX_A_CONFIRMED=0.0` (no front-run).

### Active Cross-Transaction Sub-Scores
- `cycle_recurrence` (Circular): counts prior circular-scored txs from the same origin address within a 30-day rolling window. Queries `tx_class_scores JOIN transaction_inputs` filtered by `circular > 0` and `analyzed_at >= now() - 30 days`. Uses first input address as cluster proxy.
- `attacker_sandwich_count` (Sandwich): queries historical tx count from same attacker address cluster at script addresses

### Placeholder Sub-Scores
Several sub-scores are placeholders pending cross-tx analysis infrastructure:
- `sender_recurrence` (all scorers): always 0.0
- `url_hash_recurrence`, `sender_recurrence` (Phishing delivery): always 0.0; `targeting_score` is live as the net-new-holder delivery fraction (see Attack 9), so delivery scoring uses `recipient_count` plus `targeting_score`

Two further axes are not computed on-chain and are disclosed here as known coverage limits:
- `swap_rate_delta`, `price_impact` (Sandwich): the on-chain swap economics are not decoded, so `swap_rate_victim`, `swap_rate_baseline`, and `price_impact_a` are hardcoded to `0.0` in `analysis/dex.py`, and both sub-scores are therefore always 0. These are the two largest economic axes (weights 0.30 and 0.20, i.e. 0.50 of the class total combined). The `profit` axis is the one economic signal that is actually computed (attacker net ADA via `dex._attacker_net_ada`), and it is ADA-only: profit realised in a non-ADA token is not captured. Consequence: with half the class weight pinned at zero, the sandwich score saturates at 50.0 even when link, profit, and recurrence are all maxed, so the class is structurally capped in the Moderate band and cannot reach High (`>= 60.0`) or Critical (`>= 80.0`) on economic magnitude. Pending DEX order/pool datum decoding (see the deferred-work register, `docs/follow-ups/`).
- `datum_bytes` (Large Datum): a UTxO that references its datum by hash instead of inlining it reports `datum_bytes = 0` to the `large_datum` scorer whenever the datum preimage is not also carried in the transaction's witness set (`features._extract_datum_info` returns byte size 0 for a hash-only output whose preimage is absent; when the preimage is present in the same tx it is sized from it). Known coverage gap: datum bloat hidden behind a bare datum hash is invisible to the byte-size axis, and closing it needs a standalone datum indexer.

### Weight Deviations from Polimi Spec
- **Fake Token**: `policy_age_slots` assumes the policy is new (age=1 slot) for minting transactions. A policy registry lookup would provide the actual first-seen slot.

### Minimum Recurrence Gate
Front-Running scorer caps scores below Critical band (max 79.0) when `attacker_win_count < 3`, preventing single-collision false positives from reaching the highest alert tier.
