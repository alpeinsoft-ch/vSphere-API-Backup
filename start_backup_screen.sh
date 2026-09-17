#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

SESSION="${VSPHERE_SCREEN_SESSION:-vsphere-backup}"

usage() {
  echo "Usage: $0 [--session NAME] [-- START_SELECT_VM_BACKUP_ARGS...]" >&2
  echo "Example: $0 --session backup -- --selection-file vm_backup_selection.txt --yes" >&2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --session)
      if [ "$#" -lt 2 ]; then
        usage
        exit 2
      fi
      SESSION="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    *)
      break
      ;;
  esac
done

case "$SESSION" in
  ""|*[!A-Za-z0-9_.-]*)
    echo "Invalid screen session name: $SESSION" >&2
    echo "Use only letters, numbers, dot, underscore, and dash." >&2
    exit 2
    ;;
esac

if [ -z "${VSPHERE_SCREEN_WRAPPED:-}" ] && [ -z "${STY:-}" ] && [ -z "${TMUX:-}" ]; then
  if ! command -v screen >/dev/null 2>&1; then
    echo "screen is not installed. Install it or run ./start_select_vm_backup.sh inside tmux/screen." >&2
    exit 7
  fi

  if screen -ls | grep -Eq "[[:space:]][0-9]+\\.${SESSION}([[:space:]]|$)"; then
    echo "Screen session '$SESSION' already exists; reconnecting."
    exec screen -r "$SESSION"
  fi

  export VSPHERE_SCREEN_WRAPPED=1
  exec screen -S "$SESSION" "$0" --session "$SESSION" -- "$@"
fi

set +e
./start_select_vm_backup.sh "$@"
status=$?
set -e

echo
echo "Backup command exited with status $status."
if [ -n "${STY:-}" ]; then
  echo "Detach with Ctrl+A, then D. Press Enter to close this screen."
  read -r _ || true
fi

exit "$status"
