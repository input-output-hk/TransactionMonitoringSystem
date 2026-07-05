"""Redacts secrets from uvicorn's access log.

Uvicorn logs the full request line — including the query string — on the
``uvicorn.access`` logger for every request. The magic-link flow
(``GET /api/auth/verify?token=...``) puts a live, redeemable login
credential directly in that query string, so the default access log line
writes it in plaintext to stdout (review finding: anyone with read access
to application logs during the token's TTL could redeem it, without ever
touching the victim's inbox or browser).

Redacting just this query param is narrower than disabling the access log
outright (``access_log=False`` in run.py), which would lose request-level
operational visibility for every endpoint, not only this one.
"""
from __future__ import annotations

import logging
import re

# `token=<value>` / `api_key=<value>` up to the next `&`, whitespace, or
# closing quote (uvicorn's access formatter wraps the request line in double
# quotes). `\b` keeps this from matching an unrelated param that merely ends in
# "token" (e.g. a hypothetical `csrf_token=`). api_key is included because the
# WebSocket upgrade authenticates via `GET /ws?api_key=<key>`, so a live API
# key would otherwise land in the access log verbatim.
_TOKEN_QS_RE = re.compile(r'(\b(?:token|api_key)=)[^&\s"]+', re.IGNORECASE)


class _RedactTokenFilter(logging.Filter):
    """Rewrites any `token=...` query-string value found in a log record.

    Attached directly to the logger (not a handler) so it applies no
    matter how uvicorn's handlers/formatters get reconfigured afterwards —
    ``logging.config.dictConfig`` (which uvicorn calls internally to set
    up its own handlers) only touches what its config dict explicitly
    names, and leaves filters already attached to a logger alone.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(
                _TOKEN_QS_RE.sub(r"\1<redacted>", arg)
                if isinstance(arg, str)
                else arg
                for arg in record.args
            )
        if isinstance(record.msg, str):
            record.msg = _TOKEN_QS_RE.sub(r"\1<redacted>", record.msg)
        return True


def configure_access_log_redaction() -> None:
    """Attach the token-redacting filter to uvicorn's access logger.

    Safe to call before uvicorn configures its own logging — app import
    happens as part of ``uvicorn.run("app.main:app")`` in run.py, and
    ``logging.getLogger("uvicorn.access")`` always returns the same
    singleton regardless of call order, so the filter is in place by the
    time uvicorn's handlers start emitting records either way.
    """
    logging.getLogger("uvicorn.access").addFilter(_RedactTokenFilter())
