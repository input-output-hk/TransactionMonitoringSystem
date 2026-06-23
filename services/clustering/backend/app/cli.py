"""Command-line interface: process, evaluate, cluster, list targets, serve.

Examples
--------
    python -m app.cli process  --address addr1... --reprocess
    python -m app.cli evaluate --target addr1... --feature-set shape
    python -m app.cli cluster  --target addr1... --feature-set shape --eps 1.5 --min-samples 8
"""

from __future__ import annotations

import asyncio

import typer

from app.anomaly.detect import DEFAULT_TOP_QUANTILE
from app.config import get_settings, setup_logging
from app.features import FEATURE_SETS
from app.service import (
    cluster_target,
    detect_anomalies_for_target,
    evaluate_target,
    process_contract,
)
from app.storage.clickhouse import ClickHouseRepo

app = typer.Typer(add_completion=False, help="Cardano contract transaction clustering tool.")


@app.callback()
def _main() -> None:
    """Configure logging once before any command runs."""
    setup_logging()


def _echo(msg: str) -> None:
    typer.echo(msg)


def _check_feature_set(feature_set: str) -> None:
    if feature_set not in FEATURE_SETS:
        raise typer.BadParameter(f"feature_set must be one of {FEATURE_SETS}")


def _require_one_target(address: str | None, policy_id: str | None) -> None:
    if (address is None) == (policy_id is None):
        raise typer.BadParameter("Provide exactly one of --address or --policy-id.")


# Columns of tx_contract_anomaly in the order the rebuild copies them. MUST
# mirror clickhouse/init/009_contract_anomaly.sql (minus published_at, which the
# backfill stamps from scored_at). An explicit list, not `SELECT *`, so a future
# column add can't silently misalign the copy.
_CONTRACT_ANOMALY_COLS = (
    "network, tx_hash, target, cluster_id, iso_score, lof_score, consensus, "
    "votes, verdict, model_id, feature_set, evidence, scored_at"
)

# The table body matching 009. A plain (non-f) string so the `'{}'` evidence
# default stays a literal when interpolated into the CREATE below.
_CONTRACT_ANOMALY_BODY = (
    "network String, tx_hash String, target String, cluster_id Int32, "
    "iso_score Float64, lof_score Float64, consensus Float64, votes UInt8, "
    "verdict String, model_id String, feature_set String, "
    "evidence String DEFAULT '{}', scored_at DateTime DEFAULT now(), "
    "published_at DateTime64(6) DEFAULT now64(6)"
)


def _rebuild_contract_anomaly_version(repo: ClickHouseRepo, db: str) -> None:
    """Migrate an EXISTING tx_contract_anomaly from the pre-009 schema
    (ReplacingMergeTree(scored_at), no published_at) onto the published_at
    reconciliation version, preserving every stored row.

    A ReplacingMergeTree's version column cannot be changed by ALTER, so this
    rebuilds via a staging table + a single atomic RENAME. Properties:

    - No-op once migrated (engine already ReplacingMergeTree(published_at)) —
      fresh and previously-migrated volumes short-circuit, so it is safe to
      re-run; a manually ADD COLUMN'd table whose engine is still on scored_at is
      NOT treated as migrated and gets rebuilt.
    - Refuses to guess after a partial failed run: if leftover _v2 / backup
      tables exist it aborts and asks an operator to inspect/clean up.
    - Row-count parity is checked before the swap (aborts + cleans up on drift).
    - Non-destructive: the old table is kept as a timestamped backup, to be
      dropped explicitly once the migration has been verified.
    """
    from datetime import UTC, datetime

    client = repo.client

    def _count(sql: str, params: dict | None = None) -> int:
        return int(client.query(sql, parameters=params or {}).result_rows[0][0])

    # Decide the migration state from the table's ENGINE, not just the column.
    # The unsafe state to catch is published_at added manually while the engine
    # is still ReplacingMergeTree(scored_at): a column-only check would treat
    # that as migrated and leave the recall regression in place (dedup still
    # keyed on scored_at). So skip ONLY when the engine confirms the new version;
    # the manual-bad-state then falls through to the rebuild below and self-heals.
    engine_rows = client.query(
        "SELECT engine_full FROM system.tables "
        "WHERE database = {db:String} AND name = 'tx_contract_anomaly'",
        parameters={"db": db},
    ).result_rows
    # Table absent (009's CREATE didn't run for some reason) — leave it to the
    # schema guard to report rather than masking it here.
    if not engine_rows:
        return
    # Normalise whitespace/case so the match is robust to engine_full formatting.
    engine_full = str(engine_rows[0][0]).lower().replace(" ", "")
    if "replacingmergetree(published_at)" in engine_full:
        return

    # Don't guess after a partial failure — make the operator inspect/clean up.
    leftovers = [
        r[0]
        for r in client.query(
            "SELECT name FROM system.tables WHERE database = {db:String} AND "
            "(name = 'tx_contract_anomaly_v2' OR "
            "name LIKE 'tx_contract_anomaly_backup_%')",
            parameters={"db": db},
        ).result_rows
    ]
    if leftovers:
        typer.echo(
            "Refusing to migrate tx_contract_anomaly: leftover table(s) from a "
            f"previous run present ({', '.join(leftovers)}). Inspect and drop "
            "them, then re-run migrate.",
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo("  tx_contract_anomaly: rebuilding onto published_at version…")
    # 1. Staging table with the NEW engine (versioned by published_at).
    client.command(
        f"CREATE TABLE {db}.tx_contract_anomaly_v2 ({_CONTRACT_ANOMALY_BODY}) "
        "ENGINE = ReplacingMergeTree(published_at) "
        "ORDER BY (network, tx_hash, target)"
    )
    # 2. Backfill, explicit columns. The version source depends on whether the
    #    OLD table already has a published_at column: PRESERVE it when present
    #    (the bad state — column added manually while the engine stayed on
    #    scored_at — may already hold REAL reconciliation timestamps from the new
    #    publisher; re-stamping them from scored_at would recreate the exact
    #    tombstone-vs-reraise recall bug). When the column is absent (the normal
    #    old-volume path) there is no reconciliation history yet, so seed it from
    #    scored_at (the original run/classify time).
    has_published_at = _count(
        "SELECT count() FROM system.columns WHERE database = {db:String} "
        "AND table = 'tx_contract_anomaly' AND name = 'published_at'",
        {"db": db},
    )
    version_src = "published_at" if has_published_at else "scored_at"
    client.command(
        f"INSERT INTO {db}.tx_contract_anomaly_v2 "
        f"({_CONTRACT_ANOMALY_COLS}, published_at) "
        f"SELECT {_CONTRACT_ANOMALY_COLS}, {version_src} FROM {db}.tx_contract_anomaly"
    )
    # 3. Row-count parity BEFORE the swap. The INSERT copies stored rows 1:1, so
    #    the raw (pre-merge) counts must match exactly; abort + clean up if not.
    old_n = _count(f"SELECT count() FROM {db}.tx_contract_anomaly")
    new_n = _count(f"SELECT count() FROM {db}.tx_contract_anomaly_v2")
    if old_n != new_n:
        client.command(f"DROP TABLE {db}.tx_contract_anomaly_v2")
        typer.echo(
            f"Aborting tx_contract_anomaly migration: row-count mismatch "
            f"(old {old_n}, staged {new_n}); dropped the staging table.",
            err=True,
        )
        raise typer.Exit(code=1)
    # 4. Atomic swap: old → timestamped backup, staging → canonical, in one
    #    RENAME. The backup is KEPT for verification; drop it explicitly after.
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup = f"tx_contract_anomaly_backup_{ts}"
    client.command(
        f"RENAME TABLE {db}.tx_contract_anomaly TO {db}.{backup}, "
        f"{db}.tx_contract_anomaly_v2 TO {db}.tx_contract_anomaly"
    )
    typer.echo(
        f"  tx_contract_anomaly: migrated {new_n} row(s); previous table kept as "
        f"{backup} (drop it once verified)."
    )


@app.command()
def migrate(
    init_dir: str = typer.Option(
        "/init", help="Directory of NNN_*.sql migration files (compose mounts clickhouse/init here)."
    ),
) -> None:
    """Apply the schema to the live ClickHouse, statement by statement.

    Convention: every statement in clickhouse/init/*.sql is self-idempotent
    (CREATE ... IF NOT EXISTS / ADD COLUMN IF NOT EXISTS / guarded UPDATE), so this
    simply re-applies all files in name order — no version ledger needed. Fresh
    volumes run the same files via the ClickHouse entrypoint; this command is for
    EXISTING volumes after an upgrade (the docker image only runs init SQL once).
    """
    import re
    from pathlib import Path

    files = sorted(Path(init_dir).glob("*.sql"))
    if not files:
        raise typer.BadParameter(f"no *.sql files found in {init_dir}")
    repo = ClickHouseRepo()
    # The init files name the `tms` database by default (what a fresh-volume
    # ClickHouse entrypoint needs). The module runs against `tms_clustering` on
    # the TMS's ClickHouse server, so rewrite the DB token to the configured
    # database before applying the same idempotent statements. `\btms\b` matches
    # only the `tms` DB references (never `tms_clustering` or a column name); a
    # no-op when clickhouse_db == "tms".
    db = repo._db
    try:
        for f in files:
            # Strip `--` comments BEFORE splitting on ';' — comments may contain
            # semicolons. Safe because no string literal in these files contains
            # `--` (and the convention forbids introducing one).
            sql = "\n".join(line.split("--", 1)[0] for line in f.read_text().splitlines())
            if db != "tms":
                sql = re.sub(r"\btms\b", db, sql)
            statements = [s.strip() for s in sql.split(";") if s.strip()]
            for stmt in statements:
                repo.client.command(stmt)
            typer.echo(f"  {f.name}: {len(statements)} statement(s) applied")
        # 009 changed tx_contract_anomaly's ReplacingMergeTree version column
        # from scored_at to published_at. CREATE ... IF NOT EXISTS can't migrate
        # an already-created table's engine, so rebuild it in place here
        # (guarded + data-preserving) before the schema guard runs.
        _rebuild_contract_anomaly_version(repo, db)
        missing = repo.missing_schema_objects()
        if missing:
            for obj in missing:
                typer.echo(f"STILL MISSING after migrate: {obj}", err=True)
            typer.echo(
                "The init files don't create the objects above — add the next "
                "NNN_*.sql migration (see docs/data-model.md).", err=True,
            )
            raise typer.Exit(code=1)
        typer.echo("Schema is up to date.")
    finally:
        repo.close()


@app.command()
def targets() -> None:
    """List ingested targets and their transaction counts."""
    repo = ClickHouseRepo()
    rows = repo.list_targets()
    if not rows:
        typer.echo("No targets ingested yet.")
        return
    for r in rows:
        typer.echo(f"  [{r['target_type']:>7}] {r['target']}  ({r['tx_count']} txs)")


@app.command()
def process(
    address: str = typer.Option(None, help="Script/payment address (addr1...)."),
    policy_id: str = typer.Option(None, help="Minting policy id (script hash)."),
    max_txs: int = typer.Option(None, help="Stop after this many transactions."),
    reprocess: bool = typer.Option(
        False, help="Re-run the pipeline on already-ingested txs (the host_ch path)."
    ),
) -> None:
    """Run the full canonical pipeline for a contract (metadata → cluster →
    shape/graph anomaly → publish). Used for onboarding and the in-place backfill;
    under host_ch the data is read from the host TMS, so use ``--reprocess``."""
    settings = get_settings()
    _require_one_target(address, policy_id)
    target = address or policy_id
    target_type = "address" if address else "policy"

    repo = ClickHouseRepo(settings)
    try:
        summary = asyncio.run(
            process_contract(
                repo,
                target=target,
                target_type=target_type,
                max_txs=max_txs,
                reprocess=reprocess,
                progress=_echo,
            )
        )
    except Exception as exc:  # surface a clean message, not a traceback
        typer.secho(f"\nprocess failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"\nDone: {summary}")


@app.command()
def evaluate(
    target: str = typer.Option(..., help="Target address or policy id (must be ingested)."),
    feature_set: str = typer.Option("shape", help=f"One of {FEATURE_SETS}."),
) -> None:
    """Run k-distance + grid-search parameter evaluation for a target."""
    _check_feature_set(feature_set)
    repo = ClickHouseRepo()
    report = evaluate_target(repo, target, feature_set)

    typer.echo(f"feature_set={report['feature_set']} metric={report['metric']} "
               f"n_points={report['n_points']} n_features={report['n_features']}")
    if report.get("message"):
        typer.echo(report["message"])
        return
    knee = report["k_distance"]["knee_eps"]
    typer.echo(f"k-distance knee (suggested eps) ≈ {knee:.4f} (k={report['k_distance']['k']})")
    typer.echo("\n  eps        min_samples  clusters  noise%   silhouette")
    typer.echo("  " + "-" * 56)
    for r in report["grid"]:
        sil = "  n/a " if r["silhouette"] is None else f"{r['silhouette']:+.3f}"
        typer.echo(
            f"  {r['eps']:<10.4f} {r['min_samples']:<12} {r['n_clusters']:<9} "
            f"{r['noise_ratio'] * 100:>5.1f}%   {sil}"
        )
    rec = report["recommended"]
    if rec:
        typer.echo(f"\nRecommended: eps={rec['eps']} min_samples={rec['min_samples']}")
        typer.echo(f"  ({rec['rationale']})")


@app.command()
def cluster(
    target: str = typer.Option(..., help="Target address or policy id (must be ingested)."),
    feature_set: str = typer.Option("shape", help=f"One of {FEATURE_SETS}."),
    eps: float = typer.Option(..., help="DBSCAN eps (neighbourhood radius)."),
    min_samples: int = typer.Option(..., help="DBSCAN min_samples."),
    notes: str = typer.Option("", help="Free-text note stored with the run."),
) -> None:
    """Run DBSCAN with the chosen parameters and persist a cluster run."""
    _check_feature_set(feature_set)
    repo = ClickHouseRepo()
    summary = cluster_target(repo, target, feature_set, eps, min_samples, notes=notes)
    typer.echo(
        f"run_id={summary['run_id']}\n"
        f"  n_points={summary['n_points']} n_clusters={summary['n_clusters']} "
        f"n_noise={summary['n_noise']} silhouette={summary['silhouette']}"
    )


@app.command()
def anomaly(
    target: str = typer.Option(..., help="Target address or policy id (must be ingested)."),
    feature_set: str = typer.Option("shape", help=f"One of {FEATURE_SETS}."),
    eps: float | None = typer.Option(None, help="DBSCAN eps for the noise signal (auto if unset)."),
    min_samples: int | None = typer.Option(None, help="DBSCAN min_samples (auto if unset)."),
    top_quantile: float = typer.Option(
        DEFAULT_TOP_QUANTILE, help="Per-detector flag threshold (top fraction)."
    ),
    top: int = typer.Option(20, help="How many top candidates to print."),
) -> None:
    """Rank transactions by ensemble anomaly score (Isolation Forest + LOF + DBSCAN)."""
    _check_feature_set(feature_set)
    repo = ClickHouseRepo()
    summary = detect_anomalies_for_target(
        repo, target, feature_set, eps=eps, min_samples=min_samples, top_quantile=top_quantile
    )
    typer.echo(
        f"run_id={summary['run_id']} methods={'+'.join(summary['methods'])} "
        f"n_points={summary['n_points']} flagged(>=2 votes)={summary['n_flagged']} "
        f"(dbscan eps={summary['eps']:.3f} min_samples={summary['min_samples']})"
    )
    rows = repo.top_anomalies(summary["run_id"], target, limit=top)
    typer.echo(
        f"\n  {'rank':>4} {'tx_hash':16} {'cons':>5} {'votes':>5} "
        f"{'fee':>6} {'out_ADA':>14} {'in/out':>7} {'assets':>6}"
    )
    typer.echo("  " + "-" * 74)
    for r in rows:
        io = f"{r['input_count']}/{r['output_count']}"
        typer.echo(
            f"  {r['score_rank']:>4} {r['tx_hash'][:16]} {r['consensus']:>5.2f} "
            f"{r['votes']:>5} {r['fees'] / 1e6:>6.2f} {r['total_output_lovelace'] / 1e6:>14,.0f} "
            f"{io:>7} {r['distinct_assets']:>6}"
        )


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Run the FastAPI server (development convenience)."""
    import uvicorn

    uvicorn.run("app.api.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    app()
