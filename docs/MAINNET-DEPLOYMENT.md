# Mainnet Deployment

This document is the path from an empty server to a Transaction Monitoring System
watching Cardano mainnet. It assumes you know Cardano and Docker but nothing about
this repository, and it is written to be followed once, in order, without needing
to read the source.

Everything quantitative here was measured on AP 901's mainnet instance on
2026-07-30, over an observation window of 15.17 days (2026-07-15 12:08:45 to
2026-07-30 16:33:27 UTC; the storage figures were sampled a few minutes earlier, at
15.14 days). That instance is a demonstration and an evidence source, not a system
being handed over: you will run your own. Where a figure is an extrapolation rather
than a measurement it is labelled as one.

Three companion documents carry detail this one deliberately does not repeat.
[RUNBOOK.md](../RUNBOOK.md) is the full configuration reference and the
day-to-day operations manual. [docs/ALERTING.md](ALERTING.md) is how alert
routing is configured. [docs/TECHNOLOGY-DECISIONS.md](TECHNOLOGY-DECISIONS.md)
holds the ADRs, including the node and Ogmios version contract in ADR-004 and the
raw-store design in ADR-009.

## What You Are Building: The Service Inventory

`docker-compose.yml` defines eight services across four Compose profiles. Services
with no profile start unconditionally; a profiled service starts only when you name
its profile. The mainnet-relevant command is `docker compose --profile app up -d`,
which starts postgres, clickhouse and app, and nothing else.

| Service | Container | Profile | Needed on mainnet | `mem_limit` | Healthcheck | Volumes | Published |
|---|---|---|---|---|---|---|---|
| `postgres` | `tms-postgres` | none (always) | Required | `${POSTGRES_MEM_LIMIT:-1g}` | `pg_isready`, 10s/5s/5 | `postgres_data` at `/var/lib/postgresql` | `127.0.0.1:${POSTGRES_PORT:-5433}:5432` |
| `clickhouse` | `tms-clickhouse` | none (always) | Required | `${CLICKHOUSE_MEM_LIMIT:-4g}` | HTTP `/ping`, 10s/5s/5 | `clickhouse_data`, `clickhouse_logs`, single-file bind mount of `clickhouse/config.d/log-retention.xml` | `127.0.0.1:${CLICKHOUSE_HTTP_PORT:-8123}:8123`, `127.0.0.1:${CLICKHOUSE_NATIVE_PORT:-9000}:9000` |
| `app` | `tms-app` | `app` | Required | `${APP_MEM_LIMIT:-2g}` | `/health/ready`, 30s/5s/3, 30s grace | `raw_store_data` at `/data/raw` | `127.0.0.1:${API_PORT:-8000}:8000` |
| `clustering` | `clustering-sidecar` | `clustering` | Optional | `${CLUSTERING_MEM_LIMIT:-3g}` | `/api/health`, 30s/5s/3, 40s grace | bind mount of the sidecar's init SQL | `127.0.0.1:${CLUSTERING_PORT:-8010}:8000` |
| `kupo` | `tms-kupo` | `ingestion` | Optional, backfill only | none | none | `kupo-db`, `node-ipc`, node config | `127.0.0.1:1442:1442` |
| `cardano-node` | `tms-cardano-node` | `ingestion` | No | none | none | `cardano-node-db`, `node-ipc`, node config | none |
| `ogmios` | `tms-ogmios` | `ingestion` | No | none | none | `node-ipc`, node config | `127.0.0.1:1337:1337` |
| `mailpit` | `tms-mailpit` | `mail` | No | none | none | none | `127.0.0.1:1025:1025`, `127.0.0.1:8025:8025` |

Every published port binds to loopback. Nothing in this stack terminates TLS or
faces the internet; a reverse proxy or tunnel in front of `${API_PORT}` is your
responsibility (see [RUNBOOK.md](../RUNBOOK.md#running-behind-cloudflare-tunnel)
for the client-IP attribution rules that go with it).

All four `*_MEM_LIMIT` variables are documented in the RUNBOOK's configuration
reference; in the templates, only `CLICKHOUSE_MEM_LIMIT` appears, as a commented
line in `.env.example`. They are Compose interpolations, so they must be set in
the top-level `.env` or the shell: a per-network `.env.<name>` cannot change them.

### The `ingestion` profile is a development convenience

`cardano-node`, `ogmios` and `kupo` carry no `mem_limit` and no healthcheck. That
is deliberate and it is also the reason they are unsuitable for a production
mainnet deployment. A mainnet node is the largest and most memory-hungry process
on any box that runs one, and the whole point of the limits on the other services
is that no single service can starve the rest under memory pressure. An unbounded
node inside the same Compose project defeats that. Without a healthcheck, Compose
and any orchestrator above it cannot tell a syncing node from a wedged one.

A real deployment therefore runs the node and Ogmios outside this Compose project,
with their own volumes, their own memory limit and their own lifecycle, and points
TMS at them with `OGMIOS_WS_URL`. That does not have to mean a second machine. The
reference deployment runs them as a separate Compose project on the same 8 vCPU,
31 GB host (containers `cardano-node-mainnet` and `ogmios-mainnet`, distinct from
this repository's `tms-cardano-node` and `tms-ogmios`), and that project sets a
27 GB `mem_limit` on the node which this repository's compose file does not.

Whether co-location is right for you is a memory question you can answer with the
reference numbers. The node's observed resident set was 16.41 GiB. The TMS caps
under `--profile app` add 4 GiB (ClickHouse) plus 2 GiB (app) plus 1 GiB (Postgres),
so a co-located stack is already committed to about 23.4 GiB of a 31 GB box, and
adding the clustering sidecar's 3 GiB takes it to 26.4 GiB. That is workable, it is
what the reference host does, and it is also why there is very little room left to
raise `CLICKHOUSE_MEM_LIMIT` on a co-located box. A separate node host is what buys
that headroom.

One consequence of the bundled services is easy to miss: `kupo` reads the node's
Unix socket directly through the shared `node-ipc` volume, passes
`--node-socket /ipc/node.socket`, and declares `depends_on: cardano-node`, so it
cannot be pointed at an external node. If you want Kupo, run it on the node host
alongside the node and set `KUPO_URL` to that host. See
[Kupo is optional and only for backfill](#kupo-is-optional-and-only-for-backfill)
below.

`mailpit` is an SMTP catch-all that captures every message and forwards none.
Magic-link emails are login credentials, so a mainnet deployment must point `SMTP_*`
at a real relay and must never pass `--profile mail`.

### If you bring your own ClickHouse

Nothing in the application's startup path issues `CREATE DATABASE`. The application
connects with `database=CLICKHOUSE_DB` (`backend/app/db/clickhouse.py`) and then runs
`CREATE TABLE IF NOT EXISTS` DDL, so a ClickHouse server that does not already have
the target database fails at the first statement and the container exits. With the
bundled `clickhouse` service this never comes up, because the official image creates
`${CLICKHOUSE_DB}` when it initialises its data directory. On a managed service, a
shared cluster, or any pre-existing server, create the database by hand before the
first boot:

```sql
CREATE DATABASE IF NOT EXISTS tms_analytics;
```

The clustering sidecar is different and does bootstrap itself: its container command
runs `python -m app.cli migrate`, which opens a second connection pinned to the
always present `default` database and issues `CREATE DATABASE IF NOT EXISTS
tms_clustering` before applying its init SQL. So `tms_clustering` needs no manual
step, provided the configured ClickHouse user is allowed to create databases. Grant
that or create the database in advance.

## Sizing: What Fifteen Days of Mainnet Actually Cost

The reference host is 8 vCPU, 31 GB RAM and a 387 GB SSD, running the node, Ogmios,
and the full TMS stack together. At the time of measurement it was 69% full and
about 20 of 31 GB of RAM were in use.

Over the 15.17-day window it ingested 358,852 transactions across 53,566 blocks
(heights 13,681,362 to 13,745,042, maximum slot 193,862,916). That is roughly 23,650
transactions and 3,530 blocks per day, about 6.7 transactions per block. The height
range spans 63,680 blocks, so the difference is blocks that carried no transaction
to record: `block_height` exists only on transaction rows, so a block count derived
from them counts only blocks with content. Scoring kept up: the unscored backlog was
9 transactions out of 358,048, and the last ingested block trailed the node tip by
six blocks.

Risk bands over the same period, which is the number that matters for alert routing:

| Band | Count | Share |
|---|---|---|
| Informational | 341,950 | 95.3% |
| Moderate | 16,772 | 4.7% |
| High | 32 | 0.009% |
| Critical | 106 | 0.030% |

Those four counts sum to 358,860 against the 358,852 transaction total, because the
two figures were sampled moments apart while ingestion continued. Shares are computed
against the band total.

High and Critical together are 138 findings in 15.17 days, about nine per day.
Moderate is about 1,100 per day. Keep those two rates in mind when you configure
alerting.

### Measured storage rates

| Store | Size at 15.14 days | Rate |
|---|---|---|
| ClickHouse `tms_analytics` | 939.83 MiB | 62.1 MiB/day |
| ClickHouse `tms_clustering` | 82.18 MiB | 5.4 MiB/day |
| Raw store, apparent bytes | 900 MiB | 59.4 MiB/day |
| Raw store, disk blocks consumed | 2.9 GiB | 196 MiB/day |
| Raw store, file count | 699,562 files | 46,206 files/day |
| PostgreSQL database | 187 MB | 12.3 MB/day |
| PostgreSQL volume including WAL | 303.8 MB | not a growth rate, see below |
| ClickHouse's own system logs, uncapped | about 30 GiB | about 2.0 GiB/day |

The Postgres volume is larger than the database because of write-ahead log segments.
WAL is bounded by checkpoint settings rather than by data volume, so treat the 117 MB
difference as roughly fixed overhead and project only the database.

The system-log row is the one figure here that is derived rather than read off a
single query, so its provenance matters. The ClickHouse volume measured 32 GiB
total against 939.83 MiB of `tms_analytics` and 82.18 MiB of `tms_clustering`,
which leaves about 30 GiB of `system.*` log tables, and `system.text_log` alone
accounted for 14.2 GiB of that across 380 million rows. The daily rate is that
30 GiB over the same 15.14 days. Re-measure it directly before sizing a disk
around it, with the `system.parts` query in
[RUNBOOK.md](../RUNBOOK.md#clickhouse-disk-use-its-own-logs-not-your-data), and
note that the log tables are about 30x the detection data, not a fraction of it.

### One-year projection, arithmetic extrapolation

The table below is not a measurement. It is the measured daily rate multiplied by 30
and by 365, and it assumes mainnet transaction volume stays flat and that you set no
retention. Both assumptions are wrong in detail and the arithmetic is still the right
way to choose a disk size.

| Store | 30 days | 365 days |
|---|---|---|
| ClickHouse `tms_analytics` | 1.8 GiB | 22.1 GiB |
| ClickHouse `tms_clustering` | 0.16 GiB | 1.9 GiB |
| Raw store, disk blocks | 5.7 GiB | 69.9 GiB |
| Raw store, files | 1.39 million | 16.9 million |
| PostgreSQL database | 370 MB | 4.5 GB |
| ClickHouse system logs, uncapped | 59 GiB | 723 GiB |
| ClickHouse system logs, with the shipped 7-day TTL | about 14 GiB steady state | about 14 GiB steady state |

Excluding the node, that is about 112 GiB of TMS data in year one. The node's own
database was 220 GB and growing at the time of measurement; only one sample was taken,
so its growth rate is not known here. Size the node's disk from the node's own
documentation and give it its own volume.

### Three sizing traps that the totals hide

**The raw store costs blocks and inodes, not bytes.** Each transaction produces a
gzipped JSON blob averaging about 1.3 KiB apparent, and a 4 KiB filesystem block
rounds that up, which is where the 3.3x gap between 900 MiB apparent and 2.9 GiB on
disk comes from. More importantly, at 46,206 files per day the store consumes about
17 million inodes per year. Nothing else in this system consumes inodes at that rate
and no other document records the number. Two files per transaction is expected: the
mempool monitor writes one on first observation and chain-sync writes another on
confirmation, into
`{RAW_STORE_PATH}/{confirmed|mempool}/{network}/{YYYYMMDD}/{tx_hash[:2]}/`. If your
filesystem was created with a low inode ratio, check `df -i`, not just `df`, before
you decide the disk is big enough.

**The ClickHouse memory limit has less headroom than it looks.** The container
reached 3.027 GiB against its 4 GiB default after 15 days. The consequence is already
recorded in [RUNBOOK.md](../RUNBOOK.md#clickhouse-disk-use-its-own-logs-not-your-data):
a `MODIFY TTL` on a 14 GB system log table exceeded that limit mid-mutation and failed
with `Code: 241`. Routine ingestion fits comfortably; maintenance operations that
rewrite parts do not. Raise `CLICKHOUSE_MEM_LIMIT` above the 4 GB default if the host
has RAM to spare, remembering the co-location arithmetic above (on the reference host
about 11 GB was free, but the node is the thing most likely to claim it), and when you
do have to alter a large table, `TRUNCATE` first and `MODIFY TTL` second, in that
order.

**Build-from-source deployments accumulate BuildKit cache.** There is no published
registry image for this application, so every upgrade is a local `docker compose build`.
The reference host's BuildKit cache reached 15.52 GB. It is invisible in `docker system
df` output that only counts images, and it is reclaimed with `docker builder prune`.
Put that in your disk-space runbook.

## The Cardano Node: Versions, Configuration and Bootstrap

### The version matrix, and why the two versions move together

| Component | Version | Why this version |
|---|---|---|
| cardano-node | 11.0.1 | Minimum for the van Rossem PV11 intra-era hard fork. Nodes on 8.x, 9.x or 10.x sync to the PV11 boundary and stall there. |
| Ogmios | v6.14.0 | The parser is written against the Ogmios v6 JSON-RPC schema. v6.14.0 is the newest v6 release as of the node 11.0.1 pin. |

These are recorded in [RUNBOOK.md](../RUNBOOK.md#1-cardano-node--ogmios-required),
in `docker-compose.yml`, and in ADR-004 of
[TECHNOLOGY-DECISIONS.md](TECHNOLOGY-DECISIONS.md#adr-004-ogmios-as-cardano-node-bridge).

The rule that matters operationally is that node and Ogmios move together and are
smoke-tested together. Ogmios v7 changes the message envelope shape, so it is not a
drop-in upgrade: `backend/app/ingestion/ogmios_parser.py` and
`backend/app/ingestion/ogmios_client.py` are written against v6 and would need
revalidating. ADR-004 gives the three-protocol smoke test to run after any bump:
`nextBlock` parses, `nextTransaction` returns the expected fields, and
`queryLedgerState/utxo` with `outputReferences` returns `address` and
`value.lovelace` for a known unspent output.

### Download the whole configuration directory

The mainnet configuration set is published at
`https://book.world.dev.cardano.org/environments/mainnet/`. Mirror the entire
directory rather than picking files out of it. Eight files are load-bearing for a
node that Ogmios can talk to:

`config.json` and `topology.json` are what you pass on the command line.
`config.json` names five companions **and pins each by hash**, and `topology.json`
names a sixth by filename:

| Key | File | Hash |
|---|---|---|
| `ByronGenesisFile` / `ByronGenesisHash` | `byron-genesis.json` | `5f20df93...` |
| `ShelleyGenesisFile` / `ShelleyGenesisHash` | `shelley-genesis.json` | `1a3be38b...` |
| `AlonzoGenesisFile` / `AlonzoGenesisHash` | `alonzo-genesis.json` | `7e94a15f...` |
| `ConwayGenesisFile` / `ConwayGenesisHash` | `conway-genesis.json` | `15a199f8...` |
| `CheckpointsFile` / `CheckpointsFileHash` | `checkpoints.json` | `3e6dee5b...` |
| `peerSnapshotFile` (in `topology.json`) | `peer-snapshot.json` | not hashed |

`checkpoints.json` and `peer-snapshot.json` are easy to miss, because a node
configuration set is usually described as `config.json`, `topology.json` and the four
genesis files. `README.md` and `RUNBOOK.md` both name all of them; the table above is
the authoritative list for a mainnet deployment.

A missing or edited hashed file fails the check at node startup, and the resulting
error names a hash rather than the mistake, so it reads as configuration corruption
rather than a missing download. Copy the directory verbatim, do not reformat the JSON,
and do not hand-edit anything except `topology.json` if you have a specific peering
requirement.

Mount the directory read-only and point the node at `/config/config.json` and
`/config/topology.json`. If you use the bundled `ingestion` profile for a testnet,
`CARDANO_CONFIG_DIR` selects the directory and defaults to `./cardano-config/preprod`.

### Sync strategy: bootstrap with Mithril, do not sync from genesis

A mainnet genesis sync takes days of continuous block replay and validation. The
reference deployment did not do that; it fast-bootstrapped with
[Mithril](https://mithril.network), which downloads a certified snapshot of the node
database and verifies it against a stake-based multi-signature. Mithril appears
nowhere in this repository, so the sequence is given here in full.

Mainnet parameters, verified against mithril.network on 2026-07-30:

| Parameter | Value |
|---|---|
| Mithril network | `release-mainnet` |
| Aggregator endpoint | `https://aggregator.release-mainnet.api.mithril.network/aggregator` |
| Genesis verification key | `https://raw.githubusercontent.com/IntersectMBO/mithril/main/mithril-infra/configuration/release-mainnet/genesis.vkey` |
| Ancillary verification key | `https://raw.githubusercontent.com/IntersectMBO/mithril/main/mithril-infra/configuration/release-mainnet/ancillary.vkey` |
| Client image | `ghcr.io/intersectmbo/mithril-client` |

All three values are passed to the client as environment variables, which is how the
Mithril documentation drives it and which avoids depending on flag names that have
moved between releases:

```bash
export AGGREGATOR_ENDPOINT=https://aggregator.release-mainnet.api.mithril.network/aggregator
export GENESIS_VERIFICATION_KEY=$(wget -q -O - \
  https://raw.githubusercontent.com/IntersectMBO/mithril/main/mithril-infra/configuration/release-mainnet/genesis.vkey)
export ANCILLARY_VERIFICATION_KEY=$(wget -q -O - \
  https://raw.githubusercontent.com/IntersectMBO/mithril/main/mithril-infra/configuration/release-mainnet/ancillary.vkey)

# 1. See what is available and how large it is.
docker run --rm -e AGGREGATOR_ENDPOINT -e GENESIS_VERIFICATION_KEY \
  ghcr.io/intersectmbo/mithril-client:latest cardano-db snapshot list

# 2. Download and verify the latest snapshot into the directory that will
#    become the node's --database-path. --include-ancillary also restores the
#    ledger state, which is what saves the hours the node would otherwise
#    spend recomputing it. "latest" is accepted in place of a digest.
docker run --rm -v /opt/cardano/db:/db -w /db \
  -e AGGREGATOR_ENDPOINT -e GENESIS_VERIFICATION_KEY -e ANCILLARY_VERIFICATION_KEY \
  ghcr.io/intersectmbo/mithril-client:latest \
  cardano-db download --include-ancillary latest

# 3. Start the node against the restored database, then Ogmios against the
#    node's socket. Only after both are up do you start TMS.
```

Two things to check before you run it. The client's flag set has changed across
Mithril releases, so confirm `cardano-db download --help` for the image tag you
actually pull. And Mithril's network-configurations page lists the Cardano node
versions the current `release-mainnet` snapshots are produced against, which at the
time of writing were 10.6 and 10.7 rather than the 11.0.1 this stack pins. The
immutable chunks are node-version independent; the ancillary ledger state is the part
that is version sensitive, and a node that cannot load it falls back to recomputing
it from the chunks, which costs time rather than correctness. Everything else in the
sequence is stable: verify, restore, start the node, start Ogmios, then and only then
start TMS.

### Do not start TMS until Ogmios reports full synchronisation

```bash
curl -s http://<node-host>:1337/health
```

Wait for `"networkSynchronization": 1.0` and confirm `currentEra` and `lastKnownTip`
look sane. On the reference deployment this read `1.00000`, era `conway`, epoch 646.

The reason this matters is not that a partial sync breaks anything loudly. It is that
it fails quietly. On its first run TMS has no saved checkpoint, so it issues
`findIntersection` at the node's **current** tip and streams forward from there
(`backend/app/ingestion/ogmios_client.py`, `_chain_sync_loop`). Against a node that is
still catching up, that point is wherever the node happens to have reached, and it is
written to the Postgres `sync_checkpoint` as the permanent start of your history.
Worse, `sync_lag_slots` in `/health/detail` is computed against the tip the node itself
reports, so a TMS attached to a node that is days behind the real chain will report a
lag of a few slots and look perfectly healthy.

The health words come from a small state machine in the same file: `OK` means a block
arrived within `PIPELINE_BLOCK_AGE_DEGRADED_SECONDS` (120 s), `DEGRADED` means the last
block is between 120 and 300 s old or the circuit breaker is half-open, and `DOWN` means
the last block is older than `PIPELINE_BLOCK_AGE_DOWN_SECONDS` (300 s) or the breaker is
open. A pipeline that has never seen a block reports `OK` for the first
`PIPELINE_STARTUP_GRACE_SECONDS` (60 s) and `DEGRADED` after that; it does not escalate
to `DOWN` on that path, so a persistent `DEGRADED` with no blocks at all means Ogmios is
reachable but nothing is arriving, while `DOWN` means the connection itself is failing.

### Kupo is optional and only for backfill

TMS syncs forward from wherever it starts. It has no way to find an address's older
transactions, because neither the node nor Ogmios offers an address index. Kupo
provides one, and it backs exactly one feature: `POST /api/v1/backfill`, the on-demand
historical import of a single address. With `KUPO_URL` empty, which is the default,
that endpoint returns 503 (`backend/app/api/backfill.py`) and nothing else changes.

If you want it, run Kupo on the node host, since it reads the node socket directly,
and set `KUPO_URL` to point at it. The one-time index cost is governed by `--since`:
the Compose default is `origin`, which on mainnet means indexing the entire chain. The
start point is fixed once the database is built, so going deeper later means wiping the
volume and re-indexing. The reference deployment runs no Kupo, so **the disk and time
cost of a mainnet Kupo index is unmeasured here**. Treat `--since origin` on mainnet as
a decision to research separately, not a default to accept.

## Building Your `.env`: A Checklist That Boots First Time

Configuration is layered across a shared `.env` and a per-network `.env.<TMS_ENV>`,
and under Docker Compose that layering reaches fewer settings than it appears to.
[RUNBOOK.md](../RUNBOOK.md#first-time-setup) states the rule in full and this section
does not repeat it. The short version: anything listed under the `app` service's
`environment:` block in `docker-compose.yml` resolves from the shell or the top-level
`.env` **only**, and silently overrides whatever a per-network file said. That block
covers the `POSTGRES_*` and `CLICKHOUSE_*` connection settings, `CORS_ALLOW_ORIGINS`,
`TRUSTED_PROXY_ENABLED` and the two header settings, `CLUSTERING_*`, the five pinned
`SMTP_*` settings, `RAW_STORE_PATH`, `RAW_STORE_ENABLED` and the container-side
`API_PORT`. The prefixes are shorthand, not exhaustive: `TRUSTED_PROXY_CIDRS` and the
`CLICKHOUSE_*` timeout and insert-tuning settings are NOT pinned and stay per-network
settable. When it matters, check the block in `docker-compose.yml` for the exact name
rather than assuming a prefix is covered.

`KUPO_URL` and `APP_BASE_URL` are the two settings that were pinned there once and
were deliberately unpinned, and the compose file says so in place. Everything the
block does not name still comes from the layered files, including `CARDANO_NETWORK`
and `OGMIOS_WS_URL`, which is why the per-network templates set exactly those.

Work through the following in order.

**1. Create the two files.**

```bash
cp .env.example .env
cp .env.mainnet.example .env.mainnet
```

**2. Select the network in three places.** `TMS_ENV=mainnet` in `.env` so Compose
resolves `env_file: .env.${TMS_ENV}`; `export TMS_ENV=mainnet` in the deploy shell
profile so host-side tools (`run.py`, `app.cli`, `scripts/reset.sh`) read it from the
process environment before any dotenv file loads; and `CARDANO_NETWORK=mainnet` in
`.env.mainnet`. The `TMS_ENV` default is `preprod`, so a mainnet host that also
carries a `.env.preprod` will silently monitor preprod if you skip this.

**3. Set `VITE_NETWORK=mainnet` in `.env`, and only in `.env`.** This one is not a
runtime setting. `docker-compose.yml` passes it as a build argument to
`backend/Dockerfile`, whose first stage compiles the React bundle with it baked in,
and `frontend/src/lib/api/fetch.ts` exposes it as `getNetwork()`, which the API
clients put in the `network=` query parameter on every data call the dashboard makes.
Three consequences follow. It must match `CARDANO_NETWORK`. It can only live in the
top-level `.env`, because Compose build arguments are interpolations. And changing it
requires `docker compose build app`, not a restart. Get it wrong and the dashboard is
uniformly empty against a perfectly healthy backend: every query asks for
`network=preprod`, the backend filters `WHERE network = 'preprod'`, and every endpoint
returns an empty list with HTTP 200 and no error anywhere.

**4. Set the credentials.** Generate real values for all four, for example with
`openssl rand -hex 32`:

```bash
API_KEYS=<comma-separated generated keys>
POSTGRES_PASSWORD=<generated>
CLICKHOUSE_PASSWORD=<generated>
WEBHOOK_SIGNING_SECRET=<generated>
```

Set `CLICKHOUSE_PASSWORD` before the first `docker compose up` so the app and the
server agree from the start. If you have already booted the stack without it, this is
a small problem, not a data-loss one: the official image re-applies the variable on
every container start (it writes `users.d/default-user.xml` from it), so
`docker compose up -d clickhouse` recreates the container with the password in place
and every existing table survives. Do **not** recreate the ClickHouse volume for this.
That destroys `tms_analytics` and `tms_clustering`, and it is not necessary.

Every `clickhouse-client` invocation in this document and in the RUNBOOK resolves its
credentials inside the container, from the environment Compose already populated, which
works with or without a password and keeps the secret out of `ps` on both sides:

```bash
docker exec -it tms-clickhouse sh -c \
  'exec clickhouse-client --user "${CLICKHOUSE_USER:-default}"'
```

Do not "improve" that by adding `-e CLICKHOUSE_PASSWORD`. For a variable that is unset
in your own shell, `docker exec -e VAR` removes it from the exec'd process rather than
forwarding anything, which masks the value the container already has and produces
`Code: 516 Authentication failed` against a perfectly healthy server.

**5. Set the browser-facing values.** `CORS_ALLOW_ORIGINS` must be the explicit
dashboard origin and must be in `.env` (it is pinned in the Compose `environment:`
block, so a per-network value is blanked). `APP_BASE_URL` is the public dashboard URL
baked into emailed magic links; it belongs in `.env.mainnet` and may also be set in
`.env`.

**6. Set the proxy trust to match your edge.** `TRUSTED_PROXY_ENABLED` ships as
`true` in `.env.example` and Compose interpolates the same default, even though the
code default is `false`, so the CIDR list is live by default under Compose. Narrow
`TRUSTED_PROXY_CIDRS` from the broad loopback plus RFC1918 default to the actual proxy
address, and set `TRUSTED_PROXY_CLIENT_IP_HEADER=CF-Connecting-IP` **only** if
Cloudflare fronts the app. Behind any other proxy that does not strip that header, a
client can forge it.

**7. Point SMTP at a real relay.** `SMTP_HOST` defaults to `mailpit` in Compose. In
production that means either sign-in links pile up in an unauthenticated inbox on your
host, or, more likely, sends fail silently and nobody can log in.

**8. Add the alerting variables**, which no template contains. See
[Configure Alerting Before You Rely On It](#configure-alerting-before-you-rely-on-it).

### The six guards that refuse to start

`_validate_startup_settings` in `backend/app/main.py`, called from the FastAPI lifespan
before any service initialises, refuses to boot on six configurations. No `.env`
template warns about any of them, so they are collected here. They evaluate in the
order shown and the first failure raises, so a container that exits immediately after
`docker compose up` is almost always one of these; read `docker compose logs app`.

| # | Refuses when | Raised at | `TMS_ALLOW_DEV_MODE=1` escape |
|---|---|---|---|
| 1 | `API_KEYS` is empty (open API) | `main.py:148` | Yes |
| 2 | `CLICKHOUSE_PASSWORD` is empty | `main.py:161` | Yes |
| 3 | `POSTGRES_PASSWORD` equals the well-known dev default `tms_password` | `main.py:175` | Yes |
| 4 | `RAW_DATA_MAX_BYTES > 0` while `RAW_STORE_ENABLED` is false | `main.py:188` | **No** |
| 5 | `CORS_ALLOW_ORIGINS` is empty or contains `*` while API keys are configured | `main.py:198` | Yes |
| 6 | `TRUSTED_PROXY_CIDRS` is malformed while `TRUSTED_PROXY_ENABLED` is true | `main.py:212` | **No** |

Guards 1, 2, 3 and 5 exist so that an accidental production deploy from a blank
template fails loudly instead of running an open API against an unauthenticated
database on a guessable credential. `TMS_ALLOW_DEV_MODE=1` turns each of them into a
logged warning; it is read through pydantic, so it can live in `.env` as well as the
shell. Never set it on mainnet.

Guards 4 and 6 have no escape because neither is a security posture. Guard 4 protects
recall: capping the ClickHouse `raw_data` column while the raw store is off would leave
oversized transactions, which are exactly the attack-shaped ones, with no full payload
copy anywhere, so they could never be scored at full fidelity. Guard 6 protects
correctness: `TRUSTED_PROXY_CIDRS` is re-parsed on every request and a malformed entry
would degrade to untrusted-peer silently, disabling proxy trust and quietly corrupting
rate-limit buckets and audit IPs.

A seventh refusal lives elsewhere and only bites on an upgrade: the ClickHouse schema
guard that demands `backend/scripts/migrate_dedup_schema.py` by name when it finds a
pre-ReplacingMergeTree layout. See [Upgrade and rollback](#upgrade-and-rollback).

### A minimal working mainnet pair

`.env`:

```bash
TMS_ENV=mainnet
VITE_NETWORK=mainnet

API_KEYS=<generated>
POSTGRES_USER=tms_user
POSTGRES_PASSWORD=<generated>
POSTGRES_DB=tms_db
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=<generated>
CLICKHOUSE_DB=tms_analytics

CORS_ALLOW_ORIGINS=https://tms.example.com

TRUSTED_PROXY_ENABLED=true
TRUSTED_PROXY_HOPS=1
TRUSTED_PROXY_CIDRS=172.18.0.1/32
TRUSTED_PROXY_CLIENT_IP_HEADER=

SMTP_ENABLED=true
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=<user>
SMTP_PASSWORD=<password>
SMTP_USE_STARTTLS=true
SMTP_FROM_EMAIL=tms@example.com
SMTP_FROM_NAME=TMS

EMAIL_NOTIFY_ENABLED=true
WEBHOOK_NOTIFY_ENABLED=true
WEBHOOK_SIGNING_SECRET=<generated>

# Only if the host has the RAM: see the co-location arithmetic above.
CLICKHOUSE_MEM_LIMIT=8g
LOG_LEVEL=INFO
LOG_FORMAT=json
```

`.env.mainnet`:

```bash
CARDANO_NETWORK=mainnet
OGMIOS_WS_URL=ws://<node-host>:1337
# Host runs only. Under Compose the container bind is pinned to 8000 and the
# published port comes from ${API_PORT} in .env.
API_PORT=8000
KUPO_URL=
APP_BASE_URL=https://tms.example.com
FAKE_TOKEN_TESTNET_MODE=false
```

`POSTGRES_HOST`, `POSTGRES_PORT`, `CLICKHOUSE_HOST` and the ClickHouse ports in
`.env.example` matter only for a host run of the application; under Compose the app
service pins them to the in-network service names.

## First Start and Verification

```bash
docker compose --profile app up -d
docker compose ps
docker compose logs -f app
```

Add `--profile clustering` and `CLUSTERING_ENABLED=true` if you want the tenth,
unsupervised attack class. See [docs/CLUSTERING.md](CLUSTERING.md).

Against a node already at the tip the first block usually arrives within seconds, and
because a pipeline that has seen no block still reports `OK` for the first 60 seconds,
you will normally never see anything but `OK`. A `DEGRADED` that appears after that
grace period and persists means no block has arrived: the node is not at the tip, or
`OGMIOS_WS_URL` is wrong, or a firewall is in the way.

### Health

```bash
# Liveness, unauthenticated. Should be immediate.
curl -s http://localhost:8000/health
# {"status":"healthy"}

# Readiness, unauthenticated. This is the endpoint a load balancer should gate on:
# it returns 503 while the ingestion pipeline is DOWN.
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/health/ready
# 200

# Full operational state, requires an API key.
curl -s -H "X-API-Key: $TMS_API_KEY" http://localhost:8000/health/detail
```

`/health/detail` should show `"network": "mainnet"`, `"pipeline_state": "OK"`, both
circuit breakers `CLOSED` under `ogmios`, and a small `sync_lag_slots`. One slot is one
second on mainnet, so `sync_lag_seconds` reads directly as how far behind the node's
reported tip you are. Read it together with Ogmios's own `networkSynchronization`,
because the lag is measured against the node, not against the chain.

### Create the first admin

A fresh install has no users, and the dashboard's user management needs an existing
Admin to invite anyone, so the first one comes from the CLI. It is idempotent, and
`--no-email` prints the magic link to stdout, which is what you want before SMTP is
proven:

```bash
docker compose exec app python -m app.cli create-admin you@example.com "Your Name" --no-email
```

Set `APP_BASE_URL` before running it: the printed link is built from that value, and
the `http://localhost:8000` default produces links that resolve from nowhere else.
Open the link in a browser to activate the account. Every further user is invited from
the dashboard; there are no passwords in the flow. See
[RUNBOOK.md](../RUNBOOK.md#first-admin-user-bootstrap) for the variant that works
before the app container is up.

### Prove that rows are landing and being scored

Ingestion working and detection working are two different claims, and a deployment can
satisfy the first while silently failing the second. Check both.

```bash
docker exec -it tms-clickhouse sh -c \
  'exec clickhouse-client --user "${CLICKHOUSE_USER:-default}"'
```

```sql
-- Rows are landing.
SELECT count() AS ingested,
       max(block_height) AS tip_block,
       max(slot) AS tip_slot
FROM tms_analytics.transactions
WHERE network = 'mainnet';

-- Rows are being scored. This mirrors the shape of the LEFT ANTI JOIN the analysis
-- engine polls on (backend/app/db/clickhouse_scores.py, get_unanalyzed_transactions);
-- the engine's own version additionally bounds both sides by its watermark and
-- defers a tx until its transaction_inputs rows are visible, so this query is the
-- slightly more pessimistic of the two.
SELECT count() AS unscored
FROM tms_analytics.transactions t
LEFT ANTI JOIN (
    SELECT tx_hash, network
    FROM tms_analytics.tx_class_scores
    WHERE network = 'mainnet'
) s ON t.tx_hash = s.tx_hash AND t.network = s.network
WHERE t.network = 'mainnet';

-- Scores are distributed as expected, not all in one band.
SELECT risk_band, count() AS n
FROM tms_analytics.tx_class_scores FINAL
WHERE network = 'mainnet'
GROUP BY risk_band;
```

The healthy reference: on the mainnet deployment the anti-join returned **9 unscored
out of 358,048**, which is ordinary tip lag, transactions ingested in the last few
seconds that the engine has not yet reached on its 30-second poll
(`ANALYSIS_ENGINE_INTERVAL_SECONDS`). A number in the hundreds that does not fall back
to single digits within a minute or two means the engine is behind or wedged. A number
that grows monotonically means scoring has stopped, which is a detection outage even
though ingestion looks perfectly healthy. The fact tables are `ReplacingMergeTree`, so
counts without `FINAL` can include not-yet-merged duplicate versions; that is why the
band query uses `FINAL` and the anti-join, which mirrors the engine, does not.

## Configure Alerting Before You Rely On It

This is a first-boot step, not a day-two step. Detection that nobody is told about is
not monitoring, and the gap described below means a deployment built from the shipped
templates will run for as long as you let it, scoring correctly, telling nobody.

[docs/ALERTING.md](ALERTING.md) is the reference for channels, the band by
attack-class trigger matrix, recipients, deduplication and the periodic report. Most
of that lives in a JSON document in Postgres (`notification_config`), edited through
the admin UI or `PUT /api/v1/notifications/config`, hot-reloaded without a restart.
What follows is only the environment-variable half.

The alerting variables live in the `── Alerting ──` block of `.env.example`, and
only there: `.env.mainnet.example` carries no alerting block, because none of these
settings differ by network by default. Copy the ones you need from `.env.example`
into whichever file you are using. Start from that block rather than appending your
own lines:
duplicated keys in one env file resolve to the last assignment, so a hand-added
`EMAIL_NOTIFY_ENABLED` further down the file silently wins over the one in the shipped
block, and you end up maintaining two master switches for the same channel. Nothing in
`docker-compose.yml` names them, so they are per-network settable. Two consequences are
worth calling out explicitly:

`WEBHOOK_SIGNING_SECRET` defaults to the empty string, and an empty secret means
requests go out unsigned: no `X-TMS-Signature` header, and a receiver has no way to
distinguish a genuine alert from anything else that can reach its URL. Set it, and
verify it at the receiver with the constant-time comparison shown in
[RUNBOOK.md](../RUNBOOK.md#webhook-notifications-payload-and-signature-verification).

The channel master switches `EMAIL_NOTIFY_ENABLED` and `WEBHOOK_NOTIFY_ENABLED`
default to `true` in code, so the channels are armed, but with no recipients
configured in the notification document nothing is delivered. A channel fires only
when its environment switch and its stored `enabled` flag are both on, so either layer
can unplug it, and neither layer announces that it has.

One small piece of good news follows from the same absence: because these variables are
not pinned in the Compose `environment:` block, they can be set in either `.env` or
`.env.mainnet` and both will reach the container.

Use the reference deployment's rates to route sensibly. High and Critical together
were about nine findings per day, which is a reasonable immediate-email volume.
Moderate was about 1,100 per day, which is not: route Moderate to the periodic report
or a webhook, never to immediate email.

Treat that nine per day as a steady-state average, not a ceiling. A single contract
whose normal operation trips a detector produces one finding per state transition,
and on 2026-07-26 that meant 106 Critical findings from one script in 67 minutes.
`NOTIFY_GROUP_WINDOW_MINUTES` (default 60) is what bounds that: for the attack
classes where repeat findings at one script are a single situation, the delivery
path emits one alert per script per window instead of one per transaction. Leave it
at the default before pointing Critical at a pager. It cannot cost you a detection:
an escalation to a higher band breaks through immediately, the suppression expires
with the window, and every finding is recorded and visible in the dashboard whether
or not it was notified. Set it to `0` only if you want one notification per
transaction and have somewhere for that volume to go. See
[ALERTING.md](ALERTING.md#per-group-one-alert-per-script-per-window).

Verify before you move on. Send a test to whatever you configured and confirm it
arrives, rather than confirming that the configuration was accepted.
`backend/scripts/webhook_testing/` ships a reference receiver that implements the
signature check correctly, and the egress guard will refuse a webhook URL that
resolves to a loopback, private, link-local or cloud-metadata address unless
`WEBHOOK_ALLOW_INTERNAL=true`.

## Retention Decisions Before Day 30

Every retention knob except `NOTIFY_DEDUP_RETENTION_DAYS` defaults to `0`, and `0`
means keep forever. This is verified in `backend/app/config.py`:

| Variable | Default | What it prunes |
|---|---|---|
| `CH_RETENTION_DAYS_TRANSACTIONS` | `0` | ClickHouse `transactions` |
| `CH_RETENTION_DAYS_IO` | `0` | `transaction_inputs`, `transaction_outputs`, `address_transactions` |
| `CH_RETENTION_DAYS_FEATURES` | `0` | `utxo_features`, `tx_script_features` |
| `LIFECYCLE_RETENTION_DAYS` | `0` | Terminal (`DROPPED`, `ROLLED_BACK`) Postgres lifecycle rows only |
| `MEMPOOL_COLLISION_RETENTION_DAYS` | `0` | Mempool collision bookkeeping |
| `AUDIT_LOG_RETENTION_DAYS` | `0` | Audit rows |
| `RAW_STORE_RETENTION_DAYS` | `0` | Whole day-directories of the raw store |
| `NOTIFY_DEDUP_RETENTION_DAYS` | `30` | The alert deduplication ledger |
| `RETENTION_SWEEP_INTERVAL_HOURS` | `24` | How often the sweep runs |

`tx_class_scores`, `archived_alerts` and `baselines` are absent from that list by
design and are never expired: they are the product, they are O(1) per transaction, and
the archive is the false-positive accountability record.

The default is keep-forever because the audit that produced these knobs concluded that
growth should be an operator decision, not a silent expiry. That is the right default
and it is also the reason you must make the decision explicitly, before the disk makes
it for you.

### The recall cost of each knob

These are not interchangeable dials. Three of them degrade detection, not just history.

`CH_RETENTION_DAYS_TRANSACTIONS` is the most dangerous. Every baseline percentile
query inner-joins `transactions` for chain-time windowing, so feature rows older than
the window vanish from both the percentiles and the sample counts, and enrichment
resolves parent transactions of any age from this table because dormant-funds attacks
spend arbitrarily old UTxOs. The global baseline window in `config/detection.yaml` is
180 days (`baselines.windows.global_days`), and the application logs a warning at
startup if you set this below that. If you set it at all, keep it comfortably above
180.

`CH_RETENTION_DAYS_IO` breaks input resolution for old parent transactions, which
degrades exactly the enrichment the cross-transaction scorers depend on.

`CH_RETENTION_DAYS_FEATURES` truncates the population baselines are computed from.

`AUDIT_LOG_RETENTION_DAYS` costs no recall but costs accountability: these rows record
who suppressed which alert. Prefer archiving the table to shortening the window.

`UNANALYZED_FULL_RESCAN_WINDOW_SECONDS` is not a retention knob but belongs in the same
conversation. It bounds the periodic full rescan, which is the never-skip guarantee: the
incremental poll only looks back `UNANALYZED_OVERLAP_SECONDS` (120 s), so anything
unscored and older than the rescan window is never picked up again. If ingestion ever
outlives scoring, through a crash loop, a disabled engine, or a multi-day node resync,
every row unscored past that window is permanently unscored, which is a missed
detection. It defaults to `0`, meaning unbounded. Bound it only after measuring the
rescan's cost on your own deployment, and then pick a window that comfortably exceeds
the longest plausible scoring outage.

### The raw store: what it actually costs

Earlier revisions of `RUNBOOK.md` and `.env.mainnet.example` put the raw store at
"roughly 0.5-2 GB/day at mainnet volume". That figure was wrong and both have since been
corrected. Measured on mainnet over 15.14 days, the store consumed 196 MiB/day of disk
blocks and 59.4 MiB/day of apparent bytes, so the old range overstated the real cost by
between about 2.5x and 10x.

The practical consequence of the old figure was benign, since a deployment provisioned
against it has too much disk rather than too little. It mattered because it made the raw
store look like the dominant storage cost when it is not: in apparent bytes it is level
with the analytics warehouse (59.4 against 62.1 MiB/day), and even counting block
rounding it is roughly three times it, not the ten to forty times the old range implied.

The dimension genuinely worth planning for is the one byte figures miss entirely:
46,206 files per day, about 17 million inodes per year.

### `RAW_STORE_RETENTION_DAYS` and `RAW_DATA_MAX_BYTES` are mutually exclusive

Set one, never both. They are not two settings for the same thing, which is part of why
the interaction surprises people.

`RAW_STORE_RETENTION_DAYS` is age-based pruning of the raw store on disk: the layout
`{prefix}/{network}/{YYYYMMDD}/...` makes retention a directory walk that removes whole
days.

`RAW_DATA_MAX_BYTES` is not a store size budget at all, which its name invites you to
assume (the RUNBOOK's configuration reference says the same). It is the maximum byte
length of the
`raw_data` JSON stored **per transaction in ClickHouse** (`backend/app/config.py`,
`backend/app/db/clickhouse.py`, `_serialize_raw_data`). Above the cap, an empty string
is stored with `raw_data_truncated = 1`, never a sliced JSON prefix.

Setting `RAW_DATA_MAX_BYTES > 0` therefore makes the raw store the only full copy of
oversized transactions, and the analysis engine's raw-data fallback reads them back from
it before scoring. Two behaviours follow directly from that. First, the startup guard at
`main.py:188` refuses to boot with `RAW_DATA_MAX_BYTES > 0` and `RAW_STORE_ENABLED`
false, with no dev-mode escape. Second, the retention sweep refuses to prune the raw
store while `RAW_DATA_MAX_BYTES > 0`, logging a warning and returning zero
(`backend/app/db/raw_store.py`, `prune_old_days`). That second one is the trap: you set
both, believing you have belt and braces, and instead you get neither. The cap is
enforced, the pruning silently is not, and the disk keeps filling while a warning scrolls
past in the logs.

Oversized transactions are disproportionately the interesting ones, so the recall
consequence of getting this wrong is not evenly distributed. Pick age-based pruning of
the raw store and leave `RAW_DATA_MAX_BYTES` at `0`, unless you have a specific reason to
cap the ClickHouse column, in which case accept that the raw store grows without bound.

### ClickHouse's own logs are already handled, but check the mount

The largest thing in the ClickHouse volume on a long-running deployment is ClickHouse's
own telemetry, not your data: about 30 GiB of `system.*` log tables in 15 days, roughly
2.0 GiB/day, of which `system.text_log` alone was 14.2 GiB across 380 million rows and
`processors_profile_log` 7.19 GiB across 271.3 million rows (486 MiB/day). Against
62.1 MiB/day of actual detection data, that is roughly thirty to one.

`clickhouse/config.d/log-retention.xml` fixes this for new deployments by dropping
`text_log` to `information` and putting a 7-day TTL on the nine log tables this stack
populates, and by lowering the server's own log files from trace at 1000 MB x 10 to
information at 100 MB x 3. Confirm the file exists before the first `docker compose up`.
It is bind-mounted as a single file, so if the path is missing Docker creates an empty
**directory** at the config path. ClickHouse reads only `*.xml` files from `config.d/`,
so it does not fail on that directory: it starts normally with none of these limits
applied, and you find out weeks later when the disk fills. A `git pull` deploy gets the
file for free; a hand-copied deploy needs the check, and the check is worth running
because the failure is silent:

```bash
docker exec tms-clickhouse clickhouse-client -q \
  "SELECT name, value FROM system.server_settings WHERE name = 'logger.level' FORMAT TSV"
# information = applied. trace = the mount did not land.
```

Mounting the file does not reclaim anything on a server that has already accumulated
these tables: ClickHouse renames each mismatched table to `<name>_0` on restart and
starts a fresh empty one, so the old data survives under the renamed table with no TTL.
The full procedure, including the `DROP TABLE system.<name>_0` step and the in-place
alternative where `TRUNCATE` must precede `MODIFY TTL`, is in
[RUNBOOK.md](../RUNBOOK.md#clickhouse-disk-use-its-own-logs-not-your-data).

## Day Two: Backup, Upgrade, Hard Forks

### Backup and restore

`./scripts/backup.sh [output-dir]` runs against the live containers and produces
`postgres.sql.gz` (a full `pg_dump` of the operational database: lifecycle, sync
checkpoints, collisions, audit logs, entity state, and the auth tables),
`clickhouse/<table>.native.gz` for ten tables including `tx_class_scores` and
`archived_alerts`, and a `MANIFEST` of row counts at backup time. Files are written
`umask 077` because the dumps contain the full operational database.

Two things it does not cover. The raw store is deliberately excluded, because it is
write-once files and an incremental `rsync` or `restic` against the `raw_store_data`
volume is the right tool. And the table list is scoped to `CLICKHOUSE_DB`, so if you run
the clustering sidecar, `tms_clustering` is not backed up by this script.

Be honest with yourself about the restore path. The procedure is documented in
[RUNBOOK.md](../RUNBOOK.md#backup--restore), and it is sound in principle: start empty
databases, let the app create schemas, stop the app, load the Postgres dump, load each
ClickHouse table with `INSERT ... FORMAT Native`, restore the raw-store files, start the
app, and let ingestion resume from the checkpoint in the dump and replay the gap. But
**no test in this repository and no CI job exercises `backup.sh` or the restore, and no
restore rehearsal is recorded**. Treat your first restore as a rehearsal, do it on a
scratch host before you need it, and time it.

### Upgrade and rollback

There are no image tags to roll back to: no registry image is published, the `app`
service is `build: context: .`, and `docker compose pull app` is a no-op (the RUNBOOK's
upgrade section says the same). The procedure is build-from-source, and the version you
roll back to is a git commit, so record it before you upgrade:

```bash
# BEFORE upgrading: record what is currently deployed and back up.
git rev-parse HEAD > /opt/tms/DEPLOYED_SHA
./scripts/backup.sh

# Upgrade.
git pull
docker compose build app                      # also rebuilds the embedded dashboard
docker compose --profile app up -d app        # databases keep running

# Verify: pipeline_state returns to OK once it replays the gap from the checkpoint.
curl -s -H "X-API-Key: $TMS_API_KEY" localhost:8000/health/detail

# Roll back.
git checkout "$(cat /opt/tms/DEPLOYED_SHA)"
docker compose build app
docker compose --profile app up -d app
```

Schema changes are additive and applied idempotently at startup, so a routine upgrade is
build-and-restart and a rollback to the immediately preceding commit is safe. There are
two exceptions, and only one of them announces itself. The one-shot dedup migration is
demanded by the startup guard by name if it is needed and is not backward compatible;
keep the `<table>__legacy_<date>` tables until the new version has proven healthy. A
post-upgrade **backfill** is the quiet one: a release that adds a column derived from data
already on the row leaves that column empty for all pre-upgrade history, and nothing
fails or warns, so the only symptom is a dashboard feature that looks broken for
everything older than the deploy. Check RUNBOOK.md's "Post-upgrade backfills" table
before calling a mainnet upgrade done, and run what it lists (they are dry-run by default
and safe against a live instance). If you rebuilt the frontend with a different
`VITE_NETWORK`, remember that a rollback must rebuild too, not just restart. Note that
the rollback leaves the checkout on a detached HEAD, so return to the branch before the
next `git pull`.

Remember `docker builder prune` after a few upgrade cycles.

### Hard forks

A hard fork is the one certain scheduled event on any Cardano deployment, and it is the
scenario most likely to take a working TMS down.

The symptom is distinctive. Chain-sync goes `DEGRADED` and then `DOWN`, `/health/ready`
starts returning 503, and the `app` container is marked unhealthy, while the node process
itself is perfectly healthy and its tip is frozen at an epoch boundary. A node too old
for the new protocol version does not crash; it stops following the chain. If you see a
frozen tip at an epoch boundary, check for a hard fork before you debug anything else.

The procedure:

1. Upgrade cardano-node and Ogmios **together**, to the pair named in the release notes.
   ADR-004 is explicit that these move as a unit, because the parser is bound to the
   Ogmios v6 schema.
2. Re-run the three-protocol smoke test from ADR-004 (`nextBlock`, `nextTransaction`,
   `queryLedgerState/utxo`) against the upgraded pair before promoting it.
3. Update the pins in `docker-compose.yml`, `RUNBOOK.md` and ADR-004 so the repository
   still describes reality.
4. Restart TMS.

You do not need to keep TMS running through the node upgrade, and there is no advantage
in trying. On restart it reads the last saved `sync_checkpoint` from Postgres and issues
`findIntersection` at that exact point, then streams forward from there, so the gap
replays in full (`backend/app/ingestion/ogmios_client.py`, `_chain_sync_loop`). The
checkpoint only advances after the block's ClickHouse insert succeeds, and every fact
table is a `ReplacingMergeTree`, so a replay that re-delivers blocks already stored
collapses to one row per key rather than double-counting. Era handling is refreshed on
each new session and again whenever a block arrives beyond the current forecast horizon,
which is exactly what a fork produces, so the slot-to-time conversion picks up the new era
without intervention.

Two caveats. The resume depends on the saved point still being on the node's chain: if it
is not, chain-sync raises `IntersectionNotFoundError` rather than silently re-syncing from
genesis. Note the failure mode, because it is not a crash: chain-sync is a supervised
background task, so the container starts and stays up, the API keeps serving reads, and
the only signals are the circuit breaker tripping, `/health/ready` returning 503, and the
`IntersectionNotFoundError` in the log. A permanently non-ingesting instance looks healthy
to anything that only checks whether the container is running. In practice that means do
not point a mainnet TMS at another network's node, and do not wipe Postgres while keeping
ClickHouse. A node
rebuilt from a Mithril snapshot is fine, because the snapshot carries the same immutable
chain and the saved point is still on it.

## Where to Go Next

| Topic | Document |
|---|---|
| Full configuration reference, troubleshooting, daily operations | [RUNBOOK.md](../RUNBOOK.md) |
| Alert channels, routing matrix, periodic report | [docs/ALERTING.md](ALERTING.md) |
| Which tree holds which subsystem | [docs/REPOSITORY-MAP.md](REPOSITORY-MAP.md) |
| System architecture and the three async background tasks | [docs/ARCHITECTURE.md](ARCHITECTURE.md) |
| Technology choices and the node/Ogmios version contract | [docs/TECHNOLOGY-DECISIONS.md](TECHNOLOGY-DECISIONS.md) |
| The nine attack classes, features and thresholds | [docs/TMS_DETECTION_SPEC.md](TMS_DETECTION_SPEC.md) |
| The optional clustering module | [docs/CLUSTERING.md](CLUSTERING.md) |
| Test tiers and how to run them | [docs/TESTING.md](TESTING.md) |
