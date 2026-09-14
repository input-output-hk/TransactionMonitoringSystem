#!/bin/bash
# Download the Cardano node configuration set for one network.
#
# Usage:
#   ./scripts/fetch-cardano-config.sh [NETWORK]
#
# NETWORK defaults to the NETWORK env var, then to "preprod".
# Supported: mainnet | preprod | preview
#
# Saves, flat and co-located under ./cardano-config/<network>/, the file set
# the bundled ingestion profile needs (see README.md and RUNBOOK.md):
# config.json, topology.json, the four genesis files, plus checkpoints.json
# and peer-snapshot.json when the network's config and topology reference
# them. The whole set comes from the official environments listing in one
# pass because the files cross-reference each other: config.json names the
# genesis files (and checkpoints.json, where used) by filename AND hash, and
# topology.json names peer-snapshot.json, so a partial or hand-edited set
# fails the node's startup hash check.
#
# Safe to re-run: existing files are overwritten with the upstream versions.

set -euo pipefail

NETWORK="${1:-${NETWORK:-preprod}}"
case "$NETWORK" in
    mainnet | preprod | preview) ;;
    *)
        echo "ERROR: unsupported network '${NETWORK}'. Choose from: mainnet preprod preview" >&2
        exit 1
        ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/cardano-config/${NETWORK}"
BASE="https://book.world.dev.cardano.org/environments/${NETWORK}"

fetch() {
    echo "  $1"
    curl -sSfL "${BASE}/$1" -o "${ROOT}/$1"
}

mkdir -p "$ROOT"
echo "Fetching ${NETWORK} node configuration -> ${ROOT}"
fetch config.json
fetch topology.json

# The extras are per-network: fetch each only when the set references it, so a
# network without one (preprod has no checkpoints.json today) is not an error,
# while a referenced-but-missing file still fails loudly at download time
# rather than at node startup.
if grep -q "checkpoints.json" "${ROOT}/config.json"; then
    fetch checkpoints.json
fi
if grep -q "peer-snapshot.json" "${ROOT}/topology.json"; then
    fetch peer-snapshot.json
fi

fetch byron-genesis.json
fetch shelley-genesis.json
fetch alonzo-genesis.json
fetch conway-genesis.json

echo "Done. CARDANO_CONFIG_DIR must point at ${ROOT}"
echo "(the default ./cardano-config/preprod already does for preprod), then:"
echo "  docker compose --profile app --profile ingestion up -d"
