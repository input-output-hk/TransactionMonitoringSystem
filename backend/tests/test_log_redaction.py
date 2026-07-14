"""The uvicorn access-log redaction filter must hide both the magic-link
token and the WebSocket api_key from access logs."""

import logging

from app.logging_utils import _RedactTokenFilter


def _rec(msg, *args):
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, msg, args, None)


def test_token_is_redacted():
    r = _rec("%s", "GET /api/auth/verify?token=SECRETLIVE HTTP/1.1")
    _RedactTokenFilter().filter(r)
    assert "SECRETLIVE" not in r.args[0]
    assert "token=<redacted>" in r.args[0]


def test_api_key_is_redacted():
    r = _rec("%s", "GET /ws?api_key=LIVEKEY123 HTTP/1.1")
    _RedactTokenFilter().filter(r)
    assert "LIVEKEY123" not in r.args[0]
    assert "api_key=<redacted>" in r.args[0]
