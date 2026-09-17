#!/usr/bin/env bash
set -Eeuo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKER="Codex one-time vSphere VM backup test"
TMP_CRON="$(mktemp)"

cleanup() {
  rm -f "$TMP_CRON"
}
trap cleanup EXIT

crontab -l 2>/dev/null \
  | grep -Fv "$MARKER" \
  | grep -Fv "$BASE_DIR/run_once_vm_backup_test.sh" \
  >"$TMP_CRON" || true
crontab "$TMP_CRON"

exec /bin/bash "$BASE_DIR/run_weekly_vm_backup.sh"
