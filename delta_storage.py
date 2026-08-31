#!/usr/bin/env python3
"""Local chunked delta storage for vSphere backup files.

This module deduplicates large backup files after a normal backup completed.
It does not read vSphere disks directly and does not change VM or network configuration.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set


DELTA_FORMAT = "vsphere-delta-chunks-v1"
DELTA_DIRNAME = "delta"
DELTA_MANIFEST_NAME = "delta_manifest.json"
DELTA_README_NAME = "README.txt"
DEFAULT_STORE_DIRNAME = ".delta_store"
HYDRATED_DIRNAME = ".restore_hydrated"
DEFAULT_CHUNK_SIZE = 16 * 1024 * 1024
MIN_CHUNKED_FILE_SIZE = 2 * 1024 * 1024


class DeltaStorageError(RuntimeError):
    """Raised when a delta-packed file cannot be verified or materialized."""


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_manifest_name(name: str) -> str:
    candidate = Path(name).name
    if not candidate or candidate != name or candidate in {".", ".."}:
        raise DeltaStorageError(f"Unsafe manifest file name for delta storage: {name!r}")
    return candidate


def default_store_dir(backup_dir: Path) -> Path:
    return backup_dir.parent / DEFAULT_STORE_DIRNAME


def delta_dir(backup_dir: Path) -> Path:
    return backup_dir / DELTA_DIRNAME


def manifest_path(backup_dir: Path) -> Path:
    return backup_dir / DELTA_MANIFEST_NAME


def legacy_manifest_path(backup_dir: Path) -> Path:
    return delta_dir(backup_dir) / DELTA_MANIFEST_NAME


def existing_manifest_path(backup_dir: Path) -> Path:
    current = manifest_path(backup_dir)
    if current.exists():
        return current
    return legacy_manifest_path(backup_dir)


def manifest_reference() -> str:
    return DELTA_MANIFEST_NAME


def load_delta_manifest(backup_dir: Path) -> Dict[str, Any]:
    path = existing_manifest_path(backup_dir)
    if not path.exists():
        return {}
    data = read_json(path)
    if data.get("format") != DELTA_FORMAT:
        raise DeltaStorageError(f"Unsupported delta manifest format: {data.get('format')!r}")
    return data


def write_delta_manifest(backup_dir: Path, data: Dict[str, Any]) -> None:
    write_json_atomic(manifest_path(backup_dir), data)
    legacy = legacy_manifest_path(backup_dir)
    if legacy.exists():
        legacy.unlink()
    legacy_dir = delta_dir(backup_dir)
    if legacy_dir.exists():
        legacy_readme = legacy_dir / DELTA_README_NAME
        if legacy_readme.exists():
            legacy_readme.unlink()
        try:
            legacy_dir.rmdir()
        except OSError:
            pass


def write_delta_summary(backup_dir: Path, manifest: Dict[str, Any], result: Dict[str, Any]) -> None:
    store = manifest.get("store", {})
    lines = [
        "Delta backup metadata",
        "",
        "Type: local chunked dedupe after a normal vSphere export.",
        "Note: this is not VMware CBT-only transfer.",
        f"Chunk store: {store.get('path', '')}",
        f"Packed files: {result.get('packed_files', 0)}",
        f"New chunk bytes: {result.get('new_chunk_bytes', 0)}",
        f"Reused chunk bytes: {result.get('reused_chunk_bytes', 0)}",
        f"Base reused bytes: {result.get('base_reused_bytes', result.get('base_reused_chunk_bytes', 0))}",
        f"Removed original bytes: {result.get('removed_original_bytes', 0)}",
        "",
    ]
    (backup_dir / DELTA_README_NAME).write_text("\n".join(lines), encoding="utf-8")


def store_dir_from_manifest(backup_dir: Path, delta_manifest: Optional[Dict[str, Any]] = None) -> Path:
    data = delta_manifest if delta_manifest is not None else load_delta_manifest(backup_dir)
    store = data.get("store", {}) if data else {}
    raw_path = store.get("path")
    if raw_path:
        path = Path(raw_path)
        if not path.is_absolute():
            path = backup_dir / path
        return path.resolve()
    return default_store_dir(backup_dir).resolve()


def delta_manifest_files(output_dir: Path) -> List[Path]:
    if not output_dir.exists():
        return []
    manifests = list(output_dir.rglob(DELTA_MANIFEST_NAME))
    return sorted(path for path in manifests if path.is_file())


def chunk_relative_path(digest: str) -> Path:
    return Path("chunks") / digest[:2] / f"{digest}.chunk"


def chunk_path(store_dir: Path, digest: str) -> Path:
    return store_dir / chunk_relative_path(digest)


def read_chunk_data(backup_dir: Path, store_dir: Path, chunk: Dict[str, Any]) -> bytes:
    expected_length = int(chunk.get("length") or 0)
    if chunk.get("storage") == "base_file":
        raw_base_path = str(chunk.get("base_path") or "")
        if not raw_base_path:
            raise DeltaStorageError("Base chunk has no base_path")
        base_path = Path(raw_base_path)
        if not base_path.is_absolute():
            base_path = backup_dir / base_path
        base_offset = int(chunk.get("base_offset") or chunk.get("offset") or 0)
        with base_path.open("rb") as handle:
            handle.seek(base_offset)
            data = handle.read(expected_length)
        return data

    digest = str(chunk.get("sha256", ""))
    path = store_dir / str(chunk.get("path") or chunk_relative_path(digest))
    if not path.exists():
        raise FileNotFoundError(path)
    return path.read_bytes()


def _remove_empty_dirs(path: Path, stop_at: Path) -> None:
    current = path
    stop_at = stop_at.resolve()
    while current.exists() and current.resolve() != stop_at:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def cleanup_unreferenced_chunks(output_dir: Path, remove: bool = True) -> Dict[str, Any]:
    """Remove chunks that no delta manifest references.

    The chunk store is a dedupe cache. A chunk without any manifest reference
    cannot be used for restore or verify, so deleting it does not delete a
    backup recovery point.
    """
    output_dir = output_dir.expanduser().resolve()
    stores: Set[Path] = set()
    referenced: Set[Path] = set()

    for manifest_file in delta_manifest_files(output_dir):
        backup_dir = manifest_file.parent.parent if manifest_file.parent.name == DELTA_DIRNAME else manifest_file.parent
        try:
            delta_manifest = read_json(manifest_file)
        except Exception:
            continue
        if delta_manifest.get("format") != DELTA_FORMAT:
            continue
        store_dir = store_dir_from_manifest(backup_dir, delta_manifest)
        stores.add(store_dir)
        for entry in delta_manifest.get("files", []):
            for chunk in entry.get("chunks", []):
                if chunk.get("storage") == "base_file":
                    continue
                digest = str(chunk.get("sha256", ""))
                raw_path = str(chunk.get("path") or chunk_relative_path(digest))
                referenced.add((store_dir / raw_path).resolve())

    default_store = (output_dir / DEFAULT_STORE_DIRNAME).resolve()
    if default_store.exists():
        stores.add(default_store)

    removed_files = 0
    removed_bytes = 0
    unreferenced_files = 0
    unreferenced_bytes = 0
    for store_dir in sorted(stores):
        chunks_dir = store_dir / "chunks"
        if not chunks_dir.exists():
            continue
        for chunk_file in sorted(chunks_dir.rglob("*.chunk")):
            try:
                resolved = chunk_file.resolve()
                size = chunk_file.stat().st_size
            except FileNotFoundError:
                continue
            if resolved in referenced:
                continue
            unreferenced_files += 1
            unreferenced_bytes += size
            if remove:
                chunk_file.unlink()
                removed_files += 1
                removed_bytes += size
                _remove_empty_dirs(chunk_file.parent, chunks_dir)
        if remove:
            _remove_empty_dirs(chunks_dir, store_dir)
            _remove_empty_dirs(store_dir, output_dir)

    return {
        "status": "ok",
        "removed": bool(remove),
        "referenced_chunks": len(referenced),
        "unreferenced_files": unreferenced_files,
        "unreferenced_bytes": unreferenced_bytes,
        "unreferenced_gb": round(unreferenced_bytes / float(1024**3), 2),
        "removed_files": removed_files,
        "removed_bytes": removed_bytes,
        "removed_gb": round(removed_bytes / float(1024**3), 2),
    }


def manifest_files_by_name(manifest: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(item.get("name", "")): item for item in manifest.get("files", []) if item.get("name")}


def delta_entries_by_name(delta_manifest: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(item.get("name", "")): item for item in delta_manifest.get("files", []) if item.get("name")}


def is_chunk_candidate(item: Dict[str, Any], backup_dir: Path) -> bool:
    name = str(item.get("name", ""))
    if not name.lower().endswith(".vmdk"):
        return False
    if item.get("vmdk_role") == "descriptor":
        return False
    size = int(item.get("bytes") or 0)
    if item.get("vmdk_role") != "extent" and size < MIN_CHUNKED_FILE_SIZE:
        return False
    path = backup_dir / safe_manifest_name(name)
    return path.exists() and path.is_file()


def select_chunk_candidates(manifest: Dict[str, Any], backup_dir: Path) -> List[Dict[str, Any]]:
    return [item for item in manifest.get("files", []) if is_chunk_candidate(item, backup_dir)]


def base_full_dir_for_backup(backup_dir: Path, manifest: Dict[str, Any]) -> Optional[Path]:
    chain = manifest.get("backup_chain") or {}
    if chain.get("kind") != "delta":
        return None
    raw_vm_dir = str(chain.get("vm_dir") or "")
    if raw_vm_dir:
        vm_dir = Path(raw_vm_dir)
    else:
        vm_dir = backup_dir.parent
    if not vm_dir.is_absolute():
        vm_dir = backup_dir / vm_dir
    candidate = vm_dir / "full_0001"
    return candidate.resolve() if candidate.exists() else None


def base_files_by_name_for_backup(backup_dir: Path, manifest: Dict[str, Any]) -> Dict[str, Path]:
    base_dir = base_full_dir_for_backup(backup_dir, manifest)
    if not base_dir:
        return {}
    base_manifest_file = base_dir / "backup_manifest.json"
    if not base_manifest_file.exists():
        return {}
    try:
        base_manifest = read_json(base_manifest_file)
    except Exception:
        return {}
    result: Dict[str, Path] = {}
    for item in base_manifest.get("files", []):
        name = str(item.get("name") or "")
        if not name:
            continue
        path = base_dir / safe_manifest_name(name)
        if path.exists() and path.is_file() and path.stat().st_size == int(item.get("bytes") or 0):
            result[name] = path
    return result


def _write_chunk_if_missing(store_dir: Path, digest: str, data: bytes) -> bool:
    target = chunk_path(store_dir, digest)
    if target.exists():
        if target.stat().st_size != len(data):
            raise DeltaStorageError(f"Existing chunk has wrong size: {target}")
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
    try:
        tmp.write_bytes(data)
        tmp.replace(target)
    finally:
        if tmp.exists():
            tmp.unlink()
    return True


def pack_file(
    backup_dir: Path,
    item: Dict[str, Any],
    store_dir: Path,
    chunk_size: int,
    base_file: Optional[Path] = None,
) -> Dict[str, Any]:
    name = safe_manifest_name(str(item.get("name", "")))
    source = backup_dir / name
    if not source.exists():
        raise FileNotFoundError(f"Cannot delta-pack missing file: {source}")

    expected_bytes = int(item.get("bytes") or source.stat().st_size)
    expected_sha256 = str(item.get("sha256") or "")
    full_digest = hashlib.sha256()
    chunks: List[Dict[str, Any]] = []
    offset = 0
    new_bytes = 0
    reused_bytes = 0
    base_reused_bytes = 0

    base_handle = base_file.open("rb") if base_file and base_file.exists() else None
    try:
        with source.open("rb") as handle:
            while True:
                data = handle.read(chunk_size)
                if not data:
                    break
                digest = hashlib.sha256(data).hexdigest()
                full_digest.update(data)
                chunk_entry = {
                    "index": len(chunks),
                    "offset": offset,
                    "length": len(data),
                    "sha256": digest,
                }
                if base_handle is not None:
                    base_handle.seek(offset)
                    base_data = base_handle.read(len(data))
                    if len(base_data) == len(data) and hashlib.sha256(base_data).hexdigest() == digest:
                        chunk_entry.update(
                            {
                                "storage": "base_file",
                                "base_path": os.path.relpath(base_file, backup_dir),
                                "base_offset": offset,
                            }
                        )
                        base_reused_bytes += len(data)
                        chunks.append(chunk_entry)
                        offset += len(data)
                        continue
                created = _write_chunk_if_missing(store_dir, digest, data)
                if created:
                    new_bytes += len(data)
                else:
                    reused_bytes += len(data)
                chunk_entry["path"] = str(chunk_relative_path(digest))
                chunks.append(chunk_entry)
                offset += len(data)
    finally:
        if base_handle is not None:
            base_handle.close()

    actual_sha256 = full_digest.hexdigest()
    if offset != expected_bytes:
        raise DeltaStorageError(f"Size changed while packing {name}: expected={expected_bytes} actual={offset}")
    if expected_sha256 and actual_sha256 != expected_sha256:
        raise DeltaStorageError(f"SHA256 mismatch while packing {name}")

    return {
        "name": name,
        "bytes": expected_bytes,
        "sha256": actual_sha256,
        "chunk_size": chunk_size,
        "chunk_count": len(chunks),
        "chunks": chunks,
        "original_present": True,
        "packed_at": now_utc(),
        "new_chunk_bytes": new_bytes,
        "reused_chunk_bytes": reused_bytes,
        "base_reused_bytes": base_reused_bytes,
    }


def verify_delta_entry(backup_dir: Path, entry: Dict[str, Any], skip_hash: bool = False) -> List[str]:
    errors: List[str] = []
    name = str(entry.get("name", ""))
    try:
        safe_manifest_name(name)
    except Exception as exc:
        return [str(exc)]

    delta_manifest = load_delta_manifest(backup_dir)
    store_dir = store_dir_from_manifest(backup_dir, delta_manifest)
    total = 0
    full_digest = hashlib.sha256()

    for chunk in entry.get("chunks", []):
        digest = str(chunk.get("sha256", ""))
        expected_length = int(chunk.get("length") or 0)
        try:
            data = read_chunk_data(backup_dir, store_dir, chunk)
        except Exception as exc:
            errors.append(f"Missing chunk for {name}: {exc}")
            continue
        if skip_hash:
            actual_length = len(data)
            if actual_length != expected_length:
                errors.append(f"Chunk size mismatch for {name}: index={chunk.get('index')}")
                continue
            total += actual_length
            continue
        if len(data) != expected_length:
            errors.append(f"Chunk size mismatch for {name}: index={chunk.get('index')}")
            continue
        if hashlib.sha256(data).hexdigest() != digest:
            errors.append(f"Chunk SHA256 mismatch for {name}: index={chunk.get('index')}")
            continue
        total += len(data)
        full_digest.update(data)

    expected_bytes = int(entry.get("bytes") or 0)
    if total != expected_bytes:
        errors.append(f"Delta byte count mismatch for {name}: expected={expected_bytes} actual={total}")
    expected_sha256 = str(entry.get("sha256") or "")
    if not skip_hash and expected_sha256 and full_digest.hexdigest() != expected_sha256:
        errors.append(f"Delta full SHA256 mismatch for {name}")
    return errors


def has_delta_file(backup_dir: Path, name: str) -> bool:
    data = load_delta_manifest(backup_dir)
    return safe_manifest_name(name) in delta_entries_by_name(data)


def delta_file_size(backup_dir: Path, name: str) -> Optional[int]:
    data = load_delta_manifest(backup_dir)
    entry = delta_entries_by_name(data).get(safe_manifest_name(name))
    if not entry:
        return None
    return int(entry.get("bytes") or 0)


def verify_manifest_item(backup_dir: Path, item: Dict[str, Any], skip_hash: bool = False) -> List[str]:
    name = safe_manifest_name(str(item.get("name", "")))
    file_path = backup_dir / name
    if file_path.exists():
        size = file_path.stat().st_size
        if size != item.get("bytes"):
            return [f"Size mismatch for {name}: manifest={item.get('bytes')} actual={size}"]
        expected_hash = item.get("sha256")
        if not skip_hash:
            if not expected_hash:
                return [f"Missing sha256 in manifest for {name}"]
            if sha256_file(file_path) != expected_hash:
                return [f"SHA256 mismatch for {name}"]
        return []

    delta_manifest = load_delta_manifest(backup_dir)
    entry = delta_entries_by_name(delta_manifest).get(name)
    if entry:
        if not skip_hash and not item.get("sha256"):
            return [f"Missing sha256 in manifest for {name}"]
        return verify_delta_entry(backup_dir, entry, skip_hash=skip_hash)
    return [f"Missing file: {name}"]


def materialize_delta_file(backup_dir: Path, name: str, target_dir: Optional[Path] = None) -> Path:
    safe_name = safe_manifest_name(name)
    existing = backup_dir / safe_name
    if existing.exists():
        return existing

    delta_manifest = load_delta_manifest(backup_dir)
    entry = delta_entries_by_name(delta_manifest).get(safe_name)
    if not entry:
        raise FileNotFoundError(f"No local or delta-packed file found: {safe_name}")

    store_dir = store_dir_from_manifest(backup_dir, delta_manifest)
    output_dir = target_dir or (backup_dir / HYDRATED_DIRNAME)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / safe_name
    if target.exists():
        if target.stat().st_size == int(entry.get("bytes") or 0) and sha256_file(target) == entry.get("sha256"):
            return target
        target.unlink()

    tmp = target.with_suffix(target.suffix + ".part")
    full_digest = hashlib.sha256()
    bytes_written = 0
    with tmp.open("wb") as handle:
        for chunk in entry.get("chunks", []):
            digest = str(chunk.get("sha256", ""))
            data = read_chunk_data(backup_dir, store_dir, chunk)
            if hashlib.sha256(data).hexdigest() != digest:
                raise DeltaStorageError(f"Chunk SHA256 mismatch while materializing {safe_name}: index={chunk.get('index')}")
            expected_length = int(chunk.get("length") or 0)
            if len(data) != expected_length:
                raise DeltaStorageError(f"Chunk length mismatch while materializing {safe_name}: {source}")
            handle.write(data)
            full_digest.update(data)
            bytes_written += len(data)

    expected_bytes = int(entry.get("bytes") or 0)
    expected_sha256 = str(entry.get("sha256") or "")
    if bytes_written != expected_bytes:
        tmp.unlink(missing_ok=True)
        raise DeltaStorageError(
            f"Materialized size mismatch for {safe_name}: expected={expected_bytes} actual={bytes_written}"
        )
    if expected_sha256 and full_digest.hexdigest() != expected_sha256:
        tmp.unlink(missing_ok=True)
        raise DeltaStorageError(f"Materialized SHA256 mismatch for {safe_name}")

    tmp.replace(target)
    return target


def cleanup_hydrated_files(backup_dir: Path) -> None:
    hydrated = backup_dir / HYDRATED_DIRNAME
    if hydrated.exists():
        shutil.rmtree(hydrated)


def pack_backup_dir(
    backup_dir: Path,
    remove_originals: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    store_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    backup_dir = backup_dir.expanduser().resolve()
    manifest_file = backup_dir / "backup_manifest.json"
    if not manifest_file.exists():
        raise FileNotFoundError(f"Missing backup manifest: {manifest_file}")
    if chunk_size <= 0:
        raise DeltaStorageError("chunk_size must be positive")

    manifest = read_json(manifest_file)
    candidates = select_chunk_candidates(manifest, backup_dir)
    selected_names = {str(item["name"]) for item in candidates}
    base_files = base_files_by_name_for_backup(backup_dir, manifest)
    target_store_dir = (store_dir.expanduser().resolve() if store_dir else default_store_dir(backup_dir).resolve())
    delta_manifest = load_delta_manifest(backup_dir) or {
        "format": DELTA_FORMAT,
        "created_at": now_utc(),
        "store": {"path": os.path.relpath(target_store_dir, backup_dir)},
        "files": [],
    }
    entries = delta_entries_by_name(delta_manifest)

    total_new = 0
    total_reused = 0
    total_base_reused = 0
    packed_files = 0
    for item in candidates:
        entry = pack_file(
            backup_dir,
            item,
            target_store_dir,
            chunk_size,
            base_file=base_files.get(str(item.get("name") or "")),
        )
        entries[entry["name"]] = entry
        total_new += int(entry["new_chunk_bytes"])
        total_reused += int(entry["reused_chunk_bytes"])
        total_base_reused += int(entry.get("base_reused_bytes") or 0)
        packed_files += 1

    delta_manifest["updated_at"] = now_utc()
    delta_manifest["store"] = {"path": os.path.relpath(target_store_dir, backup_dir)}
    delta_manifest["files"] = [entries[name] for name in sorted(entries)]
    write_delta_manifest(backup_dir, delta_manifest)

    removed_bytes = 0
    if remove_originals:
        for name in sorted(selected_names):
            entry = entries[name]
            errors = verify_delta_entry(backup_dir, entry)
            if errors:
                raise DeltaStorageError("; ".join(errors))
            source = backup_dir / name
            if source.exists():
                removed_bytes += source.stat().st_size
                source.unlink()
            entry["original_present"] = source.exists()
        delta_manifest["updated_at"] = now_utc()
        delta_manifest["files"] = [entries[name] for name in sorted(entries)]
        write_delta_manifest(backup_dir, delta_manifest)

    for item in manifest.get("files", []):
        name = str(item.get("name", ""))
        if name in entries:
            item["storage"] = {
                "type": "delta_chunks",
                "delta_manifest": manifest_reference(),
                "original_present": (backup_dir / name).exists(),
            }

    manifest["delta_storage"] = {
        "format": DELTA_FORMAT,
        "manifest": manifest_reference(),
        "store": {"path": os.path.relpath(target_store_dir, backup_dir)},
        "chunk_size": chunk_size,
        "packed_files": packed_files,
        "new_chunk_bytes": total_new,
        "reused_chunk_bytes": total_reused,
        "base_reused_bytes": total_base_reused,
        "removed_original_bytes": removed_bytes,
        "updated_at": now_utc(),
    }
    write_json_atomic(manifest_file, manifest)
    write_delta_summary(backup_dir, delta_manifest, manifest["delta_storage"])

    return {
        "backup_dir": str(backup_dir),
        "status": "ok",
        "packed_files": packed_files,
        "new_chunk_bytes": total_new,
        "reused_chunk_bytes": total_reused,
        "base_reused_chunk_bytes": total_base_reused,
        "removed_original_bytes": removed_bytes,
        "store_dir": str(target_store_dir),
        "delta_manifest": str(manifest_path(backup_dir)),
    }


def list_chunk_files(store_dir: Path) -> Iterable[Path]:
    chunks = store_dir / "chunks"
    if not chunks.exists():
        return []
    return chunks.rglob("*.chunk")
