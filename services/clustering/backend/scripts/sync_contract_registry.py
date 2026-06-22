"""Refresh the vendored snapshot of the StricaHQ cardano-contracts-registry.

Dev-only, manual, and the *only* part of this feature that touches the network.
It downloads every ``projects/*.json`` file (plus the upstream LICENSE) into
``app/registry/data/`` so the runtime lookup stays fully offline and hermetic
tests have a stable fixture set.

Usage (host or backend container):

    python -m scripts.sync_contract_registry        # from backend/
    python backend/scripts/sync_contract_registry.py

Re-run periodically to pick up newly registered dApps; commit the result.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

REPO = "StricaHQ/cardano-contracts-registry"
REF = "master"
CONTENTS_API = f"https://api.github.com/repos/{REPO}/contents/projects?ref={REF}"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/{REF}"

DATA_DIR = Path(__file__).resolve().parent.parent / "app" / "registry" / "data"
PROJECTS_DIR = DATA_DIR / "projects"


def main() -> int:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=30.0, headers={"Accept": "application/vnd.github+json"}) as client:
        listing = client.get(CONTENTS_API)
        listing.raise_for_status()
        files = [e["name"] for e in listing.json() if e["name"].endswith(".json")]
        print(f"Found {len(files)} project files; downloading…")

        for name in sorted(files):
            safe_name = Path(name).name  # defend against traversal in listing names
            resp = client.get(f"{RAW_BASE}/projects/{safe_name}")
            resp.raise_for_status()
            (PROJECTS_DIR / safe_name).write_bytes(resp.content)
            print(f"  + projects/{safe_name}")

        # Refresh upstream LICENSE for attribution (Apache-2.0).
        lic = client.get(f"{RAW_BASE}/LICENSE")
        if lic.status_code == 200:
            (DATA_DIR / "LICENSE").write_bytes(lic.content)
            print("  + LICENSE")

    print(f"Done. Vendored snapshot written to {DATA_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
