#!/usr/bin/env bash
set -Eeuo pipefail

DISK="${DISK:-/dev/sda}"
PART_NUM="${PART_NUM:-3}"
PART="${PART:-${DISK}${PART_NUM}}"
PV="${PV:-$PART}"
LV="${LV:-/dev/mapper/ubuntu--vg-ubuntu--lv}"
MOUNTPOINT="${MOUNTPOINT:-/}"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

human_bytes() {
  numfmt --to=iec-i --suffix=B "$1"
}

block_bytes() {
  blockdev --getsize64 "$1"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

if [ "$(id -u)" -ne 0 ]; then
  die "Run as root, for example: sudo $0"
fi

for cmd in blockdev df findmnt growpart lsblk lvextend numfmt partprobe pvresize resize2fs udevadm; do
  require_cmd "$cmd"
done

[ -b "$DISK" ] || die "Disk not found: $DISK"
[ -b "$PART" ] || die "Partition not found: $PART"
[ -b "$LV" ] || die "Logical volume not found: $LV"

root_source="$(findmnt -n -o SOURCE "$MOUNTPOINT")"
root_fstype="$(findmnt -n -o FSTYPE "$MOUNTPOINT")"

if [ "$root_source" != "$LV" ]; then
  die "$MOUNTPOINT is mounted from $root_source, expected $LV"
fi

if [ "$root_fstype" != "ext4" ]; then
  die "$MOUNTPOINT uses $root_fstype, expected ext4"
fi

echo "Current layout:"
lsblk -o NAME,SIZE,FSTYPE,TYPE,MOUNTPOINTS "$DISK"
df -hT "$MOUNTPOINT"

disk_before="$(block_bytes "$DISK")"
part_before="$(block_bytes "$PART")"

echo
echo "Rescanning $DISK for a changed VMware virtual disk size..."
echo 1 >"/sys/class/block/$(basename "$DISK")/device/rescan"
udevadm settle
partprobe "$DISK" || true
udevadm settle

disk_after="$(block_bytes "$DISK")"
part_after_rescan="$(block_bytes "$PART")"

echo "Disk before rescan:      $(human_bytes "$disk_before")"
echo "Disk after rescan:       $(human_bytes "$disk_after")"
echo "Partition before grow:   $(human_bytes "$part_after_rescan")"

if [ "$disk_after" -le "$part_after_rescan" ]; then
  die "No larger disk size is visible to Linux. Check vSphere disk size or reboot/rescan the VM."
fi

echo
echo "This will grow $PART, then pvresize $PV, then extend $LV and online-resize ext4."
echo "No files or backup folders are deleted."
read -r -p "Type GROW to continue: " answer
[ "$answer" = "GROW" ] || die "Cancelled."

echo
echo "Growing partition $PART..."
growpart "$DISK" "$PART_NUM"
partprobe "$DISK" || true
udevadm settle

part_after_grow="$(block_bytes "$PART")"
echo "Partition after grow:    $(human_bytes "$part_after_grow")"

echo
echo "Growing LVM physical volume $PV..."
pvresize "$PV"

echo
echo "Extending logical volume $LV and resizing filesystem..."
lvextend -l +100%FREE -r "$LV"

echo
echo "Final layout:"
lsblk -o NAME,SIZE,FSTYPE,TYPE,MOUNTPOINTS "$DISK"
df -hT "$MOUNTPOINT"
