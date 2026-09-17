#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

DEFAULT_SELECTION_FILE="vm_backup_selection.txt"
extra_args=()

if [ "$#" -eq 0 ] && [ -f "$DEFAULT_SELECTION_FILE" ] && [ -t 0 ]; then
  echo "Auswahl-Datei existiert bereits: $DEFAULT_SELECTION_FILE"
  printf "Datei neu aus vSphere-Inventar erzeugen und ueberschreiben? [j/N]: "
  read -r answer
  case "$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')" in
    j|ja|y|yes)
      extra_args=(--force)
      ;;
    *)
      echo "Datei bleibt unveraendert."
      exit 0
      ;;
  esac
fi

exec ./start_vm_backup_selection.sh --output "$DEFAULT_SELECTION_FILE" "${extra_args[@]}" "$@"
