#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="python3"
USING_VENV=0
DEFAULT_CONFIG="${VSPHERE_CONFIG:-credentials.env}"

check_modules() {
  set +e
  "$PYTHON_BIN" - <<'PY'
import importlib.util
import sys

missing = [module for module in ("pyVmomi", "requests") if importlib.util.find_spec(module) is None]
if missing:
    print(",".join(missing))
    sys.exit(7)
PY
  local status=$?
  set -e
  return "$status"
}

use_system_python() {
  PYTHON_BIN="python3"
  USING_VENV=0
}

if [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=".venv/bin/python"
  USING_VENV=1
else
  rm -rf .venv
  if python3 -m venv --system-site-packages .venv >/tmp/vm_backup_selection_venv.log 2>&1; then
    PYTHON_BIN=".venv/bin/python"
    USING_VENV=1
  else
    rm -rf .venv
    echo "Hinweis: python3-venv ist nicht verfuegbar; nutze System-Python." >&2
    use_system_python
  fi
fi

if ! check_modules; then
  if [ "$USING_VENV" -eq 1 ] && "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
    "$PYTHON_BIN" -m pip install -r requirements.txt
  elif [ "$USING_VENV" -eq 1 ]; then
    echo "Hinweis: lokale venv hat kein pip; versuche System-Python." >&2
    rm -rf .venv
    use_system_python
  fi
fi

if ! check_modules; then
  echo "Fehlende Python-Module auf diesem Rechner: pyVmomi/requests" >&2
  echo "Empfohlen:" >&2
  echo "  sudo apt install python3-venv" >&2
  echo "  ./start_vm_backup_selection.sh" >&2
  echo "Alternative:" >&2
  echo "  python3 -m pip install --user -r requirements.txt" >&2
  exit 7
fi

exec "$PYTHON_BIN" vm_backup_selection.py --config "$DEFAULT_CONFIG" "$@"
