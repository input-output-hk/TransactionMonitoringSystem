#!/usr/bin/env bash
# Download official Cardano node configuration files for a given network.
#
# Usage:
#   ./scripts/fetch-cardano-config.sh [NETWORK]
#
# NETWORK defaults to the NETWORK env var, then to "preprod".
# Supported: mainnet | preprod | preview | sanchonet
#
# Files are saved to ./cardano-config/<network>/ preserving the upstream
# directory structure so relative genesis paths in config.json resolve correctly:
#   cardano-config/<network>/cardano-node/config.json
#   cardano-config/<network>/cardano-node/topology.json
#   cardano-config/<network>/genesis/{alonzo,byron,conway,shelley}.json
#
# Safe to re-run — existing files are overwritten with the latest version.

set -euo pipefail

NETWORK="${1:-${NETWORK:-preprod}}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/cardano-config/${NETWORK}"

SUPPORTED=(mainnet preprod preview sanchonet)
if [[ ! " ${SUPPORTED[*]} " =~ " ${NETWORK} " ]]; then
  echo "ERROR: unsupported network '${NETWORK}'. Choose from: ${SUPPORTED[*]}" >&2
  exit 1
fi

BASE="https://raw.githubusercontent.com/input-output-hk/cardano-configurations/master/network/${NETWORK}"

fetch() {
  local src="$1" dst="$2"
  echo "  ${dst##"${ROOT}/"}"
  curl -sSfL "${src}" -o "${dst}" || {
    echo "ERROR: failed to download ${src}" >&2
    exit 1
  }
}

echo "Fetching ${NETWORK} config → ${ROOT}"
mkdir -p "${ROOT}/cardano-node" "${ROOT}/genesis"

fetch "${BASE}/cardano-node/config.json"   "${ROOT}/cardano-node/config.json"
fetch "${BASE}/cardano-node/topology.json" "${ROOT}/cardano-node/topology.json"
fetch "${BASE}/genesis/alonzo.json"        "${ROOT}/genesis/alonzo.json"
fetch "${BASE}/genesis/byron.json"         "${ROOT}/genesis/byron.json"
fetch "${BASE}/genesis/conway.json"        "${ROOT}/genesis/conway.json"
fetch "${BASE}/genesis/shelley.json"       "${ROOT}/genesis/shelley.json"

echo "Done. Start with:"
echo "  docker compose --profile ingestion up -d"
