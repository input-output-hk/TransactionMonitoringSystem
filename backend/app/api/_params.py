"""Shared FastAPI query-parameter declarations for the API layer.

Single-sources parameter metadata (notably the optional ``?network`` selector's
description) that was otherwise hand-copied across every network-scoped endpoint,
so the OpenAPI docs and validation cannot drift between routes. Lives in ``api/``
rather than ``models/`` so the model layer stays free of a FastAPI dependency.
"""

from typing import Annotated, Optional

from fastapi import Query

from app.models.transaction import NetworkType

# The optional ``?network`` selector shared by every network-scoped endpoint.
# Callers default it to None (``network: NetworkParam = None``) so the route falls
# back to the instance's CARDANO_NETWORK setting.
NetworkParam = Annotated[
    Optional[NetworkType],
    Query(
        description=(
            "Network to query: 'mainnet', 'preprod', or 'preview'. "
            "Defaults to the instance's CARDANO_NETWORK setting."
        )
    ),
]
