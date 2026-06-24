"""ClickHouse access: connection, batched inserts and the queries the rest of
the app relies on (ingest cursor, feature extraction, cluster results).

``ClickHouseRepo`` is composed from per-entity mixins (one module each) over a
shared ``_RepoBase`` (client lifecycle + insert/row-mapping primitives). The
public surface is unchanged: import ``ClickHouseRepo`` and the ``*_COLUMNS``
constants from this package exactly as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .anomaly import _AnomalyMixin
from .base import _row_to_dict
from .clusters import _ClusterMixin
from .contracts import CONTRACT_COLUMNS, _ContractMixin
from .ingest import ASSET_COLUMNS, TX_COLUMNS, UTXO_COLUMNS, _IngestMixin
from .jobs import JOB_COLUMNS, _JobMixin
from .models import _ModelMixin

if TYPE_CHECKING:
    from app.config import Settings


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


def select_repo_factory(settings: Settings) -> type[ClickHouseRepo]:
    """The repository class for the active deployment.

    ``host_ch`` reads each watched contract's chain data from the host TMS's
    ClickHouse (``tms_analytics``) through ``HostBackedRepo``'s feature-read
    overrides; the module's own raw-tx tables stay empty. A downloading adapter
    ingests into the module's own DB and uses the base ``ClickHouseRepo``.

    The per-request API repo AND the job worker / feed scheduler must resolve to
    the SAME class through this one helper: if they diverge, the worker fits on
    host data while a read endpoint (the co-spend graph, the tx list) queries the
    empty module tables and fails (an empty result yields a column-less frame).
    """
    if settings.chain_source == "host_ch":
        from app.storage.clickhouse.host_backed import HostBackedRepo

        return HostBackedRepo
    return ClickHouseRepo


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
    "select_repo_factory",
]
