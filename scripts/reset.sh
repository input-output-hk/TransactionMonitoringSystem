#!/bin/bash
# Reset database state for one or all networks.
# Usage:
#   ./scripts/reset.sh                 # reset only the current TMS_ENV's network (safe)
#   ./scripts/reset.sh --network=X     # reset only network X
#   ./scripts/reset.sh --all           # reset every network (destructive)
#
# The script honours TMS_ENV so running it from a preview-configured terminal
# will not nuke preprod data.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

NETWORK_ARG=""
ALL=0
for arg in "$@"; do
    case "$arg" in
        --all) ALL=1 ;;
        --network=*) NETWORK_ARG="${arg#--network=}" ;;
        -h|--help)
            echo "Usage: $0 [--network=<name> | --all]"
            exit 0
            ;;
    esac
done

# Activate virtual environment
if [ -f venv/bin/activate ]; then
    source venv/bin/activate
fi

cd backend
ALL="$ALL" NETWORK_ARG="$NETWORK_ARG" python -c "
import asyncio
import os
import sys

import asyncpg

from app.db.clickhouse import _get_client, init_client
from app.config import settings

all_networks = os.environ.get('ALL') == '1'
override = os.environ.get('NETWORK_ARG') or ''

if all_networks:
    scope = None
    label = 'all networks'
else:
    scope = override or settings.CARDANO_NETWORK
    label = f'network={scope!r}'

print(f'Resetting {label}...')

# ClickHouse — scoped DELETEs (never TRUNCATE, unless --all).
init_client()
c = _get_client()
tables = ['tx_class_scores', 'tx_analysis_results', 'transactions',
          'transaction_inputs', 'transaction_outputs',
          'utxo_features', 'tx_script_features']
for t in tables:
    try:
        if scope is None:
            c.execute(f'TRUNCATE TABLE {t}')
            print(f'  Truncated ClickHouse: {t}')
        else:
            c.execute(
                f'ALTER TABLE {t} DELETE WHERE network = %(n)s',
                {'n': scope},
            )
            print(f'  Cleared ClickHouse {t} for {scope}')
    except Exception as e:
        print(f'  Skipped ClickHouse {t}: {e}')


async def reset_pg():
    conn = await asyncpg.connect(
        host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
        user=settings.POSTGRES_USER, password=settings.POSTGRES_PASSWORD,
        database=settings.POSTGRES_DB,
    )
    for table in ['tx_lifecycle', 'sync_checkpoint', 'mempool_collisions']:
        try:
            if scope is None:
                await conn.execute(f'DELETE FROM {table}')
                print(f'  Cleared PostgreSQL {table} (all networks)')
            else:
                await conn.execute(
                    f'DELETE FROM {table} WHERE network = \$1', scope,
                )
                print(f'  Cleared PostgreSQL {table} for {scope}')
        except Exception as e:
            print(f'  Skipped PostgreSQL {table}: {e}')
    await conn.close()

asyncio.run(reset_pg())
print(f'Done. Sync for {label} will resume from current tip.')
"
