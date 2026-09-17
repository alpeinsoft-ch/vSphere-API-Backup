#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# VDDK's advanced transport plugin depends on the libraries shipped beside it.
# Keep the normal invocation unchanged while making those dependencies
# visible to the dynamic linker.
VDDK_LIB_DIR="$(pwd)/vendor/vddk/lib64"
if [ -d "$VDDK_LIB_DIR" ]; then
  if [ -n "${LD_LIBRARY_PATH:-}" ]; then
    export LD_LIBRARY_PATH="$VDDK_LIB_DIR:$LD_LIBRARY_PATH"
  else
    export LD_LIBRARY_PATH="$VDDK_LIB_DIR"
  fi
fi

if [ -n "${VSPHERE_USE_SCREEN:-}" ] && [ -z "${VSPHERE_SCREEN_WRAPPED:-}" ] && [ -z "${STY:-}" ] && [ -z "${TMUX:-}" ] && [ -t 0 ]; then
  SCREEN_SESSION="${VSPHERE_SCREEN_SESSION:-vsphere-backup}"
  case "$SCREEN_SESSION" in
    ""|*[!A-Za-z0-9_.-]*)
      echo "Ungueltiger Screen-Session-Name: $SCREEN_SESSION" >&2
      echo "Erlaubt sind nur Buchstaben, Zahlen, Punkt, Unterstrich und Bindestrich." >&2
      exit 2
      ;;
  esac

  if command -v screen >/dev/null 2>&1; then
    if screen -ls | grep -Eq "[[:space:]][0-9]+\\.${SCREEN_SESSION}([[:space:]]|$)"; then
      echo "Screen-Session '$SCREEN_SESSION' existiert bereits; verbinde dorthin."
      exec screen -r "$SCREEN_SESSION"
    fi

    echo "Starte Backup in Screen-Session '$SCREEN_SESSION'."
    echo "Trennen: Ctrl+A, dann D. Wieder verbinden: screen -r $SCREEN_SESSION"
    export VSPHERE_SCREEN_WRAPPED=1
    exec screen -S "$SCREEN_SESSION" "$0" "$@"
  else
    echo "Hinweis: screen ist nicht installiert; Backup bleibt an diese SSH-Sitzung gebunden." >&2
  fi
fi

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
  if python3 -m venv --system-site-packages .venv >/tmp/select_vm_backup_venv.log 2>&1; then
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
  echo "  ./start_select_vm_backup.sh" >&2
  echo "Alternative:" >&2
  echo "  python3 -m pip install --user -r requirements.txt" >&2
  exit 7
fi

DEFAULT_SELECTION_FILE="vm_backup_selection.txt"
DEFAULT_BACKUP_MODE="${VSPHERE_BACKUP_MODE:-cbt}"

if [ "$#" -eq 0 ]; then
  if [ -f "$DEFAULT_SELECTION_FILE" ] && [ -t 0 ]; then
    echo "Auswahl-Datei gefunden: $DEFAULT_SELECTION_FILE"
    printf "Diese Liste fuer das Backup nutzen? [j/N]: "
    read -r answer
    case "$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')" in
      j|ja|y|yes)
        exec "$PYTHON_BIN" select_vm_backup.py --config "$DEFAULT_CONFIG" --selection-file "$DEFAULT_SELECTION_FILE" --yes --skip-blocked --backup-mode "$DEFAULT_BACKUP_MODE"
        ;;
    esac
  fi

  exec "$PYTHON_BIN" select_vm_backup.py --config "$DEFAULT_CONFIG" --backup-mode "$DEFAULT_BACKUP_MODE"
fi

exec "$PYTHON_BIN" select_vm_backup.py --config "$DEFAULT_CONFIG" "$@"
