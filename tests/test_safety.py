import unittest
import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pyVmomi import vim, vmodl

from safe_vsphere_backup import (
    VSphereConfig,
    TARGET_VM_NAME,
    backup_method_for_power_state,
    choose_delta_pack_for_backup,
    checks_passed,
    cbt_state_from_snapshot,
    datastore_sibling_path,
    delete_datastore_path,
    download_progress_detail,
    ensure_change_tracking_enabled,
    export_snapshot_with_datastore_copy,
    is_missing_removable_media_export_error,
    load_config,
    split_vsphere_endpoint,
    successful_backup_dirs_for_vm,
    snapshot_copy_stem,
    task_state_if_available,
    transfer_http_nfc_lease_or_snapshot_copy,
    validate_docsign_target,
    verify_backup_dir,
    wait_for_task_progress,
)
from select_vm_backup import (
    batch_free_space_check,
    build_parser as build_select_vm_backup_parser,
    build_backup_plans,
    confirm_continue_with_passing_plans,
    estimate_selected_vm_free_space,
    fatal_check_names,
    next_backup_chain_slot,
    parse_selection_file,
    parse_selection_numbers,
    resolve_selection_entries,
    run_backup_plans,
    selection_file_line,
    split_passable_plans,
    validate_selected_vm_target,
    verify_any_backup_dir,
    write_selection_file,
)
from restore_vm_backup import (
    BackupArtifacts,
    adapter_type_for_import,
    alternate_datastore_folder_names,
    restore_materialization_parent,
    discover_restore_points,
    format_restore_point_table,
    format_restore_vm_table,
    imported_disk_descriptor_name,
    parse_vmdk_change_track_files,
    parse_vmdk_extent_files_from_text,
    sanitize_vmdk_descriptor_for_import,
    resolve_mac_address_mode,
    restore_point_from_manifest,
    source_mac_address,
    validate_datastore_folder_name,
    validate_vm_display_name,
)
from delta_storage import (
    cleanup_hydrated_files,
    cleanup_unreferenced_chunks,
    manifest_path,
    list_chunk_files,
    materialize_delta_file,
    pack_backup_dir,
)


def base_config():
    return VSphereConfig(
        host="example.local",
        user="backup",
        password="secret",
        output_dir=Path("/tmp"),
        min_free_gb=0.01,
    )


def base_vm_info(**overrides):
    data = {
        "name": TARGET_VM_NAME,
        "moref": "vm-123",
        "instance_uuid": "uuid-1",
        "bios_uuid": "bios-1",
        "power_state": "poweredOff",
        "template": False,
        "num_cpu": 2,
        "memory_mb": 4096,
        "guest_full_name": "Other Linux",
        "vmx_path": "[datastore1] DocuSign/DocuSign.vmx",
        "change_tracking_enabled": False,
        "has_snapshot": False,
        "disks": [
            {
                "label": "Hard disk 1",
                "key": 2000,
                "capacity_bytes": 25 * 1024**3,
                "capacity_gb": 25.0,
                "file_name": "[datastore1] DocuSign/DocuSign.vmdk",
                "datastore": "datastore1",
                "thin_provisioned": True,
                "backing_type": "vim.vm.device.VirtualDisk.FlatVer2BackingInfo",
            }
        ],
        "nics": [
            {
                "label": "Network adapter 1",
                "key": 4000,
                "mac_address": "00:50:56:aa:bb:cc",
                "address_type": "generated",
                "network_name": "VM Network",
                "connected": True,
                "start_connected": True,
                "device_type": "VirtualVmxnet3",
            }
        ],
    }
    data.update(overrides)
    return data


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SafetyValidationTests(unittest.TestCase):
    def test_expected_docsign_vm_passes(self):
        checks = validate_docsign_target(base_vm_info(), base_config())
        self.assertTrue(checks_passed(checks), checks)

    def test_wrong_name_fails(self):
        checks = validate_docsign_target(base_vm_info(name="OtherVM"), base_config())
        self.assertFalse(checks_passed(checks))

    def test_powered_on_uses_snapshot_export_and_passes(self):
        checks = validate_docsign_target(base_vm_info(power_state="poweredOn"), base_config())
        self.assertTrue(checks_passed(checks), checks)
        self.assertEqual(backup_method_for_power_state("poweredOn"), "snapshot_export")

    def test_suspended_fails(self):
        checks = validate_docsign_target(base_vm_info(power_state="suspended"), base_config())
        self.assertFalse(checks_passed(checks))

    def test_existing_snapshot_fails(self):
        checks = validate_docsign_target(base_vm_info(power_state="poweredOn", has_snapshot=True), base_config())
        self.assertFalse(checks_passed(checks))

    def test_wrong_disk_size_fails(self):
        info = base_vm_info()
        info["disks"][0]["capacity_gb"] = 30.0
        checks = validate_docsign_target(info, base_config())
        self.assertFalse(checks_passed(checks))

    def test_optional_uuid_lock_fails_when_mismatch(self):
        cfg = VSphereConfig(
            host="example.local",
            user="backup",
            password="secret",
            output_dir=Path("/tmp"),
            expected_instance_uuid="expected-uuid",
            min_free_gb=0.01,
        )
        checks = validate_docsign_target(base_vm_info(instance_uuid="other-uuid"), cfg)
        self.assertFalse(checks_passed(checks))

    def test_generic_powered_off_other_vm_passes(self):
        checks = validate_selected_vm_target(base_vm_info(name="OtherVM", power_state="poweredOff"), base_config())
        self.assertTrue(checks_passed(checks), checks)

    def test_generic_powered_on_multi_disk_passes(self):
        info = base_vm_info(name="OtherVM", power_state="poweredOn")
        info["disks"].append(
            {
                "label": "Hard disk 2",
                "key": 2001,
                "capacity_bytes": 10 * 1024**3,
                "capacity_gb": 10.0,
                "file_name": "[datastore1] OtherVM/OtherVM_1.vmdk",
                "datastore": "datastore1",
                "thin_provisioned": True,
                "backing_type": "vim.vm.device.VirtualDisk.FlatVer2BackingInfo",
            }
        )
        checks = validate_selected_vm_target(info, base_config())
        self.assertTrue(checks_passed(checks), checks)

    def test_generic_powered_on_multi_disk_rejects_bad_datastore_path(self):
        info = base_vm_info(name="OtherVM", power_state="poweredOn")
        info["disks"].append(
            {
                "label": "Hard disk 2",
                "key": 2001,
                "capacity_bytes": 10 * 1024**3,
                "capacity_gb": 10.0,
                "file_name": "OtherVM/OtherVM_1.vmdk",
                "datastore": "datastore1",
                "thin_provisioned": True,
                "backing_type": "vim.vm.device.VirtualDisk.FlatVer2BackingInfo",
            }
        )
        checks = validate_selected_vm_target(info, base_config())
        self.assertFalse(checks_passed(checks))

    def test_snapshot_datastore_copy_names_are_stable_for_multi_disk(self):
        self.assertEqual(snapshot_copy_stem(1, 1), "DocuSign-snapshot")
        self.assertEqual(snapshot_copy_stem(1, 2), "DocuSign-disk1-snapshot")
        self.assertEqual(snapshot_copy_stem(2, 2), "DocuSign-disk2-snapshot")
        self.assertEqual(datastore_sibling_path("VM/VM.vmx", "VM.nvram"), "VM/VM.nvram")

    def test_snapshot_datastore_copy_handles_multiple_disks(self):
        info = base_vm_info(power_state="poweredOn")
        info["disks"].append(
            {
                "label": "Hard disk 2",
                "key": 2001,
                "capacity_bytes": 10 * 1024**3,
                "capacity_gb": 10.0,
                "file_name": "[datastore1] DocuSign/DocuSign_1.vmdk",
                "datastore": "datastore1",
                "thin_provisioned": True,
                "backing_type": "vim.vm.device.VirtualDisk.FlatVer2BackingInfo",
            }
        )
        copied = []
        deleted = []

        class FakeFileManager:
            def MakeDirectory(self, **kwargs):
                return None

        class FakeVirtualDiskManager:
            def CopyVirtualDisk_Task(self, **kwargs):
                copied.append(kwargs)
                return SimpleNamespace()

        session = SimpleNamespace(
            content=SimpleNamespace(
                fileManager=FakeFileManager(),
                virtualDiskManager=FakeVirtualDiskManager(),
            )
        )

        def fake_read_datastore_text_file(session, datacenter, datastore, datastore_file, config):
            stem = Path(datastore_file).stem
            return f'# Disk DescriptorFile\nRW 1 SPARSE "{stem}-s001.vmdk"\nddb.adapterType = "lsilogic"\n'

        def fake_get_datastore_file(session, datacenter, datastore, datastore_file, config, target_file, optional=False):
            return {
                "name": target_file.name,
                "bytes": 1,
                "sha256": "0",
                "transfer": "datastore_http",
            }

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("safe_vsphere_backup.find_datacenter_for_vm", return_value=SimpleNamespace(name="dc1")),
                patch("safe_vsphere_backup.read_datastore_text_file", side_effect=fake_read_datastore_text_file),
                patch("safe_vsphere_backup.get_datastore_file", side_effect=fake_get_datastore_file),
                patch("safe_vsphere_backup.wait_for_task_progress", return_value=None),
                patch("safe_vsphere_backup.delete_datastore_path", side_effect=lambda *args: deleted.append(args[2])),
            ):
                result = export_snapshot_with_datastore_copy(
                    session=session,
                    vm=SimpleNamespace(),
                    vm_info=info,
                    backup_dir=Path(tmp),
                    config=base_config(),
                    backup_id="backup-1",
                )

        descriptors = [item for item in result["files"] if item.get("vmdk_role") == "descriptor"]
        extents = [item for item in result["files"] if item.get("vmdk_role") == "extent"]

        self.assertEqual([item["name"] for item in descriptors], [
            "DocuSign-disk1-snapshot.vmdk",
            "DocuSign-disk2-snapshot.vmdk",
        ])
        self.assertEqual([item["disk_index"] for item in descriptors], [1, 2])
        self.assertEqual(len(extents), 2)
        self.assertEqual(len(copied), 2)
        self.assertEqual(result["remote_temporary_copy"]["disk_count"], 2)
        self.assertEqual(len(result["remote_temporary_copy"]["copies"]), 2)
        self.assertTrue(any(path.endswith("DocuSign-disk2-snapshot-s001.vmdk") for path in deleted))

    def test_snapshot_datastore_copy_ignores_outer_pyvmomi_exception(self):
        info = base_vm_info(power_state="poweredOn")
        deleted = []

        class FakeFileManager:
            def MakeDirectory(self, **kwargs):
                return None

        class FakeVirtualDiskManager:
            def CopyVirtualDisk_Task(self, **kwargs):
                return SimpleNamespace()

        session = SimpleNamespace(
            content=SimpleNamespace(
                fileManager=FakeFileManager(),
                virtualDiskManager=FakeVirtualDiskManager(),
            )
        )

        def fake_read_datastore_text_file(session, datacenter, datastore, datastore_file, config):
            stem = Path(datastore_file).stem
            return f'# Disk DescriptorFile\nRW 1 SPARSE "{stem}-s001.vmdk"\nddb.adapterType = "lsilogic"\n'

        def fake_get_datastore_file(session, datacenter, datastore, datastore_file, config, target_file, optional=False):
            return {
                "name": target_file.name,
                "bytes": 1,
                "sha256": "0",
                "transfer": "datastore_http",
            }

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("safe_vsphere_backup.find_datacenter_for_vm", return_value=SimpleNamespace(name="dc1")),
                patch("safe_vsphere_backup.read_datastore_text_file", side_effect=fake_read_datastore_text_file),
                patch("safe_vsphere_backup.get_datastore_file", side_effect=fake_get_datastore_file),
                patch("safe_vsphere_backup.wait_for_task_progress", return_value=None),
                patch("safe_vsphere_backup.delete_datastore_path", side_effect=lambda *args: deleted.append(args[2])),
            ):
                try:
                    raise vmodl.fault.NotSupported()
                except vmodl.fault.NotSupported:
                    result = export_snapshot_with_datastore_copy(
                        session=session,
                        vm=SimpleNamespace(),
                        vm_info=info,
                        backup_dir=Path(tmp),
                        config=base_config(),
                        backup_id="backup-1",
                    )

        self.assertEqual(result["transfer_method"], "snapshot_datastore_copy")
        self.assertEqual(result["remote_temporary_copy"]["cleanup_status"], "success")
        self.assertEqual(len(deleted), 3)

    def test_missing_removable_media_export_error_is_detected(self):
        self.assertTrue(
            is_missing_removable_media_export_error(
                RuntimeError(
                    "HttpNfcLease entered error state: File "
                    "ds:///vmfs/volumes/datastore/ISO/ubuntu.iso was not found"
                )
            )
        )
        self.assertTrue(
            is_missing_removable_media_export_error(
                RuntimeError("File ds:///vmfs/volumes/datastore/floppy.flp was not found")
            )
        )
        self.assertFalse(
            is_missing_removable_media_export_error(
                RuntimeError("File ds:///vmfs/volumes/datastore/VM/VM.vmdk was not found")
            )
        )
        self.assertFalse(
            is_missing_removable_media_export_error(RuntimeError("Lease expired while exporting disk-1.iso"))
        )

    def test_http_nfc_missing_iso_falls_back_to_snapshot_datastore_copy(self):
        aborted = []

        class FakeLease:
            state = vim.HttpNfcLease.State.error
            error = "File ds:///vmfs/volumes/datastore/ISO/ubuntu-24.04.iso was not found"

            def HttpNfcLeaseAbort(self, fault):
                aborted.append(fault)

        def fake_fallback(**kwargs):
            return {
                "files": [{"name": "DocuSign-snapshot.vmdk", "bytes": 1, "sha256": "0"}],
                "skipped_device_urls": [],
                "remote_temporary_copy": {"cleanup_status": "success"},
                "transfer_method": "snapshot_datastore_copy",
            }

        manifest = {}
        with tempfile.TemporaryDirectory() as tmp:
            with patch("safe_vsphere_backup.export_snapshot_with_datastore_copy", side_effect=fake_fallback):
                transfer_http_nfc_lease_or_snapshot_copy(
                    session=SimpleNamespace(),
                    vm=SimpleNamespace(),
                    vm_info=base_vm_info(power_state="poweredOn"),
                    lease=FakeLease(),
                    backup_dir=Path(tmp),
                    config=base_config(),
                    backup_id="backup-1",
                    manifest=manifest,
                    allow_snapshot_fallback=True,
                )

        self.assertEqual(len(aborted), 1)
        self.assertEqual(manifest["transfer_method"], "snapshot_datastore_copy")
        self.assertEqual(manifest["files"][0]["name"], "DocuSign-snapshot.vmdk")
        self.assertEqual(manifest["snapshot_export_fallback"]["reason"], "missing_removable_media_image")
        self.assertIn("ubuntu-24.04.iso", manifest["snapshot_export_error"])

    def test_snapshot_datastore_copy_stall_timeout_does_not_wait_twice(self):
        waits = []
        deleted = []
        task = SimpleNamespace(info=SimpleNamespace(state=vim.TaskInfo.State.running))

        class FakeFileManager:
            def MakeDirectory(self, **kwargs):
                return None

        class FakeVirtualDiskManager:
            def CopyVirtualDisk_Task(self, **kwargs):
                return task

        session = SimpleNamespace(
            content=SimpleNamespace(
                fileManager=FakeFileManager(),
                virtualDiskManager=FakeVirtualDiskManager(),
            )
        )
        config = VSphereConfig(
            host="example.local",
            user="backup",
            password="secret",
            output_dir=Path("/tmp"),
            min_free_gb=0.01,
            datastore_copy_stall_timeout_seconds=123,
        )

        def fake_wait_for_task_progress(task, action, stall_timeout_seconds):
            waits.append((action, stall_timeout_seconds))
            raise TimeoutError(f"{action} made no progress at 70% for {stall_timeout_seconds} seconds")

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("safe_vsphere_backup.find_datacenter_for_vm", return_value=SimpleNamespace(name="dc1")),
                patch(
                    "safe_vsphere_backup.read_datastore_text_file",
                    return_value='# Disk DescriptorFile\nRW 1 SPARSE "DocuSign-snapshot-s001.vmdk"\n',
                ),
                patch("safe_vsphere_backup.wait_for_task_progress", side_effect=fake_wait_for_task_progress),
                patch("safe_vsphere_backup.delete_datastore_path", side_effect=lambda *args: deleted.append(args[2])),
            ):
                with self.assertRaises(TimeoutError) as raised:
                    export_snapshot_with_datastore_copy(
                        session=session,
                        vm=SimpleNamespace(),
                        vm_info=base_vm_info(power_state="poweredOn"),
                        backup_dir=Path(tmp),
                        config=config,
                        backup_id="backup-1",
                    )

        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0][1], 123)
        self.assertEqual(deleted, [])
        self.assertEqual(getattr(raised.exception, "transfer_method"), "snapshot_datastore_copy")
        remote_copy = getattr(raised.exception, "remote_temporary_copy")
        self.assertEqual(remote_copy["cleanup_status"], "skipped_copy_still_running")
        self.assertEqual(remote_copy["copies"][0]["source_path"], "[datastore1] DocuSign/DocuSign.vmdk")

    def test_delete_datastore_path_retries_transient_failures(self):
        attempts = []

        class FakeFileManager:
            def DeleteDatastoreFile_Task(self, **kwargs):
                attempts.append(kwargs)
                return SimpleNamespace()

        session = SimpleNamespace(content=SimpleNamespace(fileManager=FakeFileManager()))

        with (
            patch("safe_vsphere_backup.wait_for_task", side_effect=[RuntimeError("busy"), None]),
            patch("safe_vsphere_backup.time.sleep") as sleep_mock,
        ):
            delete_datastore_path(
                session=session,
                datacenter=SimpleNamespace(name="dc1"),
                datastore_path="[datastore1] tmp/file.vmdk",
                config=base_config(),
            )

        self.assertEqual(len(attempts), 2)
        sleep_mock.assert_called_once_with(30)

    def test_delete_datastore_path_does_not_retry_timeouts(self):
        attempts = []

        class FakeFileManager:
            def DeleteDatastoreFile_Task(self, **kwargs):
                attempts.append(kwargs)
                return SimpleNamespace()

        session = SimpleNamespace(content=SimpleNamespace(fileManager=FakeFileManager()))

        with (
            patch("safe_vsphere_backup.wait_for_task", side_effect=TimeoutError("still running")),
            patch("safe_vsphere_backup.time.sleep") as sleep_mock,
        ):
            with self.assertRaises(TimeoutError):
                delete_datastore_path(
                    session=session,
                    datacenter=SimpleNamespace(name="dc1"),
                    datastore_path="[datastore1] tmp/file.vmdk",
                    config=base_config(),
                )

        self.assertEqual(len(attempts), 1)
        sleep_mock.assert_not_called()

    def test_list_backup_continues_after_one_vm_fails(self):
        calls = []
        plans = [
            {
                "vm": SimpleNamespace(name="first"),
                "vm_info": {"name": "first"},
                "chain_slot": {"kind": "full", "run_name": "full_0001", "failed_dir": Path("/tmp/first.failed")},
                "checks": [],
            },
            {
                "vm": SimpleNamespace(name="second"),
                "vm_info": {"name": "second"},
                "chain_slot": {"kind": "full", "run_name": "full_0001", "failed_dir": Path("/tmp/second.failed")},
                "checks": [],
            },
        ]

        def fake_create_selected_vm_backup(session, vm, vm_info, config, checks, chain_slot):
            calls.append(vm_info["name"])
            if vm_info["name"] == "first":
                raise RuntimeError("copy failed")
            return Path(f"/tmp/{vm_info['name']}/full_0001")

        with patch(
            "select_vm_backup.create_selected_vm_backup",
            side_effect=fake_create_selected_vm_backup,
        ):
            results = run_backup_plans(
                session=SimpleNamespace(),
                config=base_config(),
                plans=plans,
                fresh_session_per_vm=False,
            )

        self.assertEqual(calls, ["first", "second"])
        self.assertEqual([item["status"] for item in results], ["failed", "success"])
        self.assertEqual(results[0]["error"], "copy failed")
        self.assertEqual(results[1]["backup_dir"], "/tmp/second/full_0001")

    def test_list_backup_opens_fresh_session_for_each_vm(self):
        sessions = []
        calls = []
        first_info = base_vm_info(name="first", moref="vm-1", instance_uuid="uuid-1", bios_uuid="bios-1")
        second_info = base_vm_info(name="second", moref="vm-2", instance_uuid="uuid-2", bios_uuid="bios-2")
        plans = [
            {
                "vm": SimpleNamespace(name="stale-first"),
                "vm_info": first_info,
                "chain_slot": {
                    "kind": "full",
                    "run_name": "full_0001",
                    "failed_dir": Path("/tmp/first.failed"),
                    "vm_dir": Path("/tmp/first"),
                    "previous_dir": "",
                    "sequence": 1,
                },
                "checks": [],
            },
            {
                "vm": SimpleNamespace(name="stale-second"),
                "vm_info": second_info,
                "chain_slot": {
                    "kind": "full",
                    "run_name": "full_0001",
                    "failed_dir": Path("/tmp/second.failed"),
                    "vm_dir": Path("/tmp/second"),
                    "previous_dir": "",
                    "sequence": 1,
                },
                "checks": [],
            },
        ]

        class FakeSession:
            def __init__(self, config):
                self.index = len(sessions)
                sessions.append(self)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

        def fake_inventory_records(session):
            if session.index == 0:
                return [(SimpleNamespace(name="fresh-first"), first_info)]
            return [(SimpleNamespace(name="fresh-second"), second_info)]

        def fake_create_selected_vm_backup(session, vm, vm_info, config, checks, chain_slot):
            calls.append((session.index, vm.name, vm_info["name"]))
            return Path(f"/tmp/{vm_info['name']}/full_0001")

        with (
            patch("select_vm_backup.core.VSphereSession", FakeSession),
            patch("select_vm_backup.inventory_records", side_effect=fake_inventory_records),
            patch("select_vm_backup.create_selected_vm_backup", side_effect=fake_create_selected_vm_backup),
        ):
            results = run_backup_plans(
                session=SimpleNamespace(),
                config=base_config(),
                plans=plans,
            )

        self.assertEqual(calls, [(0, "fresh-first", "first"), (1, "fresh-second", "second")])
        self.assertEqual([item["status"] for item in results], ["success", "success"])

    def test_wait_for_task_progress_times_out_only_after_stall(self):
        class FakeClock:
            def __init__(self):
                self.now = 0.0

            def time(self):
                return self.now

            def sleep(self, seconds):
                self.now += seconds

        class FakeTask:
            def __init__(self):
                self.states = [
                    SimpleNamespace(state=vim.TaskInfo.State.running, progress=70),
                    SimpleNamespace(state=vim.TaskInfo.State.running, progress=70),
                    SimpleNamespace(state=vim.TaskInfo.State.running, progress=71),
                    SimpleNamespace(state=vim.TaskInfo.State.running, progress=71),
                    SimpleNamespace(state=vim.TaskInfo.State.success, result="ok"),
                ]

            @property
            def info(self):
                if len(self.states) > 1:
                    return self.states.pop(0)
                return self.states[0]

        clock = FakeClock()
        with (
            patch("safe_vsphere_backup.time.time", side_effect=clock.time),
            patch("safe_vsphere_backup.time.sleep", side_effect=clock.sleep),
        ):
            result = wait_for_task_progress(FakeTask(), "Copy virtual disk test", stall_timeout_seconds=3)

        self.assertEqual(result, "ok")

    def test_wait_for_task_progress_times_out_when_percent_is_stuck(self):
        class FakeClock:
            def __init__(self):
                self.now = 0.0

            def time(self):
                return self.now

            def sleep(self, seconds):
                self.now += seconds

        class FakeTask:
            @property
            def info(self):
                return SimpleNamespace(state=vim.TaskInfo.State.running, progress=70)

        clock = FakeClock()
        with (
            patch("safe_vsphere_backup.time.time", side_effect=clock.time),
            patch("safe_vsphere_backup.time.sleep", side_effect=clock.sleep),
        ):
            with self.assertRaisesRegex(TimeoutError, "no progress at 70%"):
                wait_for_task_progress(FakeTask(), "Copy virtual disk test", stall_timeout_seconds=5)

    def test_download_progress_detail_uses_bytes_and_percent(self):
        detail = download_progress_detail(512 * 1024**2, 2 * 1024**3, 120)

        self.assertIn("512.00 MiB/2.00 GiB", detail)
        self.assertIn("25.0%", detail)
        self.assertIn("no byte progress for 120s", detail)

    def test_download_stall_timeout_config_is_separate_from_read_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "config.env"
            config_file.write_text(
                "\n".join(
                    [
                        "VSPHERE_HOST=example.local",
                        "VSPHERE_USER=backup",
                        "VSPHERE_PASSWORD=secret",
                        "READ_TIMEOUT_SECONDS=300",
                        "DOWNLOAD_STALL_TIMEOUT_SECONDS=21600",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config(str(config_file))

        self.assertEqual(config.read_timeout_seconds, 300)
        self.assertEqual(config.download_stall_timeout_seconds, 21600)

    def test_vsphere_endpoint_accepts_host_port_and_url(self):
        self.assertEqual(split_vsphere_endpoint("vcsa.example.local", "443"), ("vcsa.example.local", 443))
        self.assertEqual(split_vsphere_endpoint("vcsa.example.local:8443", "443"), ("vcsa.example.local", 8443))
        self.assertEqual(
            split_vsphere_endpoint("https://vcsa.example.local:9443/sdk", "443"),
            ("vcsa.example.local", 9443),
        )

    def test_interactive_free_space_uses_latest_successful_backup_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "DocuSign_20260622_120000_uuid"
            backup_dir.mkdir()
            (backup_dir / "backup_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "success",
                        "target_vm": TARGET_VM_NAME,
                        "vm_info": base_vm_info(),
                        "files": [{"name": "disk.vmdk", "bytes": 10 * 1024**3}],
                    }
                ),
                encoding="utf-8",
            )
            cfg = VSphereConfig(
                host="example.local",
                user="backup",
                password="secret",
                output_dir=root,
                min_free_gb=9999,
            )

            requirement = estimate_selected_vm_free_space(base_vm_info(), cfg)

            self.assertEqual(requirement["source"], "latest_successful_backup")
            self.assertEqual(requirement["required_gb"], 22.0)

    def test_interactive_free_space_scales_up_for_large_existing_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "LargeVM_20260622_120000_uuid"
            backup_dir.mkdir()
            info = base_vm_info(name="LargeVM", instance_uuid="large-uuid")
            (backup_dir / "backup_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "success",
                        "target_vm": "LargeVM",
                        "vm_info": info,
                        "files": [{"name": "disk.vmdk", "bytes": 100 * 1024**3}],
                    }
                ),
                encoding="utf-8",
            )
            cfg = VSphereConfig(
                host="example.local",
                user="backup",
                password="secret",
                output_dir=root,
                min_free_gb=0.01,
            )

            checks = validate_selected_vm_target(info, cfg, dynamic_free_space=True)
            free_space = next(check for check in checks if check["name"] == "backup_storage_free_space")

            self.assertIn("source=latest_successful_backup", free_space["detail"])
            self.assertIn("required_gb=210.0", free_space["detail"])

    def test_interactive_free_space_uses_latest_delta_new_chunk_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "DocuSign" / "delta_0002"
            backup_dir.mkdir(parents=True)
            (backup_dir / "backup_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "success",
                        "target_vm": TARGET_VM_NAME,
                        "vm_info": base_vm_info(),
                        "delta_storage": {"new_chunk_bytes": 512 * 1024**2},
                        "backup_chain": {"kind": "delta", "sequence": 2, "run_name": "delta_0002"},
                        "files": [{"name": "disk.vmdk", "bytes": 10 * 1024**3}],
                    }
                ),
                encoding="utf-8",
            )
            cfg = VSphereConfig(
                host="example.local",
                user="backup",
                password="secret",
                output_dir=root,
                min_free_gb=0.01,
            )

            requirement = estimate_selected_vm_free_space(base_vm_info(), cfg, backup_mode="delta")

            self.assertEqual(requirement["source"], "latest_successful_backup")
            self.assertEqual(requirement["base_gb"], 10.0)
            self.assertEqual(requirement["delta_extra_gb"], 0.5)
            self.assertEqual(requirement["required_gb"], 12.5)

            local_requirement = estimate_selected_vm_free_space(base_vm_info(), cfg, backup_mode="local-delta")
            self.assertEqual(local_requirement["delta_extra_gb"], 0.5)
            self.assertEqual(local_requirement["required_gb"], 12.5)

    def test_first_backup_estimate_prefers_provisioned_disk_over_committed_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = VSphereConfig(
                host="example.local",
                user="backup",
                password="secret",
                output_dir=root,
                min_free_gb=0.01,
            )
            info = base_vm_info(storage_committed_bytes=30 * 1024**3)

            requirement = estimate_selected_vm_free_space(info, cfg, backup_mode="full")

            self.assertEqual(requirement["source"], "provisioned_disk_capacity")
            self.assertEqual(requirement["base_gb"], 25.0)
            self.assertEqual(requirement["required_gb"], 27.5)

    def test_restore_name_validation_blocks_datastore_path_chars(self):
        with self.assertRaises(Exception):
            validate_vm_display_name("Bad/Name")

    def test_restore_folder_validation_blocks_datastore_path_chars(self):
        with self.assertRaises(Exception):
            validate_datastore_folder_name("Bad/Name")

    def test_restore_alternate_folder_names_add_numeric_suffix(self):
        names = list(alternate_datastore_folder_names("DocuSign_Restore", max_attempts=4))
        self.assertEqual(names, ["DocuSign_Restore_2", "DocuSign_Restore_3", "DocuSign_Restore_4"])

    def test_restore_materialization_uses_backup_volume_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "Backup" / "DocuSign" / "delta_0003"
            with patch.dict("restore_vm_backup.os.environ", {}, clear=True):
                self.assertEqual(
                    restore_materialization_parent(backup_dir),
                    Path(tmp) / "Backup" / ".restore-tmp",
                )

    def test_restore_materialization_honors_explicit_workspace(self):
        backup_dir = Path("/srv/Backup/DocuSign/delta_0003")
        with patch.dict(
            "restore_vm_backup.os.environ",
            {"VSPHERE_RESTORE_TMPDIR": "/var/tmp/vsphere-restore"},
            clear=True,
        ):
            self.assertEqual(
                restore_materialization_parent(backup_dir),
                Path("/var/tmp/vsphere-restore"),
            )

    def test_restore_vmdk_descriptor_parsing(self):
        descriptor = '\n'.join(
            [
                '# Disk DescriptorFile',
                'RW 52428800 SPARSE "DocuSign-snapshot-s001.vmdk"',
                'changeTrackPath="DocuSign-snapshot-ctk.vmdk"',
            ]
        )
        self.assertEqual(parse_vmdk_extent_files_from_text(descriptor), ["DocuSign-snapshot-s001.vmdk"])
        self.assertEqual(parse_vmdk_change_track_files(descriptor), ["DocuSign-snapshot-ctk.vmdk"])

    def test_restore_import_descriptor_strips_change_tracking(self):
        descriptor = '\n'.join(
            [
                '# Disk DescriptorFile',
                'version=3',
                'RW 52428800 SPARSE "DocuSign-snapshot-s001.vmdk"',
                '# Change Tracking File',
                'changeTrackPath="DocuSign-snapshot-ctk.vmdk"',
                'ddb.adapterType = "lsilogic"',
            ]
        )
        sanitized = sanitize_vmdk_descriptor_for_import(descriptor)
        self.assertEqual(parse_vmdk_extent_files_from_text(sanitized), ["DocuSign-snapshot-s001.vmdk"])
        self.assertEqual(parse_vmdk_change_track_files(sanitized), [])
        self.assertIn('ddb.adapterType = "lsilogic"', sanitized)

    def test_restore_import_disk_name_avoids_uploaded_backup_descriptor(self):
        artifacts = BackupArtifacts(
            backup_dir=Path("/tmp"),
            manifest={},
            vm_info={},
            descriptor_name="DocuSign_Restore.vmdk",
            extent_names=[],
            ctk_names=[],
            upload_files=["DocuSign_Restore.vmdk"],
            ovf={},
        )
        self.assertEqual(
            imported_disk_descriptor_name("DocuSign_Restore", artifacts),
            "DocuSign_Restore-imported.vmdk",
        )

    def test_restore_import_adapter_maps_pvscsi_to_esxi_import_value(self):
        artifacts = BackupArtifacts(
            backup_dir=Path("/tmp"),
            manifest={},
            vm_info={},
            descriptor_name="disk.vmdk",
            extent_names=[],
            ctk_names=[],
            upload_files=["disk.vmdk"],
            ovf={"scsi_subtype": "VirtualSCSI"},
        )
        self.assertEqual(adapter_type_for_import(artifacts), "lsiLogic")

    def test_restore_point_discovery_lists_versions_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vm_dir = root / "DocuSign"
            full = vm_dir / "full_0001"
            delta = vm_dir / "delta_0002"
            failed = vm_dir / "delta_0003.failed"
            for path in (full, delta, failed):
                path.mkdir(parents=True)

            (full / "backup_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "success",
                        "target_vm": TARGET_VM_NAME,
                        "started_at": "2026-06-24T06:05:53+00:00",
                        "finished_at": "2026-06-24T06:32:08+00:00",
                        "backup_chain": {"kind": "full", "sequence": 1, "run_name": "full_0001"},
                        "files": [{"name": "disk.vmdk", "bytes": 10 * 1024**3}],
                    }
                ),
                encoding="utf-8",
            )
            (delta / "backup_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "success",
                        "target_vm": TARGET_VM_NAME,
                        "started_at": "2026-06-26T11:35:18+00:00",
                        "finished_at": "2026-06-26T12:37:24+00:00",
                        "backup_chain": {"kind": "delta", "sequence": 2, "run_name": "delta_0002"},
                        "delta_storage": {"packed_files": 1, "removed_original_bytes": 10 * 1024**3},
                        "files": [{"name": "disk.vmdk", "bytes": 10 * 1024**3}],
                    }
                ),
                encoding="utf-8",
            )
            (failed / "backup_manifest.json").write_text(
                json.dumps({"status": "failed", "target_vm": TARGET_VM_NAME, "files": []}),
                encoding="utf-8",
            )

            points = discover_restore_points(root)
            table = format_restore_point_table(points)
            vm_table = format_restore_vm_table(points)

            self.assertEqual([point.run_name for point in points], ["delta_0002", "full_0001"])
            self.assertEqual(points[0].kind, "delta")
            self.assertEqual(points[0].delta_packed_files, 1)
            self.assertIn("Gefundene Restore-Punkte: 2", table)
            self.assertNotIn("\n\n", table)
            self.assertIn("2026-06-26 12:37:24 UTC", table)
            self.assertIn("delta_0002", table)
            self.assertIn("Gefundene VMs: 1", vm_table)
            self.assertIn("DocuSign", vm_table)
            self.assertIn("Versionen", vm_table)

    def test_restore_point_from_legacy_manifest_uses_directory_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "DocuSign_20260624_063208_uuid"
            backup_dir.mkdir()
            manifest_file = backup_dir / "backup_manifest.json"
            manifest_file.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "target_vm": TARGET_VM_NAME,
                        "finished_at": "2026-06-24T06:32:08+00:00",
                        "files": [{"name": "disk.vmdk", "bytes": 1}],
                    }
                ),
                encoding="utf-8",
            )

            point = restore_point_from_manifest(manifest_file)

            self.assertIsNotNone(point)
            self.assertEqual(point.run_name, "DocuSign_20260624_063208_uuid")
            self.assertEqual(point.kind, "full")

    def test_existing_backup_enables_delta_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "DocuSign_20260622_120000_uuid"
            backup_dir.mkdir()
            (backup_dir / "backup_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "success",
                        "target_vm": TARGET_VM_NAME,
                        "vm_info": base_vm_info(),
                        "files": [],
                    }
                ),
                encoding="utf-8",
            )
            cfg = base_config()
            cfg = VSphereConfig(
                host=cfg.host,
                user=cfg.user,
                password=cfg.password,
                output_dir=root,
                min_free_gb=cfg.min_free_gb,
            )

            self.assertEqual(successful_backup_dirs_for_vm(root, base_vm_info()), [backup_dir])
            self.assertTrue(choose_delta_pack_for_backup(base_vm_info(), cfg, requested_mode="delta"))
            self.assertFalse(choose_delta_pack_for_backup(base_vm_info(), cfg, requested_mode="full"))
            self.assertFalse(choose_delta_pack_for_backup(base_vm_info(), cfg, requested_mode="ask", assume_yes=True))

    def test_interactive_chain_starts_with_full_then_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = VSphereConfig(
                host="example.local",
                user="backup",
                password="secret",
                output_dir=root,
                min_free_gb=0.01,
            )

            first = next_backup_chain_slot(cfg, base_vm_info(), requested_mode="delta")
            self.assertEqual(first["kind"], "full")
            self.assertEqual(first["run_name"], "full_0001")

            first["final_dir"].mkdir(parents=True)
            (first["final_dir"] / "backup_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "success",
                        "target_vm": TARGET_VM_NAME,
                        "vm_info": base_vm_info(),
                        "backup_chain": {"kind": "full", "sequence": 1, "run_name": "full_0001"},
                        "files": [],
                    }
                ),
                encoding="utf-8",
            )

            second = next_backup_chain_slot(cfg, base_vm_info(), requested_mode="delta")
            self.assertEqual(successful_backup_dirs_for_vm(root, base_vm_info()), [first["final_dir"]])
            self.assertEqual(second["kind"], "delta")
            self.assertEqual(second["run_name"], "delta_0002")

    def test_cbt_chain_rebaselines_when_latest_backup_has_no_valid_cbt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = VSphereConfig(
                host="example.local",
                user="backup",
                password="secret",
                output_dir=root,
                min_free_gb=0.01,
            )
            vm_dir = root / "DocuSign"
            full = vm_dir / "full_0001"
            full.mkdir(parents=True)
            (full / "backup_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "success",
                        "target_vm": TARGET_VM_NAME,
                        "vm_info": base_vm_info(change_tracking_enabled=True),
                        "backup_chain": {"kind": "full", "sequence": 1, "run_name": "full_0001"},
                        "cbt_state": {"valid": False},
                        "files": [],
                    }
                ),
                encoding="utf-8",
            )

            slot = next_backup_chain_slot(cfg, base_vm_info(change_tracking_enabled=True), requested_mode="cbt")

        self.assertEqual(slot["kind"], "full")
        self.assertEqual(slot["run_name"], "full_0002")

    def test_cbt_chain_uses_delta_when_latest_backup_has_valid_cbt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = VSphereConfig(
                host="example.local",
                user="backup",
                password="secret",
                output_dir=root,
                min_free_gb=0.01,
            )
            vm_dir = root / "DocuSign"
            full = vm_dir / "full_0001"
            full.mkdir(parents=True)
            (full / "backup_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "success",
                        "target_vm": TARGET_VM_NAME,
                        "vm_info": base_vm_info(change_tracking_enabled=True),
                        "backup_chain": {"kind": "full", "sequence": 1, "run_name": "full_0001"},
                        "cbt_state": {"valid": True, "disks": [{"key": 2000, "change_id": "52 1"}]},
                        "files": [],
                    }
                ),
                encoding="utf-8",
            )

            slot = next_backup_chain_slot(cfg, base_vm_info(change_tracking_enabled=True), requested_mode="cbt")

        self.assertEqual(slot["kind"], "delta")
        self.assertEqual(slot["run_name"], "delta_0002")

    def test_interactive_chain_skips_stale_inprogress_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = VSphereConfig(
                host="example.local",
                user="backup",
                password="secret",
                output_dir=root,
                min_free_gb=0.01,
            )
            vm_dir = root / "DocuSign"
            full = vm_dir / "full_0001"
            stale = vm_dir / "delta_0006.inprogress"
            full.mkdir(parents=True)
            stale.mkdir(parents=True)
            (full / "backup_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "success",
                        "target_vm": TARGET_VM_NAME,
                        "vm_info": base_vm_info(),
                        "backup_chain": {"kind": "full", "sequence": 1, "run_name": "full_0001"},
                        "files": [],
                    }
                ),
                encoding="utf-8",
            )

            slot = next_backup_chain_slot(cfg, base_vm_info(), requested_mode="delta")

        self.assertEqual(slot["kind"], "delta")
        self.assertEqual(slot["run_name"], "delta_0007")

    def test_interactive_selector_defaults_to_cbt_mode(self):
        args = build_select_vm_backup_parser().parse_args([])
        self.assertEqual(args.backup_mode, "cbt")

    def test_selection_file_line_is_disabled_by_default(self):
        line = selection_file_line(base_vm_info(name="Example VM", moref="vm-44"))

        self.assertTrue(line.startswith("# "))
        self.assertIn("name='Example VM'", line)
        self.assertIn("moref=vm-44", line)

    def test_parse_selection_file_accepts_plain_names_and_key_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "selection.txt"
            path.write_text(
                "\n".join(
                    [
                        "# name=Ignored moref=vm-1",
                        "DocuSign",
                        "name='Other VM' moref='vm-456' instance_uuid='uuid-2'",
                    ]
                ),
                encoding="utf-8",
            )

            entries = parse_selection_file(path)

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["name"], "DocuSign")
        self.assertEqual(entries[1]["name"], "Other VM")
        self.assertEqual(entries[1]["moref"], "vm-456")

    def test_parse_selection_numbers_accepts_commas_and_ranges(self):
        self.assertEqual(parse_selection_numbers("3, 5-6 3", total=8), [3, 5, 6])

        with self.assertRaises(Exception):
            parse_selection_numbers("2-1", total=8)
        with self.assertRaises(Exception):
            parse_selection_numbers("9", total=8)

    def test_resolve_selection_entries_validates_identity_fields(self):
        records = [
            (object(), base_vm_info(name="DocuSign", moref="vm-123", instance_uuid="uuid-1")),
            (object(), base_vm_info(name="Other VM", moref="vm-456", instance_uuid="uuid-2")),
        ]

        resolved, errors = resolve_selection_entries(
            records,
            [{"line": 1, "name": "DocuSign", "moref": "vm-123", "instance_uuid": "uuid-1"}],
        )

        self.assertFalse(errors)
        self.assertEqual(resolved[0][1]["name"], "DocuSign")

    def test_resolve_selection_entries_blocks_renamed_uuid_match(self):
        records = [(object(), base_vm_info(name="DocuSign", moref="vm-123", instance_uuid="uuid-1"))]

        resolved, errors = resolve_selection_entries(
            records,
            [{"line": 1, "name": "OldName", "instance_uuid": "uuid-1"}],
        )

        self.assertEqual(resolved, [])
        self.assertTrue(errors)
        self.assertIn("identifier mismatch", errors[0])

    def test_resolve_selection_entries_blocks_duplicate_name_only_match(self):
        records = [
            (object(), base_vm_info(name="Duplicate", moref="vm-1", instance_uuid="uuid-1")),
            (object(), base_vm_info(name="Duplicate", moref="vm-2", instance_uuid="uuid-2")),
        ]

        resolved, errors = resolve_selection_entries(records, [{"line": 1, "name": "Duplicate"}])

        self.assertEqual(resolved, [])
        self.assertTrue(errors)
        self.assertIn("not unique", errors[0])

    def test_write_selection_file_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "selection.txt"
            records = [(object(), base_vm_info())]
            written = write_selection_file(path, records)

            self.assertEqual(written, path.resolve())
            self.assertIn("# name=DocuSign", path.read_text(encoding="utf-8"))
            with self.assertRaises(FileExistsError):
                write_selection_file(path, records)

    def test_write_selection_file_can_enable_numbered_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "selection.txt"
            records = [
                (object(), base_vm_info(name="First VM", moref="vm-1", instance_uuid="uuid-1")),
                (object(), base_vm_info(name="Second VM", moref="vm-2", instance_uuid="uuid-2")),
            ]

            write_selection_file(path, records, enabled_indexes=[2])
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if "moref=vm-" in line]

        self.assertTrue(lines[0].startswith("# "))
        self.assertFalse(lines[1].startswith("# "))
        self.assertIn("name='Second VM'", lines[1])

    def test_batch_free_space_check_uses_sequential_peak_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = VSphereConfig(
                host="example.local",
                user="backup",
                password="secret",
                output_dir=root,
                min_free_gb=0.01,
            )
            records = [
                (object(), base_vm_info(name="VM1", moref="vm-1", instance_uuid="uuid-1")),
                (object(), base_vm_info(name="VM2", moref="vm-2", instance_uuid="uuid-2")),
            ]

            plans = build_backup_plans(records, cfg, requested_backup_mode="delta", dynamic_free_space=True)
            check = batch_free_space_check(plans, cfg, dynamic_free_space=True)

        self.assertIsNotNone(check)
        self.assertEqual(check["name"], "batch_storage_free_space")
        self.assertIn("planned_vms=2", check["detail"])

    def test_split_passable_plans_separates_failed_vm_checks(self):
        passing_plan = {
            "vm_info": base_vm_info(name="PassingVM"),
            "chain_slot": {"run_name": "full_0001"},
            "checks": [{"name": "disk_count", "ok": True, "severity": "fatal", "detail": ""}],
        }
        blocked_plan = {
            "vm_info": base_vm_info(name="BlockedVM"),
            "chain_slot": {"run_name": "full_0001"},
            "checks": [
                {"name": "disk_count", "ok": True, "severity": "fatal", "detail": ""},
                {"name": "backup_storage_free_space", "ok": False, "severity": "fatal", "detail": ""},
            ],
        }

        passing, blocked = split_passable_plans([passing_plan, blocked_plan])

        self.assertEqual(passing, [passing_plan])
        self.assertEqual(blocked, [blocked_plan])
        self.assertEqual(fatal_check_names(blocked_plan["checks"]), ["backup_storage_free_space"])

    def test_skip_blocked_allows_automation_to_continue_with_passing_plans(self):
        passing_plan = {
            "vm_info": base_vm_info(name="PassingVM"),
            "chain_slot": {"run_name": "full_0001"},
            "checks": [{"name": "disk_count", "ok": True, "severity": "fatal", "detail": ""}],
        }
        blocked_plan = {
            "vm_info": base_vm_info(name="BlockedVM"),
            "chain_slot": {"run_name": "full_0001"},
            "checks": [{"name": "no_existing_snapshots", "ok": False, "severity": "fatal", "detail": ""}],
        }

        confirm_continue_with_passing_plans([passing_plan], [blocked_plan], assume_yes=True, skip_blocked=True)

    def test_require_existing_backup_blocks_automatic_initial_full(self):
        checks = validate_selected_vm_target(
            base_vm_info(name="NewVM"),
            base_config(),
            chain_slot={
                "vm_dir": Path("/tmp/NewVM"),
                "kind": "full",
                "run_name": "full_0001",
                "previous_dir": "",
                "existing_count": 0,
            },
            require_existing_backup=True,
        )

        self.assertIn("existing_backup_baseline", fatal_check_names(checks))

    def test_cbt_baseline_allows_disabled_cbt_for_auto_enable(self):
        with patch(
            "safe_vsphere_backup.vddk_backend_status",
            return_value={"available": True, "detail": "VDDK loaded", "library": "/tmp/lib.so"},
        ):
            checks = validate_selected_vm_target(
                base_vm_info(name="BaselineVM", change_tracking_enabled=False),
                base_config(),
                chain_slot={
                    "vm_dir": Path("/tmp/BaselineVM"),
                    "kind": "full",
                    "run_name": "full_0001",
                    "previous_dir": "",
                    "existing_count": 0,
                    "requested_backup_mode": "cbt",
                },
                transfer_mode="full",
            )

        self.assertNotIn("cbt_enabled", fatal_check_names(checks))

    def test_ensure_change_tracking_enabled_reconfigures_disabled_vm(self):
        calls = []

        class FakeVM:
            name = "BaselineVM"

            def ReconfigVM_Task(self, spec):
                calls.append(spec.changeTrackingEnabled)
                return SimpleNamespace()

        with patch("safe_vsphere_backup.wait_for_task", return_value=None), patch(
            "safe_vsphere_backup.collect_vm_info",
            return_value=base_vm_info(name="BaselineVM", change_tracking_enabled=True),
        ):
            result = ensure_change_tracking_enabled(
                FakeVM(),
                base_vm_info(name="BaselineVM", change_tracking_enabled=False),
                base_config(),
            )

        self.assertEqual(calls, [True])
        self.assertTrue(result["changed"])
        self.assertTrue(result["current_change_tracking_enabled"])

    def test_cbt_mode_blocks_without_valid_baseline(self):
        with patch(
            "safe_vsphere_backup.vddk_backend_status",
            return_value={"available": True, "detail": "VDDK loaded", "library": "/tmp/lib.so"},
        ):
            checks = validate_selected_vm_target(
                base_vm_info(name="DeltaVM", change_tracking_enabled=True),
                base_config(),
                chain_slot={
                    "vm_dir": Path("/tmp/DeltaVM"),
                    "kind": "delta",
                    "run_name": "delta_0002",
                    "previous_dir": "",
                    "existing_count": 1,
                },
                transfer_mode="cbt",
            )

        self.assertIn("cbt_baseline", fatal_check_names(checks))
        self.assertNotIn("cbt_transfer_implementation", fatal_check_names(checks))

    def test_cbt_state_from_snapshot_uses_snapshot_change_ids(self):
        disk = vim.vm.device.VirtualDisk()
        disk.key = 2000
        disk.capacityInKB = 1024
        disk.deviceInfo = vim.Description()
        disk.deviceInfo.label = "Hard disk 1"
        disk.backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
        disk.backing.fileName = "[datastore1] TestVM/TestVM.vmdk"
        disk.backing.changeId = "52 12345"

        snapshot = SimpleNamespace(
            _moId="snapshot-1",
            config=SimpleNamespace(
                changeTrackingEnabled=True,
                hardware=SimpleNamespace(device=[disk]),
            ),
        )

        state = cbt_state_from_snapshot(snapshot, base_vm_info(change_tracking_enabled=False))

        self.assertTrue(state["valid"])
        self.assertEqual(state["source"], "snapshot_config")
        self.assertEqual(state["snapshot_moref"], "snapshot-1")
        self.assertEqual(state["disks"][0]["change_id"], "52 12345")

    def test_restore_mac_preserve_requires_backup_mac(self):
        artifacts = BackupArtifacts(
            backup_dir=Path("/tmp"),
            manifest={},
            vm_info=base_vm_info(),
            descriptor_name="disk.vmdk",
            extent_names=[],
            ctk_names=[],
            upload_files=[],
            ovf={},
        )
        args = SimpleNamespace(
            mac_address_mode="preserve",
            preserve_mac=False,
            generated_mac=False,
            no_network=False,
            yes=True,
            dry_run=False,
        )

        self.assertEqual(source_mac_address(artifacts), "00:50:56:aa:bb:cc")
        self.assertEqual(resolve_mac_address_mode(args, artifacts), ("preserve", "00:50:56:aa:bb:cc"))

        missing = BackupArtifacts(
            backup_dir=Path("/tmp"),
            manifest={},
            vm_info={},
            descriptor_name="disk.vmdk",
            extent_names=[],
            ctk_names=[],
            upload_files=[],
            ovf={},
        )
        with self.assertRaises(Exception):
            resolve_mac_address_mode(args, missing)

    def test_missing_esxi_task_state_is_not_fatal(self):
        class MissingTask:
            @property
            def info(self):
                raise vmodl.fault.ManagedObjectNotFound(msg="task already deleted")

        self.assertIsNone(task_state_if_available(MissingTask(), "Copy virtual disk test"))

    def test_verify_rejects_failed_manifest_without_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "delta_0004.failed"
            backup_dir.mkdir()
            (backup_dir / "backup_manifest.json").write_text(
                json.dumps({"target_vm": TARGET_VM_NAME, "status": "failed", "files": []}),
                encoding="utf-8",
            )

            self.assertEqual(verify_backup_dir(backup_dir)["status"], "failed")
            self.assertEqual(verify_any_backup_dir(backup_dir)["status"], "failed")

    def test_verify_accepts_any_manifest_target_unless_expected_vm_is_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "OtherVM" / "full_0001"
            backup_dir.mkdir(parents=True)
            payload = b"ok"
            (backup_dir / "disk.vmdk").write_bytes(payload)
            (backup_dir / "backup_manifest.json").write_text(
                json.dumps(
                    {
                        "target_vm": "OtherVM",
                        "status": "success",
                        "files": [
                            {
                                "name": "disk.vmdk",
                                "bytes": len(payload),
                                "sha256": sha256_bytes(payload),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(verify_backup_dir(backup_dir)["status"], "ok")
            self.assertEqual(verify_backup_dir(backup_dir, expected_target="OtherVM")["status"], "ok")
            self.assertEqual(verify_backup_dir(backup_dir, expected_target="DifferentVM")["status"], "failed")


class DeltaStorageTests(unittest.TestCase):
    def write_backup_with_extent(self, root: Path, name: str, data: bytes) -> Path:
        backup_dir = root / name
        backup_dir.mkdir(parents=True)
        (backup_dir / "disk.vmdk").write_text(
            '# Disk DescriptorFile\nRW 1 SPARSE "disk-s001.vmdk"\n',
            encoding="utf-8",
        )
        (backup_dir / "disk-s001.vmdk").write_bytes(data)
        manifest = {
            "target_vm": TARGET_VM_NAME,
            "status": "success",
            "files": [
                {
                    "name": "disk.vmdk",
                    "bytes": (backup_dir / "disk.vmdk").stat().st_size,
                    "sha256": sha256_bytes((backup_dir / "disk.vmdk").read_bytes()),
                    "vmdk_role": "descriptor",
                },
                {
                    "name": "disk-s001.vmdk",
                    "bytes": len(data),
                    "sha256": sha256_bytes(data),
                    "vmdk_role": "extent",
                },
            ],
        }
        (backup_dir / "backup_manifest.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )
        return backup_dir

    def test_delta_pack_removed_original_still_verifies_and_materializes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = b"A" * 8 + b"B" * 8 + b"C" * 3
            backup_dir = self.write_backup_with_extent(root, "backup1", original)

            result = pack_backup_dir(backup_dir, remove_originals=True, chunk_size=8)

            self.assertEqual(result["packed_files"], 1)
            self.assertFalse((backup_dir / "disk-s001.vmdk").exists())
            self.assertTrue(manifest_path(backup_dir).exists())
            self.assertTrue((backup_dir / "README.txt").exists())
            self.assertEqual(verify_backup_dir(backup_dir)["status"], "ok")

            hydrated = materialize_delta_file(backup_dir, "disk-s001.vmdk")
            self.assertEqual(hydrated.read_bytes(), original)
            cleanup_hydrated_files(backup_dir)
            self.assertFalse(hydrated.exists())

    def test_delta_pack_reuses_existing_chunks_between_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = b"A" * 8 + b"B" * 8
            second = b"A" * 8 + b"C" * 8
            backup1 = self.write_backup_with_extent(root, "backup1", first)
            backup2 = self.write_backup_with_extent(root, "backup2", second)

            pack_backup_dir(backup1, remove_originals=True, chunk_size=8)
            result = pack_backup_dir(backup2, remove_originals=True, chunk_size=8)

            chunks = list(list_chunk_files(root / ".delta_store"))
            self.assertEqual(len(chunks), 3)
            self.assertEqual(result["reused_chunk_bytes"], 8)

    def test_cleanup_unreferenced_chunks_preserves_manifest_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = self.write_backup_with_extent(root, "backup1", b"A" * 8 + b"B" * 8)
            pack_backup_dir(backup_dir, remove_originals=True, chunk_size=8)
            orphan = root / ".delta_store" / "chunks" / "or" / "orphan.chunk"
            orphan.parent.mkdir(parents=True)
            orphan.write_bytes(b"orphan")

            result = cleanup_unreferenced_chunks(root)

            self.assertFalse(orphan.exists())
            self.assertEqual(result["removed_files"], 1)
            self.assertEqual(verify_backup_dir(backup_dir)["status"], "ok")

    def test_cleanup_unreferenced_chunks_removes_orphan_store_without_manifests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orphan = root / ".delta_store" / "chunks" / "aa" / ("a" * 64)
            orphan = orphan.with_suffix(".chunk")
            orphan.parent.mkdir(parents=True)
            orphan.write_bytes(b"orphan")

            result = cleanup_unreferenced_chunks(root)

            self.assertEqual(result["removed_files"], 1)
            self.assertFalse(orphan.exists())

    def test_delta_pack_reuses_full_0001_as_base_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vm_dir = root / "DocuSign"
            full = self.write_backup_with_extent(vm_dir, "full_0001", b"A" * 8 + b"B" * 8)
            delta = self.write_backup_with_extent(vm_dir, "delta_0002", b"A" * 8 + b"C" * 8)
            manifest = json.loads((delta / "backup_manifest.json").read_text(encoding="utf-8"))
            manifest["backup_chain"] = {
                "kind": "delta",
                "sequence": 2,
                "run_name": "delta_0002",
                "vm_dir": str(vm_dir),
            }
            (delta / "backup_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            result = pack_backup_dir(delta, remove_originals=True, chunk_size=8)
            delta_manifest = json.loads((delta / "delta_manifest.json").read_text(encoding="utf-8"))
            chunks = delta_manifest["files"][0]["chunks"]

            self.assertEqual(result["base_reused_chunk_bytes"], 8)
            self.assertEqual(result["new_chunk_bytes"], 8)
            self.assertEqual(chunks[0]["storage"], "base_file")
            self.assertEqual(verify_backup_dir(delta)["status"], "ok")


if __name__ == "__main__":
    unittest.main()
