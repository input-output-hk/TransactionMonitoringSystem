# Traceability

One row per capability: what the system does, where it is implemented, and the
test that proves it. Every test reference is a runnable identifier, so a claim
here can be checked individually rather than accepted as a whole.

Every identifier below is written from the repository root and runs from there,
copied verbatim:

```bash
uv run pytest "backend/tests/analysis/scorers/test_sandwich.py::TestScore::test_linked_attacker_high_score" -v
```

The clustering module is a separate Python project with its own dependencies, so
its rows need that project's environment:

```bash
uv run --project services/clustering/backend --extra dev pytest "services/clustering/backend/tests/test_clustering.py::test_run_dbscan_finds_two_clusters" -v
```

The Storage and Performance sections cite rows in the two opt-in tiers, which
need their databases and their tier flag: prefix the command with
`TMS_LIVE_DB_TESTS=1` or `TMS_PERF_TESTS=1`. Naming a node id explicitly bypasses
the tier's own skip guard, so without the flag those rows end in a connection
error rather than a clean skip.

`backend/scripts/check_traceability.py` runs in CI and fails the build if any
identifier on this page stops resolving, so a renamed or deleted test breaks the
build rather than quietly turning a row into a false claim.

Nothing here restates a figure published elsewhere. Counts live in
[TESTING.md](TESTING.md), which CI checks separately.

## Ingestion

| Capability | Implementation | Proving test |
|---|---|---|
| Parse blocks from a Cardano node across Ogmios v5 and v6 payload shapes | `backend/app/ingestion/ogmios_parser.py` | `backend/tests/ingestion/test_ogmios_parser.py::TestFailedV6Transaction::test_failed_tx_consumes_collateral_only` |
| Observe the mempool and resolve pending inputs | `backend/app/ingestion/mempool_monitor.py` | `backend/tests/ingestion/test_mempool_resolution.py::TestParseResolvedUtxo::test_v6_ada_nested_value` |
| Prune mempool state on a bounded cadence | `backend/app/ingestion/mempool_monitor.py` | `backend/tests/ingestion/test_mempool_prune_cadence.py::TestPruneCadence::test_sweep_actually_evicts_stale_state` |
| Never advance the sync checkpoint past an unpersisted block | `backend/app/ingestion/ogmios_client.py` | `backend/tests/ingestion/test_persistence_semantics.py::TestCheckpointNotAdvancedOnFailure::test_failed_insert_never_saves_sync_point` |
| Survive a dropped connection with backoff and a circuit breaker | `backend/app/ingestion/resilience.py` | `backend/tests/ingestion/test_reconnect_loop.py::test_connection_lost_branch_backs_off` |
| Purge derived rows when the chain rolls back | `backend/app/db/clickhouse.py` | `backend/tests/db/test_rollback_purge.py::TestClusteringPurge::test_cross_db_deletes_carry_projection_mode` |
| Find a stored raw payload written under an adjacent day's prefix | `backend/app/db/raw_store.py` | `backend/tests/ingestion/test_raw_fallback.py::TestReadConfirmed::test_adjacent_day_probe` |
| Backfill an address's history from a Kupo index, within a bound | `backend/app/ingestion/address_backfill.py` | `backend/tests/ingestion/test_address_backfill.py::test_backfill_caps_at_max_txs` |
| Stamp a block with chain time rather than wall-clock time | `backend/app/ingestion/chain_time.py` | `backend/tests/ingestion/test_chain_time.py::TestChainSyncWiring::test_block_timestamp_is_chain_time` |

## Detection

Each class's row cites the case that must keep firing. The whole set runs as
`uv run pytest backend/tests/analysis/scorers/ -m attack_must_fire`.

| Capability | Implementation | Proving test |
|---|---|---|
| Detect token-dust flooding | `backend/app/analysis/scorers/token_dust.py` | `backend/tests/analysis/scorers/test_token_dust.py::TestDosAssetThresholdDiscriminator::test_high_asset_count_one_policy_fires_composite_reason` |
| Detect extreme-quantity value attacks | `backend/app/analysis/scorers/large_value.py` | `backend/tests/analysis/scorers/test_large_value.py::TestScore::test_extreme_quantity_high_score` |
| Detect datum-bloat attacks | `backend/app/analysis/scorers/large_datum.py` | `backend/tests/analysis/scorers/test_large_datum.py::TestGate::test_high_entropy_extreme_bloat_gates` |
| Detect double satisfaction | `backend/app/analysis/scorers/multiple_sat.py` | `backend/tests/analysis/scorers/test_multiple_sat.py::TestLazyValidatorBandFloor::test_lazy_validator_floors_to_high_band` |
| Detect front-running via mempool collision | `backend/app/analysis/scorers/front_running.py` | `backend/tests/analysis/scorers/test_front_running.py::TestScore::test_confirmed_collision_high_score` |
| Detect sandwich attacks | `backend/app/analysis/scorers/sandwich.py` | `backend/tests/analysis/scorers/test_sandwich.py::TestScore::test_linked_attacker_high_score` |
| Detect circular transfer layering | `backend/app/analysis/scorers/circular.py` | `backend/tests/analysis/scorers/test_circular.py::TestScore::test_high_similarity_scores_well` |
| Detect counterfeit token distribution | `backend/app/analysis/scorers/fake_token.py` | `backend/tests/analysis/scorers/test_fake_token.py::TestScore::test_wide_critical_asset_clone_must_reach_high` |
| Detect phishing via metadata and asset names | `backend/app/analysis/scorers/phishing.py` | `backend/tests/analysis/scorers/test_phishing.py::TestNetNewHolderTargeting::test_url_airdrop_with_se_name_recall_pin_high` |

### The scoring framework the classes share

| Capability | Implementation | Proving test |
|---|---|---|
| Normalise a feature against learned percentiles | `backend/app/analysis/normalise.py` | `backend/tests/analysis/test_normalise.py::TestNormalise::test_midpoint` |
| Refuse a baseline whose spread is uninformative | `backend/app/analysis/normalise.py` | `backend/tests/analysis/test_normalise.py::TestBaselineSpreadGuard::test_baseline_is_usable_p50_zero` |
| Recompute baselines over configured windows | `backend/app/analysis/baselines.py` | `backend/tests/analysis/test_baselines_recompute.py::TestChainTimeWindows::test_global_window_days_from_config` |
| Cap a poisoned baseline so it cannot silence a real attack | `backend/app/analysis/scorer_config.py` | `backend/tests/analysis/scorers/test_multiple_sat.py::TestBaselinePoisoningResistance::test_poisoned_wide_baseline_cannot_silence_drain` |
| Record which tuning and build produced a score | `backend/app/analysis/engine.py` | `backend/tests/analysis/test_score_provenance.py::TestEngineStampsProvenance::test_config_hash_is_written` |
| Publish a digest recipe a reader can recompute from the config file | `backend/app/analysis/scorer_config.py` | `backend/tests/analysis/test_score_provenance.py::TestConfigHash::test_reproducible_from_the_shipped_file` |
| Record the baseline each axis was measured against | `backend/app/analysis/scorer_config.py` | `backend/tests/analysis/test_baseline_provenance.py::TestRecording::test_uncapped_pair_is_kept_when_a_cap_bites` |
| Measure a transfer cycle: length, amount similarity, round-amount shape | `backend/app/analysis/graph.py` | `backend/tests/analysis/test_graph.py::TestBuildCycleResult::test_basic_cycle_metrics` |
| Extract script-level features from a payload | `backend/app/analysis/features.py` | `backend/tests/analysis/test_features.py::TestDatumWitnessAndObjectDatum::test_datum_hash_sized_from_witness_preimage_hex` |
| Keep every enrichment query scoped to one network | `backend/app/analysis/engine.py` | `backend/tests/analysis/test_engine_network_scoping.py::TestEnrichmentQueriesAreNetworkScoped::test_network_is_never_cross_bound` |
| Resolve the contract an alert implicates | `backend/app/analysis/contract_identity.py` | `backend/tests/analysis/test_contract_identity.py::TestContractAnomaly::test_resolves_the_watched_target` |
| Retry rather than persist a partially scored transaction | `backend/app/analysis/engine.py` | `backend/tests/analysis/test_engine.py::TestIncompleteScoring::test_raising_scorer_is_reported_not_swallowed` |

## API and access control

| Capability | Implementation | Proving test |
|---|---|---|
| Authenticate programmatic clients by API key | `backend/app/auth/` | `backend/tests/api/test_admin_or_api_key_dep.py::test_valid_api_key_allows` |
| Reject a request carrying no credential | `backend/app/auth/` | `backend/tests/api/test_admin_or_api_key_dep.py::test_no_credential_401` |
| Separate Admin from Reviewer authority | `backend/app/api/users.py` | `backend/tests/api/test_users_api.py::TestAccessControl::test_reviewer_forbidden` |
| Authenticate dashboard users by one-shot magic link | `backend/app/api/auth.py` | `backend/tests/api/test_auth_tokens_sessions.py::TestClaimSessionToken::test_lock_delete_clear_sequence` |
| Issue and self-heal the CSRF cookie alongside the session | `backend/app/api/auth.py` | `backend/tests/api/test_auth_cookie_security.py::TestCSRFCookieIssuance::test_set_session_cookie_also_sets_csrf_cookie` |
| Reject an unauthenticated WebSocket before registering it | `backend/app/main.py` | `backend/tests/api/test_websocket.py::TestAuthCloseCode::test_rejected_socket_is_never_registered` |
| Bound WebSocket handshake attempts | `backend/app/rate_limit.py` | `backend/tests/api/test_websocket.py::TestHandshakeRateLimit::test_third_connect_rejected` |
| Refuse to boot with an insecure configuration | `backend/app/main.py` | `backend/tests/api/test_startup_guards.py::TestCorsFailFast::test_wildcard_cors_with_keys_refuses_start` |
| Attribute an audited action to the server-verified principal | `backend/app/audit.py` | `backend/tests/api/test_audit_fail_closed.py::TestActorIsAuthenticatedPrincipal::test_actor_is_server_principal_not_client_field` |
| Never log a raw API key as an actor | `backend/app/audit.py` | `backend/tests/api/test_audit_fail_closed.py::TestActorIsAuthenticatedPrincipal::test_api_key_actor_is_fingerprint_not_raw_key` |
| Map a warehouse row to the transaction contract by index | `backend/app/api/transactions.py` | `backend/tests/api/test_transactions_row_mapping.py::test_row_to_transaction_maps_every_field_by_index` |
| Group alerts by contract without losing the total | `backend/app/db/clickhouse_scores.py` | `backend/tests/api/test_grouped_alerts.py::TestAlertTotal::test_alert_total_sums_the_group_counts_and_the_lone_alerts` |
| Report pipeline and dependency health | `backend/app/main.py` | `backend/tests/api/test_health.py::TestClusteringHealthHeartbeat::test_old_heartbeat_is_stale` |
| Forward clustering mutations with the caller's authority | `backend/app/api/clustering.py` | `backend/tests/api/test_clustering_proxy.py::test_forwards_api_key_when_configured` |
| Neutralise formula injection in exported CSV | `backend/app/api/archive.py` | `backend/tests/api/test_archive.py::TestCsvInjectionNeutralization::test_formula_prefixes_are_quoted` |

## Alerting

| Capability | Implementation | Proving test |
|---|---|---|
| Trigger alerts from configurable per-class and per-band rules | `backend/app/notifications/triggers.py` | `backend/tests/notifications/test_triggers.py::test_band_default_when_no_rule_matches` |
| Drop a configured channel that resolves to no target | `backend/app/notifications/triggers.py` | `backend/tests/notifications/test_triggers.py::test_channel_with_no_target_is_dropped` |
| Collapse a burst into one alert per contract and band | `backend/app/notifications/grouping.py` | `backend/tests/notifications/test_alert_grouping.py::TestDeliveryPath::test_first_of_a_group_delivers_and_claims` |
| Leave a group unclaimed when delivery fails, so it retries | `backend/app/notifications/dispatcher.py` | `backend/tests/notifications/test_dedup_ordering.py::test_failed_delivery_is_not_claimed` |
| Suppress a duplicate before spending a delivery | `backend/app/notifications/dispatcher.py` | `backend/tests/notifications/test_dedup_ordering.py::test_duplicate_is_skipped_before_delivery` |
| Deliver anyway when the duplicate check itself fails | `backend/app/notifications/dispatcher.py` | `backend/tests/notifications/test_dedup_ordering.py::test_dedup_check_failure_still_delivers` |
| Bound concurrent deliveries | `backend/app/notifications/dispatcher.py` | `backend/tests/notifications/test_delivery_concurrency.py::test_deliveries_are_concurrency_bounded` |
| Resolve a channel by name, dropping one that is not configured | `backend/app/notifications/triggers.py` | `backend/tests/notifications/test_triggers.py::test_unknown_channel_in_trigger_is_dropped` |
| Refuse a webhook target inside the private network | `backend/app/notifications/channels/webhook.py` | `backend/tests/notifications/test_webhook_channel.py::test_resolves_internal_true_for_private_and_link_local` |
| Assemble a scheduled digest over a trailing window | `backend/app/notifications/reports.py` | `backend/tests/notifications/test_report_contract_anomaly.py::test_count_filters_by_window_and_min_band` |

## Storage

| Capability | Implementation | Proving test |
|---|---|---|
| Apply the schema idempotently on every boot | `backend/app/db/clickhouse_schema.py` | `backend/tests/live_db/test_clickhouse_live.py::TestSchema::test_schema_reapplies_idempotently` |
| Order column migrations before projection migrations | `backend/app/db/clickhouse_schema.py` | `backend/tests/db/test_schema_migrations.py::TestCreateAllMigrationOrder::test_projection_migration_runs_after_column_migrations` |
| Gate a projection swap on the live table definition | `backend/app/db/clickhouse_schema.py` | `backend/tests/db/test_schema_migrations.py::TestProjectionMigrationGate::test_legacy_projection_triggers_swap` |
| Collapse duplicates in bounded chunks rather than one pass | `backend/app/db/clickhouse_schema.py` | `backend/tests/db/test_migrate_dedup_chunking.py::TestChunkedCollapse::test_collapse_runs_one_insert_per_bucket_with_memory_settings` |
| Never alias an aggregate onto a source column name | `backend/app/db/clickhouse_schema.py` | `backend/tests/db/test_migrate_dedup_chunking.py::TestChunkedCollapse::test_collapse_select_never_aliases_aggregates` |
| Keep score provenance columns cheap to store | `backend/app/db/clickhouse_schema.py` | `backend/tests/live_db/test_clickhouse_live.py::TestScoreProvenance::test_columns_are_low_cardinality` |
| Round-trip a score through the real warehouse | `backend/app/db/clickhouse_scores.py` | `backend/tests/live_db/test_clickhouse_live.py::TestScoreReadPath::test_write_read_list_count_stats` |
| Keep the insert row aligned with its column list | `backend/app/db/clickhouse_scores.py` | `backend/tests/db/test_score_insert_columns.py::TestInsertRowAlignment::test_row_width_matches_the_column_list` |
| Exclude archived false positives from every score read | `backend/app/db/clickhouse_scores.py` | `backend/tests/db/test_class_score_filters.py::TestExcludeTxHashes::test_renders_a_not_in_clause_with_one_list_param` |
| Prune the audit log on its retention window | `backend/app/db/postgres.py` | `backend/tests/db/test_audit_retention.py::TestPruneAuditLogs::test_prune_sql_uses_created_at_window` |
| Warn when a retention setting undercuts an analysis window | `backend/app/db/clickhouse_schema.py` | `backend/tests/db/test_retention_warnings.py::TestRetentionWarnings::test_features_retention_below_window_warns` |
| Verify percentile SQL parses on the server, not only in mocks | `backend/app/analysis/baselines.py` | `backend/tests/live_db/test_clickhouse_live.py::TestBaselines::test_percentile_recompute_sql_parses_on_server` |

## Clustering module

| Capability | Implementation | Proving test |
|---|---|---|
| Cluster a contract's transactions with DBSCAN | `services/clustering/backend/app/clustering/dbscan.py` | `services/clustering/backend/tests/test_clustering.py::test_run_dbscan_finds_two_clusters` |
| Score the partition, deterministically above the sampling cap | `services/clustering/backend/app/clustering/dbscan.py` | `services/clustering/backend/tests/test_clustering.py::test_silhouette_sampling_is_deterministic_and_still_scores` |
| Rank outliers against a fitted model | `services/clustering/backend/app/anomaly/detect.py` | `services/clustering/backend/tests/test_anomaly.py::test_detect_ranks_outliers_first` |
| Flag an attack inside the default window | `services/clustering/backend/app/anomaly/detect.py` | `services/clustering/backend/tests/test_anomaly.py::test_detect_flags_attack_within_the_default_window` |
| Apply an analyst verdict to a cluster's members | `services/clustering/backend/app/service/labels.py` | `services/clustering/backend/tests/test_api.py::test_label_cluster_writes_labels` |
| Read a past run through its own stored membership, not the rolling window | `services/clustering/backend/app/storage/clickhouse/host_backed.py` | `services/clustering/backend/tests/test_storage.py::test_run_scoped_reads_never_join_the_rolling_window` |

## Performance

| Capability | Implementation | Proving test |
|---|---|---|
| Score transactions above a throughput budget | `backend/app/analysis/engine.py` | `backend/tests/perf/test_scoring_throughput.py::test_scoring_throughput_meets_budget` |
| Parse and insert a block replay above a throughput budget | `backend/app/ingestion/` | `backend/tests/perf/test_ingestion_replay.py::test_ingestion_replay` |
| Answer dashboard queries below a p95 latency budget | `backend/app/db/clickhouse_scores.py` | `backend/tests/perf/test_query_latency.py::test_dashboard_query_p95_within_budget` |
