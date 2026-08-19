# One-off operational scripts

Operational artifacts, not part of the normal analysis pipeline. What matters
before you run one is whether it re-runs the SCORERS or only rewrites a column,
because that decides whether re-running it is safe.

Run everything from `backend/`, and with `-m`: these scripts import `app.*`, so
invoking one by path fails with `ModuleNotFoundError: No module named 'app'`.

```bash
cd backend && python -m scripts.oneoff.<script>          # dry-run
cd backend && python -m scripts.oneoff.<script> --apply  # write
```

Every script here is dry-run by default and writes only with `--apply`.

## Re-scores: they re-run the engine, so read before running

Each was written to move a specific historical batch of `tx_class_scores` rows
onto scorer logic that had just landed. They re-run the full analysis engine
against the CURRENT config, so re-running one after a later recalibration moves
scores under the new config, which is usually not what the original run intended.

Do not run unattended. Read the docstring at the top of the file, confirm the
targeted band / class / date range still matches the current DB state, and use
`--apply` only after a dry-run. Some carry a required scope flag
(`rescore_saturation_floor_2026_07.py` must be run with `--all-bands`).

- `reclassify_for_tuning_2026_06_01.py`
- `rescore_saturation_floor_2026_07.py`
- `backfill_evidence.py`: named a backfill because it fills the `evidence`
  column, but it fills it BY re-running the engine and re-inserting the row, so
  it belongs to this family. The ReplacingMergeTree supersedes the old row, and
  the pipeline is deterministic given the same inputs and config, so scores
  should not move; it reports any score or class change so drift from a config
  tweak is visible rather than silent.
- `_rescore_common.py`: shared helpers, not runnable on its own.

## Column rewrites: idempotent, resumable, safe to re-run

These run no scorer. Each is a pure projection of data already on the row (or a
relabelling of it), so it cannot move a score under a config that has since been
recalibrated. Each converges: it guards on the rows it can still improve, so an
interrupted run is resumed by running it again and a completed one reports
nothing pending instead of resubmitting work.

- `backfill_contract_address.py`: fills `tx_class_scores.contract_address`, which
  the contract-grouped risk-alerts view groups on. Work is chunked by
  `analyzed_at` window so no single ClickHouse mutation covers the whole table.
  Skipping it after the deploy leaves every transaction scored before the upgrade
  ungrouped in that view, with nothing failing or warning. See RUNBOOK.md,
  "Post-upgrade backfills".
- `backfill_asset_policy_first_seen.py`: seeds `asset_policy_first_seen` from
  historical outputs and mint maps. Idempotent by the table's own
  AggregatingMergeTree, which keeps `min(first_slot)` whatever the insert order,
  so re-runs and overlap with live ingestion are both harmless.
- `migrate_risk_band_low_to_informational.py`: rewrites the stored `risk_band`
  label `Low` to `Informational`. Scores and thresholds are untouched, and a
  second run matches zero rows.

## Lifecycle

After the corresponding re-score or rewrite has run successfully on every
environment (preprod, staging, production), the script may be deleted in a
follow-up.
