#!/usr/bin/env bash
set -Eeuo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="$BASE_DIR/vendor/vddk"

usage() {
  echo "Usage: $0 /path/to/VMware-vix-disklib-*.x86_64.tar.gz" >&2
}

if [ "$#" -ne 1 ]; then
  usage
  exit 2
fi

ARCHIVE="$1"
if [ ! -f "$ARCHIVE" ]; then
  echo "VDDK archive not found: $ARCHIVE" >&2
  exit 2
fi

case "$(basename "$ARCHIVE")" in
  VMware-vix-disklib-*.x86_64.tar.gz|VMware-vix-disklib-*.tar.gz)
    ;;
  *)
    echo "Unexpected archive name. Expected VMware-vix-disklib-*.x86_64.tar.gz" >&2
    exit 2
    ;;
esac

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

mkdir -p "$BASE_DIR/vendor"
tar -xzf "$ARCHIVE" -C "$TMP_DIR"

EXTRACTED_DIR="$(find "$TMP_DIR" -maxdepth 1 -type d -name 'vmware-vix-disklib-*' | head -1)"
if [ -z "$EXTRACTED_DIR" ]; then
  echo "Could not find vmware-vix-disklib-* directory in archive." >&2
  exit 3
fi

if [ ! -f "$EXTRACTED_DIR/lib64/libvixDiskLib.so" ] && [ ! -f "$EXTRACTED_DIR/lib/libvixDiskLib.so" ]; then
  echo "Archive does not contain libvixDiskLib.so." >&2
  exit 3
fi

rm -rf "$TARGET_DIR"
mkdir -p "$TARGET_DIR"
cp -a "$EXTRACTED_DIR"/. "$TARGET_DIR"/

LIBRARY=""
if [ -f "$TARGET_DIR/lib64/libvixDiskLib.so" ]; then
  LIBRARY="$TARGET_DIR/lib64/libvixDiskLib.so"
else
  LIBRARY="$TARGET_DIR/lib/libvixDiskLib.so"
fi

echo "VDDK installed locally:"
echo "  $TARGET_DIR"
echo "VixDiskLib:"
echo "  $LIBRARY"

VDDK_LIBRARY="$LIBRARY" python3 - <<'PY'
import safe_vsphere_backup as core

status = core.vddk_backend_status()
print(status["detail"])
raise SystemExit(0 if status["available"] else 4)
PY
