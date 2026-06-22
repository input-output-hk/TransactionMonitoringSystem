"""Short opaque id generation for runs, jobs, and models."""

from __future__ import annotations

import uuid

# Hex characters of randomness in a generated id. 12 hex chars = 48 bits, ample
# to avoid collisions across a contract's runs/jobs/models while staying short
# enough to read in logs and the UI.
_ID_HEX_LEN = 12


def new_id(prefix: str) -> str:
    """A short, prefixed, opaque id: ``"<prefix>-<12 hex chars>"`` (e.g. ``job-…``,
    ``model-…``, or a feature-set-tagged cluster run id)."""
    return f"{prefix}-{uuid.uuid4().hex[:_ID_HEX_LEN]}"
