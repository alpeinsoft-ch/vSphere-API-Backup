#!/usr/bin/env python3
"""Restore one vSphere VM backup under a new VM name.

The restore path is intentionally conservative:

- it requires one verified backup directory;
- it refuses to overwrite an existing VM or datastore folder;
- it creates the restored VM powered off;
- it leaves the network adapter disconnected unless explicitly requested.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import quote
from xml.etree import ElementTree

import requests
from pyVmomi import vim, vmodl

import delta_storage
import safe_vsphere_backup as core
import vddk_cbt


DEFAULT_CONFIG = "credentials.env"
OVF_NS = "http://schemas.dmtf.org/ovf/envelope/1"
RASD_NS = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
VSSD_NS = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData"
VMW_NS = "http://www.vmware.com/schema/ovf"


@dataclass(frozen=True)
class BackupArtifacts:
    backup_dir: Path
    manifest: Dict[str, Any]
    vm_info: Dict[str, Any]
    descriptor_name: str
    extent_names: List[str]
    ctk_names: List[str]
    upload_files: List[str]
    ovf: Dict[str, Any]


@dataclass(frozen=True)
class RestoreTarget:
    datacenter: vim.Datacenter
    datastore: vim.Datastore
    datastore_name: str
    datastore_folder: str
    resource_pool: vim.ResourcePool
    vm_folder: vim.Folder
    network: Optional[vim.Network]
    network_name: str


@dataclass(frozen=True)
class RestorePoint:
    backup_dir: Path
    target_vm: str
    run_name: str
    kind: str
    sequence: int
    started_at: str
    finished_at: str
    status: str
    logical_bytes: int
    file_count: int
    delta_packed_files: int
    delta_removed_original_bytes: int


def ns(name: str, namespace: str = OVF_NS) -> str:
    return f"{{{namespace}}}{name}"


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_local_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    return candidate


def parse_manifest_datetime(value: str) -> Optional[datetime]:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def display_manifest_datetime(value: str) -> str:
    parsed = parse_manifest_datetime(value)
    if parsed is None:
        return value or "-"
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
        return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def human_bytes(value: int) -> str:
    amount = float(max(int(value), 0))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.2f} {unit}"
        amount /= 1024
    return f"{amount:.2f} TiB"


def backup_manifest_logical_bytes(manifest: Dict[str, Any]) -> int:
    total = 0
    for item in manifest.get("files", []) or []:
        try:
            total += int(item.get("bytes") or 0)
        except (TypeError, ValueError):
            continue
    return total


def chain_sequence_from_name(name: str) -> int:
    match = re.search(r"_(\d+)$", name or "")
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:
        return 0


def restore_point_from_manifest(manifest_file: Path) -> Optional[RestorePoint]:
    try:
        manifest = read_json(manifest_file)
    except Exception:
        return None

    status = str(manifest.get("status") or "")
    if status not in {"success", "success_rescued"}:
        return None

    backup_dir = manifest_file.parent.resolve()
    vm_info = manifest.get("vm_info") or {}
    chain = manifest.get("backup_chain") or {}
    delta_storage_info = manifest.get("delta_storage") or {}
    run_name = str(chain.get("run_name") or backup_dir.name)
    kind = str(chain.get("kind") or ("delta" if delta_storage_info else "full"))
    try:
        sequence = int(chain.get("sequence") or chain_sequence_from_name(run_name))
    except (TypeError, ValueError):
        sequence = chain_sequence_from_name(run_name)

    return RestorePoint(
        backup_dir=backup_dir,
        target_vm=str(manifest.get("target_vm") or vm_info.get("name") or backup_dir.parent.name),
        run_name=run_name,
        kind=kind,
        sequence=sequence,
        started_at=str(manifest.get("started_at") or ""),
        finished_at=str(manifest.get("finished_at") or manifest.get("started_at") or ""),
        status=status,
        logical_bytes=backup_manifest_logical_bytes(manifest),
        file_count=len(manifest.get("files", []) or []),
        delta_packed_files=int(delta_storage_info.get("packed_files") or 0),
        delta_removed_original_bytes=int(delta_storage_info.get("removed_original_bytes") or 0),
    )


def ignored_manifest_path(manifest_file: Path) -> bool:
    ignored_names = {delta_storage.DEFAULT_STORE_DIRNAME, delta_storage.HYDRATED_DIRNAME}
    for part in manifest_file.parts:
        if part in ignored_names or part.endswith((".failed", ".inprogress")):
            return True
    return False


def restore_point_sort_key(point: RestorePoint) -> tuple[datetime, int, str]:
    parsed = parse_manifest_datetime(point.finished_at) or datetime.fromtimestamp(0, timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed, point.sequence, str(point.backup_dir))


def sort_restore_points(points: Sequence[RestorePoint]) -> List[RestorePoint]:
    return sorted(points, key=restore_point_sort_key, reverse=True)


def discover_restore_points(backup_root: Path) -> List[RestorePoint]:
    backup_root = backup_root.expanduser().resolve()
    if not backup_root.exists():
        return []
    points: List[RestorePoint] = []
    for manifest_file in sorted(backup_root.rglob("backup_manifest.json")):
        if ignored_manifest_path(manifest_file):
            continue
        point = restore_point_from_manifest(manifest_file)
        if point is not None:
            points.append(point)

    return sort_restore_points(points)


def group_restore_points_by_vm(points: Sequence[RestorePoint]) -> List[tuple[str, List[RestorePoint]]]:
    groups: Dict[str, List[RestorePoint]] = {}
    for point in sort_restore_points(points):
        vm_name = point.target_vm or "Unbekannt"
        groups.setdefault(vm_name, []).append(point)
    return list(groups.items())


def display_backup_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def format_table(headers: Sequence[str], rows: Sequence[Sequence[str]], title: str) -> str:
    widths = [
        max(len(headers[column]), *(len(row[column]) for row in rows))
        for column in range(len(headers))
    ]
    lines = [title]
    lines.append(" | ".join(headers[column].ljust(widths[column]) for column in range(len(headers))))
    lines.append("-+-".join("-" * width for width in widths))
    for row in rows:
        lines.append(" | ".join(row[column].ljust(widths[column]) for column in range(len(row))))
    return "\n".join(lines)


def format_restore_vm_table(points: Sequence[RestorePoint]) -> str:
    groups = group_restore_points_by_vm(points)
    if not groups:
        return "Keine Restore-Punkte gefunden."

    rows: List[List[str]] = []
    for index, (vm_name, vm_points) in enumerate(groups, start=1):
        latest = vm_points[0]
        rows.append(
            [
                str(index),
                vm_name,
                str(len(vm_points)),
                latest.run_name,
                display_manifest_datetime(latest.finished_at),
                display_backup_path(latest.backup_dir.parent),
            ]
        )

    headers = ["#", "VM", "Versionen", "Neueste Version", "Neuester Stand", "Pfad"]
    return format_table(headers, rows, f"Gefundene VMs: {len(groups)}")


def format_restore_point_table(
    points: Sequence[RestorePoint],
    *,
    show_vm: bool = True,
    title: str = "",
) -> str:
    if not points:
        return "Keine Restore-Punkte gefunden."

    rows: List[List[str]] = []
    for index, point in enumerate(points, start=1):
        delta_text = "-"
        if point.delta_packed_files:
            delta_text = f"{point.delta_packed_files} Datei"
            if point.delta_packed_files != 1:
                delta_text += "en"
        rows.append(
            [
                str(index),
                point.run_name,
                point.kind,
                display_manifest_datetime(point.finished_at),
                human_bytes(point.logical_bytes),
                delta_text,
                display_backup_path(point.backup_dir),
            ]
        )
        if show_vm:
            rows[-1].insert(1, point.target_vm)

    headers = ["#", "Version", "Typ", "Datum", "Logische Groesse", "Delta", "Pfad"]
    if show_vm:
        headers.insert(1, "VM")
    return format_table(headers, rows, title or f"Gefundene Restore-Punkte: {len(points)}")


def select_restore_point_interactive(backup_root: Path) -> Path:
    points = discover_restore_points(backup_root)
    if not points:
        raise core.SafetyError(f"No successful restore points found under {backup_root}")
    if not sys.stdin.isatty():
        raise core.SafetyError("Interactive restore selection needs a TTY. Use --backup-dir.")

    groups = group_restore_points_by_vm(points)
    print(format_restore_vm_table(points))
    while True:
        raw = input("VM auswaehlen [1] oder q fuer Abbruch: ").strip()
        selected = raw or "1"
        if selected.lower() in {"q", "quit", "abbruch"}:
            raise core.SafetyError("Restore selection aborted.")
        if selected.isdigit():
            index = int(selected)
            if 1 <= index <= len(groups):
                selected_vm, selected_points = groups[index - 1]
                break
            print(f"Nummer ausserhalb der Liste: {index}")
            continue
        exact_matches = [(vm_name, vm_points) for vm_name, vm_points in groups if vm_name == selected]
        if len(exact_matches) == 1:
            selected_vm, selected_points = exact_matches[0]
            break
        candidate = resolve_local_path(selected)
        if (candidate / "backup_manifest.json").exists():
            return candidate
        print("Bitte eine Nummer aus der VM-Liste oder einen exakten VM-Namen eingeben.")

    print(format_restore_point_table(
        selected_points,
        show_vm=False,
        title=f"Restore-Versionen fuer VM {selected_vm}: {len(selected_points)}",
    ))
    while True:
        raw = input("Restore-Version auswaehlen [1], Pfad eingeben oder q fuer Abbruch: ").strip()
        selected = raw or "1"
        if selected.lower() in {"q", "quit", "abbruch"}:
            raise core.SafetyError("Restore selection aborted.")
        if selected.isdigit():
            index = int(selected)
            if 1 <= index <= len(selected_points):
                return selected_points[index - 1].backup_dir
            print(f"Nummer ausserhalb der Liste: {index}")
            continue
        candidate = resolve_local_path(selected)
        if (candidate / "backup_manifest.json").exists():
            return candidate
        print("Bitte eine Nummer aus der Liste oder einen Backup-Ordner eingeben.")


def backup_source_vm_name(backup_dir: Path) -> str:
    for filename in ("vm_metadata.json", "backup_manifest.json"):
        path = backup_dir / filename
        if not path.exists():
            continue
        try:
            data = read_json(path)
        except Exception:
            continue
        value = data.get("name") or data.get("target_vm") or (data.get("vm_info") or {}).get("name")
        if value:
            return str(value)
    return "RestoredVM"


def prompt_new_vm_name(backup_dir: Path) -> str:
    if not sys.stdin.isatty():
        raise core.SafetyError("Missing --new-name in non-interactive mode")
    default_new_name = f"{backup_source_vm_name(backup_dir)}_Restore"
    raw = input(f"Neuer VM-Name [{default_new_name}]: ").strip()
    return raw or default_new_name


def validate_vm_display_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise core.SafetyError("New VM name is empty")
    if len(cleaned) > 80:
        raise core.SafetyError("New VM name is too long; use 80 characters or less")
    if any(char in cleaned for char in "/\\[]"):
        raise core.SafetyError("New VM name must not contain /, \\, [ or ]")
    return cleaned


def validate_datastore_folder_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise core.SafetyError("Datastore folder name is empty")
    if len(cleaned) > 80:
        raise core.SafetyError("Datastore folder name is too long; use 80 characters or less")
    if any(char in cleaned for char in "/\\[]"):
        raise core.SafetyError("Datastore folder name must not contain /, \\, [ or ]")
    return cleaned


def alternate_datastore_folder_names(base_name: str, max_attempts: int = 99) -> Iterable[str]:
    base = validate_datastore_folder_name(base_name)
    for number in range(2, max_attempts + 1):
        yield f"{base}_{number}"


def validate_mac_address(value: str) -> str:
    mac = (value or "").strip().lower()
    if not re.match(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", mac):
        raise core.SafetyError(f"Invalid MAC address in backup metadata: {value!r}")
    first_octet = int(mac.split(":", 1)[0], 16)
    if first_octet & 1:
        raise core.SafetyError(f"Invalid multicast MAC address in backup metadata: {value!r}")
    return mac


def source_mac_address(artifacts: BackupArtifacts) -> str:
    for nic in artifacts.vm_info.get("nics", []) or []:
        mac = str(nic.get("mac_address") or "").strip()
        if mac:
            return validate_mac_address(mac)
    return ""


def resolve_mac_address_mode(args: argparse.Namespace, artifacts: BackupArtifacts) -> tuple[str, str]:
    if args.preserve_mac and args.generated_mac:
        raise core.SafetyError("Use either --preserve-mac or --generated-mac, not both")
    requested = args.mac_address_mode
    if args.preserve_mac:
        requested = "preserve"
    if args.generated_mac:
        requested = "generated"

    backup_mac = source_mac_address(artifacts)
    if args.no_network:
        if requested == "preserve":
            raise core.SafetyError("Cannot preserve MAC address when --no-network is used")
        return "generated", ""
    if requested == "preserve":
        if not backup_mac:
            raise core.SafetyError(
                "Cannot preserve MAC address: backup metadata has no NIC MAC. "
                "Create a new backup with the updated tool first."
            )
        return "preserve", backup_mac
    if requested == "generated":
        return "generated", ""

    if args.yes or args.dry_run or not sys.stdin.isatty() or not backup_mac:
        return "generated", ""

    print()
    print(f"Backup-MAC gefunden: {backup_mac}")
    print("Nur beibehalten, wenn die Original-VM ausgeschaltet bleibt oder geloescht wird.")
    raw = input("MAC-Adresse aus Backup beibehalten? [j/N]: ").strip().lower()
    if raw in {"j", "ja", "y", "yes"}:
        return "preserve", backup_mac
    return "generated", ""


def parse_vmdk_extent_files_from_text(descriptor: str) -> List[str]:
    return core.parse_vmdk_extent_files(descriptor)


def parse_vmdk_change_track_files(descriptor: str) -> List[str]:
    return re.findall(r'changeTrackPath\s*=\s*"([^"]+)"', descriptor)


def sanitize_vmdk_descriptor_for_import(descriptor: str) -> str:
    lines = []
    for line in descriptor.splitlines():
        stripped = line.strip()
        if stripped == "# Change Tracking File":
            continue
        if re.match(r'^changeTrackPath\s*=\s*"[^"]+"\s*$', stripped):
            continue
        lines.append(line)
    sanitized = "\n".join(lines)
    if descriptor.endswith("\n"):
        sanitized += "\n"
    return sanitized


def is_vmdk_descriptor(path: Path) -> bool:
    if path.stat().st_size > 2 * 1024 * 1024:
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except UnicodeDecodeError:
        return False
    return "# Disk DescriptorFile" in text and bool(parse_vmdk_extent_files_from_text(text))


def find_file_case_sensitive(backup_dir: Path, name: str, hydrate_delta: bool = False) -> Optional[Path]:
    path = backup_dir / name
    if path.exists():
        return path
    basename = Path(name).name
    matches = [candidate for candidate in backup_dir.iterdir() if candidate.name == basename]
    if len(matches) == 1:
        return matches[0]
    cbt_materialized = vddk_cbt.find_materialized_file(backup_dir, basename)
    if cbt_materialized is not None:
        return cbt_materialized
    if hydrate_delta and delta_storage.has_delta_file(backup_dir, basename):
        return delta_storage.materialize_delta_file(backup_dir, basename)
    return None


def backup_file_available(backup_dir: Path, name: str) -> bool:
    return (
        find_file_case_sensitive(backup_dir, name) is not None
        or delta_storage.has_delta_file(backup_dir, Path(name).name)
    )


def backup_file_logical_size(backup_dir: Path, name: str) -> int:
    path = find_file_case_sensitive(backup_dir, name)
    if path is not None:
        return path.stat().st_size
    delta_size = delta_storage.delta_file_size(backup_dir, Path(name).name)
    if delta_size is None:
        raise FileNotFoundError(f"Backup file not found locally or in delta storage: {name}")
    return delta_size


def verify_backup_files(backup_dir: Path, manifest: Dict[str, Any], skip_hash: bool = False) -> List[str]:
    errors: List[str] = []
    for item in manifest.get("files", []):
        name = item.get("name", "")
        if not name:
            errors.append("Manifest file entry without name")
            continue
        item_errors = delta_storage.verify_manifest_item(backup_dir, item, skip_hash=skip_hash)
        if item_errors:
            errors.extend(item_errors)
    if manifest.get("cbt_storage"):
        errors.extend(vddk_cbt.verify_cbt_backup(backup_dir, skip_hash=skip_hash))
    return errors


def parse_ovf(backup_dir: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "guest_id": "otherGuest64",
        "firmware": "",
        "secure_boot": False,
        "network_name": "VM Network",
        "scsi_subtype": "VirtualSCSI",
        "nic_subtype": "VmxNet3",
        "virtual_hw_version": "",
    }

    ovf_name = (manifest.get("ovf_descriptor") or {}).get("file") or ""
    ovf_path = backup_dir / ovf_name if ovf_name else None
    if not ovf_path or not ovf_path.exists():
        return result

    root = ElementTree.fromstring(ovf_path.read_text(encoding="utf-8"))
    os_section = root.find(f".//{ns('OperatingSystemSection')}")
    if os_section is not None:
        result["guest_id"] = os_section.attrib.get(ns("osType", VMW_NS), result["guest_id"])

    system_type = root.find(f".//{ns('VirtualSystemType', VSSD_NS)}")
    if system_type is not None and system_type.text:
        result["virtual_hw_version"] = system_type.text.strip()

    network = root.find(f".//{ns('Network')}")
    if network is not None:
        result["network_name"] = network.attrib.get(ns("name"), result["network_name"])

    for item in root.findall(f".//{ns('Item')}"):
        resource_type = item.find(f"{ns('ResourceType', RASD_NS)}")
        subtype = item.find(f"{ns('ResourceSubType', RASD_NS)}")
        if resource_type is None or not resource_type.text:
            continue
        if resource_type.text.strip() == "6" and subtype is not None and subtype.text:
            result["scsi_subtype"] = subtype.text.strip()
        if resource_type.text.strip() == "10" and subtype is not None and subtype.text:
            result["nic_subtype"] = subtype.text.strip()

    for config in root.findall(f".//{ns('Config', VMW_NS)}"):
        key = config.attrib.get(ns("key", VMW_NS), "")
        value = config.attrib.get(ns("value", VMW_NS), "")
        if key == "firmware":
            result["firmware"] = value
        elif key == "bootOptions.efiSecureBootEnabled":
            result["secure_boot"] = value.lower() == "true"

    return result


def load_backup_artifacts(backup_dir: Path, skip_hash: bool = False) -> BackupArtifacts:
    backup_dir = backup_dir.expanduser().resolve()
    manifest_path = backup_dir / "backup_manifest.json"
    metadata_path = backup_dir / "vm_metadata.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing backup manifest: {manifest_path}")
    if not metadata_path.exists():
        raise FileNotFoundError(f"Missing VM metadata: {metadata_path}")

    manifest = read_json(manifest_path)
    vm_info = read_json(metadata_path)
    status = manifest.get("status", "")
    if status not in {"success", "success_rescued"}:
        raise core.SafetyError(f"Backup manifest is not successful: status={status!r}")

    errors = verify_backup_files(backup_dir, manifest, skip_hash=skip_hash)
    if errors:
        raise core.SafetyError("Backup verification failed: " + "; ".join(errors))

    descriptor_name = ""
    descriptor_text = ""
    if manifest.get("cbt_storage"):
        materialized = vddk_cbt.ensure_materialized_vmdks(backup_dir, manifest=manifest)
        disks = materialized.get("disks", [])
        if not disks:
            raise core.SafetyError("CBT backup materialization produced no VMDK disks")
        descriptor_path = Path(str(disks[0].get("descriptor_path") or ""))
        if not descriptor_path.exists():
            raise core.SafetyError(f"CBT materialized descriptor missing: {descriptor_path}")
        descriptor_name = descriptor_path.name
        descriptor_text = descriptor_path.read_text(encoding="utf-8", errors="ignore")

    for item in manifest.get("files", []):
        if descriptor_name:
            break
        if item.get("vmdk_role") == "descriptor":
            candidate = backup_dir / item["name"]
            if candidate.exists():
                descriptor_name = candidate.name
                descriptor_text = candidate.read_text(encoding="utf-8", errors="ignore")
                break

    if not descriptor_name:
        for candidate in sorted(backup_dir.glob("*.vmdk")):
            if is_vmdk_descriptor(candidate):
                descriptor_name = candidate.name
                descriptor_text = candidate.read_text(encoding="utf-8", errors="ignore")
                break

    if not descriptor_name:
        raise core.SafetyError(
            "No VMDK descriptor found. This restore script supports descriptor+extent VMDK backups."
        )

    extent_names = parse_vmdk_extent_files_from_text(descriptor_text)
    ctk_names = parse_vmdk_change_track_files(descriptor_text)
    missing = [name for name in extent_names if not backup_file_available(backup_dir, name)]
    if missing:
        raise core.SafetyError(f"Descriptor references missing extent file(s): {missing}")

    upload_files = [descriptor_name]
    upload_files.extend(extent_names)
    for candidate in backup_dir.glob("*.nvram"):
        upload_files.append(candidate.name)

    deduped: List[str] = []
    for name in upload_files:
        if name not in deduped:
            deduped.append(name)

    return BackupArtifacts(
        backup_dir=backup_dir,
        manifest=manifest,
        vm_info=vm_info,
        descriptor_name=descriptor_name,
        extent_names=extent_names,
        ctk_names=ctk_names,
        upload_files=deduped,
        ovf=parse_ovf(backup_dir, manifest),
    )


def all_datacenters(session: core.VSphereSession) -> List[vim.Datacenter]:
    view = session.content.viewManager.CreateContainerView(
        session.content.rootFolder, [vim.Datacenter], True
    )
    try:
        return list(view.view)
    finally:
        view.Destroy()


def select_datacenter(session: core.VSphereSession, name: str = "") -> vim.Datacenter:
    datacenters = all_datacenters(session)
    if name:
        matches = [dc for dc in datacenters if dc.name == name]
        if len(matches) != 1:
            raise core.SafetyError(f"Datacenter not found or not unique: {name}")
        return matches[0]
    if len(datacenters) != 1:
        names = ", ".join(dc.name for dc in datacenters)
        raise core.SafetyError(f"Select --datacenter explicitly. Available: {names}")
    return datacenters[0]


def find_datastore(datacenter: vim.Datacenter, name: str) -> vim.Datastore:
    matches = [datastore for datastore in datacenter.datastore if datastore.name == name]
    if len(matches) != 1:
        raise core.SafetyError(f"Datastore not found or not unique in {datacenter.name}: {name}")
    return matches[0]


def iter_resource_pools(pool: vim.ResourcePool) -> Iterable[vim.ResourcePool]:
    yield pool
    for child in getattr(pool, "resourcePool", []) or []:
        yield from iter_resource_pools(child)


def select_resource_pool(datacenter: vim.Datacenter, name: str = "") -> vim.ResourcePool:
    pools: List[vim.ResourcePool] = []
    for entity in getattr(datacenter.hostFolder, "childEntity", []) or []:
        root_pool = getattr(entity, "resourcePool", None)
        if root_pool is not None:
            pools.extend(iter_resource_pools(root_pool))
    if not pools:
        raise core.SafetyError(f"No resource pool found in datacenter {datacenter.name}")
    if name:
        matches = [pool for pool in pools if pool.name == name]
        if len(matches) != 1:
            raise core.SafetyError(f"Resource pool not found or not unique: {name}")
        return matches[0]
    return pools[0]


def find_network(datacenter: vim.Datacenter, name: str) -> vim.Network:
    matches = [network for network in datacenter.network if network.name == name]
    if len(matches) != 1:
        raise core.SafetyError(f"Network not found or not unique in {datacenter.name}: {name}")
    return matches[0]


def vm_name_exists(session: core.VSphereSession, name: str) -> bool:
    return any(vm.name == name for vm in session.all_vms())


def datastore_file_url(config: core.VSphereConfig, datacenter_name: str, datastore: str, path: str) -> str:
    return (
        f"https://{core.endpoint_netloc(config)}/folder/{quote(path)}"
        f"?dcPath={quote(datacenter_name)}&dsName={quote(datastore)}"
    )


def datastore_path_exists(
    session: core.VSphereSession,
    datacenter: vim.Datacenter,
    datastore: vim.Datastore,
    datastore_folder: str,
    config: core.VSphereConfig,
) -> bool:
    spec = vim.host.DatastoreBrowser.SearchSpec()
    try:
        task = datastore.browser.SearchDatastore_Task(
            datastorePath=core.datastore_path_join(datastore.name, datastore_folder),
            searchSpec=spec,
        )
        core.wait_for_task(task, f"Browse datastore folder {datastore_folder}", config.task_timeout_seconds)
        return True
    except Exception as exc:
        text = str(exc).lower()
        if "not found" in text or "does not exist" in text or "cannot complete" in text:
            return False
        return False


def first_available_datastore_folder(
    session: core.VSphereSession,
    datacenter: vim.Datacenter,
    datastore: vim.Datastore,
    base_folder: str,
    config: core.VSphereConfig,
) -> str:
    for candidate in alternate_datastore_folder_names(base_folder):
        if not datastore_path_exists(session, datacenter, datastore, candidate, config):
            return candidate
    raise core.SafetyError(f"No free datastore folder name found for base name: {base_folder}")


def upload_datastore_file(
    session: core.VSphereSession,
    config: core.VSphereConfig,
    datacenter_name: str,
    datastore_name: str,
    local_file: Path,
    datastore_folder: str,
    remote_name: str,
) -> None:
    url = datastore_file_url(config, datacenter_name, datastore_name, f"{datastore_folder}/{remote_name}")
    size = local_file.stat().st_size
    headers = {
        "Cookie": session.cookie,
        "Content-Type": "application/octet-stream",
        "Content-Length": str(size),
    }
    core.LOGGER.info("Uploading %s to [%s] %s/%s", local_file.name, datastore_name, datastore_folder, remote_name)
    with local_file.open("rb") as handle:
        response = requests.put(
            url,
            headers=headers,
            data=handle,
            verify=config.ssl_verify,
            timeout=(30, config.read_timeout_seconds),
        )
    response.raise_for_status()


def upload_datastore_bytes(
    session: core.VSphereSession,
    config: core.VSphereConfig,
    datacenter_name: str,
    datastore_name: str,
    data: bytes,
    datastore_folder: str,
    remote_name: str,
    source_label: str,
) -> None:
    url = datastore_file_url(config, datacenter_name, datastore_name, f"{datastore_folder}/{remote_name}")
    headers = {
        "Cookie": session.cookie,
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(data)),
    }
    core.LOGGER.info("Uploading %s to [%s] %s/%s", source_label, datastore_name, datastore_folder, remote_name)
    response = requests.put(
        url,
        headers=headers,
        data=data,
        verify=config.ssl_verify,
        timeout=(30, config.read_timeout_seconds),
    )
    response.raise_for_status()


def upload_vmdk_descriptor_for_import(
    session: core.VSphereSession,
    config: core.VSphereConfig,
    datacenter_name: str,
    datastore_name: str,
    local_file: Path,
    datastore_folder: str,
    remote_name: str,
) -> None:
    descriptor = local_file.read_text(encoding="utf-8", errors="ignore")
    sanitized = sanitize_vmdk_descriptor_for_import(descriptor)
    if sanitized == descriptor:
        upload_datastore_file(
            session=session,
            config=config,
            datacenter_name=datacenter_name,
            datastore_name=datastore_name,
            local_file=local_file,
            datastore_folder=datastore_folder,
            remote_name=remote_name,
        )
        return

    upload_datastore_bytes(
        session=session,
        config=config,
        datacenter_name=datacenter_name,
        datastore_name=datastore_name,
        data=sanitized.encode("utf-8"),
        datastore_folder=datastore_folder,
        remote_name=remote_name,
        source_label=f"sanitized {local_file.name}",
    )


def imported_disk_descriptor_name(new_name: str, artifacts: BackupArtifacts) -> str:
    base = core.safe_filename(new_name, "restored-vm")
    used = set(artifacts.upload_files)
    for suffix in ("", "-imported", "-disk0"):
        candidate = f"{base}{suffix}.vmdk"
        if candidate not in used:
            return candidate
    index = 1
    while True:
        candidate = f"{base}-imported-{index}.vmdk"
        if candidate not in used:
            return candidate
        index += 1


def adapter_type_for_import(artifacts: BackupArtifacts) -> str:
    subtype = str(artifacts.ovf.get("scsi_subtype") or "").lower()
    if "para" in subtype or "virtualscsi" in subtype or "pvscsi" in subtype:
        return "lsiLogic"
    if "buslogic" in subtype:
        return "busLogic"
    if "lsilogic" in subtype or "lsi logic" in subtype:
        return "lsiLogic"

    descriptor_path = find_file_case_sensitive(artifacts.backup_dir, artifacts.descriptor_name, hydrate_delta=True)
    if descriptor_path is None:
        raise FileNotFoundError(f"VMDK descriptor not found: {artifacts.descriptor_name}")
    descriptor = descriptor_path.read_text(encoding="utf-8", errors="ignore")
    return core.parse_vmdk_adapter_type(descriptor)


def import_uploaded_vmdk(
    session: core.VSphereSession,
    config: core.VSphereConfig,
    artifacts: BackupArtifacts,
    target: RestoreTarget,
    new_name: str,
) -> str:
    source_path = core.datastore_path_join(
        target.datastore_name,
        target.datastore_folder,
        artifacts.descriptor_name,
    )
    imported_name = imported_disk_descriptor_name(new_name, artifacts)
    imported_path = core.datastore_path_join(
        target.datastore_name,
        target.datastore_folder,
        imported_name,
    )

    spec = vim.VirtualDiskManager.VirtualDiskSpec()
    spec.diskType = vim.VirtualDiskManager.VirtualDiskType.thin
    spec.adapterType = adapter_type_for_import(artifacts)

    core.LOGGER.info("Importing uploaded VMDK %s to %s", source_path, imported_path)
    task = session.content.virtualDiskManager.CopyVirtualDisk_Task(
        sourceName=source_path,
        sourceDatacenter=target.datacenter,
        destName=imported_path,
        destDatacenter=target.datacenter,
        destSpec=spec,
        force=False,
    )
    core.wait_for_task(task, f"Import VMDK {source_path}", config.task_timeout_seconds)
    return imported_name


def cleanup_uploaded_import_sources(
    session: core.VSphereSession,
    config: core.VSphereConfig,
    artifacts: BackupArtifacts,
    target: RestoreTarget,
    imported_name: str,
) -> None:
    source_names = [artifacts.descriptor_name, *artifacts.extent_names, *artifacts.ctk_names]
    for name in source_names:
        if name == imported_name or name not in artifacts.upload_files:
            continue
        datastore_path = core.datastore_path_join(target.datastore_name, target.datastore_folder, name)
        try:
            core.LOGGER.info("Deleting uploaded import source %s", datastore_path)
            core.delete_datastore_path(session, target.datacenter, datastore_path, config)
        except Exception as exc:
            core.LOGGER.warning("Could not delete uploaded import source %s: %s", datastore_path, exc)


def build_scsi_controller(subtype: str) -> vim.vm.device.VirtualSCSIController:
    text = subtype.lower()
    if "para" in text or "virtualscsi" in text or "pvscsi" in text:
        controller = vim.vm.device.ParaVirtualSCSIController()
    elif "buslogic" in text:
        controller = vim.vm.device.VirtualBusLogicController()
    else:
        controller = vim.vm.device.VirtualLsiLogicController()
    controller.key = -100
    controller.busNumber = 0
    controller.sharedBus = vim.vm.device.VirtualSCSIController.Sharing.noSharing
    return controller


def build_nic(subtype: str) -> vim.vm.device.VirtualEthernetCard:
    text = subtype.lower()
    if "e1000e" in text:
        return vim.vm.device.VirtualE1000e()
    if "e1000" in text:
        return vim.vm.device.VirtualE1000()
    return vim.vm.device.VirtualVmxnet3()


def build_vm_config_spec(
    new_name: str,
    artifacts: BackupArtifacts,
    target: RestoreTarget,
    connect_network: bool,
    add_network: bool,
    use_backup_nvram: bool,
    disk_descriptor_name: str,
    mac_address_mode: str,
    preserved_mac_address: str,
) -> vim.vm.ConfigSpec:
    vm_info = artifacts.vm_info
    ovf = artifacts.ovf

    spec = vim.vm.ConfigSpec()
    spec.name = new_name
    spec.guestId = ovf.get("guest_id") or "otherGuest64"
    spec.numCPUs = int(vm_info.get("num_cpu") or 1)
    spec.memoryMB = int(vm_info.get("memory_mb") or 1024)
    vmx_name = f"{core.safe_filename(new_name, 'restored-vm')}.vmx"
    spec.files = vim.vm.FileInfo(
        vmPathName=core.datastore_path_join(target.datastore_name, target.datastore_folder, vmx_name)
    )
    if ovf.get("virtual_hw_version"):
        spec.version = ovf["virtual_hw_version"]
    if ovf.get("firmware") in {"efi", "bios"}:
        spec.firmware = ovf["firmware"]
    if ovf.get("firmware") == "efi":
        boot_options = vim.vm.BootOptions()
        boot_options.efiSecureBootEnabled = bool(ovf.get("secure_boot"))
        spec.bootOptions = boot_options

    extra_config = [
        vim.option.OptionValue(key="uuid.action", value="create"),
    ]
    if add_network:
        extra_config.append(
            vim.option.OptionValue(
                key="ethernet0.addressType",
                value="manual" if mac_address_mode == "preserve" else "generated",
            )
        )
    nvram_files = [name for name in artifacts.upload_files if name.lower().endswith(".nvram")]
    if use_backup_nvram and nvram_files:
        extra_config.append(vim.option.OptionValue(key="nvram", value=nvram_files[0]))
    spec.extraConfig = extra_config

    device_changes: List[vim.vm.device.VirtualDeviceSpec] = []

    scsi = build_scsi_controller(str(ovf.get("scsi_subtype") or "VirtualSCSI"))
    scsi_spec = vim.vm.device.VirtualDeviceSpec()
    scsi_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
    scsi_spec.device = scsi
    device_changes.append(scsi_spec)

    disk = vim.vm.device.VirtualDisk()
    disk.key = -101
    disk.controllerKey = scsi.key
    disk.unitNumber = 0
    disk.capacityInKB = int(vm_info.get("disks", [{}])[0].get("capacity_bytes") or 0) // 1024
    if disk.capacityInKB <= 0:
        disk.capacityInKB = 1
    disk.backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
    disk.backing.fileName = core.datastore_path_join(
        target.datastore_name,
        target.datastore_folder,
        disk_descriptor_name,
    )
    disk.backing.datastore = target.datastore
    disk.backing.diskMode = "persistent"
    disk_spec = vim.vm.device.VirtualDeviceSpec()
    disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
    disk_spec.device = disk
    device_changes.append(disk_spec)

    if add_network and target.network is not None:
        nic = build_nic(str(ovf.get("nic_subtype") or "VmxNet3"))
        nic.key = -102
        if mac_address_mode == "preserve":
            nic.addressType = "manual"
            nic.macAddress = preserved_mac_address
        else:
            nic.addressType = "generated"
        nic.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
        nic.backing.network = target.network
        nic.backing.deviceName = target.network_name
        nic.connectable = vim.vm.device.VirtualDevice.ConnectInfo()
        nic.connectable.allowGuestControl = True
        nic.connectable.connected = False
        nic.connectable.startConnected = bool(connect_network)
        nic_spec = vim.vm.device.VirtualDeviceSpec()
        nic_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        nic_spec.device = nic
        device_changes.append(nic_spec)

    spec.deviceChange = device_changes
    return spec


def print_restore_checks(checks: List[Dict[str, Any]]) -> None:
    width = max((len(item["name"]) for item in checks), default=10)
    for item in checks:
        state = "PASS" if item["ok"] else "FAIL"
        print(f"{state:<4} {item['name']:<{width}} {item['detail']}")


def resolve_restore_target(
    session: core.VSphereSession,
    config: core.VSphereConfig,
    artifacts: BackupArtifacts,
    args: argparse.Namespace,
    new_name: str,
) -> tuple[RestoreTarget, List[Dict[str, Any]]]:
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, severity: str = "fatal") -> None:
        checks.append({"name": name, "ok": bool(ok), "severity": severity, "detail": detail})

    datacenter = select_datacenter(session, args.datacenter or "")
    datastore_name = args.datastore or ""
    if not datastore_name:
        datastore_name = (artifacts.vm_info.get("disks") or [{}])[0].get("datastore") or ""
    if not datastore_name:
        raise core.SafetyError("Select --datastore explicitly; backup metadata has no datastore")
    datastore = find_datastore(datacenter, datastore_name)
    resource_pool = select_resource_pool(datacenter, args.resource_pool or "")
    explicit_folder_name = bool(args.folder_name)
    folder_name = validate_datastore_folder_name(
        args.folder_name or core.safe_filename(new_name, "restored-vm")
    )
    network_name = args.network or artifacts.ovf.get("network_name") or "VM Network"
    network = None if args.no_network else find_network(datacenter, network_name)

    add("backup_dir", artifacts.backup_dir.exists(), str(artifacts.backup_dir))
    add("backup_target", True, f"source_vm={artifacts.manifest.get('target_vm')}")
    add("vmdk_descriptor", bool(artifacts.descriptor_name), artifacts.descriptor_name)
    add("vmdk_extents", bool(artifacts.extent_names), ", ".join(artifacts.extent_names) or "none")
    add("vmdk_import", True, "backup VMDK will be converted to a thin vSphere disk")
    add("target_vm_absent", not vm_name_exists(session, new_name), f"name={new_name}")
    folder_exists = datastore_path_exists(session, datacenter, datastore, folder_name, config)
    if (
        folder_exists
        and not explicit_folder_name
        and not args.yes
        and not args.dry_run
        and sys.stdin.isatty()
    ):
        suggested_folder = first_available_datastore_folder(
            session,
            datacenter,
            datastore,
            folder_name,
            config,
        )
        print()
        existing_path = core.datastore_path_join(datastore_name, folder_name)
        print(f"Datastore-Ordner ist bereits vorhanden: {existing_path}")
        raw = input(
            f"Anderen Datastore-Ordner verwenden [{suggested_folder}], q fuer Abbruch: "
        ).strip()
        if raw.lower() in {"q", "quit", "abbruch"}:
            raise core.SafetyError("Restore selection aborted.")
        folder_name = validate_datastore_folder_name(raw or suggested_folder)
        folder_exists = datastore_path_exists(session, datacenter, datastore, folder_name, config)
    add("target_folder_absent", not folder_exists, core.datastore_path_join(datastore_name, folder_name))
    upload_bytes = sum(backup_file_logical_size(artifacts.backup_dir, name) for name in artifacts.upload_files)
    free_bytes = int(datastore.summary.freeSpace)
    required_bytes = upload_bytes * 2 + 1024**3
    add(
        "datastore_free_space",
        free_bytes > required_bytes,
        (
            f"datastore={datastore_name}, free_gb={round(free_bytes / 1024**3, 2)}, "
            f"upload_gb={round(upload_bytes / 1024**3, 2)}, "
            f"import_staging_gb={round(required_bytes / 1024**3, 2)}"
        ),
    )
    if args.no_network:
        add("network", True, "no network adapter requested", severity="info")
    else:
        add("network", network is not None, f"name={network_name}, start_connected={args.connect_network}")
    add("power_on", True, f"power_on={args.power_on}")

    return (
        RestoreTarget(
            datacenter=datacenter,
            datastore=datastore,
            datastore_name=datastore_name,
            datastore_folder=folder_name,
            resource_pool=resource_pool,
            vm_folder=datacenter.vmFolder,
            network=network,
            network_name=network_name,
        ),
        checks,
    )


def checks_passed(checks: Iterable[Dict[str, Any]]) -> bool:
    return all(item["ok"] or item.get("severity") != "fatal" for item in checks)


def confirm_restore(new_name: str, args: argparse.Namespace) -> None:
    if args.yes or args.dry_run:
        return
    print()
    print(f"Restore target VM name: {new_name}")
    print("The restored VM will be created powered off.")
    raw = input(f"Type the new VM name exactly to continue ({new_name}): ").strip()
    if raw != new_name:
        raise core.SafetyError("Restore confirmation did not match new VM name")


def cleanup_failed_folder(
    session: core.VSphereSession,
    target: RestoreTarget,
    config: core.VSphereConfig,
) -> None:
    datastore_path = core.datastore_path_join(target.datastore_name, target.datastore_folder)
    try:
        core.LOGGER.info("Deleting failed restore datastore folder %s", datastore_path)
        core.delete_datastore_path(session, target.datacenter, datastore_path, config)
    except Exception as exc:
        core.LOGGER.warning("Could not delete failed restore folder %s: %s", datastore_path, exc)


def run_restore(
    session: core.VSphereSession,
    config: core.VSphereConfig,
    artifacts: BackupArtifacts,
    target: RestoreTarget,
    args: argparse.Namespace,
    new_name: str,
) -> vim.VirtualMachine:
    target_path = core.datastore_path_join(target.datastore_name, target.datastore_folder)
    core.LOGGER.info("Creating datastore folder %s", target_path)
    session.content.fileManager.MakeDirectory(
        name=target_path,
        datacenter=target.datacenter,
        createParentDirectories=False,
    )

    try:
        for name in artifacts.upload_files:
            local_path = find_file_case_sensitive(artifacts.backup_dir, name, hydrate_delta=True)
            if local_path is None:
                raise core.SafetyError(f"Upload file disappeared from backup: {name}")
            if name == artifacts.descriptor_name:
                upload_vmdk_descriptor_for_import(
                    session=session,
                    config=config,
                    datacenter_name=target.datacenter.name,
                    datastore_name=target.datastore_name,
                    local_file=local_path,
                    datastore_folder=target.datastore_folder,
                    remote_name=name,
                )
            else:
                upload_datastore_file(
                    session=session,
                    config=config,
                    datacenter_name=target.datacenter.name,
                    datastore_name=target.datastore_name,
                    local_file=local_path,
                    datastore_folder=target.datastore_folder,
                    remote_name=name,
                )

        imported_disk_name = import_uploaded_vmdk(session, config, artifacts, target, new_name)
        cleanup_uploaded_import_sources(session, config, artifacts, target, imported_disk_name)

        spec = build_vm_config_spec(
            new_name=new_name,
            artifacts=artifacts,
            target=target,
            connect_network=args.connect_network,
            add_network=not args.no_network,
            use_backup_nvram=args.use_backup_nvram,
            disk_descriptor_name=imported_disk_name,
            mac_address_mode=args.resolved_mac_address_mode,
            preserved_mac_address=args.resolved_mac_address,
        )
        core.LOGGER.info("Creating restored VM %s", new_name)
        task = target.vm_folder.CreateVM_Task(config=spec, pool=target.resource_pool)
        vm = core.wait_for_task(task, f"Create VM {new_name}", config.task_timeout_seconds)
        if vm is None:
            matches = [candidate for candidate in session.all_vms() if candidate.name == new_name]
            if len(matches) != 1:
                raise RuntimeError(f"VM was created but could not be located by name: {new_name}")
            vm = matches[0]

        if args.power_on:
            core.LOGGER.info("Powering on restored VM %s", new_name)
            power_task = vm.PowerOnVM_Task()
            core.wait_for_task(power_task, f"Power on VM {new_name}", config.task_timeout_seconds)

        return vm
    except Exception:
        if not args.keep_failed:
            cleanup_failed_folder(session, target, config)
        raise
    finally:
        delta_storage.cleanup_hydrated_files(artifacts.backup_dir)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Restore one verified vSphere backup under a new VM name")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"Path to credentials.env/config.env. Default: {DEFAULT_CONFIG}")
    parser.add_argument("--backup-root", default="backups", help="Root directory for interactive restore-point discovery")
    parser.add_argument("--list-backups", action="store_true", help="List available restore points and exit")
    parser.add_argument(
        "--list-backup-paths",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--backup-dir", help="Backup directory containing backup_manifest.json")
    parser.add_argument("--new-name", help="New VM display name to create")
    parser.add_argument("--folder-name", default="", help="Datastore folder name. Default: sanitized --new-name")
    parser.add_argument("--datacenter", default="", help="Datacenter name. Required only if more than one exists")
    parser.add_argument("--datastore", default="", help="Target datastore. Default: source datastore from metadata")
    parser.add_argument("--resource-pool", default="", help="Target resource pool. Default: first pool")
    parser.add_argument("--network", default="", help="Network name. Default: network from OVF, usually VM Network")
    parser.add_argument("--no-network", action="store_true", help="Create the VM without a network adapter")
    parser.add_argument("--connect-network", action="store_true", help="Start the NIC connected")
    parser.add_argument(
        "--mac-address-mode",
        choices=["ask", "generated", "preserve"],
        default="ask",
        help="Default ask: prompt whether to preserve the backup MAC when metadata is available.",
    )
    parser.add_argument("--preserve-mac", action="store_true", help="Use the NIC MAC address saved in backup metadata")
    parser.add_argument("--generated-mac", action="store_true", help="Force a newly generated NIC MAC address")
    parser.add_argument("--use-backup-nvram", action="store_true", help="Reference the backed-up NVRAM file")
    parser.add_argument("--power-on", action="store_true", help="Power on the restored VM after creation")
    parser.add_argument("--dry-run", action="store_true", help="Run all validation checks without writing to vSphere")
    parser.add_argument("--yes", action="store_true", help="Skip typed confirmation")
    parser.add_argument("--skip-hash-check", action="store_true", help="Only check file sizes, not SHA256 hashes")
    parser.add_argument("--keep-failed", action="store_true", help="Keep uploaded datastore folder after failure")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    core.setup_logging(verbose=args.verbose)

    try:
        backup_root = resolve_local_path(args.backup_root)
        if args.list_backups or args.list_backup_paths:
            points = discover_restore_points(backup_root)
            if args.list_backup_paths:
                for point in points:
                    print(display_backup_path(point.backup_dir))
            else:
                print(format_restore_point_table(points))
            return 0 if points else 2

        backup_dir = resolve_local_path(args.backup_dir) if args.backup_dir else select_restore_point_interactive(backup_root)
        new_name = validate_vm_display_name(args.new_name or prompt_new_vm_name(backup_dir))
        artifacts = load_backup_artifacts(backup_dir, skip_hash=args.skip_hash_check)
        config = core.load_config(args.config)
        with core.VSphereSession(config) as session:
            target, checks = resolve_restore_target(session, config, artifacts, args, new_name)
            mac_mode, mac_address = resolve_mac_address_mode(args, artifacts)
            args.resolved_mac_address_mode = mac_mode
            args.resolved_mac_address = mac_address
            checks.append(
                {
                    "name": "mac_address",
                    "ok": True,
                    "severity": "info",
                    "detail": (
                        f"mode=preserve, mac={mac_address}"
                        if mac_mode == "preserve"
                        else "mode=generated"
                    ),
                }
            )
            print_restore_checks(checks)
            if not checks_passed(checks):
                raise core.SafetyError("Restore preflight checks failed")
            if args.dry_run:
                print(f"Dry-run OK: restore would create VM {new_name!r} in {core.datastore_path_join(target.datastore_name, target.datastore_folder)}")
                return 0
            confirm_restore(new_name, args)
            vm = run_restore(session, config, artifacts, target, args, new_name)
            vm_info = core.collect_vm_info(vm)

        print(
            json.dumps(
                {
                    "status": "success",
                    "restored_vm": vm_info,
                    "network_start_connected": bool(args.connect_network),
                    "powered_on_requested": bool(args.power_on),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except core.SafetyError as exc:
        core.LOGGER.error("Blocked by safety rule: %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 10
    except KeyboardInterrupt:
        core.LOGGER.error(core.INTERRUPTED_MESSAGE)
        print(f"ERROR: {core.INTERRUPTED_MESSAGE}", file=sys.stderr)
        return 130
    except Exception as exc:
        core.LOGGER.error("Restore command failed: %s", exc, exc_info=args.verbose)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
