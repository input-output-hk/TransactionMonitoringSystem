#!/bin/bash
# TMS backup: Postgres dump + ClickHouse table exports (+ raw-store rsync note).
#
# Usage:
#   ./scripts/backup.sh [output-dir]      # default: ./backups/<UTC timestamp>
#
# What it captures:
#   postgres.sql.gz       full pg_dump of the operational DB (lifecycle,
#                         checkpoints, collisions, audit logs, entity state)
#   clickhouse/<t>.native.gz   per-table Native-format export of the
#                         analytics warehouse (portable across CH versions;
#                         restore with INSERT ... FORMAT Native)
#   MANIFEST              row counts per table at backup time
#
# The raw store (Data Lake) is plain files: back it up with rsync/restic of
# the raw_store_data volume (or RAW_STORE_PATH on host runs). It is the
# write-once source of raw payloads, so an incremental file backup is the
# right tool; this script deliberately does not duplicate it into a tarball.
#
# Restore procedure: RUNBOOK.md, section "Backup & restore".

set -euo pipefail

# Dumps contain the full operational DB (audit logs, entity state): keep
# every file owner-only readable.
umask 077

STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="${1:-./backups/$STAMP}"
mkdir -p "$OUT/clickhouse"
chmod 700 "$OUT"

POSTGRES_USER="${POSTGRES_USER:-tms_user}"
POSTGRES_DB="${POSTGRES_DB:-tms_db}"
CLICKHOUSE_DB="${CLICKHOUSE_DB:-tms_analytics}"
CLICKHOUSE_USER="${CLICKHOUSE_USER:-default}"
CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:-}"

# Name-only -e forwards the password from THIS shell's environment into the
# exec'd process without putting the secret in argv (visible in host `ps`);
# clickhouse-client natively reads the CLICKHOUSE_PASSWORD env var.
export CLICKHOUSE_PASSWORD
CH_CLIENT=(docker exec -e CLICKHOUSE_PASSWORD tms-clickhouse
           clickhouse-client --user "$CLICKHOUSE_USER")

# Every persistent table. tx_class_scores and archived_alerts are the
# product; the fact tables are recoverable from the raw store in principle
# but exporting them makes restore O(minutes) instead of O(re-ingest).
TABLES=(
    transactions transaction_inputs transaction_outputs address_transactions
    utxo_features tx_script_features tx_class_scores archived_alerts
    baselines baseline_drift_events
)

echo "Backing up to $OUT"

echo "[1/3] PostgreSQL ($POSTGRES_DB)"
docker exec tms-postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" \
    | gzip > "$OUT/postgres.sql.gz"

echo "[2/3] ClickHouse ($CLICKHOUSE_DB)"
: > "$OUT/MANIFEST"
for table in "${TABLES[@]}"; do
    count=$("${CH_CLIENT[@]}" --query "SELECT count() FROM $CLICKHOUSE_DB.$table" 2>/dev/null || echo "absent")
    if [ "$count" = "absent" ]; then
        echo "$table: absent (skipped)" >> "$OUT/MANIFEST"
        continue
    fi
    "${CH_CLIENT[@]}" --query "SELECT * FROM $CLICKHOUSE_DB.$table FORMAT Native" \
        | gzip > "$OUT/clickhouse/$table.native.gz"
    echo "$table: $count rows" >> "$OUT/MANIFEST"
    echo "  $table ($count rows)"
done

echo "[3/3] Raw store: NOT included (see header). Volume: raw_store_data"

echo "Done. Contents:"
cat "$OUT/MANIFEST"
