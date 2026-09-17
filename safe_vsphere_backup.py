#!/usr/bin/env python3
"""
Safe vSphere/vCenter API backup tool.

The protected single-VM path is still deliberately gated: the operator must
confirm the configured VM name before a live backup starts. The interactive
selector can back up explicitly selected VMs, but it never runs a blind
"backup all VMs" job.
"""

from __future__ import annotations

import argparse
import ctypes.util
import hashlib
import json
import logging
import os
import re
import shutil
import ssl
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import requests
import urllib3
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl

import delta_storage
import vddk_cbt

DEFAULT_TARGET_VM_NAME = "DocuSign"
TARGET_VM_NAME = DEFAULT_TARGET_VM_NAME
EXPECTED_CPU = 2
EXPECTED_RAM_MB = 4096
EXPECTED_DISK_COUNT = 1
EXPECTED_DISK_SIZE_GB = 25.0
ALLOWED_POWER_STATES = {"poweredOff", "poweredOn"}
LIVE_SNAPSHOT_PREFIX = "vsphere-live-backup"
REMOTE_TEMP_DIR = "vsphere_api_backup_tmp"
SKIPPED_REMOVABLE_EXTENSIONS = {".iso", ".flp"}

SCRIPT_DIR = Path(__file__).resolve().parent
# The dedicated 5-TB filesystem is mounted into the SMB share at this path.
# Keep this absolute so every entry point uses the same storage even when it
# is launched from a different working directory.
DEFAULT_OUTPUT_DIR = Path("/srv/samba/Backup-Alpein/Backup")
DEFAULT_LOG_FILE = SCRIPT_DIR / "logs" / "vsphere_backup.log"
DEFAULT_MIN_FREE_GB = 30.0
DEFAULT_READ_TIMEOUT_SECONDS = 300
DEFAULT_DATASTORE_COPY_STALL_TIMEOUT_SECONDS = 3600
DEFAULT_DOWNLOAD_STALL_TIMEOUT_SECONDS = DEFAULT_READ_TIMEOUT_SECONDS
DEFAULT_CLEANUP_RETRY_ATTEMPTS = 3
DEFAULT_CLEANUP_RETRY_DELAY_SECONDS = 30

LOGGER = logging.getLogger("vsphere_api_backup")
INTERRUPTED_MESSAGE = (
    "Interrupted by user or closed terminal. "
    "For long backups, start with ./start_backup_screen.sh or run inside screen/tmux."
)


def vddk_library_path() -> str:
    """Return the VixDiskLib shared library path if VMware VDDK is installed."""
    env_path = os.environ.get("VDDK_LIBRARY") or os.environ.get("VIXDISKLIB_LIBRARY")
    candidates = [
        env_path,
        str(SCRIPT_DIR / "vendor" / "vddk" / "lib64" / "libvixDiskLib.so"),
        str(SCRIPT_DIR / "vendor" / "vddk" / "lib" / "libvixDiskLib.so"),
        "/usr/lib/vmware-vix-disklib/lib64/libvixDiskLib.so",
        "/usr/lib/vmware-vix-disklib/libvixDiskLib.so",
        "/opt/vmware-vix-disklib/lib64/libvixDiskLib.so",
        "/opt/vmware-vix-disklib/libvixDiskLib.so",
        ctypes.util.find_library("vixDiskLib"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if candidate.startswith("/"):
            if Path(candidate).exists():
                return candidate
            continue
        return candidate
    return ""


def vddk_backend_status() -> Dict[str, Any]:
    return vddk_cbt.probe_vddk(vddk_library_path())


@dataclass(frozen=True)
class VSphereConfig:
    host: str
    user: str
    password: str
    port: int = 443
    ssl_verify: bool = False
    vddk_thumbprint: str = ""
    output_dir: Path = DEFAULT_OUTPUT_DIR
    target_vm_name: str = DEFAULT_TARGET_VM_NAME
    expected_cpu: int = EXPECTED_CPU
    expected_ram_mb: int = EXPECTED_RAM_MB
    expected_disk_count: int = EXPECTED_DISK_COUNT
    expected_disk_size_gb: float = EXPECTED_DISK_SIZE_GB
    expected_instance_uuid: str = ""
    min_free_gb: float = DEFAULT_MIN_FREE_GB
    lease_timeout_seconds: int = 300
    read_timeout_seconds: int = DEFAULT_READ_TIMEOUT_SECONDS
    download_stall_timeout_seconds: int = DEFAULT_DOWNLOAD_STALL_TIMEOUT_SECONDS
    datastore_copy_stall_timeout_seconds: int = DEFAULT_DATASTORE_COPY_STALL_TIMEOUT_SECONDS
    live_snapshot_quiesce: bool = False


# Backward-compatible aliases for older local tests/scripts copied from the
# previous prototype.
EsxiConfig = VSphereConfig


class SafetyError(RuntimeError):
    """Raised when a production safety rule blocks the requested action."""


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def exception_message(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def run_with_cleanup_retries(action: str, func: Any) -> Any:
    for attempt in range(1, DEFAULT_CLEANUP_RETRY_ATTEMPTS + 1):
        try:
            return func()
        except TimeoutError:
            raise
        except Exception as exc:
            if attempt >= DEFAULT_CLEANUP_RETRY_ATTEMPTS:
                raise
            delay = DEFAULT_CLEANUP_RETRY_DELAY_SECONDS * attempt
            LOGGER.warning(
                "%s failed on attempt %s/%s: %s; retrying in %ss",
                action,
                attempt,
                DEFAULT_CLEANUP_RETRY_ATTEMPTS,
                exc,
                delay,
            )
            time.sleep(delay)


def attach_transfer_failure_metadata(
    exc: BaseException,
    remote_copy: Dict[str, Any],
    transfer_method: str,
) -> None:
    if remote_copy.get("copies") or remote_copy.get("directories"):
        try:
            setattr(exc, "remote_temporary_copy", remote_copy)
            setattr(exc, "transfer_method", transfer_method)
        except Exception:
            LOGGER.debug(
                "Could not attach transfer failure metadata to %s",
                type(exc).__name__,
                exc_info=True,
            )


def record_transfer_failure_metadata(manifest: Dict[str, Any], exc: BaseException) -> None:
    remote_copy = getattr(exc, "remote_temporary_copy", None)
    if isinstance(remote_copy, dict):
        manifest["remote_temporary_copy"] = remote_copy
    transfer_method = getattr(exc, "transfer_method", None)
    if transfer_method:
        manifest["transfer_method"] = transfer_method


def is_missing_removable_media_export_error(exc: BaseException) -> bool:
    text = exception_message(exc).lower()
    if "not found" not in text:
        return False
    return any(ext in text for ext in SKIPPED_REMOVABLE_EXTENSIONS)


def parse_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def split_vsphere_endpoint(raw_host: str, raw_port: str = "443") -> tuple[str, int]:
    value = (raw_host or "").strip()
    if not value:
        return "", int(raw_port or "443")
    candidate = value if "://" in value else f"//{value}"
    parsed = urlsplit(candidate)
    host = parsed.hostname or parsed.netloc or value
    host = host.strip("[]")
    port = int(raw_port or "443")
    if parsed.port is not None and (not raw_port or raw_port == "443"):
        port = int(parsed.port)
    return host, port


def endpoint_netloc(config: VSphereConfig) -> str:
    if config.port == 443:
        return config.host
    return f"{config.host}:{config.port}"


def read_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def discover_config_file(explicit_path: Optional[str]) -> Optional[Path]:
    if explicit_path:
        path = Path(explicit_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        return path

    candidates = [
        Path.cwd() / "credentials.env",
        SCRIPT_DIR / "credentials.env",
        Path.cwd() / "config.env",
        SCRIPT_DIR / "config.env",
        Path.cwd() / ".env",
        SCRIPT_DIR / ".env",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return None


def load_config(explicit_path: Optional[str]) -> VSphereConfig:
    config_file = discover_config_file(explicit_path)
    values: Dict[str, str] = {}
    if config_file:
        values.update(read_env_file(config_file))

    # Environment variables override the env file. This allows temporary
    # password injection without writing secrets to disk.
    keys = {
        "VSPHERE_SERVER",
        "VSPHERE_HOST",
        "VSPHERE_USER",
        "VSPHERE_PASSWORD",
        "VSPHERE_PORT",
        "VSPHERE_SSL_VERIFY",
        "VDDK_THUMBPRINT",
        "VSPHERE_TARGET_VM",
        "VSPHERE_EXPECTED_CPU",
        "VSPHERE_EXPECTED_RAM_MB",
        "VSPHERE_EXPECTED_DISK_COUNT",
        "VSPHERE_EXPECTED_DISK_SIZE_GB",
        "ESXI_HOST",
        "ESXI_USER",
        "ESXI_PASSWORD",
        "ESXI_PORT",
        "ESXI_SSL_VERIFY",
        "BACKUP_OUTPUT_DIR",
        "EXPECTED_INSTANCE_UUID",
        "MIN_FREE_GB",
        "LEASE_TIMEOUT_SECONDS",
        "READ_TIMEOUT_SECONDS",
        "DOWNLOAD_STALL_TIMEOUT_SECONDS",
        "DATASTORE_COPY_STALL_TIMEOUT_SECONDS",
        "DATASTORE_COPY_TIMEOUT_SECONDS",
        "LIVE_SNAPSHOT_QUIESCE",
    }
    for key in keys:
        if key in os.environ:
            values[key] = os.environ[key]

    legacy_map = {
        "VSPHERE_HOST": "ESXI_HOST",
        "VSPHERE_USER": "ESXI_USER",
        "VSPHERE_PASSWORD": "ESXI_PASSWORD",
        "VSPHERE_PORT": "ESXI_PORT",
        "VSPHERE_SSL_VERIFY": "ESXI_SSL_VERIFY",
    }
    for new_key, old_key in legacy_map.items():
        if not values.get(new_key) and values.get(old_key):
            values[new_key] = values[old_key]

    if not values.get("VSPHERE_HOST") and values.get("VSPHERE_SERVER"):
        values["VSPHERE_HOST"] = values["VSPHERE_SERVER"]

    missing = [k for k in ("VSPHERE_HOST", "VSPHERE_USER", "VSPHERE_PASSWORD") if not values.get(k)]
    if missing:
        hint = "Fill credentials.env from credentials.example.env or export the variables."
        raise SafetyError(f"Missing required config value(s): {', '.join(missing)}. {hint}")

    output_dir = Path(values.get("BACKUP_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR))).expanduser()
    if not output_dir.is_absolute():
        output_dir = (SCRIPT_DIR / output_dir).resolve()

    host, port = split_vsphere_endpoint(values["VSPHERE_HOST"], values.get("VSPHERE_PORT", "443"))

    config = VSphereConfig(
        host=host,
        user=values["VSPHERE_USER"],
        password=values["VSPHERE_PASSWORD"],
        port=port,
        ssl_verify=parse_bool(values.get("VSPHERE_SSL_VERIFY", "false")),
        vddk_thumbprint=values.get("VDDK_THUMBPRINT", "").strip(),
        output_dir=output_dir,
        target_vm_name=values.get("VSPHERE_TARGET_VM", DEFAULT_TARGET_VM_NAME).strip() or DEFAULT_TARGET_VM_NAME,
        expected_cpu=int(values.get("VSPHERE_EXPECTED_CPU", EXPECTED_CPU)),
        expected_ram_mb=int(values.get("VSPHERE_EXPECTED_RAM_MB", EXPECTED_RAM_MB)),
        expected_disk_count=int(values.get("VSPHERE_EXPECTED_DISK_COUNT", EXPECTED_DISK_COUNT)),
        expected_disk_size_gb=float(values.get("VSPHERE_EXPECTED_DISK_SIZE_GB", EXPECTED_DISK_SIZE_GB)),
        expected_instance_uuid=values.get("EXPECTED_INSTANCE_UUID", "").strip(),
        min_free_gb=float(values.get("MIN_FREE_GB", DEFAULT_MIN_FREE_GB)),
        lease_timeout_seconds=int(values.get("LEASE_TIMEOUT_SECONDS", "300")),
        read_timeout_seconds=int(values.get("READ_TIMEOUT_SECONDS", DEFAULT_READ_TIMEOUT_SECONDS)),
        download_stall_timeout_seconds=int(
            values.get(
                "DOWNLOAD_STALL_TIMEOUT_SECONDS",
                values.get("READ_TIMEOUT_SECONDS", DEFAULT_DOWNLOAD_STALL_TIMEOUT_SECONDS),
            )
        ),
        datastore_copy_stall_timeout_seconds=int(
            values.get(
                "DATASTORE_COPY_STALL_TIMEOUT_SECONDS",
                values.get("DATASTORE_COPY_TIMEOUT_SECONDS", DEFAULT_DATASTORE_COPY_STALL_TIMEOUT_SECONDS),
            )
        ),
        live_snapshot_quiesce=parse_bool(values.get("LIVE_SNAPSHOT_QUIESCE", "false")),
    )
    apply_config_defaults(config)
    return config


def apply_config_defaults(config: VSphereConfig) -> None:
    """Keep legacy module globals aligned for helper code and old tests."""
    global TARGET_VM_NAME, EXPECTED_CPU, EXPECTED_RAM_MB, EXPECTED_DISK_COUNT, EXPECTED_DISK_SIZE_GB
    TARGET_VM_NAME = config.target_vm_name
    EXPECTED_CPU = config.expected_cpu
    EXPECTED_RAM_MB = config.expected_ram_mb
    EXPECTED_DISK_COUNT = config.expected_disk_count
    EXPECTED_DISK_SIZE_GB = config.expected_disk_size_gb


def setup_logging(verbose: bool = False) -> None:
    DEFAULT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handlers: List[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(DEFAULT_LOG_FILE, encoding="utf-8"),
    ]
    logging.basicConfig(level=level, handlers=handlers)
    for handler in logging.root.handlers:
        handler.setFormatter(formatter)


def normalize_enum(value: Any) -> str:
    text = str(value)
    return text.rsplit(".", 1)[-1]


def safe_filename(value: str, fallback: str) -> str:
    value = unquote(value or "").strip()
    value = Path(value).name
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    value = value.strip("._")
    return value or fallback


def bytes_to_gb(value: int) -> float:
    return value / float(1024**3)


def backup_method_for_power_state(power_state: str) -> str:
    if power_state == "poweredOff":
        return "vm_export"
    if power_state == "poweredOn":
        return "snapshot_export"
    return "unsupported"


def parse_datastore_path(value: str) -> tuple[str, str]:
    match = re.match(r"^\[(?P<datastore>[^\]]+)\]\s*(?P<path>.+)$", value or "")
    if not match:
        raise ValueError(f"Invalid datastore path: {value!r}")
    return match.group("datastore"), match.group("path").strip("/")


def build_datastore_url(config: VSphereConfig, datacenter_name: str, datastore: str, path: str) -> str:
    return (
        f"https://{endpoint_netloc(config)}/folder/{quote(path)}"
        f"?dcPath={quote(datacenter_name)}&dsName={quote(datastore)}"
    )


def datastore_path_join(datastore: str, *parts: str) -> str:
    clean = [part.strip("/") for part in parts if part.strip("/")]
    return f"[{datastore}] {'/'.join(clean)}"


def datastore_sibling_path(datastore_file: str, filename: str) -> str:
    folder = str(Path(datastore_file).parent)
    if folder in {"", "."}:
        return filename
    return f"{folder}/{filename}"


def unwrap_vsphere_value(payload: Any) -> Any:
    if isinstance(payload, dict) and set(payload.keys()) == {"value"}:
        return payload["value"]
    return payload


class VSphereRestClient:
    """Small vCenter REST client for session handling and inventory lookups."""

    def __init__(self, config: VSphereConfig):
        self.config = config
        self.http = requests.Session()
        self.session_id = ""
        self.session_path = ""

    @property
    def base_url(self) -> str:
        return f"https://{endpoint_netloc(self.config)}"

    def login(self) -> None:
        attempts = [
            ("/api/session", "json"),
            ("/rest/com/vmware/cis/session", "value"),
        ]
        errors: List[str] = []
        for path, mode in attempts:
            try:
                response = self.http.post(
                    f"{self.base_url}{path}",
                    auth=(self.config.user, self.config.password),
                    verify=self.config.ssl_verify,
                    timeout=(30, self.config.read_timeout_seconds),
                )
                if response.status_code in {404, 405}:
                    errors.append(f"{path}: HTTP {response.status_code}")
                    continue
                response.raise_for_status()
                payload = response.json()
                if mode == "value":
                    payload = unwrap_vsphere_value(payload)
                if isinstance(payload, str):
                    session_id = payload
                elif isinstance(payload, dict) and isinstance(payload.get("value"), str):
                    session_id = payload["value"]
                else:
                    session_id = str(payload or "").strip('"')
                if not session_id:
                    raise RuntimeError(f"{path}: empty vSphere REST session id")
                self.session_id = session_id
                self.session_path = path
                self.http.headers.update(
                    {
                        "vmware-api-session-id": session_id,
                        "x-api-session-id": session_id,
                    }
                )
                LOGGER.info("Connected to vCenter REST API via %s", path)
                return
            except Exception as exc:
                errors.append(f"{path}: {exception_message(exc)}")
        raise RuntimeError("vCenter REST login failed: " + "; ".join(errors))

    def close(self) -> None:
        if not self.session_id or not self.session_path:
            self.http.close()
            return
        try:
            self.http.delete(
                f"{self.base_url}{self.session_path}",
                verify=self.config.ssl_verify,
                timeout=(30, self.config.read_timeout_seconds),
            )
        except Exception as exc:
            LOGGER.debug("vCenter REST logout failed: %s", exc)
        finally:
            self.session_id = ""
            self.http.close()

    def get(self, path: str) -> Any:
        response = self.http.get(
            f"{self.base_url}{path}",
            verify=self.config.ssl_verify,
            timeout=(30, self.config.read_timeout_seconds),
        )
        response.raise_for_status()
        return unwrap_vsphere_value(response.json())

    def list_vms(self) -> List[Dict[str, Any]]:
        attempts = ("/api/vcenter/vm", "/rest/vcenter/vm")
        errors: List[str] = []
        for path in attempts:
            try:
                payload = self.get(path)
                if isinstance(payload, list):
                    return [dict(item) for item in payload if isinstance(item, dict)]
                raise RuntimeError(f"{path}: expected list, got {type(payload).__name__}")
            except Exception as exc:
                errors.append(f"{path}: {exception_message(exc)}")
        raise RuntimeError("Could not read vCenter VM inventory: " + "; ".join(errors))

    def vm_details(self, vm_id: str) -> Dict[str, Any]:
        for path in (f"/api/vcenter/vm/{quote(vm_id)}", f"/rest/vcenter/vm/{quote(vm_id)}"):
            try:
                payload = self.get(path)
                if isinstance(payload, dict):
                    return payload
            except Exception:
                continue
        return {}


class VSphereSession:
    def __init__(self, config: VSphereConfig):
        self.config = config
        self.si = None
        self.rest: Optional[VSphereRestClient] = None
        self.rest_error = ""

    def __enter__(self) -> "VSphereSession":
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if not self.config.ssl_verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        LOGGER.info("Connecting to vSphere endpoint %s:%s", self.config.host, self.config.port)
        self.si = SmartConnect(
            host=self.config.host,
            port=self.config.port,
            user=self.config.user,
            pwd=self.config.password,
            sslContext=context,
            connectionPoolTimeout=900,
        )
        self.rest = VSphereRestClient(self.config)
        try:
            self.rest.login()
        except Exception as exc:
            self.rest_error = exception_message(exc)
            LOGGER.warning("vCenter REST API login unavailable; managed-object API remains active: %s", exc)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.rest:
            self.rest.close()
            self.rest = None
        if self.si:
            Disconnect(self.si)
            self.si = None
            LOGGER.info("Disconnected from vSphere endpoint")

    @property
    def content(self):
        if not self.si:
            raise RuntimeError("vSphere session is not connected")
        return self.si.RetrieveContent()

    @property
    def cookie(self) -> str:
        if not self.si:
            raise RuntimeError("vSphere session is not connected")
        cookie = getattr(self.si._stub, "cookie", "")
        if not cookie:
            raise RuntimeError("Could not read vSphere API session cookie")
        return cookie

    def all_vms(self) -> List[vim.VirtualMachine]:
        view = self.content.viewManager.CreateContainerView(
            self.content.rootFolder, [vim.VirtualMachine], True
        )
        try:
            return list(view.view)
        finally:
            view.Destroy()

    def rest_inventory_by_moref(self) -> Dict[str, Dict[str, Any]]:
        if not self.rest or not self.rest.session_id:
            return {}
        try:
            result: Dict[str, Dict[str, Any]] = {}
            for item in self.rest.list_vms():
                vm_id = str(item.get("vm") or item.get("vm_id") or "")
                if vm_id:
                    result[vm_id] = item
            return result
        except Exception as exc:
            self.rest_error = exception_message(exc)
            LOGGER.warning("vCenter REST inventory unavailable: %s", exc)
            return {}

    def find_target_vm(self) -> vim.VirtualMachine:
        matches = [vm for vm in self.all_vms() if vm.name == TARGET_VM_NAME]
        if not matches:
            raise SafetyError(f"Target VM not found: {TARGET_VM_NAME}")
        if len(matches) > 1:
            ids = ", ".join(getattr(vm, "_moId", "?") for vm in matches)
            raise SafetyError(f"Multiple VMs named {TARGET_VM_NAME} found: {ids}")
        return matches[0]

    def find_docsign_vm(self) -> vim.VirtualMachine:
        return self.find_target_vm()


EsxiSession = VSphereSession


def get_disk_info(device: vim.vm.device.VirtualDisk) -> Dict[str, Any]:
    backing = device.backing
    datastore = ""
    try:
        datastore = backing.datastore.name
    except Exception:
        datastore = ""

    return {
        "label": getattr(device.deviceInfo, "label", f"disk-{device.key}"),
        "key": int(device.key),
        "capacity_bytes": int(device.capacityInKB) * 1024,
        "capacity_gb": round((int(device.capacityInKB) * 1024) / float(1024**3), 3),
        "file_name": getattr(backing, "fileName", ""),
        "datastore": datastore,
        "thin_provisioned": getattr(backing, "thinProvisioned", None),
        "backing_type": type(backing).__name__,
        "change_id": getattr(backing, "changeId", ""),
    }


def cbt_state_from_vm_info(vm_info: Dict[str, Any]) -> Dict[str, Any]:
    disks = []
    for disk in vm_info.get("disks", []) or []:
        disks.append(
            {
                "key": disk.get("key"),
                "label": disk.get("label"),
                "file_name": disk.get("file_name"),
                "capacity_bytes": disk.get("capacity_bytes"),
                "change_id": disk.get("change_id") or "",
            }
        )
    return {
        "change_tracking_enabled": bool(vm_info.get("change_tracking_enabled")),
        "valid": bool(vm_info.get("change_tracking_enabled")) and all(item.get("change_id") for item in disks),
        "disks": disks,
    }


def cbt_state_from_snapshot(snapshot: vim.vm.Snapshot, fallback_vm_info: Dict[str, Any]) -> Dict[str, Any]:
    snapshot_config = getattr(snapshot, "config", None)
    hardware = getattr(snapshot_config, "hardware", None)
    devices = getattr(hardware, "device", None) or []
    if not devices:
        state = cbt_state_from_vm_info(fallback_vm_info)
        state["source"] = "vm_config_fallback"
        state["snapshot_moref"] = getattr(snapshot, "_moId", "")
        return state

    change_tracking_enabled = bool(
        getattr(snapshot_config, "changeTrackingEnabled", fallback_vm_info.get("change_tracking_enabled", False))
    )
    disks = []
    for device in devices:
        if isinstance(device, vim.vm.device.VirtualDisk):
            disk = get_disk_info(device)
            disks.append(
                {
                    "key": disk.get("key"),
                    "label": disk.get("label"),
                    "file_name": disk.get("file_name"),
                    "capacity_bytes": disk.get("capacity_bytes"),
                    "change_id": disk.get("change_id") or "",
                }
            )

    return {
        "change_tracking_enabled": change_tracking_enabled,
        "valid": change_tracking_enabled and bool(disks) and all(item.get("change_id") for item in disks),
        "source": "snapshot_config",
        "snapshot_moref": getattr(snapshot, "_moId", ""),
        "disks": disks,
    }


def query_changed_disk_areas(
    vm: vim.VirtualMachine,
    snapshot: vim.vm.Snapshot,
    disk_key: int,
    previous_change_id: str,
    capacity_bytes: int,
) -> List[Dict[str, int]]:
    """Query VMware CBT changed areas in byte offsets for one disk."""
    if not previous_change_id:
        raise SafetyError(f"Missing previous CBT changeId for disk key {disk_key}")
    if capacity_bytes <= 0:
        raise SafetyError(f"Invalid disk capacity for CBT query: disk key {disk_key}")

    areas: List[Dict[str, int]] = []
    offset = 0
    while offset < capacity_bytes:
        info = vm.QueryChangedDiskAreas(
            snapshot=snapshot,
            deviceKey=int(disk_key),
            startOffset=int(offset),
            changeId=str(previous_change_id),
        )
        changed = getattr(info, "changedArea", []) or []
        for area in changed:
            start = int(getattr(area, "start", 0) or 0)
            length = int(getattr(area, "length", 0) or 0)
            if length > 0:
                areas.append({"start": start, "length": length})

        start_offset = int(getattr(info, "startOffset", offset) or offset)
        length = int(getattr(info, "length", 0) or 0)
        next_offset = start_offset + length
        if length <= 0 or next_offset <= offset:
            break
        offset = next_offset

    return areas


def get_nic_info(device: vim.vm.device.VirtualEthernetCard) -> Dict[str, Any]:
    backing = device.backing
    network_name = getattr(backing, "deviceName", "")
    try:
        if not network_name and getattr(backing, "network", None) is not None:
            network_name = backing.network.name
    except Exception:
        network_name = ""
    connectable = getattr(device, "connectable", None)
    return {
        "label": getattr(device.deviceInfo, "label", f"nic-{device.key}"),
        "key": int(device.key),
        "mac_address": getattr(device, "macAddress", ""),
        "address_type": getattr(device, "addressType", ""),
        "network_name": network_name,
        "connected": bool(getattr(connectable, "connected", False)) if connectable else False,
        "start_connected": bool(getattr(connectable, "startConnected", False)) if connectable else False,
        "device_type": type(device).__name__,
    }


def collect_vm_info(vm: vim.VirtualMachine) -> Dict[str, Any]:
    config = vm.config
    summary = vm.summary
    storage = getattr(summary, "storage", None)
    storage_committed_bytes = int(getattr(storage, "committed", 0) or 0)
    storage_uncommitted_bytes = int(getattr(storage, "uncommitted", 0) or 0)
    disks = []
    nics = []
    for device in config.hardware.device:
        if isinstance(device, vim.vm.device.VirtualDisk):
            disks.append(get_disk_info(device))
        elif isinstance(device, vim.vm.device.VirtualEthernetCard):
            nics.append(get_nic_info(device))

    return {
        "name": vm.name,
        "moref": getattr(vm, "_moId", ""),
        "instance_uuid": getattr(config, "instanceUuid", ""),
        "bios_uuid": getattr(summary.config, "uuid", ""),
        "power_state": normalize_enum(summary.runtime.powerState),
        "template": bool(getattr(config, "template", False)),
        "num_cpu": int(config.hardware.numCPU),
        "memory_mb": int(config.hardware.memoryMB),
        "guest_full_name": getattr(config, "guestFullName", ""),
        "vmx_path": getattr(config.files, "vmPathName", ""),
        "storage_committed_bytes": storage_committed_bytes,
        "storage_committed_gb": round(bytes_to_gb(storage_committed_bytes), 3),
        "storage_uncommitted_bytes": storage_uncommitted_bytes,
        "storage_uncommitted_gb": round(bytes_to_gb(storage_uncommitted_bytes), 3),
        "change_tracking_enabled": bool(getattr(config, "changeTrackingEnabled", False)),
        "has_snapshot": bool(getattr(vm, "snapshot", None)),
        "disks": disks,
        "nics": nics,
    }


def ensure_change_tracking_enabled(
    vm: vim.VirtualMachine,
    vm_info: Dict[str, Any],
    config: VSphereConfig,
) -> Dict[str, Any]:
    target_name = str(vm_info.get("name") or getattr(vm, "name", TARGET_VM_NAME))
    already_enabled = bool(vm_info.get("change_tracking_enabled"))
    result: Dict[str, Any] = {
        "requested": True,
        "changed": False,
        "previous_change_tracking_enabled": already_enabled,
        "current_change_tracking_enabled": already_enabled,
    }
    if already_enabled:
        result["detail"] = "already_enabled"
        return result

    LOGGER.info("Enabling Changed Block Tracking for %s", target_name)
    spec = vim.vm.ConfigSpec()
    spec.changeTrackingEnabled = True
    task = vm.ReconfigVM_Task(spec=spec)
    wait_for_task(task, f"Enable CBT for {target_name}")

    refreshed = collect_vm_info(vm)
    current_enabled = bool(refreshed.get("change_tracking_enabled"))
    result.update(
        {
            "changed": True,
            "current_change_tracking_enabled": current_enabled,
            "enabled_at": now_utc(),
            "detail": "enabled",
        }
    )
    if not current_enabled:
        raise RuntimeError(
            f"CBT enable task completed, but vSphere still reports CBT disabled for {target_name}"
        )
    LOGGER.info("Changed Block Tracking enabled for %s", target_name)
    return result


def check_free_space(output_dir: Path, required_gb: float) -> Dict[str, Any]:
    probe = output_dir
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    free_gb = bytes_to_gb(usage.free)
    return {
        "path": str(probe),
        "free_gb": round(free_gb, 2),
        "required_gb": required_gb,
        "ok": free_gb >= required_gb,
    }


def validate_target_vm(vm_info: Dict[str, Any], config: VSphereConfig) -> List[Dict[str, Any]]:
    disks = vm_info["disks"]
    target_vm_name = config.target_vm_name or TARGET_VM_NAME
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, severity: str = "fatal") -> None:
        checks.append({"name": name, "ok": bool(ok), "severity": severity, "detail": detail})

    add("vm_name", vm_info["name"] == target_vm_name, f"found={vm_info['name']!r}, expected={target_vm_name!r}")
    if config.expected_instance_uuid:
        add(
            "instance_uuid",
            vm_info["instance_uuid"] == config.expected_instance_uuid,
            f"found={vm_info['instance_uuid']!r}, expected={config.expected_instance_uuid!r}",
        )

    add("not_template", not vm_info["template"], f"template={vm_info['template']}")
    backup_method = backup_method_for_power_state(vm_info["power_state"])
    add(
        "power_state",
        vm_info["power_state"] in ALLOWED_POWER_STATES,
        f"power_state={vm_info['power_state']}, backup_method={backup_method}",
    )
    if config.expected_cpu > 0:
        add(
            "cpu",
            vm_info["num_cpu"] == config.expected_cpu,
            f"cpu={vm_info['num_cpu']}, expected={config.expected_cpu}",
        )
    if config.expected_ram_mb > 0:
        add(
            "memory",
            vm_info["memory_mb"] == config.expected_ram_mb,
            f"memory_mb={vm_info['memory_mb']}, expected={config.expected_ram_mb}",
        )
    if config.expected_disk_count > 0:
        add(
            "disk_count",
            len(disks) == config.expected_disk_count,
            f"disk_count={len(disks)}, expected={config.expected_disk_count}",
        )
    if disks and config.expected_disk_size_gb > 0:
        disk_gb = float(disks[0]["capacity_gb"])
        add(
            "disk_size",
            abs(disk_gb - config.expected_disk_size_gb) <= 0.05,
            f"disk_gb={disk_gb}, expected={config.expected_disk_size_gb}",
        )
    if disks:
        add("disk_file", bool(disks[0]["file_name"]), f"file_name={disks[0]['file_name']!r}")
    add("no_existing_snapshots", not vm_info["has_snapshot"], f"has_snapshot={vm_info['has_snapshot']}")

    space = check_free_space(config.output_dir, config.min_free_gb)
    add(
        "backup_storage_free_space",
        space["ok"],
        f"path={space['path']}, free_gb={space['free_gb']}, required_gb={space['required_gb']}",
    )
    return checks


def validate_docsign_target(vm_info: Dict[str, Any], config: VSphereConfig) -> List[Dict[str, Any]]:
    return validate_target_vm(vm_info, config)


def checks_passed(checks: Iterable[Dict[str, Any]]) -> bool:
    return all(check["ok"] or check.get("severity") != "fatal" for check in checks)


def print_checks(checks: List[Dict[str, Any]]) -> None:
    width = max((len(c["name"]) for c in checks), default=10)
    for check in checks:
        state = "PASS" if check["ok"] else "FAIL"
        print(f"{state:<4} {check['name']:<{width}} {check['detail']}")


def print_inventory(vms: List[Dict[str, Any]]) -> None:
    header = f"{'VM Name':<30} | {'Power':<10} | {'CPU':<3} | {'RAM MB':<7} | {'Disks'}"
    print(header)
    print("-" * len(header))
    for info in sorted(vms, key=lambda item: item["name"].lower()):
        disk_text = ", ".join(f"{d['label']} ({d['capacity_gb']} GB)" for d in info["disks"])
        print(
            f"{info['name']:<30} | {info['power_state']:<10} | "
            f"{info['num_cpu']:<3} | {info['memory_mb']:<7} | {disk_text}"
        )


def resolve_nfc_url(raw_url: str, host: str) -> str:
    parts = urlsplit(raw_url)
    netloc = parts.netloc
    if "*" in netloc:
        if ":" in netloc:
            _, port = netloc.rsplit(":", 1)
            netloc = f"{host}:{port}"
        else:
            netloc = host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def redacted_url(raw_url: str) -> str:
    parts = urlsplit(raw_url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def should_skip_lease_file(filename: str) -> Optional[str]:
    suffix = Path(filename).suffix.lower()
    if suffix in SKIPPED_REMOVABLE_EXTENSIONS:
        return "removable media image is not part of the VM disk backup"
    return None


class LeaseProgress:
    def __init__(self, lease: vim.HttpNfcLease, total_bytes: int):
        self.lease = lease
        self.total_bytes = max(total_bytes, 1)
        self.transferred_bytes = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lease-progress", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def add(self, amount: int) -> None:
        with self._lock:
            self.transferred_bytes += amount

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _percent(self) -> int:
        with self._lock:
            percent = int((self.transferred_bytes / self.total_bytes) * 100)
        return max(1, min(percent, 99))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.lease.HttpNfcLeaseProgress(self._percent())
            except Exception as exc:
                LOGGER.debug("Lease progress update failed: %s", exc)
            self._stop.wait(10)


def wait_for_lease_ready(lease: vim.HttpNfcLease, timeout_seconds: int) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        state = lease.state
        if state == vim.HttpNfcLease.State.ready:
            return
        if state == vim.HttpNfcLease.State.error:
            error = getattr(lease, "error", None)
            raise RuntimeError(f"HttpNfcLease entered error state: {error}")
        time.sleep(1)
    raise TimeoutError(f"HttpNfcLease was not ready after {timeout_seconds} seconds")


def abort_lease(lease: vim.HttpNfcLease, message: str) -> None:
    try:
        fault = vmodl.fault.SystemError(reason=message)
        lease.HttpNfcLeaseAbort(fault)
    except Exception as exc:
        LOGGER.warning("Could not abort HttpNfcLease cleanly: %s", exc)


def wait_for_task(task: vim.Task, action: str) -> Any:
    """Wait for a vSphere task until it succeeds or reports an error.

    Long-running datastore operations must use wait_for_task_progress() so a
    timeout is based on actual progress rather than total wall-clock time.
    """
    last_log_at = 0.0
    last_progress = None
    while True:
        info = task.info
        state = info.state
        if state == vim.TaskInfo.State.success:
            return getattr(info, "result", None)
        if state == vim.TaskInfo.State.error:
            error = getattr(info, "error", None)
            message = getattr(error, "msg", None) or str(error)
            raise RuntimeError(f"{action} failed: {message}")
        progress = getattr(info, "progress", None)
        if progress != last_progress or time.time() - last_log_at >= 30:
            LOGGER.info("%s still running%s", action, f" ({progress}%)" if progress is not None else "")
            last_progress = progress
            last_log_at = time.time()
        time.sleep(2)


def wait_for_task_progress(task: vim.Task, action: str, stall_timeout_seconds: int) -> Any:
    last_log_at = 0.0
    last_progress = None
    last_progress_at = time.time()
    while True:
        now = time.time()
        info = task.info
        state = info.state
        if state == vim.TaskInfo.State.success:
            return getattr(info, "result", None)
        if state == vim.TaskInfo.State.error:
            error = getattr(info, "error", None)
            message = getattr(error, "msg", None) or str(error)
            raise RuntimeError(f"{action} failed: {message}")

        progress = getattr(info, "progress", None)
        progress_changed = progress != last_progress
        if progress_changed:
            last_progress = progress
            last_progress_at = now

        stalled_seconds = int(max(0.0, now - last_progress_at))
        if progress_changed or now - last_log_at >= 30:
            if progress is None:
                detail = f" (no reported progress for {stalled_seconds}s)"
            else:
                detail = f" ({progress}%, no progress for {stalled_seconds}s)"
            LOGGER.info("%s still running%s", action, detail)
            last_log_at = now

        if now - last_progress_at >= stall_timeout_seconds:
            if progress is None:
                raise TimeoutError(f"{action} reported no progress for {stall_timeout_seconds} seconds")
            raise TimeoutError(f"{action} made no progress at {progress}% for {stall_timeout_seconds} seconds")
        time.sleep(2)


def task_state_if_available(task: vim.Task, action: str) -> Optional[Any]:
    try:
        return task.info.state
    except vmodl.fault.ManagedObjectNotFound as exc:
        LOGGER.info("%s task is no longer available on vSphere; assuming it is not running: %s", action, exc)
        return None


def find_snapshot_by_name(vm: vim.VirtualMachine, name: str) -> Optional[vim.vm.Snapshot]:
    snapshot_info = getattr(vm, "snapshot", None)
    if not snapshot_info:
        return None
    return search_snapshot_tree(getattr(snapshot_info, "rootSnapshotList", []) or [], name)


def search_snapshot_tree(snapshot_tree: Iterable[Any], name: str) -> Optional[vim.vm.Snapshot]:
    for item in snapshot_tree:
        if getattr(item, "name", "") == name:
            return getattr(item, "snapshot", None)
        child = search_snapshot_tree(getattr(item, "childSnapshotList", []) or [], name)
        if child:
            return child
    return None


def create_temporary_snapshot(
    vm: vim.VirtualMachine,
    config: VSphereConfig,
) -> tuple[vim.vm.Snapshot, Dict[str, Any]]:
    vm_name = str(getattr(vm, "name", "") or config.target_vm_name or TARGET_VM_NAME)
    snapshot_name = f"{LIVE_SNAPSHOT_PREFIX}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    snapshot_info: Dict[str, Any] = {
        "required": True,
        "created": False,
        "removed": False,
        "name": snapshot_name,
        "memory": False,
        "quiesce": config.live_snapshot_quiesce,
    }
    description = (
        f"Temporary snapshot for {vm_name} vSphere API backup. "
        f"Started at {now_utc()}."
    )

    LOGGER.info("Creating temporary snapshot %s for %s", snapshot_name, vm_name)
    task = vm.CreateSnapshot_Task(
        name=snapshot_name,
        description=description,
        memory=False,
        quiesce=config.live_snapshot_quiesce,
    )
    snapshot = wait_for_task(task, f"Create snapshot {snapshot_name}")
    if snapshot is None:
        snapshot = find_snapshot_by_name(vm, snapshot_name)
    if snapshot is None:
        raise RuntimeError(f"Snapshot was created but could not be located: {snapshot_name}")

    snapshot_info["created"] = True
    snapshot_info["created_at"] = now_utc()
    snapshot_info["moref"] = getattr(snapshot, "_moId", "")
    LOGGER.info("Temporary snapshot created: %s", snapshot_name)
    return snapshot, snapshot_info


def remove_temporary_snapshot(
    snapshot: vim.vm.Snapshot,
    snapshot_info: Dict[str, Any],
    config: VSphereConfig,
) -> None:
    snapshot_name = snapshot_info.get("name", "<unknown>")
    LOGGER.info("Removing temporary snapshot %s", snapshot_name)
    try:
        task = snapshot.RemoveSnapshot_Task(removeChildren=False, consolidate=True)
    except TypeError:
        task = snapshot.RemoveSnapshot_Task(removeChildren=False)
    wait_for_task(task, f"Remove snapshot {snapshot_name}")
    snapshot_info["removed"] = True
    snapshot_info["removed_at"] = now_utc()
    snapshot_info["cleanup_status"] = "success"
    LOGGER.info("Temporary snapshot removed: %s", snapshot_name)


def find_datacenter_for_vm(vm: vim.VirtualMachine) -> vim.Datacenter:
    current = vm
    while current:
        if isinstance(current, vim.Datacenter):
            return current
        current = getattr(current, "parent", None)
    raise RuntimeError(f"Could not find datacenter for VM {TARGET_VM_NAME}")


def human_bytes(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def download_progress_detail(bytes_written: int, expected_size: int, stalled_seconds: int) -> str:
    if expected_size > 0:
        percent = min(100.0, (bytes_written / expected_size) * 100.0)
        return (
            f"{human_bytes(bytes_written)}/{human_bytes(expected_size)}, "
            f"{percent:.1f}%, no byte progress for {stalled_seconds}s"
        )
    return f"{human_bytes(bytes_written)}, no byte progress for {stalled_seconds}s"


def get_datastore_file(
    session: VSphereSession,
    datacenter: vim.Datacenter,
    datastore: str,
    datastore_file: str,
    config: VSphereConfig,
    target_file: Path,
    optional: bool = False,
) -> Optional[Dict[str, Any]]:
    url = build_datastore_url(config, datacenter.name, datastore, datastore_file)
    target_name = target_file.name
    part_file = target_file.with_suffix(target_file.suffix + ".part")
    sha256 = hashlib.sha256()
    bytes_written = 0
    expected_size = 0
    last_log_at = 0.0
    last_logged_percent: Optional[int] = None
    last_byte_at = time.time()

    LOGGER.info("Downloading datastore file %s to %s", datastore_file, target_name)
    try:
        with requests.get(
            url,
            headers={"Cookie": session.cookie},
            verify=config.ssl_verify,
            stream=True,
            timeout=(30, config.download_stall_timeout_seconds),
        ) as response:
            if optional and response.status_code in {404, 500}:
                LOGGER.info("Optional datastore file unavailable, skipping: %s", datastore_file)
                return None
            response.raise_for_status()
            expected_size = int(response.headers.get("Content-Length") or 0)
            with open(part_file, "wb") as fh:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    sha256.update(chunk)
                    bytes_written += len(chunk)

                    now = time.time()
                    last_byte_at = now
                    percent = int((bytes_written / expected_size) * 100) if expected_size else None
                    should_log = now - last_log_at >= 30
                    if percent is not None and percent != last_logged_percent:
                        should_log = True
                    if should_log:
                        LOGGER.info(
                            "Download datastore file %s still running (%s)",
                            datastore_file,
                            download_progress_detail(bytes_written, expected_size, 0),
                        )
                        last_log_at = now
                        last_logged_percent = percent
    except requests.exceptions.RequestException as exc:
        stalled_seconds = int(max(0.0, time.time() - last_byte_at))
        raise TimeoutError(
            f"Download datastore file {datastore_file} stalled or failed after "
            f"{stalled_seconds}s without byte progress "
            f"({download_progress_detail(bytes_written, expected_size, stalled_seconds)}): {exc}"
        ) from exc

    if expected_size and bytes_written != expected_size:
        raise IOError(f"Size mismatch for {target_name}: expected {expected_size}, wrote {bytes_written}")
    part_file.rename(target_file)
    return {
        "name": target_name,
        "bytes": bytes_written,
        "sha256": sha256.hexdigest(),
        "source": build_datastore_url(config, datacenter.name, datastore, datastore_file).split("?", 1)[0],
        "transfer": "datastore_http",
    }


def read_datastore_text_file(
    session: VSphereSession,
    datacenter: vim.Datacenter,
    datastore: str,
    datastore_file: str,
    config: VSphereConfig,
) -> str:
    url = build_datastore_url(config, datacenter.name, datastore, datastore_file)
    with requests.get(
        url,
        headers={"Cookie": session.cookie},
        verify=config.ssl_verify,
        timeout=(30, config.read_timeout_seconds),
    ) as response:
        response.raise_for_status()
        return response.text


def parse_vmdk_adapter_type(descriptor: str) -> str:
    match = re.search(r'ddb\.adapterType\s*=\s*"([^"]+)"', descriptor)
    if match:
        return normalize_virtual_disk_manager_adapter_type(match.group(1))
    return "lsiLogic"


def normalize_virtual_disk_manager_adapter_type(value: str) -> str:
    mapping = {
        "buslogic": "busLogic",
        "lsilogic": "lsiLogic",
        # VirtualDiskManager.CopyVirtualDisk_Task does not accept pvscsi as an
        # adapterType on some hosts. This value affects only VMDK import
        # metadata; the VM can still attach the imported disk to a PVSCSI
        # controller.
        "pvscsi": "lsiLogic",
        "ide": "ide",
    }
    return mapping.get(value.strip().lower(), value)


def parse_vmdk_extent_files(descriptor: str) -> List[str]:
    extents: List[str] = []
    for raw_line in descriptor.splitlines():
        line = raw_line.strip()
        match = re.match(r'^(RW|RDONLY|NOACCESS)\s+\d+\s+\S+\s+"([^"]+)"', line)
        if match:
            extents.append(match.group(2))
    return extents


def delete_datastore_path(
    session: VSphereSession,
    datacenter: vim.Datacenter,
    datastore_path: str,
    config: VSphereConfig,
) -> None:
    action = f"Delete datastore path {datastore_path}"

    def delete_once() -> None:
        task = session.content.fileManager.DeleteDatastoreFile_Task(
            name=datastore_path,
            datacenter=datacenter,
        )
        wait_for_task(task, action)

    run_with_cleanup_retries(action, delete_once)


def snapshot_copy_stem(disk_index: int, disk_count: int) -> str:
    if disk_count == 1:
        return f"{TARGET_VM_NAME}-snapshot"
    return f"{TARGET_VM_NAME}-disk{disk_index}-snapshot"


def export_snapshot_with_datastore_copy(
    session: VSphereSession,
    vm: vim.VirtualMachine,
    vm_info: Dict[str, Any],
    backup_dir: Path,
    config: VSphereConfig,
    backup_id: str,
) -> Dict[str, Any]:
    disks = vm_info["disks"]
    if not disks:
        raise SafetyError("Datastore-copy fallback requires at least one disk")

    datacenter = find_datacenter_for_vm(vm)
    remote_paths_to_delete: List[str] = []
    remote_dirs_to_delete: List[str] = []
    created_remote_dirs: set[str] = set()
    copy_task = None
    copy_task_action = ""
    copy_wait_timed_out = False
    active_copy: Optional[Dict[str, Any]] = None
    active_source_path = ""
    remote_copy: Dict[str, Any] = {
        "method": "VirtualDiskManager.CopyVirtualDisk_Task",
        "disk_count": len(disks),
        "disk_type": "sparseMonolithic",
        "directories": [],
        "copies": [],
        "cleanup_status": "not_started",
    }
    raised_exc: Optional[BaseException] = None

    try:
        files: List[Dict[str, Any]] = []
        first_datastore = ""
        first_source_file = ""
        for disk_index, disk in enumerate(disks, start=1):
            source_path = disk["file_name"]
            active_source_path = source_path
            datastore, source_file = parse_datastore_path(source_path)
            if not first_datastore:
                first_datastore = datastore
                first_source_file = source_file

            descriptor = read_datastore_text_file(session, datacenter, datastore, source_file, config)
            adapter_type = parse_vmdk_adapter_type(descriptor)
            remote_dir = datastore_path_join(datastore, REMOTE_TEMP_DIR, backup_id)
            if remote_dir not in created_remote_dirs:
                LOGGER.info("Creating remote temp datastore directory %s", remote_dir)
                session.content.fileManager.MakeDirectory(
                    name=remote_dir,
                    datacenter=datacenter,
                    createParentDirectories=True,
                )
                created_remote_dirs.add(remote_dir)
                remote_dirs_to_delete.append(remote_dir)
                remote_copy["directories"].append(
                    {
                        "datastore": datastore,
                        "path": remote_dir,
                        "created": True,
                    }
                )

            copy_stem = snapshot_copy_stem(disk_index, len(disks))
            remote_disk_path = datastore_path_join(datastore, REMOTE_TEMP_DIR, backup_id, f"{copy_stem}.vmdk")
            copy_record: Dict[str, Any] = {
                "disk_index": disk_index,
                "disk_key": disk.get("key", ""),
                "disk_label": disk.get("label", f"disk-{disk_index}"),
                "source_path": source_path,
                "datastore": datastore,
                "directory": remote_dir,
                "disk_path": remote_disk_path,
                "adapter_type": adapter_type,
                "created": False,
            }
            remote_copy["copies"].append(copy_record)
            active_copy = copy_record
            remote_paths_to_delete.append(remote_disk_path)

            spec = vim.VirtualDiskManager.VirtualDiskSpec()
            spec.diskType = vim.VirtualDiskManager.VirtualDiskType.sparseMonolithic
            spec.adapterType = adapter_type

            LOGGER.info(
                "Copying snapshot-stable disk %s/%s to remote temp file %s",
                disk_index,
                len(disks),
                remote_disk_path,
            )
            copy_task_action = f"Copy virtual disk {source_path}"
            copy_task = session.content.virtualDiskManager.CopyVirtualDisk_Task(
                sourceName=source_path,
                sourceDatacenter=datacenter,
                destName=remote_disk_path,
                destDatacenter=datacenter,
                destSpec=spec,
                force=False,
            )
            try:
                wait_for_task_progress(
                    copy_task,
                    copy_task_action,
                    config.datastore_copy_stall_timeout_seconds,
                )
            except TimeoutError:
                copy_wait_timed_out = True
                raise
            copy_task = None
            copy_record["created"] = True
            copy_record["created_at"] = now_utc()

            _, remote_file = parse_datastore_path(remote_disk_path)
            remote_descriptor = read_datastore_text_file(session, datacenter, datastore, remote_file, config)
            extent_names = parse_vmdk_extent_files(remote_descriptor)
            copy_record["extent_files"] = extent_names

            remote_folder = str(Path(remote_file).parent)
            for extent_name in extent_names:
                remote_paths_to_delete.append(datastore_path_join(datastore, remote_folder, extent_name))

            descriptor_result = get_datastore_file(
                session=session,
                datacenter=datacenter,
                datastore=datastore,
                datastore_file=remote_file,
                config=config,
                target_file=backup_dir / f"{copy_stem}.vmdk",
            )
            if descriptor_result is not None:
                descriptor_result["source_datastore_path"] = remote_disk_path
                descriptor_result["source_disk_path"] = source_path
                descriptor_result["vmdk_role"] = "descriptor"
                descriptor_result["disk_index"] = disk_index
                descriptor_result["disk_key"] = disk.get("key", "")
                descriptor_result["disk_label"] = disk.get("label", f"disk-{disk_index}")
                files.append(descriptor_result)

            for extent_name in extent_names:
                extent_file = f"{remote_folder}/{extent_name}"
                extent_path = datastore_path_join(datastore, remote_folder, extent_name)
                extent_result = get_datastore_file(
                    session=session,
                    datacenter=datacenter,
                    datastore=datastore,
                    datastore_file=extent_file,
                    config=config,
                    target_file=backup_dir / safe_filename(extent_name, f"{copy_stem}-extent.vmdk"),
                )
                if extent_result is not None:
                    extent_result["source_datastore_path"] = extent_path
                    extent_result["source_disk_path"] = source_path
                    extent_result["vmdk_role"] = "extent"
                    extent_result["disk_index"] = disk_index
                    extent_result["disk_key"] = disk.get("key", "")
                    extent_result["disk_label"] = disk.get("label", f"disk-{disk_index}")
                    files.append(extent_result)

        try:
            nvram_datastore, vmx_file = parse_datastore_path(vm_info.get("vmx_path", ""))
            nvram_path = datastore_sibling_path(vmx_file, f"{TARGET_VM_NAME}.nvram")
        except Exception:
            nvram_datastore = first_datastore
            nvram_path = datastore_sibling_path(first_source_file, f"{TARGET_VM_NAME}.nvram")

        skipped: List[Dict[str, Any]] = []
        nvram_result = get_datastore_file(
            session=session,
            datacenter=datacenter,
            datastore=nvram_datastore,
            datastore_file=nvram_path,
            config=config,
            target_file=backup_dir / f"{TARGET_VM_NAME}.nvram",
            optional=True,
        )
        if nvram_result is not None:
            nvram_result["source_datastore_path"] = datastore_path_join(nvram_datastore, nvram_path)
            files.append(nvram_result)
        else:
            skipped.append(
                {
                    "name": f"{TARGET_VM_NAME}.nvram",
                    "reason": "optional NVRAM datastore file unavailable",
                    "source_datastore_path": datastore_path_join(nvram_datastore, nvram_path),
                }
            )

        descriptor_count = sum(1 for item in files if item.get("vmdk_role") == "descriptor")
        extent_count = sum(1 for item in files if item.get("vmdk_role") == "extent")
        expected_extent_count = sum(len(copy.get("extent_files", [])) for copy in remote_copy["copies"])
        if descriptor_count != len(disks):
            raise RuntimeError(f"Downloaded {descriptor_count} VMDK descriptor(s), expected {len(disks)}")
        if expected_extent_count and extent_count != expected_extent_count:
            raise RuntimeError(
                f"Downloaded {extent_count} VMDK extent file(s), expected {expected_extent_count}"
            )

        if len(remote_copy["copies"]) == 1:
            single = remote_copy["copies"][0]
            remote_copy.update(
                {
                    "datastore": single.get("datastore", ""),
                    "directory": single.get("directory", ""),
                    "disk_path": single.get("disk_path", ""),
                    "adapter_type": single.get("adapter_type", ""),
                    "directory_created": bool(remote_copy["directories"]),
                    "created": bool(single.get("created")),
                    "created_at": single.get("created_at", ""),
                    "extent_files": single.get("extent_files", []),
                }
            )

        return {
            "files": files,
            "skipped_device_urls": skipped,
            "remote_temporary_copy": remote_copy,
            "transfer_method": "snapshot_datastore_copy",
        }
    except BaseException as exc:
        raised_exc = exc
        raise
    finally:
        skip_remote_cleanup = False
        if (
            copy_task is not None
            and task_state_if_available(copy_task, copy_task_action or f"Copy virtual disk {active_source_path}")
            == vim.TaskInfo.State.running
        ):
            if copy_wait_timed_out:
                LOGGER.warning(
                    "Virtual disk copy task is still running after progress timeout; "
                    "leaving remote temp files in place"
                )
                remote_copy["cleanup_status"] = "skipped_copy_still_running"
                remote_copy["cleanup_reason"] = (
                    "Virtual disk copy task was still running after the datastore copy progress timeout. "
                    "Remote temp files were left in place because deleting them while vSphere is "
                    "writing the copy is unsafe."
                )
                skip_remote_cleanup = True
            else:
                LOGGER.warning("Virtual disk copy task is still running; waiting before cleanup")
                try:
                    wait_for_task(
                        copy_task,
                        f"Finish virtual disk copy {active_source_path}",
                    )
                except BaseException as exc:
                    remote_copy["cleanup_status"] = "skipped_copy_still_running"
                    remote_copy["cleanup_reason"] = exception_message(exc)
                    attach_transfer_failure_metadata(exc, remote_copy, "snapshot_datastore_copy")
                    raise
                if active_copy is not None:
                    active_copy["created"] = True
                    active_copy["created_at"] = active_copy.get("created_at", now_utc())
        if created_remote_dirs and not skip_remote_cleanup:
            remote_copy["cleanup_status"] = "running"
            cleanup_errors: List[str] = []
            try:
                for path in reversed(remote_paths_to_delete):
                    try:
                        LOGGER.info("Deleting remote temp datastore file %s", path)
                        delete_datastore_path(session, datacenter, path, config)
                    except Exception as file_exc:
                        cleanup_errors.append(f"{path}: {file_exc}")
                        LOGGER.warning("Remote datastore temp file cleanup failed: %s", file_exc)
                for remote_dir in reversed(remote_dirs_to_delete):
                    try:
                        LOGGER.info("Deleting remote temp datastore directory %s", remote_dir)
                        delete_datastore_path(session, datacenter, remote_dir, config)
                    except Exception as dir_exc:
                        cleanup_errors.append(f"{remote_dir}: {dir_exc}")
                        LOGGER.warning("Remote datastore temp directory cleanup failed: %s", dir_exc)
                remote_copy["directories_deleted"] = not cleanup_errors
                if len(remote_copy.get("copies", [])) == 1:
                    remote_copy["directory_deleted"] = remote_copy["directories_deleted"]
            except Exception as cleanup_exc:
                cleanup_errors.append(str(cleanup_exc))
                LOGGER.error("Remote datastore temp cleanup failed: %s", cleanup_exc)
            if cleanup_errors:
                remote_copy["cleanup_status"] = "failed"
                remote_copy["cleanup_errors"] = cleanup_errors
            else:
                remote_copy["cleanup_status"] = "success"
                remote_copy["deleted_at"] = now_utc()
        if raised_exc is not None:
            attach_transfer_failure_metadata(raised_exc, remote_copy, "snapshot_datastore_copy")


def run_snapshot_datastore_copy_fallback(
    session: VSphereSession,
    vm: vim.VirtualMachine,
    vm_info: Dict[str, Any],
    backup_dir: Path,
    config: VSphereConfig,
    backup_id: str,
    manifest: Dict[str, Any],
    trigger: BaseException,
    reason: str,
) -> None:
    error_text = exception_message(trigger)
    LOGGER.warning(
        "Using snapshot datastore-copy fallback for %s after %s: %s",
        TARGET_VM_NAME,
        reason,
        error_text,
    )
    manifest["snapshot_export_error"] = error_text
    manifest["snapshot_export_fallback"] = {
        "reason": reason,
        "trigger_error": error_text,
        "started_at": now_utc(),
    }
    transfer_result = export_snapshot_with_datastore_copy(
        session=session,
        vm=vm,
        vm_info=vm_info,
        backup_dir=backup_dir,
        config=config,
        backup_id=backup_id,
    )
    manifest["files"] = transfer_result["files"]
    manifest["skipped_device_urls"] = transfer_result["skipped_device_urls"]
    manifest["remote_temporary_copy"] = transfer_result["remote_temporary_copy"]
    manifest["transfer_method"] = transfer_result["transfer_method"]
    manifest["snapshot_export_fallback"]["completed_at"] = now_utc()
    manifest["snapshot_export_fallback"]["transfer_method"] = transfer_result["transfer_method"]


def write_ovf_descriptor(session: VSphereSession, vm: vim.VirtualMachine, backup_dir: Path) -> Dict[str, Any]:
    result: Dict[str, Any] = {"created": False, "warnings": [], "errors": []}
    try:
        params = vim.OvfManager.CreateDescriptorParams()
        params.name = TARGET_VM_NAME
        params.includeImageFiles = False
        descriptor_result = session.content.ovfManager.CreateDescriptor(vm, params)
        descriptor = getattr(descriptor_result, "ovfDescriptor", "")
        if descriptor:
            ovf_file = backup_dir / f"{TARGET_VM_NAME}.ovf"
            ovf_file.write_text(descriptor, encoding="utf-8")
            result["created"] = True
            result["file"] = ovf_file.name

        for warning in getattr(descriptor_result, "warning", []) or []:
            result["warnings"].append(str(warning))
        for error in getattr(descriptor_result, "error", []) or []:
            result["errors"].append(str(error))
    except Exception as exc:
        result["errors"].append(str(exc))
        LOGGER.warning("OVF descriptor creation failed, continuing with VMDK export: %s", exc)
    return result


def download_lease_files(
    session: VSphereSession,
    lease: vim.HttpNfcLease,
    backup_dir: Path,
    config: VSphereConfig,
) -> Dict[str, List[Dict[str, Any]]]:
    lease_info = lease.info
    total_bytes = int(getattr(lease_info, "totalDiskCapacityInKB", 0) or 0) * 1024
    progress = LeaseProgress(lease, total_bytes)
    progress.start()

    headers = {"Cookie": session.cookie}
    downloaded: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    vmdk_count = 0

    try:
        device_urls = list(getattr(lease_info, "deviceUrl", []) or [])
        if not device_urls:
            raise RuntimeError("HttpNfcLease did not provide device URLs")

        for index, device_url in enumerate(device_urls, start=1):
            raw_url = getattr(device_url, "url", "")
            url = resolve_nfc_url(raw_url, config.host)
            parsed = urlsplit(url)
            source_name = safe_filename(Path(parsed.path).name, f"lease_device_{index}")
            skip_reason = should_skip_lease_file(source_name)
            if skip_reason:
                skipped.append(
                    {
                        "name": source_name,
                        "reason": skip_reason,
                        "source": redacted_url(url),
                        "device_key": getattr(device_url, "key", ""),
                        "import_key": getattr(device_url, "importKey", ""),
                    }
                )
                LOGGER.info("Skipping non-VMDK lease device %s: %s", index, source_name)
                continue

            fallback_name = f"disk_{index}.vmdk"
            target_name = safe_filename(source_name, fallback_name)

            target_file = backup_dir / target_name
            if target_file.exists():
                target_file = backup_dir / f"{index}_{target_name}"
            part_file = target_file.with_suffix(target_file.suffix + ".part")

            LOGGER.info("Downloading lease file %s to %s", index, target_file.name)
            sha256 = hashlib.sha256()
            bytes_written = 0

            with requests.get(
                url,
                headers=headers,
                verify=config.ssl_verify,
                stream=True,
                timeout=(30, config.read_timeout_seconds),
            ) as response:
                response.raise_for_status()
                expected_size = int(response.headers.get("Content-Length") or 0)
                last_log_at = time.time()
                last_progress_at = last_log_at
                last_bytes_written = 0
                with open(part_file, "wb") as fh:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        sha256.update(chunk)
                        bytes_written += len(chunk)
                        progress.add(len(chunk))
                        now = time.time()
                        if bytes_written != last_bytes_written:
                            last_bytes_written = bytes_written
                            last_progress_at = now
                        if now - last_log_at >= 30:
                            stalled_seconds = int(max(0.0, now - last_progress_at))
                            LOGGER.info(
                                "Download lease file %s to %s still running (%s)",
                                index,
                                target_file.name,
                                download_progress_detail(bytes_written, expected_size, stalled_seconds),
                            )
                            last_log_at = now

            if expected_size and bytes_written != expected_size:
                raise IOError(
                    f"Size mismatch for {target_file.name}: expected {expected_size}, wrote {bytes_written}"
                )
            part_file.rename(target_file)
            LOGGER.info(
                "Download lease file %s to %s completed (%s)",
                index,
                target_file.name,
                download_progress_detail(bytes_written, expected_size, 0),
            )
            if target_file.name.lower().endswith(".vmdk"):
                vmdk_count += 1
            downloaded.append(
                {
                    "name": target_file.name,
                    "bytes": bytes_written,
                    "sha256": sha256.hexdigest(),
                    "source": redacted_url(url),
                    "device_key": getattr(device_url, "key", ""),
                    "import_key": getattr(device_url, "importKey", ""),
                }
            )
    finally:
        progress.stop()

    if vmdk_count == 0:
        raise RuntimeError("No VMDK files were exported from the HttpNfcLease")

    return {"files": downloaded, "skipped_device_urls": skipped}


def transfer_http_nfc_lease_or_snapshot_copy(
    session: VSphereSession,
    vm: vim.VirtualMachine,
    vm_info: Dict[str, Any],
    lease: vim.HttpNfcLease,
    backup_dir: Path,
    config: VSphereConfig,
    backup_id: str,
    manifest: Dict[str, Any],
    allow_snapshot_fallback: bool,
) -> None:
    try:
        wait_for_lease_ready(lease, config.lease_timeout_seconds)
        transfer_result = download_lease_files(session, lease, backup_dir, config)
        manifest["files"] = transfer_result["files"]
        manifest["skipped_device_urls"] = transfer_result["skipped_device_urls"]
        manifest["transfer_method"] = "http_nfc_lease"
        lease.HttpNfcLeaseProgress(100)
        lease.HttpNfcLeaseComplete()
    except BaseException as exc:
        if allow_snapshot_fallback and is_missing_removable_media_export_error(exc):
            abort_lease(lease, exception_message(exc))
            run_snapshot_datastore_copy_fallback(
                session=session,
                vm=vm,
                vm_info=vm_info,
                backup_dir=backup_dir,
                config=config,
                backup_id=backup_id,
                manifest=manifest,
                trigger=exc,
                reason="missing_removable_media_image",
            )
            return
        abort_lease(lease, exception_message(exc))
        raise


def create_backup(session: VSphereSession, vm: vim.VirtualMachine, config: VSphereConfig) -> Path:
    apply_config_defaults(config)
    vm_info = collect_vm_info(vm)
    checks = validate_target_vm(vm_info, config)
    if not checks_passed(checks):
        print_checks(checks)
        raise SafetyError("Preflight checks failed. Backup blocked.")
    backup_method = backup_method_for_power_state(vm_info["power_state"])
    if backup_method == "unsupported":
        raise SafetyError(f"Unsupported power state for backup: {vm_info['power_state']}")

    config.output_dir.mkdir(parents=True, exist_ok=True)
    target_slug = safe_filename(TARGET_VM_NAME, "vm")
    backup_id = f"{target_slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{vm_info['instance_uuid'][:8]}"
    staging_dir = config.output_dir / f"{backup_id}.inprogress"
    final_dir = config.output_dir / backup_id
    if staging_dir.exists() or final_dir.exists():
        raise FileExistsError(f"Backup directory already exists for id {backup_id}")
    staging_dir.mkdir(parents=True)

    manifest: Dict[str, Any] = {
        "tool": "vsphere_api_backup",
        "started_at": now_utc(),
        "target_vm": TARGET_VM_NAME,
        "safety_model": {
            "allowed_vm_name": TARGET_VM_NAME,
            "allowed_power_states": sorted(ALLOWED_POWER_STATES),
            "powered_off_method": "vm_export",
            "powered_on_method": "temporary_snapshot_export_with_datastore_copy_fallback",
            "expected_cpu": config.expected_cpu,
            "expected_ram_mb": config.expected_ram_mb,
            "expected_disk_count": config.expected_disk_count,
            "expected_disk_size_gb": config.expected_disk_size_gb,
            "inventory_api": "vcenter_rest_and_managed_object",
            "export_api": "vSphere managed object HttpNfcLease",
        },
        "backup_method": backup_method,
        "vm_info": vm_info,
        "preflight_checks": checks,
        "files": [],
        "ovf_descriptor": {},
        "snapshot": {"required": backup_method == "snapshot_export"},
        "transfer_method": "",
        "status": "running",
        "cbt_state": cbt_state_from_vm_info(vm_info),
    }

    (staging_dir / "vm_metadata.json").write_text(
        json.dumps(vm_info, indent=2, sort_keys=True), encoding="utf-8"
    )
    LOGGER.info("Backup directory prepared: %s", staging_dir)

    lease = None
    snapshot = None
    snapshot_info: Dict[str, Any] = {"required": backup_method == "snapshot_export"}
    try:
        manifest["ovf_descriptor"] = write_ovf_descriptor(session, vm, staging_dir)

        if backup_method == "snapshot_export":
            snapshot, snapshot_info = create_temporary_snapshot(vm, config)
            manifest["snapshot"] = snapshot_info
            manifest["cbt_state"] = cbt_state_from_snapshot(snapshot, vm_info)
            LOGGER.info("Requesting ExportSnapshot HttpNfcLease for %s", TARGET_VM_NAME)
            try:
                lease = snapshot.ExportSnapshot()
            except vmodl.fault.NotSupported as exc:
                run_snapshot_datastore_copy_fallback(
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
            LOGGER.info("Requesting ExportVm HttpNfcLease for %s", TARGET_VM_NAME)
            lease = vm.ExportVm()

        if lease is not None:
            try:
                transfer_http_nfc_lease_or_snapshot_copy(
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
            remove_temporary_snapshot(snapshot, snapshot_info, config)
            snapshot = None
            manifest["snapshot"] = snapshot_info

        manifest["status"] = "success"
        manifest["finished_at"] = now_utc()
        (staging_dir / "backup_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        staging_dir.rename(final_dir)
        LOGGER.info("Backup completed: %s", final_dir)
        return final_dir
    except BaseException as exc:
        manifest["status"] = "failed"
        manifest["finished_at"] = now_utc()
        manifest["error"] = exception_message(exc)
        record_transfer_failure_metadata(manifest, exc)
        if lease is not None:
            abort_lease(lease, exception_message(exc))
        if snapshot is not None:
            try:
                remove_temporary_snapshot(snapshot, snapshot_info, config)
            except Exception as cleanup_exc:
                snapshot_info["removed"] = False
                snapshot_info["cleanup_status"] = "failed"
                snapshot_info["cleanup_error"] = str(cleanup_exc)
                LOGGER.error("Temporary snapshot cleanup failed: %s", cleanup_exc)
            manifest["snapshot"] = snapshot_info
        try:
            (staging_dir / "backup_manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
        except Exception:
            pass
        failed_dir = config.output_dir / f"{backup_id}.failed"
        try:
            if staging_dir.exists() and not failed_dir.exists():
                staging_dir.rename(failed_dir)
        except Exception:
            pass
        raise


def verify_backup_dir(path: Path, expected_target: str = "") -> Dict[str, Any]:
    manifest_file = path / "backup_manifest.json"
    if not manifest_file.exists():
        raise FileNotFoundError(f"Missing backup manifest: {manifest_file}")

    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    errors: List[str] = []
    checked_files = 0

    manifest_target = str(manifest.get("target_vm") or "")
    if not manifest_target:
        errors.append("Manifest has no target_vm")
    elif expected_target and manifest_target != expected_target:
        errors.append(f"Manifest target_vm is not {expected_target}: {manifest_target}")

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
        "status": "ok" if not errors else "failed",
        "checked_files": checked_files,
        "errors": errors,
    }


def successful_backup_dirs_for_vm(output_dir: Path, vm_info: Dict[str, Any]) -> List[Path]:
    if not output_dir.exists():
        return []
    vm_name = str(vm_info.get("name") or "")
    instance_uuid = str(vm_info.get("instance_uuid") or "")
    matches: List[Path] = []
    for manifest_file in sorted(output_dir.rglob("backup_manifest.json")):
        if any(part.endswith((".inprogress", ".failed")) for part in manifest_file.parts):
            continue
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if manifest.get("status") not in {"success", "success_rescued"}:
            continue
        manifest_vm_info = manifest.get("vm_info") or {}
        manifest_uuid = str(manifest_vm_info.get("instance_uuid") or "")
        manifest_name = str(manifest.get("target_vm") or manifest_vm_info.get("name") or "")
        if instance_uuid and manifest_uuid:
            if manifest_uuid == instance_uuid:
                matches.append(manifest_file.parent)
            continue
        if manifest_name == vm_name:
            matches.append(manifest_file.parent)
    return matches


def choose_delta_pack_for_backup(
    vm_info: Dict[str, Any],
    config: VSphereConfig,
    requested_mode: str = "ask",
    assume_yes: bool = False,
) -> bool:
    if requested_mode == "delta":
        return True
    if requested_mode == "full":
        return False

    existing = successful_backup_dirs_for_vm(config.output_dir, vm_info)
    if not existing:
        return False
    if assume_yes or not sys.stdin.isatty():
        LOGGER.info("Existing backups found for %s; defaulting to full backup in non-interactive mode", vm_info["name"])
        return False

    latest = max(existing, key=lambda path: path.stat().st_mtime)
    print()
    print(f"Es gibt bereits {len(existing)} erfolgreiches Backup fuer {vm_info['name']}.")
    print(f"Letztes Backup: {latest}")
    raw = input("Delta-Speicher fuer dieses neue Backup nutzen? [j/N]: ").strip().lower()
    return raw in {"j", "ja", "y", "yes"}


def delta_pack_completed_backup(
    backup_dir: Path,
    keep_originals: bool = False,
    chunk_size_mb: int = 16,
) -> Dict[str, Any]:
    return delta_storage.pack_backup_dir(
        backup_dir,
        remove_originals=not keep_originals,
        chunk_size=int(chunk_size_mb * 1024 * 1024),
    )


def command_inventory(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    with VSphereSession(config) as session:
        infos = [collect_vm_info(vm) for vm in session.all_vms()]
        rest_by_moref = session.rest_inventory_by_moref()
        for info in infos:
            rest_record = rest_by_moref.get(str(info.get("moref") or ""))
            if rest_record:
                info["vcenter_rest"] = rest_record
    if not args.show_all:
        infos = [info for info in infos if info["name"] == TARGET_VM_NAME]
    if args.json:
        print(json.dumps(infos, indent=2, sort_keys=True))
    else:
        print_inventory(infos)
        if not infos:
            print(f"Target VM not found: {TARGET_VM_NAME}")
            return 2
    return 0


def command_preflight(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    with VSphereSession(config) as session:
        vm = session.find_target_vm()
        info = collect_vm_info(vm)
        checks = validate_target_vm(info, config)
    if args.json:
        print(json.dumps({"vm_info": info, "checks": checks, "ok": checks_passed(checks)}, indent=2, sort_keys=True))
    else:
        print_inventory([info])
        print()
        print_checks(checks)
    return 0 if checks_passed(checks) else 3


def command_backup(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.confirm_vm != config.target_vm_name:
        raise SafetyError(f"Confirmation must be exactly: {config.target_vm_name}")
    with VSphereSession(config) as session:
        vm = session.find_target_vm()
        vm_info = collect_vm_info(vm)
        use_delta = choose_delta_pack_for_backup(vm_info, config, args.backup_mode)
        final_dir = create_backup(session, vm, config)
    if use_delta:
        pack_result = delta_pack_completed_backup(
            final_dir,
            keep_originals=args.keep_delta_originals,
            chunk_size_mb=args.chunk_size_mb,
        )
        print(json.dumps({"backup_dir": str(final_dir), "delta_pack": pack_result}, indent=2, sort_keys=True))
    else:
        print(f"Backup completed: {final_dir}")
    return 0


def command_backup_delta(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.confirm_vm != config.target_vm_name:
        raise SafetyError(f"Confirmation must be exactly: {config.target_vm_name}")
    with VSphereSession(config) as session:
        vm = session.find_target_vm()
        final_dir = create_backup(session, vm, config)
    pack_result = delta_pack_completed_backup(
        final_dir,
        keep_originals=args.keep_originals,
        chunk_size_mb=args.chunk_size_mb,
    )
    print(json.dumps({"backup_dir": str(final_dir), "delta_pack": pack_result}, indent=2, sort_keys=True))
    return 0


def command_delta_pack(args: argparse.Namespace) -> int:
    result = delta_storage.pack_backup_dir(
        Path(args.backup_dir).expanduser().resolve(),
        remove_originals=args.remove_originals,
        chunk_size=int(args.chunk_size_mb * 1024 * 1024),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_verify(args: argparse.Namespace) -> int:
    result = verify_backup_dir(Path(args.backup_dir).expanduser().resolve(), expected_target=args.expected_vm)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "ok" else 4


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        help="Path to credentials.env/config.env. Default: ./credentials.env, script-dir/credentials.env, then config.env",
    )
    common.add_argument("--verbose", action="store_true", help="Enable debug logging")

    parser = argparse.ArgumentParser(description="Safe vSphere/vCenter API backup tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser("inventory", parents=[common], help="Read-only VM inventory discovery")
    inventory.add_argument("--show-all", action="store_true", help="Print all VMs. Default prints only the configured target VM.")
    inventory.add_argument("--json", action="store_true", help="Print JSON")
    inventory.set_defaults(func=command_inventory)

    preflight = subparsers.add_parser("preflight", parents=[common], help="Validate configured target VM safety checks")
    preflight.add_argument("--json", action="store_true", help="Print JSON")
    preflight.set_defaults(func=command_preflight)

    backup = subparsers.add_parser("backup", parents=[common], help="Run live full backup for the configured target VM")
    backup.add_argument(
        "--confirm-vm",
        "--confirm-docsign",
        dest="confirm_vm",
        required=True,
        help="Required safety confirmation. Must be exactly the configured VSPHERE_TARGET_VM.",
    )
    backup.add_argument(
        "--backup-mode",
        choices=["ask", "full", "delta"],
        default="ask",
        help="Default ask: if an older backup exists, prompt for full or local delta storage.",
    )
    backup.add_argument(
        "--keep-delta-originals",
        action="store_true",
        help="When delta mode is selected, keep original large VMDK files after writing chunks",
    )
    backup.add_argument("--chunk-size-mb", type=int, default=16, help="Delta chunk size in MiB. Default: 16")
    backup.set_defaults(func=command_backup)

    backup_delta = subparsers.add_parser(
        "backup-delta",
        parents=[common],
        help="Run normal target VM backup, then delta-pack large VMDK files locally",
    )
    backup_delta.add_argument(
        "--confirm-vm",
        "--confirm-docsign",
        dest="confirm_vm",
        required=True,
        help="Required safety confirmation. Must be exactly the configured VSPHERE_TARGET_VM.",
    )
    backup_delta.add_argument(
        "--keep-originals",
        action="store_true",
        help="Keep original large VMDK files after writing delta chunks",
    )
    backup_delta.add_argument("--chunk-size-mb", type=int, default=16, help="Delta chunk size in MiB. Default: 16")
    backup_delta.set_defaults(func=command_backup_delta)

    delta_pack = subparsers.add_parser(
        "delta-pack",
        parents=[common],
        help="Delta-pack one existing backup directory locally",
    )
    delta_pack.add_argument("backup_dir", help="Backup directory containing backup_manifest.json")
    delta_pack.add_argument(
        "--remove-originals",
        action="store_true",
        help="Remove packed large VMDK files after chunk verification",
    )
    delta_pack.add_argument("--chunk-size-mb", type=int, default=16, help="Delta chunk size in MiB. Default: 16")
    delta_pack.set_defaults(func=command_delta_pack)

    verify = subparsers.add_parser("verify", parents=[common], help="Verify hashes of one backup directory")
    verify.add_argument("backup_dir", help="Backup directory containing backup_manifest.json")
    verify.add_argument("--expected-vm", default="", help="Optional target VM name that must match the manifest")
    verify.set_defaults(func=command_verify)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(verbose=getattr(args, "verbose", False))
    try:
        return int(args.func(args))
    except SafetyError as exc:
        LOGGER.error("Blocked by safety rule: %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 10
    except KeyboardInterrupt:
        LOGGER.error(INTERRUPTED_MESSAGE)
        print(f"ERROR: {INTERRUPTED_MESSAGE}", file=sys.stderr)
        return 130
    except Exception as exc:
        LOGGER.error("Command failed: %s", exc, exc_info=getattr(args, "verbose", False))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
