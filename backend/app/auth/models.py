"""Pydantic shapes for users, sessions, magic-link tokens.

Kept light: only the fields the API actually returns. Domain objects (DB
row dicts) are passed around as plain dicts elsewhere in `app.auth`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


UserRole = Literal["Admin", "Reviewer"]
UserStatus = Literal["pending", "active", "disabled"]
TokenPurpose = Literal["invite", "login"]


class User(BaseModel):
    """Public user shape — returned by ``GET /api/auth/me`` and admin endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    full_name: str
    role: UserRole
    status: UserStatus
    created_at: datetime
    last_login_at: datetime | None = None


class UserCreate(BaseModel):
    """Admin payload for ``POST /api/users``. Status is always 'pending'
    until the invitee redeems their magic link."""

    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    role: UserRole


class RequestLinkPayload(BaseModel):
    """``POST /api/auth/request-link`` body."""

    email: EmailStr


class Session(BaseModel):
    """Internal session row — never serialized to the wire."""

    session_id: str
    user_id: UUID
    expires_at: datetime
