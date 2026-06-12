"""Audit-log retention prune (capture-the-SQL).

audit_logs previously had no retention knob while every other growing
table did (review finding); the prune must key on created_at (indexed)
and stay opt-in.
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from app.db import postgres


class TestPruneAuditLogs:
    def test_prune_sql_uses_created_at_window(self, monkeypatch):
        conn = AsyncMock()
        conn.execute.return_value = "DELETE 7"

        @asynccontextmanager
        async def fake_connection():
            yield conn

        monkeypatch.setattr(postgres, "get_connection", fake_connection)
        deleted = asyncio.run(postgres.prune_audit_logs(30))
        assert deleted == 7
        sql = conn.execute.call_args.args[0]
        assert "DELETE FROM audit_logs" in sql
        assert "created_at < NOW() - ($1 * INTERVAL '1 day')" in sql
        assert conn.execute.call_args.args[1] == 30
