#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
BIN_DIR="${HOME}/.local/bin"
TARGET_BIN=""

if [ ! -x "${VENV}/bin/python" ]; then
  python3 -m venv "${VENV}"
fi

if [ -x "${VENV}/bin/pip" ]; then
  "${VENV}/bin/python" -m pip install --upgrade pip setuptools wheel >/dev/null
  "${VENV}/bin/python" -m pip install -e "${ROOT}" >/dev/null
  TARGET_BIN="${VENV}/bin/ax"
else
  if command -v uv >/dev/null 2>&1; then
    uv tool uninstall ax-cli >/dev/null 2>&1 || true
    rm -f "${BIN_DIR}/ax"
    uv tool install --editable --force "${ROOT}" >/dev/null
    TARGET_BIN="${HOME}/.local/bin/ax"
  else
    echo "Error: no usable installer found. Install pip in ${VENV} or ensure 'uv' is available." >&2
    exit 1
  fi
fi

mkdir -p "${BIN_DIR}"
if [ "${TARGET_BIN}" != "${BIN_DIR}/ax" ]; then
  ln -sfn "${TARGET_BIN}" "${BIN_DIR}/ax"
fi

echo "Installed ax -> ${BIN_DIR}/ax"
echo "Source package: ${ROOT}"
echo "Re-run this script after pulling updates to refresh the host install."
