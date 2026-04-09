#!/usr/bin/env bash
# run.sh — launch an aX agent powered by hermes-agent.
#
# This wires `ax listen` to hermes_bridge.py so every @mention received
# by your agent is routed through a hermes AIAgent run, and whatever
# hermes produces is posted back to aX as the reply.
#
# Usage:
#   cp .env.example .env
#   # edit .env to point at your hermes-agent checkout + provide an LLM key
#   ./run.sh <agent_name>
#
# Requirements:
#   - ax-cli installed (`pip install axctl`)
#   - hermes-agent cloned and venv set up (see .env HERMES_REPO_PATH)
#   - aX agent registered with a valid PAT in ~/.ax/config.toml
set -euo pipefail

AGENT_NAME="${1:?Usage: $0 <agent_name>}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load .env if present — shell-safe, single source of truth for config.
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -o allexport
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.env"
    set +o allexport
else
    echo "note: no $SCRIPT_DIR/.env found — relying on exported env vars." >&2
fi

# Sanity checks — fail loudly if core config is missing.
: "${HERMES_REPO_PATH:?set HERMES_REPO_PATH in .env or export it}"
if [ ! -d "$HERMES_REPO_PATH" ]; then
    echo "ERROR: HERMES_REPO_PATH=$HERMES_REPO_PATH does not exist." >&2
    exit 1
fi

# Prefer hermes's own venv python so dependencies resolve correctly.
PYTHON_BIN="${PYTHON_BIN:-$HERMES_REPO_PATH/.venv/bin/python3}"
if [ ! -x "$PYTHON_BIN" ]; then
    echo "WARN: $PYTHON_BIN not executable, falling back to system python3." >&2
    PYTHON_BIN="$(command -v python3)"
fi

BRIDGE="$SCRIPT_DIR/hermes_bridge.py"
if [ ! -f "$BRIDGE" ]; then
    echo "ERROR: bridge script missing at $BRIDGE." >&2
    exit 1
fi

echo "Starting hermes_sentinel example"
echo "  Agent:       $AGENT_NAME"
echo "  Model:       ${HERMES_MODEL:-codex:gpt-5.4}"
echo "  Hermes repo: $HERMES_REPO_PATH"
echo "  Python:      $PYTHON_BIN"
echo "  Bridge:      $BRIDGE"
echo

# ax listen --exec: the handler receives mention content as $1 and
# $AX_MENTION_CONTENT. Whatever it prints to stdout becomes the reply.
exec ax listen \
    --agent "$AGENT_NAME" \
    --exec "$PYTHON_BIN $BRIDGE"
