"""API endpoints for querying entity state"""

import logging
from typing import Optional, Dict, Any, Literal
from fastapi import APIRouter, HTTPException, Security, Query
from pydantic import BaseModel

from app.db import postgres
from app.auth import verify_api_key
from app.config import settings

# Network type for API parameters
NetworkType = Literal["mainnet", "preprod"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/entities", tags=["entities"])


class EntityStateResponse(BaseModel):
    """Entity state response model"""
    entity_type: str
    entity_id: str
    state: Dict[str, Any]


@router.get("/{entity_type}/{entity_id}", response_model=EntityStateResponse)
async def get_entity_state(
    entity_type: str,
    entity_id: str,
    network: Optional[NetworkType] = Query(
        None,
        description="Network to query: 'mainnet' or 'preprod'. Defaults to 'preprod' if not specified."
    ),
    api_key: str = Security(verify_api_key),
):
    """Get entity state by type and ID"""
    try:
        query_network = network or settings.CARDANO_NETWORK
        state = await postgres.get_entity_state(entity_type, entity_id, query_network)
        if not state:
            raise HTTPException(status_code=404, detail="Entity not found")

        return EntityStateResponse(
            entity_type=entity_type,
            entity_id=entity_id,
            state=state
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error querying entity {entity_type}/{entity_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to query entity state")


@router.put("/{entity_type}/{entity_id}")
async def set_entity_state(
    entity_type: str,
    entity_id: str,
    state: Dict[str, Any],
    network: Optional[NetworkType] = Query(
        None,
        description="Network to query: 'mainnet' or 'preprod'. Defaults to 'preprod' if not specified."
    ),
    api_key: str = Security(verify_api_key),
):
    """Set or update entity state"""
    try:
        query_network = network or settings.CARDANO_NETWORK
        await postgres.set_entity_state(entity_type, entity_id, state, query_network)
        return {
            "message": "Entity state updated",
            "entity_type": entity_type,
            "entity_id": entity_id
        }
    except Exception as e:
        logger.error(f"Error setting entity state {entity_type}/{entity_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update entity state")
