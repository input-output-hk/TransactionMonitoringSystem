"""Command-line utilities for TMS administration.

Usage:
    python -m app.cli create-admin <email> <full-name> [--no-email]

This module exists to break the chicken-and-egg bootstrap problem:

    - The magic-link UI requires an existing admin to invite anyone.
    - The first admin therefore can't be created through the UI.

`create-admin` is idempotent: if the email already exists the user is
promoted to ``Admin`` (status untouched) and a fresh invite token is
issued. The magic-link URL is always printed to stdout so an operator
can bootstrap before SMTP is configured.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid

from pydantic import EmailStr, TypeAdapter, ValidationError

from app.auth.email import send_magic_link
from app.auth.schema import execute_auth_schema
from app.auth.tokens import issue_token
from app.config import settings
from app.db.postgres import close_pool, get_connection, init_pool
from app.logging_utils import setup_logging

logger = logging.getLogger(__name__)

# Same validation the API applies on POST /api/users (UserCreate.email).
# The CLI must not be a side door: a user row whose address fails EmailStr
# (e.g. special-use domains like .local/.test) used to be insertable here.
_EMAIL_VALIDATOR = TypeAdapter(EmailStr)


async def create_admin(
    email: str,
    full_name: str,
    send_email: bool = True,
) -> None:
    """Upsert an admin user and emit an invite link.

    On a fresh install this is the only way to get the very first admin
    into the system. After that, additional users should be added via
    the admin UI (``POST /api/users``).
    """
    # Validate BEFORE touching the DB: the API and UI reject addresses
    # that fail EmailStr, so an invalid row created here could neither
    # be listed by GET /api/users nor redeem its magic link.
    try:
        email = str(_EMAIL_VALIDATOR.validate_python(email))
    except ValidationError as exc:
        reason = exc.errors()[0].get("msg", "invalid email address")
        raise SystemExit(
            f"Invalid email {email!r}: {reason}\n"
            "Note: special-use domains (.local, .test, localhost) are not "
            "accepted anywhere in TMS. For local development with mailpit, "
            "any routable-looking domain works, e.g. admin@example.com."
        )

    await init_pool()
    try:
        # Make sure the schema exists — handy when this is the first command
        # ever run, before the FastAPI lifespan has bootstrapped the DB.
        await execute_auth_schema()

        async with get_connection() as conn:
            existing = await conn.fetchrow(
                "SELECT id, role, status FROM users WHERE lower(email) = lower($1)",
                email,
            )
            if existing:
                user_id = existing["id"]
                await conn.execute(
                    """
                    UPDATE users
                    SET full_name = $1,
                        role = 'Admin'
                    WHERE id = $2
                    """,
                    full_name,
                    user_id,
                )
                print(
                    f"User {email} already exists — promoted to Admin "
                    f"(id={user_id}, status={existing['status']})",
                )
            else:
                user_id = uuid.uuid4()
                await conn.execute(
                    """
                    INSERT INTO users (id, email, full_name, role, status)
                    VALUES ($1, $2, $3, 'Admin', 'pending')
                    """,
                    user_id,
                    email,
                    full_name,
                )
                print(f"Created admin user {email} (id={user_id}, status=pending)")

        token = await issue_token(user_id, "invite")
        link = f"{settings.APP_BASE_URL.rstrip('/')}/auth/verify?token={token}"
        print()
        print(f"Magic link (expires in {settings.MAGIC_LINK_TTL_MINUTES} min):")
        print(f"  {link}")
        print()

        if send_email:
            sent = await send_magic_link(
                to_email=email,
                full_name=full_name,
                token=token,
                purpose="invite",
            )
            if sent:
                print(f"Invite email sent to {email} via {settings.SMTP_HOST}")
            else:
                print(
                    "SMTP disabled or send failed — use the link above to complete the invite.",
                )
    finally:
        await close_pool()


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="TMS admin utilities.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_admin = sub.add_parser(
        "create-admin",
        help="Create or promote an Admin user and emit an invite magic link.",
    )
    p_admin.add_argument("email")
    p_admin.add_argument(
        "full_name",
        help="Full name (quote if it contains spaces).",
    )
    p_admin.add_argument(
        "--no-email",
        action="store_true",
        help="Skip the SMTP send — the link is still printed to stdout.",
    )

    args = parser.parse_args()
    if args.cmd == "create-admin":
        asyncio.run(
            create_admin(
                args.email,
                args.full_name,
                send_email=not args.no_email,
            ),
        )
        return

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
