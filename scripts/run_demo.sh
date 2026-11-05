#!/usr/bin/env bash
# Launch Hazard Commons in DEMO mode (DEMO=1)
# Refuses MOCK_BRAIN. Strict 0G decisions only.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${ROOT_DIR}"

export DEMO=1
unset MOCK_BRAIN || true

echo "=== Starting Hazard Commons (DEMO=1) ==="
echo "Python: ${ROOT_DIR}/.venv/bin/python"

exec "${ROOT_DIR}/.venv/bin/python" "${ROOT_DIR}/scripts/run_demo.py" "$@"
