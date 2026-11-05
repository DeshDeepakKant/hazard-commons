#!/usr/bin/env bash
# One-command HazardRegistry deploy to 0G Galileo (chain 16602) + register ZipKart/MinuteMart/DashKirana.
# Needs: DEPLOYER_PRIVATE_KEY (>= ~0.0125 0G for gas) and the 3 OPERATOR_*_PRIVATE_KEY in .env.
# Refuses (exit 3, nothing sent) if the deployer balance is too low.
# Usage: chain/deploy_galileo.sh [--env-file /path/to/.env] [--fund-rewards-wei N]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${PY:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)/../.venv/bin/python"
PYTHONPATH="$ROOT" "$PY" -m chain.deploy --network galileo --rebuild "$@"
echo "Explorer: https://chainscan-galileo.0g.ai/address/$("$PY" -c 'import json;print(json.load(open("chain/deployments/galileo.json"))["HazardRegistry"]["address"])')"
