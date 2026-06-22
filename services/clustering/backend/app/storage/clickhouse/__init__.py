"""ClickHouse access: connection, batched inserts and the queries the rest of
the app relies on (ingest cursor, feature extraction, cluster results).

``ClickHouseRepo`` is composed from per-entity mixins (one module each) over a
shared ``_RepoBase`` (client lifecycle + insert/row-mapping primitives). The
public surface is unchanged: import ``ClickHouseRepo`` and the ``*_COLUMNS``
constants from this package exactly as before.
"""

from __future__ import annotations

from .anomaly import _AnomalyMixin
from .base import _row_to_dict
from .clusters import _ClusterMixin
from .contracts import CONTRACT_COLUMNS, _ContractMixin
from .ingest import ASSET_COLUMNS, TX_COLUMNS, UTXO_COLUMNS, _IngestMixin
from .jobs import JOB_COLUMNS, _JobMixin
from .models import _ModelMixin


class ClickHouseRepo(
    _IngestMixin,
    _ClusterMixin,
    _AnomalyMixin,
    _ContractMixin,
    _JobMixin,
    _ModelMixin,
):
    """Thin repository over a ClickHouse HTTP client.

    Each mixin derives from ``_RepoBase`` (client lifecycle + insert/row-mapping
    primitives), so it sits once at the tail of the MRO here.
    """


def _drift_check() -> None:  # pragma: no cover - exists for mypy only
    """mypy fails here if ``ClickHouseRepo`` drifts from the ``Repo`` protocol
    the engine programs against (app/storage/protocol.py)."""
    from app.storage.protocol import Repo

    _: Repo = ClickHouseRepo()


__all__ = [
    "ASSET_COLUMNS",
    "CONTRACT_COLUMNS",
    "JOB_COLUMNS",
    "TX_COLUMNS",
    "UTXO_COLUMNS",
    "ClickHouseRepo",
    "_row_to_dict",
]
