# Follow-up: Fake-Token Policy Age

## Status: deferred (UI hidden, scorer hardcoded)

The fake_token scorer's `policy_age_inverted` sub-score is currently
backed by a constant (`policy_age_slots = 1`). The "New Policy" donut
and the "Age" line in the FAKE TOKEN row are hidden in the UI so
operators don't read the hardcoded value as real signal.

To re-enable: ship an indexed `asset_policy_first_seen` lookup table,
wire it into the scorer, surface the real age in evidence, and re-add
the donut + Age row to the UI.

## Why deferred

Computing real policy age at scoring time requires an indexed point
lookup `(network, policy_id) -> first_slot`. The data exists in
`transaction_outputs.assets` (and `transactions.raw_data.mint`) but is
not indexed by policy_id. A substring scan
(`positionCaseInsensitive(assets, policy_id) > 0`) is acceptable on
preprod's ~14k outputs; on mainnet's millions of rows it's expensive
to run on every fake_token-gated transaction at ingestion time.

A dedicated lookup table populated incrementally at ingestion gives O(1)
queries and is the clean production solution.

## Implementation plan

### 1. ClickHouse schema (5 lines)

Add to [backend/app/db/clickhouse.py](../../backend/app/db/clickhouse.py) `execute_schema()`:

```sql
CREATE TABLE IF NOT EXISTS asset_policy_first_seen (
    network        String,
    policy_id      String,
    first_slot     UInt64,
    first_seen_at  DateTime DEFAULT now(),
    INDEX idx_policy policy_id TYPE bloom_filter GRANULARITY 1
) ENGINE = ReplacingMergeTree(first_seen_at)
ORDER BY (network, policy_id)
PARTITION BY network
```

ReplacingMergeTree on `(network, policy_id)` keeps the
earliest-inserted row (insertion order in our ingester guarantees
slot-ascending writes per partition). For belt-and-braces, swap to
`AggregatingMergeTree` + `minState(first_slot)` if cross-partition
ingestion races become a concern.

### 2. Ingester hook (~20 lines)

In the existing tx-write path (backend/app/ingestion/), after
`raw_data` is parsed:

```python
policy_ids: set[str] = set()
for out in raw_data.get("outputs", []):
    for k in out.get("value", {}).keys():
        if k not in ("lovelace", "ada"):
            policy_ids.add(k)
for policy_id in raw_data.get("mint", {}):
    policy_ids.add(policy_id)

if policy_ids:
    clickhouse.insert_asset_policy_first_seen(
        [(network, p, slot) for p in policy_ids]
    )
```

`insert_asset_policy_first_seen` is a thin wrapper; the
ReplacingMergeTree handles dedupe. Same connection / transaction
boundary as the existing tx writes — no new infra.

### 3. Scorer integration (5 lines)

In [backend/app/analysis/scorers/fake_token.py](../../backend/app/analysis/scorers/fake_token.py), replace the hardcoded block at
`policy_age_slots = 1` with:

```python
first_seen = clickhouse.get_policy_first_seen(network, policy_id)
if first_seen is not None and current_slot > first_seen:
    policy_age_slots = max(1, current_slot - first_seen)
else:
    policy_age_slots = 1  # fall back to most-suspicious when unknown
```

Plus a `get_policy_first_seen(network, policy_id) -> Optional[int]`
helper in `clickhouse.py` (point query, < 1ms with the bloom filter).

### 4. Evidence + UI (~10 lines)

- Emit `policy_age_slots` and `policy_age_days` in fake_token
  evidence.
- Re-add the "Age: <days> days" segment to the FAKE TOKEN row in
  [frontend/src/pages/AttackDetailPage.tsx](../../frontend/src/pages/AttackDetailPage.tsx)
  (revert the hide done in the same commit that introduced this doc).
- Re-add the "New Policy" donut to
  [frontend/src/mocks/attacks.ts](../../frontend/src/mocks/attacks.ts) `SUB_SCORE_LABELS["Fake Token"]`.

### 5. One-time backfill script (~30 lines)

`backend/scripts/oneoff/backfill_asset_policy_first_seen.py`:

```python
# Walk transaction_outputs in chunks, extract policy_ids per row,
# group by (network, policy_id), take min(slot) via JOIN with
# transactions, bulk-insert. Idempotent (ReplacingMergeTree dedupes).
```

Pattern mirrors [backfill_evidence.py](../../backend/scripts/oneoff/backfill_evidence.py): chunked
inserts, progress lines, dry-run vs `--apply`. Budget one full mainnet
pass at ~5-15 minutes for a few-million-row table.

### 6. Tests (~20 lines)

In `backend/tests/analysis/scorers/test_fake_token.py`:

- `test_policy_age_uses_lookup_when_present` — patch the helper to
  return a known slot, assert `s_policy_age` reflects the computed age
  rather than the hardcoded 1.
- `test_policy_age_falls_back_when_missing` — patch to return None,
  assert behaviour matches the current hardcoded path.
- `test_evidence_includes_policy_age_slots` — extend
  `test_evidence.py::test_fake_token_evidence`.

## Cost / risk summary

| Aspect | Cost | Notes |
|---|---|---|
| Storage | One row per `(network, policy_id)` | <1MB even at mainnet scale; tiny. |
| Ingestion hot path | One additional batch insert per tx | Same connection, micro-cost. |
| Scorer query | One bloom-filter-backed point lookup | < 1ms typically. |
| Backfill | One-time scan of `transaction_outputs` | 5-15 min on mainnet; cheap operational cost. |
| Score drift | Significant on existing fake_token alerts | Most go from "100% New Policy" to whatever their real age says; lots of alerts will drop in band. **Run the backfill in a maintenance window and announce the recalibration.** |

## Definition of done

- [ ] Schema migration shipped + verified idempotent.
- [ ] Ingester writes policy_ids on every tx; spot-check a few txs.
- [ ] Scorer queries the new table; unit tests cover both lookup-hit
      and lookup-miss paths.
- [ ] Evidence carries `policy_age_slots` / `policy_age_days`.
- [ ] UI re-shows the Age line and the New Policy donut, with
      tooltip wording updated to reference the real data source.
- [ ] One-time backfill complete; class-band shift documented in
      release notes.
