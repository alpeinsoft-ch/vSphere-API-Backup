#!/usr/bin/env python3
"""VMware VDDK backed Changed Block Tracking patch storage.

The local delta storage module deduplicates full VMDK exports after download.
This module stores only VMware CBT changed sectors read through VixDiskLib.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import delta_storage


CBT_FORMAT = "vsphere-cbt-patches-v1"
CBT_MANIFEST_NAME = "cbt_manifest.json"
CBT_PATCH_SUFFIX = ".cbtpatch"
CBT_HYDRATED_SUBDIR = "cbt"
SECTOR_SIZE = 512
VIXDISKLIB_VERSION_MAJOR = 6
VIXDISKLIB_VERSION_MINOR = 8
VIXDISKLIB_CRED_UID = 1
VIXDISKLIB_SPEC_VMX = 0
VIXDISKLIB_FLAG_OPEN_READ_ONLY = 1 << 2
DEFAULT_TRANSPORT_MODES = "nbdssl:nbd"
DEFAULT_READ_CHUNK_BYTES = 4 * 1024 * 1024


SCRIPT_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger("vsphere_api_backup")
PROGRESS_INTERVAL_BYTES = 1 * 1024**3
PROGRESS_INTERVAL_SECONDS = 30


class CBTStorageError(RuntimeError):
    """Raised when CBT patch storage cannot be verified or materialized."""


class VddkError(RuntimeError):
    """Raised for VixDiskLib errors."""


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


def safe_file_name(name: str) -> str:
    candidate = Path(str(name)).name
    if not candidate or candidate != str(name) or candidate in {".", ".."}:
        raise CBTStorageError(f"Unsafe CBT file name: {name!r}")
    return candidate


def manifest_path(backup_dir: Path) -> Path:
    return backup_dir / CBT_MANIFEST_NAME


def load_cbt_manifest(backup_dir: Path) -> Dict[str, Any]:
    path = manifest_path(backup_dir)
    if not path.exists():
        return {}
    data = read_json(path)
    if data.get("format") != CBT_FORMAT:
        raise CBTStorageError(f"Unsupported CBT manifest format: {data.get('format')!r}")
    return data


def write_cbt_manifest(backup_dir: Path, data: Dict[str, Any]) -> None:
    write_json_atomic(manifest_path(backup_dir), data)


def has_cbt_storage(backup_dir: Path, manifest: Optional[Dict[str, Any]] = None) -> bool:
    manifest = manifest if manifest is not None else {}
    return bool((manifest.get("cbt_storage") or {}).get("manifest") or manifest_path(backup_dir).exists())


def hydrated_cbt_dir(backup_dir: Path, materialization_root: Optional[Path] = None) -> Path:
    """Return the directory used for locally materialized CBT disks.

    The historic default keeps the old location for callers outside the
    restore command.  The restore command passes a temporary root so that
    materialization never creates files inside the backup chain itself.
    """
    if materialization_root is None:
        return backup_dir / delta_storage.HYDRATED_DIRNAME / CBT_HYDRATED_SUBDIR
    root = Path(materialization_root).expanduser().resolve()
    vm_name = safe_file_name(backup_dir.parent.name or "backup")
    run_name = safe_file_name(backup_dir.name or "run")
    return root / vm_name / run_name / CBT_HYDRATED_SUBDIR


def find_materialized_file(backup_dir: Path, name: str) -> Optional[Path]:
    safe_name = safe_file_name(name)
    candidate = hydrated_cbt_dir(backup_dir) / safe_name
    if candidate.exists():
        return candidate
    return None


class VixDiskLibUidPasswdCreds(ctypes.Structure):
    _fields_ = [
        ("userName", ctypes.c_char_p),
        ("password", ctypes.c_char_p),
    ]


class VixDiskLibSessionIdCreds(ctypes.Structure):
    _fields_ = [
        ("cookie", ctypes.c_char_p),
        ("userName", ctypes.c_char_p),
        ("key", ctypes.c_char_p),
    ]


class VixDiskLibCreds(ctypes.Union):
    _fields_ = [
        ("uid", VixDiskLibUidPasswdCreds),
        ("sessionId", VixDiskLibSessionIdCreds),
        ("ticketId", ctypes.c_void_p),
    ]


class VixDiskLibVStorageObjectSpec(ctypes.Structure):
    _fields_ = [
        ("id", ctypes.c_char_p),
        ("datastoreMoRef", ctypes.c_char_p),
        ("ssId", ctypes.c_char_p),
    ]


class VixDiskLibDatastoreSpec(ctypes.Structure):
    _fields_ = [
        ("datastoreMoRef", ctypes.c_char_p),
        ("diskFolder", ctypes.c_char_p),
    ]


class VixDiskLibSpecUnion(ctypes.Union):
    _fields_ = [
        ("vStorageObjSpec", VixDiskLibVStorageObjectSpec),
        ("dsSpec", VixDiskLibDatastoreSpec),
    ]


class VixDiskLibConnectParams(ctypes.Structure):
    _fields_ = [
        ("vmxSpec", ctypes.c_char_p),
        ("serverName", ctypes.c_char_p),
        ("thumbPrint", ctypes.c_char_p),
        ("privateUse", ctypes.c_long),
        ("credType", ctypes.c_int),
        ("creds", VixDiskLibCreds),
        ("port", ctypes.c_uint32),
        ("nfcHostPort", ctypes.c_uint32),
        ("vimApiVer", ctypes.c_char_p),
        ("reserved", ctypes.c_char * 8),
        ("state", ctypes.c_void_p),
        ("spec", VixDiskLibSpecUnion),
        ("specType", ctypes.c_int),
    ]


class VixDiskLibGeometry(ctypes.Structure):
    _fields_ = [
        ("cylinders", ctypes.c_uint32),
        ("heads", ctypes.c_uint32),
        ("sectors", ctypes.c_uint32),
    ]


class VixDiskLibInfo(ctypes.Structure):
    _fields_ = [
        ("biosGeo", VixDiskLibGeometry),
        ("physGeo", VixDiskLibGeometry),
        ("capacity", ctypes.c_uint64),
        ("adapterType", ctypes.c_int),
        ("numLinks", ctypes.c_int),
        ("parentFileNameHint", ctypes.c_char_p),
        ("uuid", ctypes.c_char_p),
        ("logicalSectorSize", ctypes.c_uint32),
        ("physicalSectorSize", ctypes.c_uint32),
    ]


_VDDK_SINGLETON: Optional["VddkLibrary"] = None
_VDDK_LOCK = threading.Lock()


def default_vddk_library_path() -> str:
    env_path = os.environ.get("VDDK_LIBRARY") or os.environ.get("VIXDISKLIB_LIBRARY")
    candidates = [
        env_path,
        str(SCRIPT_DIR / "vendor" / "vddk" / "lib64" / "libvixDiskLib.so"),
        str(SCRIPT_DIR / "vendor" / "vddk" / "lib" / "libvixDiskLib.so"),
        "/usr/lib/vmware-vix-disklib/lib64/libvixDiskLib.so",
        "/usr/lib/vmware-vix-disklib/libvixDiskLib.so",
        "/opt/vmware-vix-disklib/lib64/libvixDiskLib.so",
        "/opt/vmware-vix-disklib/libvixDiskLib.so",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate).resolve())
    return ""


def get_vddk(library_path: str = "") -> "VddkLibrary":
    global _VDDK_SINGLETON
    path = library_path or default_vddk_library_path()
    if not path:
        raise VddkError("VDDK/VixDiskLib is not installed")
    with _VDDK_LOCK:
        if _VDDK_SINGLETON is None:
            _VDDK_SINGLETON = VddkLibrary(path)
        elif Path(_VDDK_SINGLETON.library_path).resolve() != Path(path).resolve():
            raise VddkError(
                f"VDDK already initialized from {_VDDK_SINGLETON.library_path}; cannot switch to {path}"
            )
        return _VDDK_SINGLETON


def probe_vddk(library_path: str = "") -> Dict[str, Any]:
    path = library_path or default_vddk_library_path()
    if not path:
        return {
            "available": False,
            "library": "",
            "transport_modes": "",
            "detail": (
                "VDDK/VixDiskLib is not installed. True VMware CBT delta transfer "
                "requires VMware VDDK."
            ),
        }
    try:
        vddk = get_vddk(path)
        modes = vddk.list_transport_modes()
        return {
            "available": True,
            "library": path,
            "transport_modes": modes,
            "detail": f"VDDK/VixDiskLib loaded from {path}; transport_modes={modes}",
        }
    except Exception as exc:
        return {
            "available": False,
            "library": path,
            "transport_modes": "",
            "detail": f"VDDK/VixDiskLib exists at {path} but could not be loaded: {exc}",
        }


class VddkLibrary:
    def __init__(self, library_path: str):
        self.library_path = str(Path(library_path).resolve())
        library_dir = Path(self.library_path).parent
        # VixDiskLib_InitEx expects the VDDK installation root.  Passing the
        # lib64 directory makes it search for lib64/libdiskLibPlugin.so.
        self.lib_dir = str(
            library_dir.parent if library_dir.name in {"lib", "lib64"} else library_dir
        )
        self.lib = ctypes.CDLL(self.library_path)
        self._configure_functions()
        err = self.lib.VixDiskLib_InitEx(
            VIXDISKLIB_VERSION_MAJOR,
            VIXDISKLIB_VERSION_MINOR,
            None,
            None,
            None,
            self.lib_dir.encode("utf-8"),
            None,
        )
        self.check(err, "VixDiskLib_InitEx")

    def _configure_functions(self) -> None:
        params_ptr = ctypes.POINTER(VixDiskLibConnectParams)
        info_ptr = ctypes.POINTER(VixDiskLibInfo)

        self.lib.VixDiskLib_InitEx.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]
        self.lib.VixDiskLib_InitEx.restype = ctypes.c_uint64
        self.lib.VixDiskLib_Exit.argtypes = []
        self.lib.VixDiskLib_Exit.restype = None
        self.lib.VixDiskLib_ListTransportModes.argtypes = []
        self.lib.VixDiskLib_ListTransportModes.restype = ctypes.c_char_p
        self.lib.VixDiskLib_GetErrorText.argtypes = [ctypes.c_uint64, ctypes.c_char_p]
        self.lib.VixDiskLib_GetErrorText.restype = ctypes.c_void_p
        self.lib.VixDiskLib_FreeErrorText.argtypes = [ctypes.c_void_p]
        self.lib.VixDiskLib_FreeErrorText.restype = None
        self.lib.VixDiskLib_AllocateConnectParams.argtypes = []
        self.lib.VixDiskLib_AllocateConnectParams.restype = params_ptr
        self.lib.VixDiskLib_FreeConnectParams.argtypes = [params_ptr]
        self.lib.VixDiskLib_FreeConnectParams.restype = None
        self.lib.VixDiskLib_PrepareForAccess.argtypes = [params_ptr, ctypes.c_char_p]
        self.lib.VixDiskLib_PrepareForAccess.restype = ctypes.c_uint64
        self.lib.VixDiskLib_EndAccess.argtypes = [params_ptr, ctypes.c_char_p]
        self.lib.VixDiskLib_EndAccess.restype = ctypes.c_uint64
        self.lib.VixDiskLib_Connect.argtypes = [params_ptr, ctypes.POINTER(ctypes.c_void_p)]
        self.lib.VixDiskLib_Connect.restype = ctypes.c_uint64
        self.lib.VixDiskLib_ConnectEx.argtypes = [
            params_ptr,
            ctypes.c_char,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.lib.VixDiskLib_ConnectEx.restype = ctypes.c_uint64
        self.lib.VixDiskLib_Disconnect.argtypes = [ctypes.c_void_p]
        self.lib.VixDiskLib_Disconnect.restype = ctypes.c_uint64
        self.lib.VixDiskLib_Open.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.lib.VixDiskLib_Open.restype = ctypes.c_uint64
        self.lib.VixDiskLib_Close.argtypes = [ctypes.c_void_p]
        self.lib.VixDiskLib_Close.restype = ctypes.c_uint64
        self.lib.VixDiskLib_GetInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(VixDiskLibInfo))]
        self.lib.VixDiskLib_GetInfo.restype = ctypes.c_uint64
        self.lib.VixDiskLib_FreeInfo.argtypes = [info_ptr]
        self.lib.VixDiskLib_FreeInfo.restype = None
        self.lib.VixDiskLib_GetTransportMode.argtypes = [ctypes.c_void_p]
        self.lib.VixDiskLib_GetTransportMode.restype = ctypes.c_char_p
        self.lib.VixDiskLib_Read.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.c_void_p,
        ]
        self.lib.VixDiskLib_Read.restype = ctypes.c_uint64

    def error_text(self, err: int) -> str:
        ptr = self.lib.VixDiskLib_GetErrorText(int(err), None)
        if not ptr:
            return f"VixDiskLib error {err}"
        try:
            return ctypes.string_at(ptr).decode("utf-8", errors="replace")
        finally:
            self.lib.VixDiskLib_FreeErrorText(ptr)

    def check(self, err: int, action: str) -> None:
        if int(err) != 0:
            raise VddkError(f"{action} failed: {self.error_text(int(err))} ({int(err)})")

    def list_transport_modes(self) -> str:
        raw = self.lib.VixDiskLib_ListTransportModes()
        return raw.decode("utf-8", errors="replace") if raw else ""

    def open_remote(
        self,
        host: str,
        user: str,
        password: str,
        port: int,
        vm_moref: str,
        snapshot_moref: str,
        transport_modes: str = "",
        thumbprint: str = "",
    ) -> "VddkConnection":
        return VddkConnection.remote(
            self,
            host=host,
            user=user,
            password=password,
            port=port,
            vm_moref=vm_moref,
            snapshot_moref=snapshot_moref,
            transport_modes=transport_modes,
            thumbprint=thumbprint,
        )

    def prepare_remote(
        self,
        host: str,
        user: str,
        password: str,
        port: int,
        vm_moref: str,
        thumbprint: str = "",
    ) -> "VddkConnection":
        """Prepare a remote VM before its vSphere snapshot is created.

        VDDK requires PrepareForAccess to happen before snapshot creation. The
        returned object keeps the prepared connection parameters alive and can
        be connected to the newly-created snapshot afterwards.
        """
        return VddkConnection.prepare_remote(
            self,
            host=host,
            user=user,
            password=password,
            port=port,
            vm_moref=vm_moref,
            thumbprint=thumbprint,
        )

    def open_local(self) -> "VddkConnection":
        return VddkConnection.local(self)


class VddkConnection:
    def __init__(
        self,
        vddk: VddkLibrary,
        params: Optional[ctypes.POINTER(VixDiskLibConnectParams)],
        connection: ctypes.c_void_p,
        keepalive: Optional[List[bytes]] = None,
        prepared: bool = False,
    ):
        self.vddk = vddk
        self.params = params
        self.connection = connection
        self.keepalive = keepalive or []
        self.prepared = prepared

    @classmethod
    def local(cls, vddk: VddkLibrary) -> "VddkConnection":
        connection = ctypes.c_void_p()
        err = vddk.lib.VixDiskLib_Connect(None, ctypes.byref(connection))
        vddk.check(err, "VixDiskLib_Connect")
        return cls(vddk, None, connection)

    @classmethod
    def remote(
        cls,
        vddk: VddkLibrary,
        host: str,
        user: str,
        password: str,
        port: int,
        vm_moref: str,
        snapshot_moref: str,
        transport_modes: str = "",
        thumbprint: str = "",
    ) -> "VddkConnection":
        prepared_connection = cls.prepare_remote(
            vddk,
            host=host,
            user=user,
            password=password,
            port=port,
            vm_moref=vm_moref,
            thumbprint=thumbprint,
        )
        try:
            prepared_connection.connect_snapshot(snapshot_moref, transport_modes)
            return prepared_connection
        except Exception:
            try:
                prepared_connection.close()
            except Exception:
                pass
            raise

    @classmethod
    def prepare_remote(
        cls,
        vddk: VddkLibrary,
        host: str,
        user: str,
        password: str,
        port: int,
        vm_moref: str,
        thumbprint: str = "",
    ) -> "VddkConnection":
        params = vddk.lib.VixDiskLib_AllocateConnectParams()
        if not params:
            raise VddkError("VixDiskLib_AllocateConnectParams returned NULL")
        keepalive: List[bytes] = []

        def keep(value: str) -> bytes:
            raw = str(value).encode("utf-8")
            keepalive.append(raw)
            return raw

        try:
            params.contents.vmxSpec = keep(f"moref={vm_moref}")
            params.contents.serverName = keep(host)
            configured_thumbprint = (
                str(thumbprint).strip()
                or os.environ.get("VDDK_THUMBPRINT", "").strip()
                or os.environ.get("VIXDISKLIB_THUMBPRINT", "").strip()
            )
            if configured_thumbprint:
                params.contents.thumbPrint = keep(configured_thumbprint)
            params.contents.credType = VIXDISKLIB_CRED_UID
            params.contents.creds.uid.userName = keep(user)
            params.contents.creds.uid.password = keep(password)
            params.contents.port = int(port or 443)
            params.contents.nfcHostPort = int(os.environ.get("VDDK_NFC_HOST_PORT", "902"))
            params.contents.specType = VIXDISKLIB_SPEC_VMX

            identity = keep("vSphere-API-Backup")
            err = vddk.lib.VixDiskLib_PrepareForAccess(params, identity)
            vddk.check(err, "VixDiskLib_PrepareForAccess")
            prepared = True
            return cls(
                vddk,
                params,
                ctypes.c_void_p(),
                keepalive=keepalive,
                prepared=prepared,
            )
        except Exception:
            try:
                vddk.lib.VixDiskLib_EndAccess(params, b"vSphere-API-Backup")
            except Exception:
                pass
            vddk.lib.VixDiskLib_FreeConnectParams(params)
            raise

    def connect_snapshot(self, snapshot_moref: str, transport_modes: str = "") -> None:
        """Connect the prepared access object to a specific VM snapshot."""
        if not self.params:
            raise VddkError("VDDK connection parameters are no longer available")
        if self.connection:
            raise VddkError("VDDK connection is already connected")
        if not snapshot_moref:
            raise VddkError("Snapshot MoRef is required for VixDiskLib_ConnectEx")

        keepalive_value = str(snapshot_moref).encode("utf-8")
        self.keepalive.append(keepalive_value)
        modes = transport_modes or os.environ.get("VDDK_TRANSPORT_MODES") or DEFAULT_TRANSPORT_MODES
        modes_value = str(modes).encode("utf-8")
        self.keepalive.append(modes_value)
        connection = ctypes.c_void_p()
        err = self.vddk.lib.VixDiskLib_ConnectEx(
            self.params,
            ctypes.c_char(1),
            keepalive_value,
            modes_value,
            ctypes.byref(connection),
        )
        self.vddk.check(err, "VixDiskLib_ConnectEx")
        self.connection = connection

    def disconnect(self) -> None:
        if self.connection:
            err = self.vddk.lib.VixDiskLib_Disconnect(self.connection)
            self.connection = ctypes.c_void_p()
            self.vddk.check(err, "VixDiskLib_Disconnect")

    def end_access(self) -> None:
        if self.params and self.prepared:
            err = self.vddk.lib.VixDiskLib_EndAccess(self.params, b"vSphere-API-Backup")
            self.prepared = False
            self.vddk.check(err, "VixDiskLib_EndAccess")

    def close(self) -> None:
        """Disconnect and release access parameters.

        Callers using a VM snapshot must delete that snapshot between
        disconnect() and end_access().
        """
        first_error: Optional[BaseException] = None
        try:
            self.disconnect()
        except Exception as exc:
            first_error = exc
        try:
            self.end_access()
        except Exception as exc:
            if first_error is None:
                first_error = exc
        if self.params:
            self.vddk.lib.VixDiskLib_FreeConnectParams(self.params)
            self.params = None
        if first_error is not None:
            raise first_error

    def __enter__(self) -> "VddkConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def open_disk(self, path: str) -> "VddkDisk":
        handle = ctypes.c_void_p()
        err = self.vddk.lib.VixDiskLib_Open(
            self.connection,
            str(path).encode("utf-8"),
            VIXDISKLIB_FLAG_OPEN_READ_ONLY,
            ctypes.byref(handle),
        )
        self.vddk.check(err, f"VixDiskLib_Open {path}")
        return VddkDisk(self.vddk, handle, path)


class VddkDisk:
    def __init__(self, vddk: VddkLibrary, handle: ctypes.c_void_p, path: str):
        self.vddk = vddk
        self.handle = handle
        self.path = path

    def close(self) -> None:
        if self.handle:
            err = self.vddk.lib.VixDiskLib_Close(self.handle)
            self.handle = ctypes.c_void_p()
            self.vddk.check(err, f"VixDiskLib_Close {self.path}")

    def __enter__(self) -> "VddkDisk":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def transport_mode(self) -> str:
        raw = self.vddk.lib.VixDiskLib_GetTransportMode(self.handle)
        return raw.decode("utf-8", errors="replace") if raw else ""

    def info(self) -> Dict[str, Any]:
        info_ptr = ctypes.POINTER(VixDiskLibInfo)()
        err = self.vddk.lib.VixDiskLib_GetInfo(self.handle, ctypes.byref(info_ptr))
        self.vddk.check(err, f"VixDiskLib_GetInfo {self.path}")
        try:
            info = info_ptr.contents
            return {
                "capacity_sectors": int(info.capacity),
                "capacity_bytes": int(info.capacity) * SECTOR_SIZE,
                "adapter_type": int(info.adapterType),
                "logical_sector_size": int(info.logicalSectorSize or SECTOR_SIZE),
                "physical_sector_size": int(info.physicalSectorSize or SECTOR_SIZE),
            }
        finally:
            self.vddk.lib.VixDiskLib_FreeInfo(info_ptr)

    def read_sectors(self, start_sector: int, num_sectors: int) -> bytes:
        if num_sectors <= 0:
            return b""
        buffer = ctypes.create_string_buffer(int(num_sectors) * SECTOR_SIZE)
        err = self.vddk.lib.VixDiskLib_Read(self.handle, int(start_sector), int(num_sectors), buffer)
        self.vddk.check(err, f"VixDiskLib_Read {self.path} sector={start_sector} count={num_sectors}")
        return buffer.raw


@dataclass(frozen=True)
class ChangedArea:
    start: int
    length: int


def coalesce_changed_areas(areas: Iterable[Dict[str, Any]]) -> List[ChangedArea]:
    normalized: List[ChangedArea] = []
    for item in areas:
        start = int(item.get("start") if "start" in item else item.get("startOffset") or 0)
        length = int(item.get("length") or 0)
        if length <= 0:
            continue
        if start < 0:
            raise CBTStorageError(f"Negative changed-area offset: {start}")
        normalized.append(ChangedArea(start=start, length=length))
    normalized.sort(key=lambda item: item.start)

    merged: List[ChangedArea] = []
    for area in normalized:
        if area.start % SECTOR_SIZE != 0 or area.length % SECTOR_SIZE != 0:
            raise CBTStorageError(f"Changed area is not sector aligned: start={area.start}, length={area.length}")
        if not merged:
            merged.append(area)
            continue
        previous = merged[-1]
        previous_end = previous.start + previous.length
        if area.start <= previous_end:
            merged[-1] = ChangedArea(previous.start, max(previous_end, area.start + area.length) - previous.start)
        else:
            merged.append(area)
    return merged


def read_changed_areas_to_patch(
    connection: VddkConnection,
    disk_path: str,
    areas: Iterable[Dict[str, Any]],
    patch_file: Path,
    read_chunk_bytes: int = DEFAULT_READ_CHUNK_BYTES,
) -> Dict[str, Any]:
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    chunk_bytes = max(SECTOR_SIZE, int(read_chunk_bytes))
    chunk_bytes -= chunk_bytes % SECTOR_SIZE
    if chunk_bytes <= 0:
        chunk_bytes = SECTOR_SIZE
    merged = coalesce_changed_areas(areas)
    patch_sha = hashlib.sha256()
    patch_offset = 0
    total_changed = 0
    area_records: List[Dict[str, Any]] = []

    tmp = patch_file.with_suffix(patch_file.suffix + ".part")
    with connection.open_disk(disk_path) as disk, tmp.open("wb") as output:
        disk_info = disk.info()
        transport_mode = disk.transport_mode()
        for area in merged:
            remaining = area.length
            cursor = area.start
            area_sha = hashlib.sha256()
            area_patch_offset = patch_offset
            while remaining:
                read_bytes = min(remaining, chunk_bytes)
                sectors = read_bytes // SECTOR_SIZE
                data = disk.read_sectors(cursor // SECTOR_SIZE, sectors)
                output.write(data)
                patch_sha.update(data)
                area_sha.update(data)
                cursor += len(data)
                remaining -= len(data)
                patch_offset += len(data)
                total_changed += len(data)
            area_records.append(
                {
                    "start": area.start,
                    "length": area.length,
                    "patch_offset": area_patch_offset,
                    "sha256": area_sha.hexdigest(),
                }
            )
    tmp.replace(patch_file)

    return {
        "patch_file": patch_file.name,
        "patch_bytes": patch_file.stat().st_size,
        "patch_sha256": patch_sha.hexdigest(),
        "changed_bytes": total_changed,
        "area_count": len(area_records),
        "areas": area_records,
        "transport_mode": transport_mode if "transport_mode" in locals() else "",
        "vddk_disk_info": disk_info if "disk_info" in locals() else {},
    }


def write_flat_vmdk_descriptor(descriptor_path: Path, extent_name: str, capacity_bytes: int, adapter_type: str) -> None:
    sectors = int(capacity_bytes) // SECTOR_SIZE
    descriptor = "\n".join(
        [
            "# Disk DescriptorFile",
            "version=1",
            'encoding="UTF-8"',
            "CID=fffffffe",
            "parentCID=ffffffff",
            'createType="monolithicFlat"',
            "",
            "# Extent description",
            f'RW {sectors} FLAT "{extent_name}" 0',
            "",
            "# The Disk Data Base",
            "#DDB",
            f'ddb.adapterType = "{adapter_type or "lsilogic"}"',
            "",
        ]
    )
    descriptor_path.write_text(descriptor, encoding="utf-8")


def parse_vmdk_extent_files(descriptor: str) -> List[str]:
    extents: List[str] = []
    for raw_line in descriptor.splitlines():
        line = raw_line.strip()
        match = re.match(r'^(RW|RDONLY|NOACCESS)\s+\d+\s+\S+\s+"([^"]+)"', line)
        if match:
            extents.append(match.group(2))
    return extents


def parse_vmdk_adapter_type(descriptor: str) -> str:
    match = re.search(r'ddb\.adapterType\s*=\s*"([^"]+)"', descriptor)
    if match:
        return match.group(1).strip() or "lsilogic"
    return "lsilogic"


def is_vmdk_descriptor(path: Path) -> bool:
    if not path.exists() or path.stat().st_size > 2 * 1024 * 1024:
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    return "# Disk DescriptorFile" in text and bool(parse_vmdk_extent_files(text))


def select_descriptor_for_disk(backup_dir: Path, disk: Dict[str, Any]) -> Tuple[Path, str, List[str]]:
    manifest = read_json(backup_dir / "backup_manifest.json")
    disk_key = str(disk.get("disk_key") or disk.get("key") or "")
    disk_index = int(disk.get("disk_index") or 0)
    descriptors = []
    for item in manifest.get("files", []):
        if item.get("vmdk_role") != "descriptor":
            continue
        score = 0
        if disk_key and str(item.get("disk_key") or "") == disk_key:
            score += 10
        if disk_index and int(item.get("disk_index") or 0) == disk_index:
            score += 5
        descriptors.append((score, item))
    descriptors.sort(key=lambda entry: entry[0], reverse=True)
    for _, item in descriptors:
        name = safe_file_name(str(item.get("name") or ""))
        path = backup_dir / name
        if path.exists() and is_vmdk_descriptor(path):
            text = path.read_text(encoding="utf-8", errors="ignore")
            return path, text, parse_vmdk_extent_files(text)

    for path in sorted(backup_dir.glob("*.vmdk")):
        if is_vmdk_descriptor(path):
            text = path.read_text(encoding="utf-8", errors="ignore")
            return path, text, parse_vmdk_extent_files(text)
    raise CBTStorageError(f"No VMDK descriptor found in baseline backup: {backup_dir}")


def select_stream_optimized_file(
    backup_dir: Path,
    disk: Dict[str, Any],
    manifest: Optional[Dict[str, Any]] = None,
) -> Path:
    """Select a stream-optimized VMDK exported by an HttpNfcLease.

    A normal HttpNfcLease export records disk files as ``disk-0.vmdk``,
    ``disk-1.vmdk``, ... without a local descriptor/extent pair.  The
    manifest's device key ends in the zero-based controller unit number, so
    it is a reliable mapping for multi-disk exports.  The list order remains
    a fallback for older manifests without that key.
    """
    backup_dir = backup_dir.expanduser().resolve()
    manifest = manifest if manifest is not None else read_json(backup_dir / "backup_manifest.json")
    disk_key = str(disk.get("disk_key") or disk.get("key") or "")
    try:
        disk_index = int(disk.get("disk_index") or 0)
    except (TypeError, ValueError):
        disk_index = 0

    candidates: List[Tuple[int, int, Path]] = []
    vmdk_position = 0
    for item_position, item in enumerate(manifest.get("files", []) or []):
        name = str(item.get("name") or "")
        if not name.lower().endswith(".vmdk") or item.get("vmdk_role") in {"descriptor", "extent"}:
            continue
        try:
            path = backup_dir / safe_file_name(name)
        except Exception:
            continue
        if not path.exists():
            continue

        score = 0
        if disk_key and str(item.get("disk_key") or "") == disk_key:
            score += 100
        try:
            if disk_index and int(item.get("disk_index") or 0) == disk_index:
                score += 90
        except (TypeError, ValueError):
            pass
        device_key = str(item.get("device_key") or "")
        if disk_index:
            match = re.search(r":(\d+)$", device_key)
            if match and int(match.group(1)) == disk_index - 1:
                score += 80
        candidates.append((score, -vmdk_position, path))
        vmdk_position += 1

    if not candidates:
        raise CBTStorageError(f"No stream-optimized VMDK file found in backup: {backup_dir}")

    candidates.sort(reverse=True)
    return candidates[0][2]


def materialize_stream_optimized_vmdk(
    backup_dir: Path,
    disk: Dict[str, Any],
    output_dir: Path,
    manifest: Optional[Dict[str, Any]] = None,
    library_path: str = "",
) -> Dict[str, Any]:
    """Convert one stream-optimized VMDK into a local descriptor/flat pair."""
    backup_dir = backup_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source_path = select_stream_optimized_file(backup_dir, disk, manifest=manifest)
    disk_index = int(disk.get("disk_index") or 1)
    capacity_bytes = int(disk.get("capacity_bytes") or 0)
    if capacity_bytes <= 0:
        raise CBTStorageError(f"Missing stream-optimized disk capacity for {source_path}")

    extent_name = f"stream-disk{disk_index}-flat.vmdk"
    descriptor_name = f"stream-disk{disk_index}.vmdk"
    extent_path = output_dir / extent_name
    descriptor_path = output_dir / descriptor_name
    if not extent_path.exists() or extent_path.stat().st_size != capacity_bytes:
        LOGGER.info(
            "Restore: materialisiere VMDK %s nach %s (%.2f GiB)",
            source_path,
            extent_path,
            capacity_bytes / 1024**3,
        )
        result = read_local_vmdk_to_flat(source_path, extent_path, library_path=library_path)
        if int(result.get("bytes") or 0) != capacity_bytes:
            raise CBTStorageError(
                f"Materialized stream-optimized VMDK size mismatch for {source_path.name}: "
                f"expected={capacity_bytes} actual={result.get('bytes')}"
            )
    adapter_type = str(disk.get("adapter_type") or "lsilogic")
    if not descriptor_path.exists():
        write_flat_vmdk_descriptor(descriptor_path, extent_name, capacity_bytes, adapter_type)
    return {
        "source_path": str(source_path),
        "descriptor_path": str(descriptor_path),
        "extent_path": str(extent_path),
        "descriptor_name": descriptor_name,
        "extent_name": extent_name,
        "capacity_bytes": capacity_bytes,
        "adapter_type": adapter_type,
    }


def prepare_local_vmdk_source(backup_dir: Path, disk: Dict[str, Any], work_dir: Path) -> Tuple[Path, str]:
    descriptor_path, descriptor_text, extent_names = select_descriptor_for_disk(backup_dir, disk)
    adapter_type = parse_vmdk_adapter_type(descriptor_text)
    work_dir.mkdir(parents=True, exist_ok=True)
    local_descriptor = work_dir / descriptor_path.name
    local_descriptor.write_text(descriptor_text, encoding="utf-8")

    for extent_name in extent_names:
        safe_name = safe_file_name(extent_name)
        source = backup_dir / safe_name
        if not source.exists() and delta_storage.has_delta_file(backup_dir, safe_name):
            source = delta_storage.materialize_delta_file(backup_dir, safe_name)
        if not source.exists():
            raise CBTStorageError(f"Baseline extent not found: {extent_name} in {backup_dir}")
        target = work_dir / safe_name
        if not target.exists():
            try:
                os.link(source, target)
            except OSError:
                shutil.copyfile(source, target)
    return local_descriptor, adapter_type


def read_local_vmdk_to_flat(
    descriptor_path: Path,
    target_flat: Path,
    library_path: str = "",
    read_chunk_bytes: int = DEFAULT_READ_CHUNK_BYTES,
) -> Dict[str, Any]:
    vddk = get_vddk(library_path)
    chunk_bytes = max(SECTOR_SIZE, int(read_chunk_bytes))
    chunk_bytes -= chunk_bytes % SECTOR_SIZE
    if chunk_bytes <= 0:
        chunk_bytes = SECTOR_SIZE
    target_flat.parent.mkdir(parents=True, exist_ok=True)
    tmp = target_flat.with_suffix(target_flat.suffix + ".part")
    digest = hashlib.sha256()
    bytes_written = 0
    with vddk.open_local() as connection, connection.open_disk(str(descriptor_path)) as disk, tmp.open("wb") as output:
        info = disk.info()
        total_sectors = int(info["capacity_sectors"])
        total_bytes = total_sectors * SECTOR_SIZE
        LOGGER.info(
            "Restore-Fortschritt gestartet: %s -> %s (%.2f GiB)",
            descriptor_path,
            target_flat,
            total_bytes / 1024**3,
        )
        sector_cursor = 0
        sectors_per_read = max(1, chunk_bytes // SECTOR_SIZE)
        next_report_bytes = PROGRESS_INTERVAL_BYTES
        last_report_at = time.monotonic()
        while sector_cursor < total_sectors:
            sectors = min(sectors_per_read, total_sectors - sector_cursor)
            data = disk.read_sectors(sector_cursor, sectors)
            output.write(data)
            digest.update(data)
            bytes_written += len(data)
            sector_cursor += sectors
            now = time.monotonic()
            if bytes_written >= next_report_bytes or now - last_report_at >= PROGRESS_INTERVAL_SECONDS:
                percent = (bytes_written / total_bytes * 100) if total_bytes else 100.0
                LOGGER.info(
                    "Restore-Fortschritt: %.2f/%.2f GiB (%.1f%%) -> %s",
                    bytes_written / 1024**3,
                    total_bytes / 1024**3,
                    percent,
                    target_flat.name,
                )
                while next_report_bytes <= bytes_written:
                    next_report_bytes += PROGRESS_INTERVAL_BYTES
                last_report_at = now
    tmp.replace(target_flat)
    LOGGER.info(
        "Restore-Fortschritt abgeschlossen: %s (%.2f GiB)",
        target_flat,
        bytes_written / 1024**3,
    )
    return {"bytes": bytes_written, "sha256": digest.hexdigest()}


def disk_identity_key(disk: Dict[str, Any]) -> str:
    if disk.get("disk_key") or disk.get("key"):
        return f"key:{disk.get('disk_key') or disk.get('key')}"
    if disk.get("disk_index"):
        return f"index:{disk.get('disk_index')}"
    return f"label:{disk.get('label', '')}"


def materialized_disk_entry_for(
    cbt_manifest: Dict[str, Any],
    requested_disk: Dict[str, Any],
) -> Dict[str, Any]:
    requested_key = disk_identity_key(requested_disk)
    disks = cbt_manifest.get("disks", [])
    for disk in disks:
        if disk_identity_key(disk) == requested_key:
            return disk
    requested_index = int(requested_disk.get("disk_index") or 0)
    if requested_index:
        for disk in disks:
            if int(disk.get("disk_index") or 0) == requested_index:
                return disk
    if len(disks) == 1:
        return disks[0]
    raise CBTStorageError(f"Could not match CBT disk for baseline: {requested_disk}")


def materialize_baseline_flat(
    backup_dir: Path,
    disk: Dict[str, Any],
    output_dir: Path,
    library_path: str = "",
    materialization_root: Optional[Path] = None,
) -> Tuple[Path, str]:
    backup_dir = backup_dir.expanduser().resolve()
    cbt_manifest = load_cbt_manifest(backup_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if cbt_manifest:
        materialized = ensure_materialized_vmdks(
            backup_dir,
            library_path=library_path,
            materialization_root=materialization_root,
        )
        entry = materialized_disk_entry_for(materialized, disk)
        return Path(entry["extent_path"]), str(entry.get("adapter_type") or "lsilogic")

    flat_name = f"baseline-{safe_file_name(str(disk.get('disk_index') or '1'))}-flat.vmdk"
    flat_path = output_dir / flat_name
    if flat_path.exists() and flat_path.stat().st_size == int(disk.get("capacity_bytes") or 0):
        return flat_path, str(disk.get("adapter_type") or "lsilogic")

    source_dir = output_dir / "source"
    try:
        descriptor_path, adapter_type = prepare_local_vmdk_source(backup_dir, disk, source_dir)
        read_local_vmdk_to_flat(descriptor_path, flat_path, library_path=library_path)
        return flat_path, adapter_type
    except CBTStorageError as descriptor_error:
        # HttpNfcLease full exports are stream-optimized VMDKs and do not have
        # a small descriptor file.  VDDK can read them directly, after which
        # the restore path uses the generated flat representation.
        try:
            materialized = materialize_stream_optimized_vmdk(
                backup_dir,
                disk,
                output_dir,
                library_path=library_path,
            )
        except Exception as stream_error:
            raise CBTStorageError(
                f"Baseline has no VMDK descriptor and stream-optimized "
                f"materialization failed: {stream_error}"
            ) from stream_error
        return Path(materialized["extent_path"]), str(materialized["adapter_type"])


def apply_patch_file(base_flat: Path, patch_file: Path, areas: List[Dict[str, Any]], target_flat: Path) -> Dict[str, Any]:
    target_flat.parent.mkdir(parents=True, exist_ok=True)
    if target_flat.exists():
        target_flat.unlink()
    LOGGER.info(
        "Restore: CBT-Patch wird angewendet (%s, %d Bereich(e))",
        patch_file.name,
        len(areas),
    )
    shutil.copyfile(base_flat, target_flat)
    digest = hashlib.sha256()
    with patch_file.open("rb") as patch, target_flat.open("r+b") as target:
        for area in areas:
            start = int(area.get("start") or 0)
            length = int(area.get("length") or 0)
            patch_offset = int(area.get("patch_offset") or 0)
            expected_sha = str(area.get("sha256") or "")
            patch.seek(patch_offset)
            data = patch.read(length)
            if len(data) != length:
                raise CBTStorageError(
                    f"CBT patch is truncated: {patch_file.name} offset={patch_offset} length={length}"
                )
            if expected_sha and hashlib.sha256(data).hexdigest() != expected_sha:
                raise CBTStorageError(f"CBT area checksum mismatch in {patch_file.name} at offset={patch_offset}")
            target.seek(start)
            target.write(data)
    with target_flat.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    LOGGER.info("Restore: CBT-Patch fertig, Zieldatei %s (%.2f GiB)", target_flat, target_flat.stat().st_size / 1024**3)
    return {"bytes": target_flat.stat().st_size, "sha256": digest.hexdigest()}


def ensure_materialized_vmdks(
    backup_dir: Path,
    manifest: Optional[Dict[str, Any]] = None,
    library_path: str = "",
    materialization_root: Optional[Path] = None,
) -> Dict[str, Any]:
    backup_dir = backup_dir.expanduser().resolve()
    cbt_manifest = load_cbt_manifest(backup_dir)
    if not cbt_manifest:
        raise CBTStorageError(f"No CBT manifest found: {backup_dir}")
    target_dir = hydrated_cbt_dir(backup_dir, materialization_root=materialization_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Restore: CBT-Kette wird vorbereitet: %s", backup_dir)
    raw_previous_dir = str(cbt_manifest.get("previous_dir") or "")
    if not raw_previous_dir:
        raise CBTStorageError("CBT manifest has no previous_dir baseline")
    previous_dir = Path(raw_previous_dir)
    if not previous_dir.is_absolute():
        previous_dir = (backup_dir / previous_dir).resolve()
    if not previous_dir.exists():
        raise CBTStorageError(f"CBT baseline backup not found: {previous_dir}")

    result_disks: List[Dict[str, Any]] = []
    for disk in cbt_manifest.get("disks", []):
        descriptor_name = safe_file_name(str(disk.get("descriptor_name") or f"cbt-disk{disk.get('disk_index', 1)}.vmdk"))
        extent_name = safe_file_name(str(disk.get("extent_name") or f"{Path(descriptor_name).stem}-flat.vmdk"))
        descriptor_path = target_dir / descriptor_name
        extent_path = target_dir / extent_name
        capacity_bytes = int(disk.get("capacity_bytes") or 0)
        adapter_type = str(disk.get("adapter_type") or "lsilogic")
        if extent_path.exists() and extent_path.stat().st_size == capacity_bytes and descriptor_path.exists():
            LOGGER.info("Restore: bereits materialisierte Disk wird wiederverwendet: %s", extent_path)
            result_disks.append({**disk, "descriptor_path": str(descriptor_path), "extent_path": str(extent_path)})
            continue

        baseline_dir = target_dir / f"baseline-{disk.get('disk_index', 1)}"
        LOGGER.info(
            "Restore: Baseline fuer Disk %s wird materialisiert (%.2f GiB)",
            disk.get("disk_index", 1),
            capacity_bytes / 1024**3,
        )
        base_flat, base_adapter_type = materialize_baseline_flat(
            previous_dir,
            disk,
            baseline_dir,
            library_path=library_path,
            materialization_root=materialization_root,
        )
        adapter_type = adapter_type or base_adapter_type
        patch_file = backup_dir / safe_file_name(str(disk.get("patch_file") or ""))
        if not patch_file.exists():
            raise CBTStorageError(f"CBT patch file not found: {patch_file}")
        apply_patch_file(base_flat, patch_file, list(disk.get("areas") or []), extent_path)
        write_flat_vmdk_descriptor(descriptor_path, extent_name, capacity_bytes, adapter_type)
        LOGGER.info("Restore: Disk %s fertig materialisiert: %s", disk.get("disk_index", 1), extent_path)
        result_disks.append(
            {
                **disk,
                "adapter_type": adapter_type,
                "descriptor_name": descriptor_name,
                "extent_name": extent_name,
                "descriptor_path": str(descriptor_path),
                "extent_path": str(extent_path),
            }
        )

    return {
        "format": CBT_FORMAT,
        "directory": str(target_dir),
        "disks": result_disks,
    }


def verify_cbt_backup(backup_dir: Path, skip_hash: bool = False) -> List[str]:
    errors: List[str] = []
    try:
        cbt_manifest = load_cbt_manifest(backup_dir)
    except Exception as exc:
        return [str(exc)]
    if not cbt_manifest:
        return ["Missing CBT manifest"]

    raw_previous_dir = str(cbt_manifest.get("previous_dir") or "")
    if not raw_previous_dir:
        errors.append("CBT manifest has no previous_dir baseline")
        previous_dir = Path()
    else:
        previous_dir = Path(raw_previous_dir)
    if raw_previous_dir and not previous_dir.is_absolute():
        previous_dir = (backup_dir / previous_dir).resolve()
    if raw_previous_dir and not previous_dir.exists():
        errors.append(f"Missing CBT baseline backup: {previous_dir}")

    for disk in cbt_manifest.get("disks", []):
        patch_name = str(disk.get("patch_file") or "")
        try:
            patch_path = backup_dir / safe_file_name(patch_name)
        except Exception as exc:
            errors.append(str(exc))
            continue
        if not patch_path.exists():
            errors.append(f"Missing CBT patch file: {patch_name}")
            continue
        expected_bytes = int(disk.get("patch_bytes") or 0)
        actual_bytes = patch_path.stat().st_size
        if actual_bytes != expected_bytes:
            errors.append(f"CBT patch size mismatch for {patch_name}: expected={expected_bytes} actual={actual_bytes}")
        if not skip_hash:
            expected_hash = str(disk.get("patch_sha256") or "")
            if expected_hash and sha256_file(patch_path) != expected_hash:
                errors.append(f"CBT patch SHA256 mismatch for {patch_name}")
    return errors
