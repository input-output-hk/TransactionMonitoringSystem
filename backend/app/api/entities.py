"""API endpoints for querying entity state"""

import json
import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Security
from pydantic import BaseModel

from app import audit
from app.api._params import NetworkParam
from app.auth import verify_api_key
from app.config import settings
from app.db import postgres

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/entities", tags=["entities"])

# Entity identifiers are short slugs and Cardano address / policy strings.
# Reject anything that could contain SQL-metadata or path characters; this is
# defence-in-depth — the DB layer parameterises all values.
_ENTITY_TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ENTITY_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
# Upper bound on a single PUT payload. Entity state blobs are small-and-many;
# anything larger is almost certainly abuse.
_MAX_STATE_BYTES = 10_000


def _validate_entity_identifiers(entity_type: str, entity_id: str) -> None:
    if not _ENTITY_TYPE_RE.match(entity_type):
        raise HTTPException(
            status_code=400,
            detail="entity_type must match [a-z][a-z0-9_-]{0,31}",
        )
    if not _ENTITY_ID_RE.match(entity_id):
        raise HTTPException(
            status_code=400,
            detail="entity_id must be 1-256 chars of [A-Za-z0-9_.:-]",
        )


class EntityStateResponse(BaseModel):
    """Entity state response model"""

    entity_type: str
    entity_id: str
    state: dict[str, Any]


@router.get("/{entity_type}/{entity_id}", response_model=EntityStateResponse)
async def get_entity_state(
    entity_type: str,
    entity_id: str,
    network: NetworkParam = None,
    api_key: str = Security(verify_api_key),
):
    """Get entity state by type and ID"""
    _validate_entity_identifiers(entity_type, entity_id)
    try:
        query_network = network or settings.CARDANO_NETWORK
        state = await postgres.get_entity_state(entity_type, entity_id, query_network)
        if not state:
            raise HTTPException(status_code=404, detail="Entity not found")

        return EntityStateResponse(entity_type=entity_type, entity_id=entity_id, state=state)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error querying entity {entity_type}/{entity_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to query entity state")


@router.put("/{entity_type}/{entity_id}")
async def set_entity_state(
    request: Request,
    entity_type: str,
    entity_id: str,
    state: dict[str, Any],
    network: NetworkParam = None,
    principal: str = Security(verify_api_key),
):
    """Set or update entity state.

    Validates identifier shape and caps payload size to ``_MAX_STATE_BYTES``.
    The DB layer parameterises every value; these checks are defence-in-depth
    and also reject obviously abusive payloads before they hit Postgres.
    """
    _validate_entity_identifiers(entity_type, entity_id)
    try:
        size = len(json.dumps(state, separators=(",", ":")))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="state must be JSON-serialisable")
    if size > _MAX_STATE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"state exceeds {_MAX_STATE_BYTES} bytes (got {size})",
        )
    try:
        query_network = network or settings.CARDANO_NETWORK
        await postgres.set_entity_state(entity_type, entity_id, state, query_network)
        logger.info(
            "Entity state updated: network=%s type=%s id=%s size=%d",
            query_network,
            entity_type,
            entity_id,
            size,
        )
        await audit.record(
            event_type="entity_state",
            action="put",
            entity_type=entity_type,
            entity_id=f"{query_network}:{entity_id}",
            details={"size_bytes": size},
            request=request,
            actor=audit.actor_from_principal(principal),
        )
        return {
            "message": "Entity state updated",
            "entity_type": entity_type,
            "entity_id": entity_id,
        }
    except Exception as e:
        logger.error(f"Error setting entity state {entity_type}/{entity_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update entity state")
