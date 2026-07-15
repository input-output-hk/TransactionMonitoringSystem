"""Shared FastAPI query-parameter declarations for the API layer.

Single-sources parameter metadata (notably the optional ``?network`` selector's
description) that was otherwise hand-copied across every network-scoped endpoint,
so the OpenAPI docs and validation cannot drift between routes. Lives in ``api/``
rather than ``models/`` so the model layer stays free of a FastAPI dependency.
"""

from datetime import datetime
from typing import Annotated

from fastapi import Query

from app.models.transaction import NetworkType

# The optional ``?network`` selector shared by every network-scoped endpoint.
# Callers default it to None (``network: NetworkParam = None``) so the route falls
# back to the instance's CARDANO_NETWORK setting.
NetworkParam = Annotated[
    NetworkType | None,
    Query(
        description=(
            "Network to query: 'mainnet', 'preprod', or 'preview'. "
            "Defaults to the instance's CARDANO_NETWORK setting."
        )
    ),
]

# Pagination shared by every list endpoint. Callers default them
# (``limit: PageLimit = 100``, ``offset: PageOffset = 0``); the 1000 cap bounds
# a single response's memory/serialization cost while staying comfortably above
# every dashboard page size.
PageLimit = Annotated[int, Query(ge=1, le=1000, description="Page size (max 1000)")]
PageOffset = Annotated[int, Query(ge=0, description="Rows to skip before the first result")]

# Time-range filtering shared by every time-filtered endpoint. The wire names
# are ``from``/``to`` and the interval convention is HALF-OPEN: [from, to).
# ``from`` is inclusive and ``to`` is exclusive, so consecutive windows chain
# without double-counting the boundary instant. Naive timestamps are UTC.
TimeFromParam = Annotated[
    datetime | None,
    Query(alias="from", description="Inclusive lower bound, ISO 8601 (naive = UTC)"),
]
TimeToParam = Annotated[
    datetime | None,
    Query(alias="to", description="Exclusive upper bound, ISO 8601 (naive = UTC)"),
]
