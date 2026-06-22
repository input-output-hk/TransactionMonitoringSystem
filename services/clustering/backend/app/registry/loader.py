"""Load the vendored contracts-registry snapshot into a script-hash → label map.

The map is built once from ``data/projects/*.json`` and cached for the process.
Label is ``"{labelPrefix} {contract.name}"`` (e.g. ``"Minswap Order Contract"``).
Loading is defensive: a malformed project file or entry is skipped rather than
breaking onboarding for everything else.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent / "data"
_PROJECTS_DIR = _DATA_DIR / "projects"
# Local, hand-maintained labels for contracts not in (or ahead of) the upstream
# snapshot. A flat ``{script_hash: label}`` map; entries here win over upstream
# and are NOT touched by ``sync_contract_registry``.
_OVERRIDES_FILE = _DATA_DIR / "overrides.json"


@lru_cache(maxsize=1)
def label_map() -> dict[str, str]:
    """Return ``{script_hash(lowercase 56-hex): label}`` from the vendored data.

    Built from the upstream ``projects/*.json`` snapshot, then ``overrides.json``
    is layered on top (local entries take precedence). Cached for the process
    lifetime; refreshing the data requires a backend restart to take effect.
    """
    out: dict[str, str] = {}
    for path in sorted(_PROJECTS_DIR.glob("*.json")):
        try:
            project = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("contracts-registry: skipping unreadable %s", path.name)
            continue
        prefix = str(project.get("labelPrefix") or project.get("projectName") or "").strip()
        for contract in project.get("contracts", []):
            script_hash = str(contract.get("scriptHash") or "").strip().lower()
            name = str(contract.get("name") or "").strip()
            if not script_hash or script_hash in out:
                continue  # first occurrence wins on duplicate hash
            out[script_hash] = f"{prefix} {name}".strip()

    try:
        overrides = json.loads(_OVERRIDES_FILE.read_text(encoding="utf-8"))
        for script_hash, label in overrides.items():
            key = str(script_hash).strip().lower()
            if key and str(label).strip():
                out[key] = str(label).strip()  # local overrides win over upstream
    except FileNotFoundError:
        pass
    except ValueError:
        logger.warning("contracts-registry: ignoring malformed overrides.json")
    return out
