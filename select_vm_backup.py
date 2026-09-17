#!/usr/bin/env python3
"""Interactive vSphere VM backup selector.

This entry point reads the vCenter inventory, lets the operator select exactly
one VM or an explicit selection file, then runs the same guarded export flow.
It never backs up all VMs implicitly.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pyVmomi import vmodl

import safe_vsphere_backup as core
import delta_storage
import vddk_cbt


VMRecord = Tuple[Any, Dict[str, Any]]
FREE_SPACE_GROWTH_RATIO = 0.10
FREE_SPACE_MIN_MARGIN_GB = 2.0
SELECTION_MATCH_KEYS = ("moref", "instance_uuid", "bios_uuid", "name")
LOCAL_DELTA_MODE = "local-delta"
LEGACY_DELTA_MODE = "delta"
CBT_DELTA_MODE = "cbt"
DEFAULT_BACKUP_MODE = CBT_DELTA_MODE


def normalize_backup_mode(mode: str) -> str:
    if mode == LEGACY_DELTA_MODE:
        return LOCAL_DELTA_MODE
    return mode


def delta_transfer_mode(requested_mode: str, chain_slot: Dict[str, Any]) -> str:
    if chain_slot["kind"] != "delta":
        return "full"
    if normalize_backup_mode(requested_mode) == CBT_DELTA_MODE:
        return CBT_DELTA_MODE
    return LOCAL_DELTA_MODE


def vm_backup_set_dir(config: core.VSphereConfig, vm_info: Dict[str, Any]) -> Path:
    return config.output_dir / core.safe_filename(str(vm_info.get("name") or ""), "vm")


def parse_chain_name(path: Path) -> Tuple[str, int]:
    stem = path.name
    for suffix in (".inprogress", ".failed"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if "_" not in stem:
        return "", 0
    kind, raw_number = stem.rsplit("_", 1)
    if kind not in {"full", "delta"} or not raw_number.isdigit():
        return "", 0
    return kind, int(raw_number)


def backup_chain_info(backup_dir: Path) -> Dict[str, Any]:
    manifest_file = backup_dir / "backup_manifest.json"
    manifest: Dict[str, Any] = {}
    if manifest_file.exists():
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}

    chain = manifest.get("backup_chain") or {}
    kind = str(chain.get("kind") or "")
    sequence = int(chain.get("sequence") or 0)
    if not kind or not sequence:
        parsed_kind, parsed_sequence = parse_chain_name(backup_dir)
        kind = kind or parsed_kind
        sequence = sequence or parsed_sequence
    if not kind:
        kind = "delta" if manifest.get("delta_storage") else "full"
    return {"kind": kind, "sequence": sequence, "manifest": manifest}


def previous_backup_manifest(chain_slot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not chain_slot:
        return {}
    previous = str(chain_slot.get("previous_dir") or "")
    if not previous:
        return {}
    manifest_file = Path(previous) / "backup_manifest.json"
    if not manifest_file.exists():
        return {}
    try:
        return json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def next_backup_chain_slot(
    config: core.VSphereConfig,
    vm_info: Dict[str, Any],
    requested_mode: str,
) -> Dict[str, Any]:
    vm_dir = vm_backup_set_dir(config, vm_info)
    existing = core.successful_backup_dirs_for_vm(config.output_dir, vm_info)
    existing_info = [(path, backup_chain_info(path)) for path in existing]
    max_sequence = 0
    previous_dir = ""
    if existing_info:
        indexed = []
        for index, (path, info) in enumerate(sorted(existing_info, key=lambda item: item[0].stat().st_mtime), start=1):
            sequence = int(info.get("sequence") or index)
            max_sequence = max(max_sequence, sequence)
            indexed.append((path, info, sequence))
        previous_dir = str(indexed[-1][0])
    if vm_dir.exists():
        for child in vm_dir.iterdir():
            if not child.is_dir():
                continue
            _, sequence = parse_chain_name(child)
            max_sequence = max(max_sequence, sequence)

    if not existing_info:
        kind = "full"
        sequence = max_sequence + 1 if max_sequence else 1
    elif requested_mode == "full":
        kind = "full"
        sequence = max_sequence + 1
    elif normalize_backup_mode(requested_mode) == CBT_DELTA_MODE:
        latest_manifest = indexed[-1][1].get("manifest") if existing_info else {}
        latest_cbt = (latest_manifest or {}).get("cbt_state") or {}
        kind = "delta" if latest_cbt.get("valid") else "full"
        sequence = max_sequence + 1
    else:
        kind = "delta"
        sequence = max_sequence + 1

    run_name = f"{kind}_{sequence:04d}"
    return {
        "vm_dir": vm_dir,
        "kind": kind,
        "sequence": sequence,
        "run_name": run_name,
        "final_dir": vm_dir / run_name,
        "staging_dir": vm_dir / f"{run_name}.inprogress",
        "failed_dir": vm_dir / f"{run_name}.failed",
        "previous_dir": previous_dir,
        "existing_count": len(existing_info),
    }


@contextmanager
def target_file_context(vm_info: Dict[str, Any]):
    """Temporarily set file-oriented target names used by shared helpers."""
    old_target = core.TARGET_VM_NAME
    old_prefix = core.LIVE_SNAPSHOT_PREFIX
    file_base = vm_file_base(vm_info)
    slug = core.safe_filename(vm_info["name"], "vm")
    core.TARGET_VM_NAME = file_base
    core.LIVE_SNAPSHOT_PREFIX = f"{slug}-live-backup"
    try:
        yield slug, file_base
    finally:
        core.TARGET_VM_NAME = old_target
        core.LIVE_SNAPSHOT_PREFIX = old_prefix


def vm_file_base(vm_info: Dict[str, Any]) -> str:
    try:
        _, vmx_path = core.parse_datastore_path(vm_info.get("vmx_path", ""))
        return core.safe_filename(Path(vmx_path).stem, "vm")
    except Exception:
        return core.safe_filename(vm_info.get("name", ""), "vm")


def disk_text(vm_info: Dict[str, Any]) -> str:
    disks = vm_info.get("disks", [])
    if not disks:
        return "-"
    return ", ".join(f"{disk['label']} ({disk['capacity_gb']} GB)" for disk in disks)


def inventory_records(session: core.VSphereSession) -> List[VMRecord]:
    records = [(vm, core.collect_vm_info(vm)) for vm in session.all_vms()]
    records.sort(key=lambda item: (item[1]["name"].lower(), item[1]["moref"]))
    return records


def selection_file_header() -> str:
    return "\n".join(
        [
            "# vSphere VM backup selection file",
            "# Remove '# ' at the start of a VM line to include it in the next list backup.",
            "# Keep moref or instance_uuid when possible; this detects renamed or duplicated VMs.",
            "# A manually added plain VM name is also accepted, one VM per line.",
            "# Examples:",
            "# name='Example VM' moref='17' instance_uuid='uuid-here'",
            "# Example VM",
            "",
        ]
    )


def selection_file_line(vm_info: Dict[str, Any], enabled: bool = False) -> str:
    values = {
        "name": str(vm_info.get("name") or ""),
        "moref": str(vm_info.get("moref") or ""),
        "instance_uuid": str(vm_info.get("instance_uuid") or ""),
        "bios_uuid": str(vm_info.get("bios_uuid") or ""),
        "power": str(vm_info.get("power_state") or ""),
        "cpu": str(vm_info.get("num_cpu") or ""),
        "ram_mb": str(vm_info.get("memory_mb") or ""),
        "disks": str(len(vm_info.get("disks") or [])),
    }
    fields = [f"{key}={shlex.quote(value)}" for key, value in values.items() if value]
    prefix = "" if enabled else "# "
    return prefix + " ".join(fields)


def parse_selection_numbers(raw_value: str, total: int) -> List[int]:
    raw_value = raw_value.strip()
    if not raw_value:
        return []
    indexes: List[int] = []
    seen = set()
    for part in raw_value.replace(",", " ").split():
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                raise core.SafetyError(f"Ungueltige Auswahl: {part!r}")
            start = int(start_text)
            end = int(end_text)
            if start > end:
                raise core.SafetyError(f"Ungueltiger Bereich: {part!r}")
            candidates = range(start, end + 1)
        else:
            if not part.isdigit():
                raise core.SafetyError(f"Ungueltige Auswahl: {part!r}")
            candidates = [int(part)]
        for index in candidates:
            if index < 1 or index > total:
                raise core.SafetyError(f"VM-Nummer ausserhalb der Liste: {index}")
            if index not in seen:
                indexes.append(index)
                seen.add(index)
    return indexes


def write_selection_file(
    path: Path,
    records: Sequence[VMRecord],
    overwrite: bool = False,
    enabled_indexes: Optional[Iterable[int]] = None,
) -> Path:
    path = path.expanduser().resolve()
    if path.exists() and not overwrite:
        raise FileExistsError(f"Selection file already exists: {path}. Use --force to overwrite it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    enabled = set(enabled_indexes or [])
    lines = [selection_file_header()]
    lines.extend(selection_file_line(info, enabled=index in enabled) for index, (_, info) in enumerate(records, start=1))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def parse_selection_file(path: Path) -> List[Dict[str, Any]]:
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Selection file not found: {path}")

    entries: List[Dict[str, Any]] = []
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        entry: Dict[str, Any] = {"line": line_no, "raw": raw_line}
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                raise core.SafetyError(f"{path}:{line_no}: invalid JSON selection line: {exc}") from exc
            if not isinstance(parsed, dict):
                raise core.SafetyError(f"{path}:{line_no}: JSON selection line must be an object")
            entry.update({str(key): str(value).strip() for key, value in parsed.items() if value is not None})
        elif "=" in line:
            try:
                tokens = shlex.split(line.replace("|", " "), comments=True)
            except ValueError as exc:
                raise core.SafetyError(f"{path}:{line_no}: invalid selection syntax: {exc}") from exc
            for token in tokens:
                if "=" not in token:
                    if "name" not in entry:
                        entry["name"] = token.strip()
                        continue
                    raise core.SafetyError(f"{path}:{line_no}: token without key=value: {token!r}")
                key, value = token.split("=", 1)
                key = key.strip().lower().replace("-", "_")
                if key == "uuid":
                    key = "instance_uuid"
                entry[key] = value.strip()
        else:
            entry["name"] = line

        if not any(str(entry.get(key) or "").strip() for key in SELECTION_MATCH_KEYS):
            raise core.SafetyError(f"{path}:{line_no}: selection line has no VM name, MoRef or UUID")
        entries.append(entry)

    return entries


def _matches_for_field(records: Sequence[VMRecord], key: str, value: str) -> List[VMRecord]:
    return [record for record in records if str(record[1].get(key) or "") == value]


def _entry_label(entry: Dict[str, Any]) -> str:
    for key in ("name", "moref", "instance_uuid", "bios_uuid"):
        value = str(entry.get(key) or "").strip()
        if value:
            return f"{key}={value!r}"
    return f"line={entry.get('line')}"


def resolve_selection_entries(
    records: Sequence[VMRecord],
    entries: Sequence[Dict[str, Any]],
) -> Tuple[List[VMRecord], List[str]]:
    resolved: List[VMRecord] = []
    errors: List[str] = []
    seen: Dict[str, int] = {}

    for entry in entries:
        provided = {
            key: str(entry.get(key) or "").strip()
            for key in SELECTION_MATCH_KEYS
            if str(entry.get(key) or "").strip()
        }
        line_no = entry.get("line", "?")
        anchor_key = next((key for key in ("moref", "instance_uuid", "bios_uuid", "name") if provided.get(key)), "")
        if not anchor_key:
            errors.append(f"line {line_no}: no usable VM identifier in selection line")
            continue

        matches = _matches_for_field(records, anchor_key, provided[anchor_key])
        if not matches:
            errors.append(f"line {line_no}: VM not found for {_entry_label(entry)}")
            continue
        if len(matches) > 1:
            ids = ", ".join(str(match[1].get("moref") or "?") for match in matches)
            errors.append(f"line {line_no}: VM selection is not unique for {anchor_key}={provided[anchor_key]!r}: {ids}")
            continue

        record = matches[0]
        info = record[1]
        mismatches = []
        for key, expected in provided.items():
            current = str(info.get(key) or "")
            if current != expected:
                mismatches.append(f"{key}: file={expected!r}, vsphere={current!r}")
        if mismatches:
            errors.append(f"line {line_no}: identifier mismatch for {_entry_label(entry)}; " + "; ".join(mismatches))
            continue

        identity = str(info.get("instance_uuid") or info.get("moref") or info.get("name") or "")
        if identity in seen:
            errors.append(f"line {line_no}: duplicate VM selection, already selected on line {seen[identity]}")
            continue
        seen[identity] = int(line_no) if isinstance(line_no, int) else 0
        resolved.append(record)

    return resolved, errors


def resolve_plan_record(records: Sequence[VMRecord], plan: Dict[str, Any]) -> VMRecord:
    """Resolve a planned VM against a current inventory snapshot."""
    vm_info = plan["vm_info"]
    entry: Dict[str, Any] = {"line": "current-backup"}
    for key in SELECTION_MATCH_KEYS:
        value = str(vm_info.get(key) or "").strip()
        if value:
            entry[key] = value

    selected, errors = resolve_selection_entries(records, [entry])
    if errors or len(selected) != 1:
        detail = "; ".join(errors) if errors else "selection did not resolve to exactly one VM"
        raise core.SafetyError(f"Selected VM could not be resolved in fresh vSphere session: {detail}")
    return selected[0]


def plan_requires_existing_backup(plan: Dict[str, Any]) -> bool:
    return any(check.get("name") == "existing_backup_baseline" for check in plan.get("checks", []))


def refresh_plan_for_session(
    session: core.VSphereSession,
    config: core.VSphereConfig,
    plan: Dict[str, Any],
    dynamic_free_space: bool,
) -> Tuple[Any, Dict[str, Any], List[Dict[str, Any]]]:
    records = inventory_records(session)
    vm, vm_info = resolve_plan_record(records, plan)
    chain_slot = plan["chain_slot"]
    checks = validate_selected_vm_target(
        vm_info,
        config,
        dynamic_free_space=dynamic_free_space,
        backup_mode=plan.get("delta_transfer_mode", LOCAL_DELTA_MODE) if chain_slot["kind"] == "delta" else chain_slot["kind"],
        chain_slot=chain_slot,
        transfer_mode=plan.get("delta_transfer_mode", LOCAL_DELTA_MODE),
        require_existing_backup=plan_requires_existing_backup(plan),
    )
    return vm, vm_info, checks


def read_backup_manifest(backup_dir: Path) -> Dict[str, Any]:
    manifest_file = backup_dir / "backup_manifest.json"
    if not manifest_file.exists():
        return {}
    try:
        return json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def backup_manifest_payload_bytes(backup_dir: Path, manifest: Optional[Dict[str, Any]] = None) -> int:
    manifest = manifest if manifest is not None else read_backup_manifest(backup_dir)
    if not manifest:
        return 0

    total = 0
    for item in manifest.get("files", []):
        try:
            total += int(item.get("bytes") or 0)
        except (TypeError, ValueError):
            continue

    ovf_name = str((manifest.get("ovf_descriptor") or {}).get("file") or "")
    if ovf_name:
        ovf_path = backup_dir / Path(ovf_name).name
        if ovf_path.exists():
            total += ovf_path.stat().st_size
    return total


def free_space_requirement_from_bytes(
    source_bytes: int,
    source: str,
    detail: str = "",
    backup_mode: str = "delta",
    delta_extra_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    base_gb = core.bytes_to_gb(source_bytes)
    normalized_mode = normalize_backup_mode(backup_mode)
    if normalized_mode == CBT_DELTA_MODE:
        estimated_delta_bytes = (
            int(delta_extra_bytes)
            if delta_extra_bytes is not None
            else max(int(source_bytes * FREE_SPACE_GROWTH_RATIO), 1024**3)
        )
        delta_extra_gb = core.bytes_to_gb(estimated_delta_bytes)
        margin_gb = max(FREE_SPACE_MIN_MARGIN_GB, delta_extra_gb * FREE_SPACE_GROWTH_RATIO)
        return {
            "required_gb": round(delta_extra_gb + margin_gb, 2),
            "base_gb": round(base_gb, 2),
            "delta_extra_gb": round(delta_extra_gb, 2),
            "temporary_export_gb": 0.0,
            "estimated_final_growth_gb": round(delta_extra_gb, 2),
            "margin_gb": round(margin_gb, 2),
            "model": "vddk_cbt_changed_blocks",
            "source": source,
            "detail": detail,
        }

    local_delta = normalized_mode in {LOCAL_DELTA_MODE, "ask"}
    if local_delta:
        delta_extra_gb = core.bytes_to_gb(delta_extra_bytes if delta_extra_bytes is not None else source_bytes)
    else:
        delta_extra_gb = 0.0
    margin_gb = max(FREE_SPACE_MIN_MARGIN_GB, base_gb * FREE_SPACE_GROWTH_RATIO)
    model = "local_delta_full_export_then_pack" if local_delta else "full_export"
    return {
        "required_gb": round(base_gb + delta_extra_gb + margin_gb, 2),
        "base_gb": round(base_gb, 2),
        "delta_extra_gb": round(delta_extra_gb, 2),
        "temporary_export_gb": round(base_gb, 2),
        "estimated_final_growth_gb": round(delta_extra_gb if local_delta else base_gb, 2),
        "margin_gb": round(margin_gb, 2),
        "model": model,
        "source": source,
        "detail": detail,
    }


def estimate_selected_vm_free_space(
    vm_info: Dict[str, Any],
    config: core.VSphereConfig,
    backup_mode: str = "delta",
) -> Dict[str, Any]:
    existing = core.successful_backup_dirs_for_vm(config.output_dir, vm_info)
    if existing:
        latest = max(existing, key=lambda path: path.stat().st_mtime)
        latest_manifest = read_backup_manifest(latest)
        payload_bytes = backup_manifest_payload_bytes(latest, latest_manifest)
        if payload_bytes > 0:
            delta_storage_info = latest_manifest.get("delta_storage") or {}
            cbt_storage_info = latest_manifest.get("cbt_storage") or {}
            delta_extra_bytes = None
            normalized_mode = normalize_backup_mode(backup_mode)
            if (normalized_mode == LOCAL_DELTA_MODE or backup_mode == "ask") and delta_storage_info:
                delta_extra_bytes = int(delta_storage_info.get("new_chunk_bytes") or 0)
            elif normalized_mode == CBT_DELTA_MODE and cbt_storage_info:
                delta_extra_bytes = int(cbt_storage_info.get("changed_bytes") or cbt_storage_info.get("patch_bytes") or 0)
            return free_space_requirement_from_bytes(
                payload_bytes,
                "latest_successful_backup",
                str(latest),
                backup_mode=backup_mode,
                delta_extra_bytes=delta_extra_bytes,
            )

    disk_capacity_bytes = sum(int(disk.get("capacity_bytes") or 0) for disk in vm_info.get("disks", []))
    if disk_capacity_bytes > 0:
        return free_space_requirement_from_bytes(
            disk_capacity_bytes,
            "provisioned_disk_capacity",
            f"disk_capacity_gb={round(core.bytes_to_gb(disk_capacity_bytes), 2)}",
            backup_mode=backup_mode,
        )

    committed_bytes = int(vm_info.get("storage_committed_bytes") or 0)
    if committed_bytes > 0:
        return free_space_requirement_from_bytes(
            committed_bytes,
            "vm_committed_storage",
            f"committed_gb={round(core.bytes_to_gb(committed_bytes), 2)}",
            backup_mode=backup_mode,
        )

    return {
        "required_gb": config.min_free_gb,
        "base_gb": 0,
        "delta_extra_gb": 0,
        "margin_gb": 0,
        "source": "config_min_free_gb",
        "detail": "",
    }


def print_numbered_inventory(records: Sequence[VMRecord]) -> None:
    header = (
        f"{'#':>3} | {'VM Name':<32} | {'Power':<10} | {'CPU':>3} | "
        f"{'RAM MB':>7} | {'Snap':<4} | {'MoRef':<8} | Disks"
    )
    print(header)
    print("-" * len(header))
    for index, (_, info) in enumerate(records, start=1):
        snap = "yes" if info["has_snapshot"] else "no"
        print(
            f"{index:>3} | {info['name']:<32} | {info['power_state']:<10} | "
            f"{info['num_cpu']:>3} | {info['memory_mb']:>7} | {snap:<4} | "
            f"{info['moref']:<8} | {disk_text(info)}"
        )


def select_record(
    records: Sequence[VMRecord],
    vm_name: str = "",
    vm_moref: str = "",
) -> VMRecord:
    if vm_moref:
        matches = [record for record in records if record[1]["moref"] == vm_moref]
        if len(matches) != 1:
            raise core.SafetyError(f"MoRef selection is not unique or not found: {vm_moref}")
        return matches[0]

    if vm_name:
        matches = [record for record in records if record[1]["name"] == vm_name]
        if len(matches) != 1:
            raise core.SafetyError(f"VM name selection is not unique or not found: {vm_name}")
        return matches[0]

    if not sys.stdin.isatty():
        raise core.SafetyError("Interactive selection needs a TTY. Use --vm-name or --vm-moref.")

    while True:
        raw = input("VM-Nummer auswaehlen oder q fuer Abbruch: ").strip()
        if raw.lower() in {"q", "quit", "abbruch"}:
            raise core.SafetyError("Selection aborted.")
        if not raw.isdigit():
            print("Bitte eine Nummer aus der Liste eingeben.")
            continue
        index = int(raw)
        if 1 <= index <= len(records):
            return records[index - 1]
        print(f"Nummer ausserhalb der Liste: {index}")


def validate_selected_vm_target(
    vm_info: Dict[str, Any],
    config: core.VSphereConfig,
    dynamic_free_space: bool = False,
    backup_mode: str = "delta",
    chain_slot: Optional[Dict[str, Any]] = None,
    transfer_mode: str = LOCAL_DELTA_MODE,
    require_existing_backup: bool = False,
) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    disks = vm_info.get("disks", [])

    def add(name: str, ok: bool, detail: str, severity: str = "fatal") -> None:
        checks.append({"name": name, "ok": bool(ok), "severity": severity, "detail": detail})

    force_cbt_baseline_snapshot = bool(
        chain_slot
        and chain_slot.get("kind") == "full"
        and normalize_backup_mode(str(chain_slot.get("requested_backup_mode") or "")) == CBT_DELTA_MODE
    )
    backup_method = "snapshot_export" if force_cbt_baseline_snapshot else core.backup_method_for_power_state(vm_info["power_state"])
    add("selected_vm", True, f"name={vm_info['name']!r}, moref={vm_info['moref']!r}")
    if chain_slot:
        add(
            "backup_chain",
            True,
            (
                f"vm_dir={chain_slot['vm_dir']}, run={chain_slot['run_name']}, "
                f"kind={chain_slot['kind']}, previous={chain_slot['previous_dir'] or 'none'}"
            ),
            severity="info",
        )
        if require_existing_backup:
            existing_count = int(chain_slot.get("existing_count") or 0)
            add(
                "existing_backup_baseline",
                existing_count > 0,
                f"existing_successful_backups={existing_count}",
            )
    add("not_template", not vm_info["template"], f"template={vm_info['template']}")
    add(
        "power_state",
        vm_info["power_state"] in core.ALLOWED_POWER_STATES,
        f"power_state={vm_info['power_state']}, backup_method={backup_method}",
    )
    add("no_existing_snapshots", not vm_info["has_snapshot"], f"has_snapshot={vm_info['has_snapshot']}")
    add("disk_count", len(disks) >= 1, f"disk_count={len(disks)}")

    missing_disk_files = [disk["label"] for disk in disks if not disk.get("file_name")]
    add("disk_files", not missing_disk_files, f"missing={missing_disk_files or 'none'}")

    if vm_info["power_state"] == "poweredOn":
        add(
            "live_backup_disk_support",
            len(disks) >= 1,
            f"poweredOn fallback supports {len(disks)} virtual disk(s)",
        )
        parse_errors = []
        for disk in disks:
            try:
                core.parse_datastore_path(disk.get("file_name", ""))
            except Exception as exc:
                parse_errors.append(f"{disk.get('label')}: {exc}")
        add("datastore_paths", not parse_errors, f"errors={parse_errors or 'none'}")

    if chain_slot and chain_slot.get("kind") == "delta" and transfer_mode == CBT_DELTA_MODE:
        vddk = core.vddk_backend_status()
        previous_manifest = previous_backup_manifest(chain_slot)
        previous_cbt = previous_manifest.get("cbt_state") or {}
        add(
            "cbt_enabled",
            bool(vm_info.get("change_tracking_enabled")),
            f"change_tracking_enabled={vm_info.get('change_tracking_enabled')}",
        )
        add("cbt_backend", bool(vddk["available"]), vddk["detail"])
        add(
            "cbt_baseline",
            bool(previous_cbt.get("valid")),
            (
                f"previous={chain_slot.get('previous_dir') or 'none'}, "
                f"valid={previous_cbt.get('valid', False)}"
            ),
        )
        add(
            "cbt_transfer_implementation",
            True,
            (
                "VDDK-backed CBT patch transfer and restore materialization are enabled. "
                "No full-download fallback is used in cbt mode."
            ),
            severity="info",
        )
    elif force_cbt_baseline_snapshot:
        vddk = core.vddk_backend_status()
        cbt_enabled = bool(vm_info.get("change_tracking_enabled"))
        add(
            "cbt_enabled",
            True,
            (
                "change_tracking_enabled=True"
                if cbt_enabled
                else "change_tracking_enabled=False; will_enable_before_baseline_snapshot"
            ),
            severity="info" if not cbt_enabled else "fatal",
        )
        add("cbt_backend", bool(vddk["available"]), vddk["detail"])
        add(
            "cbt_baseline",
            True,
            "initial full backup will use a temporary snapshot to establish CBT changeIds",
            severity="info",
        )

    requirement = (
        estimate_selected_vm_free_space(vm_info, config, backup_mode=transfer_mode if backup_mode == "delta" else backup_mode)
        if dynamic_free_space
        else {
            "required_gb": config.min_free_gb,
            "base_gb": 0,
            "delta_extra_gb": 0,
            "temporary_export_gb": 0,
            "estimated_final_growth_gb": config.min_free_gb,
            "margin_gb": 0,
            "model": "manual_min_free_gb",
            "source": "config_min_free_gb",
            "detail": "",
        }
    )
    space = core.check_free_space(config.output_dir, float(requirement["required_gb"]))
    requirement_detail = (
        f", source={requirement['source']}, base_gb={requirement['base_gb']}, "
        f"delta_extra_gb={requirement['delta_extra_gb']}, "
        f"temporary_export_gb={requirement.get('temporary_export_gb', requirement['base_gb'])}, "
        f"estimated_final_growth_gb={requirement.get('estimated_final_growth_gb', requirement['delta_extra_gb'])}, "
        f"margin_gb={requirement['margin_gb']}, model={requirement.get('model', 'unknown')}"
    )
    if requirement.get("detail"):
        requirement_detail += f", detail={requirement['detail']}"
    add(
        "backup_storage_free_space",
        space["ok"],
        f"path={space['path']}, free_gb={space['free_gb']}, required_gb={space['required_gb']}"
        f"{requirement_detail}",
    )
    return checks


def build_backup_plans(
    selected_records: Sequence[VMRecord],
    config: core.VSphereConfig,
    requested_backup_mode: str,
    dynamic_free_space: bool,
    require_existing_backup: bool = False,
) -> List[Dict[str, Any]]:
    plans: List[Dict[str, Any]] = []
    for vm, vm_info in selected_records:
        chain_slot = next_backup_chain_slot(config, vm_info, requested_backup_mode)
        transfer_mode = delta_transfer_mode(requested_backup_mode, chain_slot)
        chain_slot["requested_backup_mode"] = requested_backup_mode
        chain_slot["delta_transfer_mode"] = transfer_mode
        checks = validate_selected_vm_target(
            vm_info,
            config,
            dynamic_free_space=dynamic_free_space,
            backup_mode=transfer_mode if chain_slot["kind"] == "delta" else chain_slot["kind"],
            chain_slot=chain_slot,
            transfer_mode=transfer_mode,
            require_existing_backup=require_existing_backup,
        )
        plans.append(
            {
                "vm": vm,
                "vm_info": vm_info,
                "chain_slot": chain_slot,
                "delta_transfer_mode": transfer_mode,
                "checks": checks,
            }
        )
    return plans


def batch_free_space_check(
    plans: Sequence[Dict[str, Any]],
    config: core.VSphereConfig,
    dynamic_free_space: bool,
    keep_delta_originals: bool = False,
) -> Optional[Dict[str, Any]]:
    if len(plans) < 2:
        return None
    if not dynamic_free_space:
        space = core.check_free_space(config.output_dir, config.min_free_gb)
        return {
            "name": "batch_storage_free_space",
            "ok": space["ok"],
            "severity": "fatal",
            "detail": (
                f"path={space['path']}, free_gb={space['free_gb']}, required_gb={space['required_gb']}, "
                "source=manual_min_free_gb"
            ),
        }

    cumulative_final_gb = 0.0
    peak_required_gb = 0.0
    details = []
    for plan in plans:
        vm_info = plan["vm_info"]
        chain_slot = plan["chain_slot"]
        requirement = estimate_selected_vm_free_space(vm_info, config, backup_mode=chain_slot["kind"])
        if chain_slot["kind"] == "delta":
            requirement = estimate_selected_vm_free_space(
                vm_info,
                config,
                backup_mode=plan.get("delta_transfer_mode") or chain_slot["kind"],
            )
        base_gb = float(requirement.get("base_gb") or 0.0)
        delta_extra_gb = float(requirement.get("delta_extra_gb") or 0.0)
        temporary_export_gb = float(requirement.get("temporary_export_gb") or base_gb)
        margin_gb = float(requirement.get("margin_gb") or 0.0)
        if chain_slot["kind"] == "delta":
            current_peak = cumulative_final_gb + temporary_export_gb + delta_extra_gb + margin_gb
            if plan.get("delta_transfer_mode") == CBT_DELTA_MODE:
                final_extra = delta_extra_gb
            else:
                final_extra = base_gb + delta_extra_gb if keep_delta_originals else delta_extra_gb
        else:
            current_peak = cumulative_final_gb + base_gb + margin_gb
            final_extra = base_gb
        peak_required_gb = max(peak_required_gb, current_peak)
        cumulative_final_gb += final_extra
        details.append(f"{vm_info['name']}:{chain_slot['run_name']}={round(current_peak, 2)}GB")

    space = core.check_free_space(config.output_dir, peak_required_gb)
    return {
        "name": "batch_storage_free_space",
        "ok": space["ok"],
        "severity": "fatal",
        "detail": (
            f"path={space['path']}, free_gb={space['free_gb']}, required_gb={space['required_gb']}, "
            f"planned_vms={len(plans)}, peak_model=sequential, details={'; '.join(details)}"
        ),
    }


def print_backup_plan(plans: Sequence[Dict[str, Any]]) -> None:
    print()
    print(f"Geplantes Listen-Backup: {len(plans)} VM(s)")
    header = f"{'#':>3} | {'VM Name':<32} | {'Power':<10} | {'MoRef':<8} | {'Run':<10} | {'Mode':<11} | Disks"
    print(header)
    print("-" * len(header))
    for index, plan in enumerate(plans, start=1):
        vm_info = plan["vm_info"]
        chain_slot = plan["chain_slot"]
        mode = plan.get("delta_transfer_mode") or chain_slot["kind"]
        print(
            f"{index:>3} | {vm_info['name']:<32} | {vm_info['power_state']:<10} | "
            f"{vm_info['moref']:<8} | {chain_slot['run_name']:<10} | {mode:<11} | {disk_text(vm_info)}"
        )


def print_plan_checks(plans: Sequence[Dict[str, Any]], extra_checks: Sequence[Dict[str, Any]] = ()) -> None:
    print()
    if extra_checks:
        print("Listen-Pruefungen:")
        core.print_checks(list(extra_checks))
        print()
    for plan in plans:
        vm_info = plan["vm_info"]
        print(f"Preflight: {vm_info['name']} ({plan['chain_slot']['run_name']})")
        core.print_checks(plan["checks"])
        print()


def plan_has_fatal_failures(plan: Dict[str, Any]) -> bool:
    return not core.checks_passed(plan["checks"])


def split_passable_plans(plans: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    passing: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    for plan in plans:
        if plan_has_fatal_failures(plan):
            blocked.append(plan)
        else:
            passing.append(plan)
    return passing, blocked


def fatal_check_names(checks: Sequence[Dict[str, Any]]) -> List[str]:
    return [
        str(check.get("name") or "")
        for check in checks
        if not check.get("ok") and check.get("severity", "fatal") == "fatal"
    ]


def confirm_continue_with_passing_plans(
    passing_plans: Sequence[Dict[str, Any]],
    blocked_plans: Sequence[Dict[str, Any]],
    assume_yes: bool = False,
    skip_blocked: bool = False,
) -> None:
    if not blocked_plans:
        return
    if not passing_plans:
        raise core.SafetyError("No selected VM passed preflight. Backup blocked.")
    if skip_blocked:
        print("Blockierte VM(s) werden uebersprungen:")
        for plan in blocked_plans:
            vm_info = plan["vm_info"]
            failures = ", ".join(fatal_check_names(plan["checks"])) or "unknown"
            print(f"- {vm_info['name']} ({plan['chain_slot']['run_name']}): {failures}")
        return
    if assume_yes or not sys.stdin.isatty():
        raise core.SafetyError("Some selected VMs failed preflight. Backup blocked.")

    print("Blockierte VM(s) werden nicht gesichert:")
    for plan in blocked_plans:
        vm_info = plan["vm_info"]
        failures = ", ".join(fatal_check_names(plan["checks"])) or "unknown"
        print(f"- {vm_info['name']} ({plan['chain_slot']['run_name']}): {failures}")

    print()
    print_backup_plan(passing_plans)
    raw = input(f"Nur diese {len(passing_plans)} bestandene(n) VM(s) weiter bearbeiten? [j/N]: ").strip().lower()
    if raw not in {"j", "ja", "y", "yes"}:
        raise core.SafetyError("Partial list backup was not confirmed. Backup blocked.")


def confirm_batch_start(plans: Sequence[Dict[str, Any]], assume_yes: bool = False) -> None:
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise core.SafetyError("List backup confirmation needs a TTY. Use --yes only for deliberate automation.")
    raw = input(f"Listen-Backup fuer {len(plans)} VM(s) starten? [ja/NEIN]: ").strip().lower()
    if raw not in {"j", "ja", "y", "yes"}:
        raise core.SafetyError("List backup was not confirmed. Backup blocked.")


def cbt_disks_by_key(cbt_state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for disk in cbt_state.get("disks", []) or []:
        key = str(disk.get("key") or "")
        if key:
            result[key] = disk
    return result


def create_selected_cbt_backup(
    session: core.VSphereSession,
    vm: Any,
    vm_info: Dict[str, Any],
    config: core.VSphereConfig,
    checks: List[Dict[str, Any]],
    chain_slot: Dict[str, Any],
) -> Path:
    if not core.checks_passed(checks):
        core.print_checks(checks)
        raise core.SafetyError("Preflight checks failed. Backup blocked.")
    if chain_slot.get("kind") != "delta" or chain_slot.get("delta_transfer_mode") != CBT_DELTA_MODE:
        raise core.SafetyError("CBT backup requires a delta chain slot with cbt transfer mode")

    previous_manifest = previous_backup_manifest(chain_slot)
    previous_cbt = previous_manifest.get("cbt_state") or {}
    previous_disks = cbt_disks_by_key(previous_cbt)
    if not previous_cbt.get("valid") or not previous_disks:
        raise core.SafetyError("CBT backup requires a previous successful backup with valid CBT changeIds")

    with target_file_context(vm_info) as (target_slug, file_base):
        uuid_short = (vm_info.get("instance_uuid") or vm_info.get("moref") or "unknown")[:8]
        backup_id = f"{target_slug}_{chain_slot['run_name']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid_short}"
        chain_slot["vm_dir"].mkdir(parents=True, exist_ok=True)
        staging_dir = chain_slot["staging_dir"]
        final_dir = chain_slot["final_dir"]
        failed_dir = chain_slot["failed_dir"]
        if staging_dir.exists() or final_dir.exists():
            raise FileExistsError(f"Backup directory already exists: {final_dir}")
        staging_dir.mkdir(parents=True)

        manifest: Dict[str, Any] = {
            "tool": "interactive_vsphere_vm_backup",
            "started_at": core.now_utc(),
            "target_vm": vm_info["name"],
            "target_moref": vm_info["moref"],
            "target_file_base": file_base,
            "backup_chain": {
                "vm_dir": str(chain_slot["vm_dir"]),
                "kind": chain_slot["kind"],
                "sequence": chain_slot["sequence"],
                "run_name": chain_slot["run_name"],
                "previous_dir": chain_slot["previous_dir"],
                "model": "one_full_then_vddk_cbt_patches",
                "requested_delta_transfer": CBT_DELTA_MODE,
            },
            "safety_model": {
                "selection": "exactly_one_operator_selected_vm",
                "allowed_power_states": sorted(core.ALLOWED_POWER_STATES),
                "powered_off_method": "temporary_snapshot_vddk_cbt",
                "powered_on_method": "temporary_snapshot_vddk_cbt",
            },
            "backup_method": "vddk_cbt_snapshot",
            "vm_info": vm_info,
            "preflight_checks": checks,
            "files": [],
            "ovf_descriptor": {},
            "snapshot": {"required": True},
            "transfer_method": "vddk_cbt",
            "status": "running",
            "cbt_state": core.cbt_state_from_vm_info(vm_info),
        }

        (staging_dir / "vm_metadata.json").write_text(
            json.dumps(vm_info, indent=2, sort_keys=True), encoding="utf-8"
        )
        core.LOGGER.info("CBT backup directory prepared: %s", staging_dir)

        snapshot = None
        vddk_connection = None
        snapshot_info: Dict[str, Any] = {"required": True}
        try:
            # VDDK requires PrepareForAccess before the VM snapshot is made.
            # Keep this access object alive until the VDDK snapshot connection
            # has been disconnected and the temporary snapshot is removed.
            vddk_status = core.vddk_backend_status()
            if not vddk_status.get("available"):
                raise core.SafetyError(str(vddk_status.get("detail") or "VDDK backend unavailable"))
            vddk = vddk_cbt.get_vddk(str(vddk_status.get("library") or ""))
            vddk_connection = vddk.prepare_remote(
                host=config.host,
                user=config.user,
                password=config.password,
                port=config.port,
                vm_moref=str(vm_info["moref"]),
                thumbprint=config.vddk_thumbprint,
            )

            manifest["ovf_descriptor"] = core.write_ovf_descriptor(session, vm, staging_dir)
            snapshot, snapshot_info = core.create_temporary_snapshot(vm, config)
            manifest["snapshot"] = snapshot_info
            current_cbt = core.cbt_state_from_snapshot(snapshot, vm_info)
            if not current_cbt.get("valid"):
                raise core.SafetyError("Snapshot did not provide valid CBT changeIds")
            manifest["cbt_state"] = current_cbt

            snapshot_moref = str(current_cbt.get("snapshot_moref") or snapshot_info.get("moref") or "")
            if not snapshot_moref:
                raise core.SafetyError("Temporary snapshot has no MoRef for VDDK ConnectEx")
            vddk_connection.connect_snapshot(
                snapshot_moref,
                transport_modes=vddk_cbt.DEFAULT_TRANSPORT_MODES,
            )

            cbt_manifest: Dict[str, Any] = {
                "format": vddk_cbt.CBT_FORMAT,
                "created_at": core.now_utc(),
                "previous_dir": str(chain_slot["previous_dir"]),
                "snapshot_moref": snapshot_moref,
                "vm_moref": vm_info["moref"],
                "vddk": {
                    "library": vddk_status.get("library", ""),
                    "transport_modes": vddk_status.get("transport_modes", ""),
                    "requested_transport_modes": vddk_cbt.DEFAULT_TRANSPORT_MODES,
                },
                "disks": [],
            }

            files: List[Dict[str, Any]] = []
            total_changed = 0
            total_patch = 0
            connection = vddk_connection
            for disk_index, disk in enumerate(current_cbt.get("disks", []) or [], start=1):
                disk_key = str(disk.get("key") or "")
                previous_disk = previous_disks.get(disk_key)
                if not previous_disk:
                    raise core.SafetyError(f"No previous CBT baseline for disk key {disk_key}")
                previous_change_id = str(previous_disk.get("change_id") or "")
                current_change_id = str(disk.get("change_id") or "")
                capacity_bytes = int(disk.get("capacity_bytes") or 0)
                changed_areas = core.query_changed_disk_areas(
                    vm=vm,
                    snapshot=snapshot,
                    disk_key=int(disk_key),
                    previous_change_id=previous_change_id,
                    capacity_bytes=capacity_bytes,
                )
                patch_name = f"disk-{disk_index}.cbtpatch"
                patch_result = vddk_cbt.read_changed_areas_to_patch(
                    connection=connection,
                    disk_path=str(disk.get("file_name") or ""),
                    areas=changed_areas,
                    patch_file=staging_dir / patch_name,
                )
                descriptor_name = f"cbt-disk{disk_index}.vmdk"
                extent_name = f"cbt-disk{disk_index}-flat.vmdk"
                disk_record = {
                    "disk_index": disk_index,
                    "disk_key": disk_key,
                    "label": disk.get("label", f"Hard disk {disk_index}"),
                    "capacity_bytes": capacity_bytes,
                    "source_disk_path": disk.get("file_name", ""),
                    "previous_change_id": previous_change_id,
                    "change_id": current_change_id,
                    "patch_file": patch_name,
                    "patch_bytes": patch_result["patch_bytes"],
                    "patch_sha256": patch_result["patch_sha256"],
                    "changed_bytes": patch_result["changed_bytes"],
                    "area_count": patch_result["area_count"],
                    "areas": patch_result["areas"],
                    "transport_mode": patch_result.get("transport_mode", ""),
                    "vddk_disk_info": patch_result.get("vddk_disk_info", {}),
                    "descriptor_name": descriptor_name,
                    "extent_name": extent_name,
                    "adapter_type": "lsilogic",
                }
                cbt_manifest["disks"].append(disk_record)
                total_changed += int(patch_result["changed_bytes"])
                total_patch += int(patch_result["patch_bytes"])
                files.append(
                    {
                        "name": patch_name,
                        "bytes": patch_result["patch_bytes"],
                        "sha256": patch_result["patch_sha256"],
                        "role": "cbt_patch",
                        "disk_index": disk_index,
                        "disk_key": disk_key,
                        "source_disk_path": disk.get("file_name", ""),
                    }
                )

            cbt_manifest["updated_at"] = core.now_utc()
            cbt_manifest["changed_bytes"] = total_changed
            cbt_manifest["patch_bytes"] = total_patch
            vddk_cbt.write_cbt_manifest(staging_dir, cbt_manifest)
            manifest["files"] = files
            manifest["cbt_storage"] = {
                "format": vddk_cbt.CBT_FORMAT,
                "manifest": vddk_cbt.CBT_MANIFEST_NAME,
                "disk_count": len(cbt_manifest["disks"]),
                "changed_bytes": total_changed,
                "patch_bytes": total_patch,
                "changed_gb": round(total_changed / float(1024**3), 3),
                "patch_gb": round(total_patch / float(1024**3), 3),
            }

            # Disconnect the VDDK disk connection before removing its snapshot.
            # EndAccess must happen after the snapshot has been deleted.
            if vddk_connection is not None:
                vddk_connection.disconnect()
            if snapshot is not None:
                core.remove_temporary_snapshot(snapshot, snapshot_info, config)
                snapshot = None
                manifest["snapshot"] = snapshot_info
            if vddk_connection is not None:
                vddk_connection.close()
                vddk_connection = None

            manifest["status"] = "success"
            manifest["finished_at"] = core.now_utc()
            (staging_dir / "backup_manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
            staging_dir.rename(final_dir)
            core.LOGGER.info("CBT backup completed: %s", final_dir)
            return final_dir
        except BaseException as exc:
            manifest["status"] = "failed"
            manifest["finished_at"] = core.now_utc()
            manifest["error"] = core.exception_message(exc)
            if vddk_connection is not None:
                try:
                    # Keep EndAccess until after the temporary snapshot has
                    # been removed, as required by VDDK.
                    vddk_connection.disconnect()
                except Exception as cleanup_exc:
                    core.LOGGER.error("VDDK disk connection cleanup failed: %s", cleanup_exc)
            if snapshot is not None:
                try:
                    core.remove_temporary_snapshot(snapshot, snapshot_info, config)
                except Exception as cleanup_exc:
                    snapshot_info["removed"] = False
                    snapshot_info["cleanup_status"] = "failed"
                    snapshot_info["cleanup_error"] = str(cleanup_exc)
                    core.LOGGER.error("Temporary snapshot cleanup failed: %s", cleanup_exc)
                manifest["snapshot"] = snapshot_info
            if vddk_connection is not None:
                try:
                    vddk_connection.close()
                except Exception as cleanup_exc:
                    core.LOGGER.error("VDDK access cleanup failed: %s", cleanup_exc)
            try:
                (staging_dir / "backup_manifest.json").write_text(
                    json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
                )
            except Exception:
                pass
            try:
                if staging_dir.exists() and not failed_dir.exists():
                    staging_dir.rename(failed_dir)
            except Exception:
                pass
            raise


def run_backup_plans(
    session: core.VSphereSession,
    config: core.VSphereConfig,
    plans: Sequence[Dict[str, Any]],
    keep_delta_originals: bool = False,
    chunk_size_mb: int = 16,
    verbose: bool = False,
    fresh_session_per_vm: bool = True,
    dynamic_free_space: bool = True,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for plan in plans:
        planned_vm_info = plan["vm_info"]
        chain_slot = plan["chain_slot"]
        use_delta = chain_slot["kind"] == "delta"
        result_vm_name = str(planned_vm_info.get("name") or "?")
        print(f"Starte Backup: {result_vm_name} -> {chain_slot['run_name']}")
        try:
            if fresh_session_per_vm:
                core.LOGGER.info(
                    "Opening fresh vSphere session for list backup item: vm=%s run=%s",
                    result_vm_name,
                    chain_slot["run_name"],
                )
                with core.VSphereSession(config) as item_session:
                    vm, vm_info, checks = refresh_plan_for_session(
                        item_session,
                        config,
                        plan,
                        dynamic_free_space=dynamic_free_space,
                    )
                    result_vm_name = vm_info["name"]
                    if plan.get("delta_transfer_mode") == CBT_DELTA_MODE:
                        final_dir = create_selected_cbt_backup(
                            item_session,
                            vm,
                            vm_info,
                            config,
                            checks,
                            chain_slot=chain_slot,
                        )
                    else:
                        final_dir = create_selected_vm_backup(
                            item_session,
                            vm,
                            vm_info,
                            config,
                            checks,
                            chain_slot=chain_slot,
                        )
            else:
                vm_info = planned_vm_info
                if plan.get("delta_transfer_mode") == CBT_DELTA_MODE:
                    final_dir = create_selected_cbt_backup(
                        session,
                        plan["vm"],
                        vm_info,
                        config,
                        plan["checks"],
                        chain_slot=chain_slot,
                    )
                else:
                    final_dir = create_selected_vm_backup(
                        session,
                        plan["vm"],
                        vm_info,
                        config,
                        plan["checks"],
                        chain_slot=chain_slot,
                    )
        except Exception as exc:
            core.LOGGER.error(
                "List backup item failed: vm=%s run=%s: %s",
                result_vm_name,
                chain_slot["run_name"],
                exc,
                exc_info=verbose,
            )
            failed_dir = chain_slot.get("failed_dir")
            result = {
                "vm": result_vm_name,
                "run": chain_slot["run_name"],
                "kind": chain_slot["kind"],
                "status": "failed",
                "error": core.exception_message(exc),
            }
            if isinstance(failed_dir, Path) and failed_dir.exists():
                result["backup_dir"] = str(failed_dir)
            results.append(result)
            print(f"Backup fehlgeschlagen: {result_vm_name} -> {chain_slot['run_name']}: {exc}", file=sys.stderr)
            continue

        result = {
            "vm": result_vm_name,
            "run": chain_slot["run_name"],
            "backup_dir": str(final_dir),
            "kind": chain_slot["kind"],
            "status": "success",
        }
        if use_delta:
            if plan.get("delta_transfer_mode") == CBT_DELTA_MODE:
                result["cbt_storage"] = read_backup_manifest(final_dir).get("cbt_storage") or {}
                results.append(result)
                continue
            try:
                result["delta_pack"] = core.delta_pack_completed_backup(
                    final_dir,
                    keep_originals=keep_delta_originals,
                    chunk_size_mb=chunk_size_mb,
                )
            except Exception as exc:
                core.LOGGER.error(
                    "Delta pack failed after backup: vm=%s run=%s backup_dir=%s: %s",
                    result_vm_name,
                    chain_slot["run_name"],
                    final_dir,
                    exc,
                    exc_info=verbose,
                )
                result["status"] = "failed"
                result["error"] = f"delta_pack: {core.exception_message(exc)}"
                print(
                    f"Delta-Pack fehlgeschlagen: {result_vm_name} -> {chain_slot['run_name']}: {exc}",
                    file=sys.stderr,
                )
        results.append(result)
    return results


def confirm_start(vm_info: Dict[str, Any], assume_yes: bool = False) -> None:
    if assume_yes:
        return
    print()
    print("Ausgewaehlte VM:")
    print(f"  Name:  {vm_info['name']}")
    print(f"  MoRef: {vm_info['moref']}")
    print(f"  UUID:  {vm_info['instance_uuid']}")
    print(f"  Power: {vm_info['power_state']}")
    print(f"  Disks: {disk_text(vm_info)}")
    expected = vm_info["name"]
    raw = input(f"Zum Start den VM-Namen exakt eingeben ({expected}): ").strip()
    if raw != expected:
        raise core.SafetyError("Confirmation did not match selected VM name. Backup blocked.")


def create_selected_vm_backup(
    session: core.VSphereSession,
    vm: Any,
    vm_info: Dict[str, Any],
    config: core.VSphereConfig,
    checks: List[Dict[str, Any]],
    chain_slot: Optional[Dict[str, Any]] = None,
) -> Path:
    if not core.checks_passed(checks):
        core.print_checks(checks)
        raise core.SafetyError("Preflight checks failed. Backup blocked.")

    force_cbt_baseline_snapshot = bool(
        chain_slot
        and chain_slot.get("kind") == "full"
        and normalize_backup_mode(str(chain_slot.get("requested_backup_mode") or "")) == CBT_DELTA_MODE
    )
    backup_method = "snapshot_export" if force_cbt_baseline_snapshot else core.backup_method_for_power_state(vm_info["power_state"])
    if backup_method == "unsupported":
        raise core.SafetyError(f"Unsupported power state for backup: {vm_info['power_state']}")

    with target_file_context(vm_info) as (target_slug, file_base):
        uuid_short = (vm_info.get("instance_uuid") or vm_info.get("moref") or "unknown")[:8]
        if chain_slot is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_id = f"{target_slug}_{timestamp}_{uuid_short}"
            staging_dir = config.output_dir / f"{backup_id}.inprogress"
            final_dir = config.output_dir / backup_id
            failed_dir = config.output_dir / f"{backup_id}.failed"
            chain_slot = {
                "vm_dir": config.output_dir,
                "kind": "full",
                "sequence": 1,
                "run_name": backup_id,
                "previous_dir": "",
            }
        else:
            backup_id = f"{target_slug}_{chain_slot['run_name']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid_short}"
            chain_slot["vm_dir"].mkdir(parents=True, exist_ok=True)
            staging_dir = chain_slot["staging_dir"]
            final_dir = chain_slot["final_dir"]
            failed_dir = chain_slot["failed_dir"]
        if staging_dir.exists() or final_dir.exists():
            raise FileExistsError(f"Backup directory already exists: {final_dir}")
        staging_dir.mkdir(parents=True)

        manifest: Dict[str, Any] = {
            "tool": "interactive_vsphere_vm_backup",
            "started_at": core.now_utc(),
            "target_vm": vm_info["name"],
            "target_moref": vm_info["moref"],
            "target_file_base": file_base,
            "backup_chain": {
                "vm_dir": str(chain_slot["vm_dir"]),
                "kind": chain_slot["kind"],
                "sequence": chain_slot["sequence"],
                "run_name": chain_slot["run_name"],
                "previous_dir": chain_slot["previous_dir"],
                "model": "vddk_cbt_baseline_full" if force_cbt_baseline_snapshot else "one_full_then_numbered_local_delta_chunks",
                "requested_delta_transfer": CBT_DELTA_MODE
                if force_cbt_baseline_snapshot
                else chain_slot.get("delta_transfer_mode", LOCAL_DELTA_MODE),
                "requested_backup_mode": chain_slot.get("requested_backup_mode", ""),
            },
            "safety_model": {
                "selection": "exactly_one_operator_selected_vm",
                "allowed_power_states": sorted(core.ALLOWED_POWER_STATES),
                "powered_off_method": "vm_export",
                "powered_on_method": "temporary_snapshot_export_with_datastore_copy_fallback",
            },
            "backup_method": backup_method,
            "vm_info": vm_info,
            "preflight_checks": checks,
            "files": [],
            "ovf_descriptor": {},
            "snapshot": {"required": backup_method == "snapshot_export"},
            "transfer_method": "",
            "status": "running",
            "cbt_state": core.cbt_state_from_vm_info(vm_info),
        }

        (staging_dir / "vm_metadata.json").write_text(
            json.dumps(vm_info, indent=2, sort_keys=True), encoding="utf-8"
        )
        core.LOGGER.info("Backup directory prepared: %s", staging_dir)

        lease = None
        snapshot = None
        snapshot_info: Dict[str, Any] = {"required": backup_method == "snapshot_export"}
        try:
            manifest["ovf_descriptor"] = core.write_ovf_descriptor(session, vm, staging_dir)

            if backup_method == "snapshot_export":
                if force_cbt_baseline_snapshot:
                    manifest["cbt_enablement"] = core.ensure_change_tracking_enabled(vm, vm_info, config)
                    vm_info = core.collect_vm_info(vm)
                    manifest["vm_info"] = vm_info
                    manifest["cbt_state"] = core.cbt_state_from_vm_info(vm_info)
                    (staging_dir / "vm_metadata.json").write_text(
                        json.dumps(vm_info, indent=2, sort_keys=True), encoding="utf-8"
                    )
                snapshot, snapshot_info = core.create_temporary_snapshot(vm, config)
                manifest["snapshot"] = snapshot_info
                manifest["cbt_state"] = core.cbt_state_from_snapshot(snapshot, vm_info)
                if force_cbt_baseline_snapshot and not manifest["cbt_state"].get("valid"):
                    raise core.SafetyError(
                        "Initial CBT baseline full backup did not receive valid snapshot changeIds"
                    )
                core.LOGGER.info("Requesting ExportSnapshot HttpNfcLease for %s", vm_info["name"])
                try:
                    lease = snapshot.ExportSnapshot()
                except vmodl.fault.NotSupported as exc:
                    core.run_snapshot_datastore_copy_fallback(
                        session=session,
                        vm=vm,
                        vm_info=vm_info,
                        backup_dir=staging_dir,
                        config=config,
                        backup_id=backup_id,
                        manifest=manifest,
                        trigger=exc,
                        reason="snapshot_export_not_supported",
                    )
                    lease = None
            else:
                core.LOGGER.info("Requesting ExportVm HttpNfcLease for %s", vm_info["name"])
                lease = vm.ExportVm()

            if lease is not None:
                try:
                    core.transfer_http_nfc_lease_or_snapshot_copy(
                        session=session,
                        vm=vm,
                        vm_info=vm_info,
                        lease=lease,
                        backup_dir=staging_dir,
                        config=config,
                        backup_id=backup_id,
                        manifest=manifest,
                        allow_snapshot_fallback=backup_method == "snapshot_export",
                    )
                finally:
                    lease = None

            if snapshot is not None:
                core.remove_temporary_snapshot(snapshot, snapshot_info, config)
                snapshot = None
                manifest["snapshot"] = snapshot_info

            manifest["status"] = "success"
            manifest["finished_at"] = core.now_utc()
            (staging_dir / "backup_manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
            staging_dir.rename(final_dir)
            core.LOGGER.info("Backup completed: %s", final_dir)
            return final_dir
        except BaseException as exc:
            manifest["status"] = "failed"
            manifest["finished_at"] = core.now_utc()
            manifest["error"] = core.exception_message(exc)
            core.record_transfer_failure_metadata(manifest, exc)
            if lease is not None:
                core.abort_lease(lease, core.exception_message(exc))
            if snapshot is not None:
                try:
                    core.remove_temporary_snapshot(snapshot, snapshot_info, config)
                except Exception as cleanup_exc:
                    snapshot_info["removed"] = False
                    snapshot_info["cleanup_status"] = "failed"
                    snapshot_info["cleanup_error"] = str(cleanup_exc)
                    core.LOGGER.error("Temporary snapshot cleanup failed: %s", cleanup_exc)
                manifest["snapshot"] = snapshot_info
            try:
                (staging_dir / "backup_manifest.json").write_text(
                    json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
                )
            except Exception:
                pass
            try:
                if staging_dir.exists() and not failed_dir.exists():
                    staging_dir.rename(failed_dir)
            except Exception:
                pass
            raise


def verify_any_backup_dir(path: Path) -> Dict[str, Any]:
    manifest_file = path / "backup_manifest.json"
    if not manifest_file.exists():
        raise FileNotFoundError(f"Missing backup manifest: {manifest_file}")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    errors: List[str] = []
    checked_files = 0

    if not manifest.get("target_vm"):
        errors.append("Manifest has no target_vm")

    if manifest.get("status") not in {"success", "success_rescued"}:
        errors.append(f"Manifest status is not success: {manifest.get('status')}")

    files = manifest.get("files", [])
    if not files:
        errors.append("Manifest has no exported files")

    for item in files:
        checked_files += 1
        errors.extend(delta_storage.verify_manifest_item(path, item))

    if manifest.get("cbt_storage"):
        errors.extend(vddk_cbt.verify_cbt_backup(path))

    return {
        "path": str(path),
        "target_vm": manifest.get("target_vm"),
        "status": "ok" if not errors else "failed",
        "checked_files": checked_files,
        "errors": errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Select one VM or an explicit VM selection file for vSphere backup")
    parser.add_argument("--config", help="Path to credentials.env/config.env")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--list-only", action="store_true", help="Only list VMs, do not start a backup")
    parser.add_argument("--vm-name", default="", help="Select VM by exact name instead of prompting")
    parser.add_argument("--vm-moref", default="", help="Select VM by exact MoRef instead of prompting")
    parser.add_argument("--selection-file", help="Backup VMs selected in an editable text file")
    parser.add_argument("--write-selection-file", help="Write an editable VM selection file and exit")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing selection file")
    parser.add_argument("--yes", action="store_true", help="Skip final typed-name confirmation")
    parser.add_argument(
        "--skip-blocked",
        action="store_true",
        help="In --selection-file mode, skip VMs that fail preflight and continue with passing VMs",
    )
    parser.add_argument(
        "--require-existing-backup",
        action="store_true",
        help="Block automatic initial full backups; selected VMs need an existing successful backup baseline",
    )
    parser.add_argument(
        "--min-free-gb",
        type=float,
        help="Manual free-space override for this run; disables automatic VM-size estimate",
    )
    parser.add_argument(
        "--backup-mode",
        choices=["ask", "full", "delta", "local-delta", "cbt"],
        default=DEFAULT_BACKUP_MODE,
        help=(
            "Default cbt: true VMware CBT transfer after a CBT baseline full backup. "
            "delta/local-delta: normal export plus local chunk dedupe."
        ),
    )
    parser.add_argument(
        "--keep-delta-originals",
        action="store_true",
        help="When delta mode is selected, keep original large VMDK files after writing chunks",
    )
    parser.add_argument("--chunk-size-mb", type=int, default=16, help="Delta chunk size in MiB. Default: 16")
    parser.add_argument("--verify", help="Verify one backup directory created by this script")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    core.setup_logging(verbose=args.verbose)

    try:
        if args.selection_file and (args.vm_name or args.vm_moref):
            raise core.SafetyError("--selection-file cannot be combined with --vm-name or --vm-moref")
        if args.write_selection_file and args.selection_file:
            raise core.SafetyError("--write-selection-file cannot be combined with --selection-file")

        if args.verify:
            result = verify_any_backup_dir(Path(args.verify).expanduser().resolve())
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["status"] == "ok" else 4

        config = core.load_config(args.config)
        if args.min_free_gb is not None:
            config = replace(config, min_free_gb=args.min_free_gb)

        with core.VSphereSession(config) as session:
            records = inventory_records(session)
            print_numbered_inventory(records)

            if args.write_selection_file:
                written = write_selection_file(Path(args.write_selection_file), records, overwrite=args.force)
                print()
                print(f"Selection file written: {written}")
                print("Edit the file and remove '# ' from every VM line that should be backed up.")
                return 0

            if args.list_only and not args.selection_file:
                return 0

            if args.selection_file:
                entries = parse_selection_file(Path(args.selection_file))
                if not entries:
                    raise core.SafetyError("Selection file contains no active VM lines.")
                selected_records, selection_errors = resolve_selection_entries(records, entries)
                if selection_errors:
                    print()
                    print("Selection file validation failed:")
                    for error in selection_errors:
                        print(f"- {error}")
                    raise core.SafetyError("Selection file does not match current vSphere inventory.")

                delta_cleanup = delta_storage.cleanup_unreferenced_chunks(config.output_dir)
                plans = build_backup_plans(
                    selected_records,
                    config,
                    requested_backup_mode=args.backup_mode,
                    dynamic_free_space=args.min_free_gb is None,
                    require_existing_backup=args.require_existing_backup,
                )
                passing_plans, blocked_plans = split_passable_plans(plans)
                storage_check_plans = passing_plans if args.skip_blocked else plans
                extra_checks: List[Dict[str, Any]] = []
                batch_check = batch_free_space_check(
                    storage_check_plans,
                    config,
                    dynamic_free_space=args.min_free_gb is None,
                    keep_delta_originals=args.keep_delta_originals,
                )
                if batch_check:
                    extra_checks.append(batch_check)
                if delta_cleanup["unreferenced_files"] or delta_cleanup["removed_files"]:
                    extra_checks.append(
                        {
                            "name": "delta_store_gc",
                            "ok": True,
                            "severity": "info",
                            "detail": (
                                f"removed_files={delta_cleanup['removed_files']}, "
                                f"removed_gb={delta_cleanup['removed_gb']}, "
                                f"referenced_chunks={delta_cleanup['referenced_chunks']}"
                            ),
                        }
                    )

                print_backup_plan(plans)
                print_plan_checks(plans, extra_checks=extra_checks)
                if blocked_plans:
                    if args.list_only and not args.skip_blocked:
                        raise core.SafetyError("Preflight checks failed. Backup blocked.")
                    filtered_extra_checks = extra_checks
                    if not args.skip_blocked:
                        filtered_extra_checks = []
                        filtered_batch_check = batch_free_space_check(
                            passing_plans,
                            config,
                            dynamic_free_space=args.min_free_gb is None,
                            keep_delta_originals=args.keep_delta_originals,
                        )
                        if filtered_batch_check:
                            filtered_extra_checks.append(filtered_batch_check)
                        for check in extra_checks:
                            if check["name"] != "batch_storage_free_space":
                                filtered_extra_checks.append(check)

                    if filtered_extra_checks:
                        print()
                        print("Listen-Pruefungen fuer verbleibende VM(s):")
                        core.print_checks(filtered_extra_checks)
                    if filtered_extra_checks and not core.checks_passed(filtered_extra_checks):
                        raise core.SafetyError("Remaining selected VMs do not fit into local backup storage.")
                    if not args.list_only:
                        confirm_continue_with_passing_plans(
                            passing_plans,
                            blocked_plans,
                            assume_yes=args.yes,
                            skip_blocked=args.skip_blocked,
                        )
                    plans = passing_plans
                    extra_checks = filtered_extra_checks
                if extra_checks and not core.checks_passed(extra_checks):
                    raise core.SafetyError("List preflight checks failed. Backup blocked.")
                if args.list_only:
                    return 0

                confirm_batch_start(plans, assume_yes=args.yes)
                results = run_backup_plans(
                    session,
                    config,
                    plans,
                    keep_delta_originals=args.keep_delta_originals,
                    chunk_size_mb=args.chunk_size_mb,
                    verbose=args.verbose,
                    dynamic_free_space=args.min_free_gb is None,
                )
                failed_results = [result for result in results if result.get("status") != "success"]
                print(
                    json.dumps(
                        {
                            "backups": results,
                            "failed": len(failed_results),
                            "succeeded": len(results) - len(failed_results),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                if failed_results:
                    print(
                        f"ERROR: {len(failed_results)} of {len(results)} list backup(s) failed.",
                        file=sys.stderr,
                    )
                    return 1
                return 0

            vm, vm_info = select_record(records, vm_name=args.vm_name, vm_moref=args.vm_moref)
            delta_cleanup = delta_storage.cleanup_unreferenced_chunks(config.output_dir)
            plans = build_backup_plans(
                [(vm, vm_info)],
                config,
                requested_backup_mode=args.backup_mode,
                dynamic_free_space=args.min_free_gb is None,
                require_existing_backup=args.require_existing_backup,
            )
            plan = plans[0]
            chain_slot = plan["chain_slot"]
            checks = plan["checks"]
            if delta_cleanup["unreferenced_files"] or delta_cleanup["removed_files"]:
                checks.append(
                    {
                        "name": "delta_store_gc",
                        "ok": True,
                        "severity": "info",
                        "detail": (
                            f"removed_files={delta_cleanup['removed_files']}, "
                            f"removed_gb={delta_cleanup['removed_gb']}, "
                            f"referenced_chunks={delta_cleanup['referenced_chunks']}"
                        ),
                    }
                )
            print()
            core.print_checks(checks)
            if not core.checks_passed(checks):
                raise core.SafetyError("Preflight checks failed. Backup blocked.")
            use_delta = chain_slot["kind"] == "delta"
            if use_delta and plan.get("delta_transfer_mode") == CBT_DELTA_MODE:
                confirm_start(vm_info, assume_yes=args.yes)
                final_dir = create_selected_cbt_backup(session, vm, vm_info, config, checks, chain_slot=chain_slot)
                print(json.dumps({"backup_dir": str(final_dir), "cbt_storage": read_backup_manifest(final_dir).get("cbt_storage") or {}}, indent=2, sort_keys=True))
                return 0
            confirm_start(vm_info, assume_yes=args.yes)
            final_dir = create_selected_vm_backup(session, vm, vm_info, config, checks, chain_slot=chain_slot)
            if use_delta:
                pack_result = core.delta_pack_completed_backup(
                    final_dir,
                    keep_originals=args.keep_delta_originals,
                    chunk_size_mb=args.chunk_size_mb,
                )
                print(json.dumps({"backup_dir": str(final_dir), "delta_pack": pack_result}, indent=2, sort_keys=True))
            else:
                print(f"Backup completed: {final_dir}")
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
        core.LOGGER.error("Command failed: %s", exc, exc_info=args.verbose)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
