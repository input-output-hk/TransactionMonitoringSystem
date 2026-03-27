#!/bin/bash
# Reset all databases and sync checkpoint to start fresh from current chain tip.
# Usage: ./scripts/reset.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

# Activate virtual environment
if [ -f venv/bin/activate ]; then
    source venv/bin/activate
fi

echo "Resetting all data..."

cd backend
python -c "
import asyncio, asyncpg
from app.db.clickhouse import _get_client, init_client
from app.config import settings

# ClickHouse
init_client()
c = _get_client()
tables = ['tx_class_scores', 'tx_analysis_results', 'transactions']
for t in tables:
    try:
        c.execute(f'TRUNCATE TABLE {t}')
        print(f'  Cleared ClickHouse: {t}')
    except Exception as e:
        print(f'  Skipped ClickHouse {t}: {e}')

# PostgreSQL
async def reset_pg():
    conn = await asyncpg.connect(
        host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
        user=settings.POSTGRES_USER, password=settings.POSTGRES_PASSWORD,
        database=settings.POSTGRES_DB,
    )
    for table in ['tx_lifecycle', 'sync_checkpoint']:
        try:
            await conn.execute(f'DELETE FROM {table}')
            print(f'  Cleared PostgreSQL: {table}')
        except Exception as e:
            print(f'  Skipped PostgreSQL {table}: {e}')
    await conn.close()

asyncio.run(reset_pg())
print('Done. All data cleared, sync will start from current tip.')
"
