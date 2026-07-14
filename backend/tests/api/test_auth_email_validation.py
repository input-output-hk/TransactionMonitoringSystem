"""Regression tests for the email-validation asymmetry (poison-pill rows).

A user row whose email fails EmailStr (special-use domains such as
``.local``) must be impossible to CREATE through any entry point (API
payload, bootstrap CLI), but must not break read paths if such a row
already exists (legacy data, manual SQL): one bad row used to 500 both
magic-link redemption and the entire ``GET /api/users`` listing.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.auth.models import (
    _EMAIL_MAX_LEN,
    RequestLinkPayload,
    User,
    UserCreate,
)
from app.cli import create_admin

# .local is reserved for mDNS (RFC 6762); email-validator classifies it
# as a special-use domain and EmailStr rejects it.
SPECIAL_USE_EMAIL = "ops@tms.local"


def _user_row(email: str) -> dict:
    return {
        "id": uuid.uuid4(),
        "email": email,
        "full_name": "Legacy Row",
        "role": "Admin",
        "status": "active",
        "created_at": datetime.now(timezone.utc),
        "last_login_at": None,
    }


def test_user_response_model_tolerates_special_use_email():
    """Rows already in the DB must serialize, whatever their address."""
    user = User(**_user_row(SPECIAL_USE_EMAIL))
    assert user.email == SPECIAL_USE_EMAIL
    # The 500s observed in the field happened at .model_dump() time.
    user.model_dump(mode="json")


def test_user_create_payload_still_rejects_special_use_email():
    """Input validation stays strict — only the OUTBOUND model relaxed."""
    with pytest.raises(ValidationError):
        UserCreate(
            email=SPECIAL_USE_EMAIL,
            full_name="Poison Test",
            role="Admin",
        )


def test_request_link_payload_accepts_any_bounded_string():
    """request-link's contract is a silent 200 for ANYTHING that matches
    no active user — including unparseable addresses and legacy
    special-use domains. Validation here was its only non-200 response."""
    RequestLinkPayload(email="not an email at all")
    RequestLinkPayload(email=SPECIAL_USE_EMAIL)


def test_request_link_payload_bounds_length():
    """The raw value keys the per-email rate limiter, so it stays bounded."""
    with pytest.raises(ValidationError):
        RequestLinkPayload(email="a" * _EMAIL_MAX_LEN + "@example.com")


class _DbTouched(Exception):
    """Sentinel: create_admin got past validation to the DB layer."""


@pytest.fixture
def no_db(monkeypatch):
    """Replace the CLI's init_pool so no test can reach a live database
    (the default settings may point at a running dev Postgres)."""

    async def _refuse() -> None:
        raise _DbTouched()

    monkeypatch.setattr("app.cli.init_pool", _refuse)


def test_cli_rejects_special_use_email_before_any_db_work(no_db):
    """create-admin must fail fast on a bad address, before init_pool()."""
    with pytest.raises(SystemExit, match="Invalid email"):
        asyncio.run(
            create_admin(SPECIAL_USE_EMAIL, "Poison Test", send_email=False),
        )


def test_cli_accepts_routable_dev_email(no_db):
    """example.com passes EmailStr — the documented mailpit dev path.

    Hitting the _DbTouched sentinel proves validation let the address
    through (and that the test never reached a real database).
    """
    with pytest.raises(_DbTouched):
        asyncio.run(
            create_admin("admin@example.com", "Dev Admin", send_email=False),
        )
