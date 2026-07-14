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
    """Public user shape — returned by ``GET /api/auth/me`` and admin endpoints.

    ``email`` is a plain ``str`` on purpose: this model serializes rows
    coming OUT of the DB. Email validity is enforced at the entry points
    (``UserCreate``, the CLI) — re-validating here turns a single
    legacy/hand-inserted row with a non-routable address (e.g.
    ``user@tms.local``) into a 500 for magic-link redemption and for the
    entire ``GET /api/users`` listing.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
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


# Maximum total length of an email address: RFC 5321 §4.5.3.1 path limit
# minus angle brackets (erratum 1690).
_EMAIL_MAX_LEN = 254


class RequestLinkPayload(BaseModel):
    """``POST /api/auth/request-link`` body.

    ``email`` is a plain bounded ``str``, not ``EmailStr``: this endpoint's
    contract is to reveal nothing, and an EmailStr 422 was its only
    non-200 response — both an inconsistency in that posture and a wall
    for legacy rows whose stored address EmailStr rejects. Anything that
    matches no active user gets the same silent 200. The length bound
    exists because the raw value keys the per-email rate limiter.
    """

    email: str = Field(max_length=_EMAIL_MAX_LEN)


class Session(BaseModel):
    """Internal session row — never serialized to the wire."""

    session_id: str
    user_id: UUID
    expires_at: datetime
