"""Unit tests for ``_safe_error`` — the mapper from a source exception to the
client-facing string persisted on a failed job."""

from __future__ import annotations

from app.service._common import _safe_error
from app.sources.base import SourceError, SourceNotFound, SourceRateLimited


def test_client_safe_message_is_passed_through() -> None:
    exc = SourceNotFound("no transactions in this instance's mainnet data", client_safe=True)
    assert _safe_error(exc) == "no transactions in this instance's mainnet data"


def test_non_client_safe_not_found_is_generic() -> None:
    # A bare provider 404 must not leak; it collapses to the generic string.
    assert _safe_error(SourceNotFound("404 Not Found: <raw body>")) == (
        "address or policy id not found on-chain"
    )


def test_rate_limited_message_is_fixed() -> None:
    assert "request limit" in _safe_error(SourceRateLimited("HTTP 429"))


def test_generic_source_error_hides_body() -> None:
    msg = _safe_error(SourceError("upstream 500: <stack trace>"))
    assert "stack trace" not in msg
    assert "server logs" in msg


def test_unknown_exception_names_type_only() -> None:
    msg = _safe_error(ValueError("secret detail"))
    assert "secret detail" not in msg
    assert "ValueError" in msg
