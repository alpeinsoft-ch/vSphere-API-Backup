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
  if python3 -m venv --system-site-packages .venv >/tmp/restore_vm_backup_venv.log 2>&1; then
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
  echo "  ./start_restore_vm_backup.sh --backup-dir backups/<backup-ordner> --new-name RestoreTest --dry-run" >&2
  echo "Alternative:" >&2
  echo "  python3 -m pip install --user -r requirements.txt" >&2
  exit 7
fi

arg_present() {
  local name="$1"
  shift
  local arg
  for arg in "$@"; do
    if [[ "$arg" == "$name" || "$arg" == "$name="* ]]; then
      return 0
    fi
  done
  return 1
}

wants_help() {
  local arg
  for arg in "$@"; do
    if [[ "$arg" == "-h" || "$arg" == "--help" ]]; then
      return 0
    fi
  done
  return 1
}

latest_backup_dir() {
  find backups -mindepth 2 -maxdepth 2 -type f -name backup_manifest.json -printf '%T@ %h\n' 2>/dev/null \
    | sort -nr \
    | head -n 1 \
    | cut -d' ' -f2-
}

print_restore_usage_hint() {
  echo "Aufruf:" >&2
  echo "  ./start_restore_vm_backup.sh --backup-dir backups/<backup-ordner> --new-name <neuer-vm-name> [--dry-run|--yes]" >&2
  local latest
  latest="$(latest_backup_dir || true)"
  if [[ -n "$latest" ]]; then
    echo >&2
    echo "Aktuell gefundenes Backup:" >&2
    echo "  $latest" >&2
  fi
}

RESTORE_ARGS=("$@")
if ! wants_help "${RESTORE_ARGS[@]}" \
  && ! arg_present --list-backups "${RESTORE_ARGS[@]}" \
  && ! arg_present --list-backup-paths "${RESTORE_ARGS[@]}"; then
  if ! arg_present --backup-dir "${RESTORE_ARGS[@]}" || ! arg_present --new-name "${RESTORE_ARGS[@]}"; then
    if [[ ! -t 0 ]]; then
      print_restore_usage_hint
      exit 2
    fi
  fi
fi

exec "$PYTHON_BIN" restore_vm_backup.py --config "$DEFAULT_CONFIG" "${RESTORE_ARGS[@]}"
