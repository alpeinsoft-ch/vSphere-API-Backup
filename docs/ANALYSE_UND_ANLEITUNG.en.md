# Analysis and guide: vSphere API Backup

[German version](ANALYSE_UND_ANLEITUNG.md)

> **Developed by:** Alpein Software Swiss AG<br>
> **Programmed by:** [Samuel Werner (Cyberwerner)](https://github.com/Cyberwerner4444)<br>
> **Supported by:** AI<br>
> **Attention:** WIPCODING content

Status: 2026-09-17 UTC

This document describes the current state of the project
`vSphere-API-Backup`, its technical structure, safe operation, and the GitLab
initial import.

## 1. Summary

The project is a conservative vSphere/vCenter backup system for virtual
machines. It uses vCenter REST for sessions and inventory, and the vSphere
Managed Object API for snapshots, export, datastore access, and restore.

The code is syntactically valid locally and the existing unit tests pass:

```bash
python3 -m py_compile *.py tests/test_safety.py
python3 -m unittest discover -s tests -v
```

Validated result on 2026-09-17: 71 tests passed.

Production runtime data does not belong in the GitLab repository:

- `credentials.env`
- `/var/backups/vsphere/` (configured backup filesystem)
- `logs/`
- `vendor/vddk/`
- VMware VDDK archive
- local VM selection files

## 2. Current local inventory

Project path:

```text
<PROJECT_ROOT>
```

Important files:

```text
backup_vsphere.py              CLI entry point for protected single-VM path
safe_vsphere_backup.py         Core vSphere session, backup, and verification logic
select_vm_backup.py            Interactive VM selection and list backups
vm_backup_selection.py         Generator for editable VM selection lists
inventory_discovery.py         Read-only inventory
restore_vm_backup.py           Restore under a new VM name
delta_storage.py               Local chunk/deduplication storage
vddk_cbt.py                    VDDK/CBT patch storage
start_*.sh                     Operational wrappers
run_weekly_vm_backup.sh        Automated list run with lock file
requirements.txt               Python dependencies
README.md                      German quick guide
README.en.md                   English quick guide
RUNBOOK.md                     German operations runbook
RUNBOOK.en.md                  English operations runbook
tests/test_safety.py           Unit tests for safety and restore logic
```

Runtime artifacts such as backups, logs, virtual environments, VDDK files,
credentials, and VM selection files are intentionally excluded from version
control.

## 3. Architecture

The system combines two vSphere access paths:

1. vCenter REST API
   - Login through `/api/session`.
   - Fallback through `/rest/com/vmware/cis/session`.
   - Inventory through `/api/vcenter/vm`.
   - Fallback through `/rest/vcenter/vm`.

2. vSphere Managed Object API through pyVmomi
   - VM objects and hardware details.
   - Create and remove snapshots.
   - HttpNfcLease for OVF/VMDK transfer.
   - Datastore file access.
   - VM restore and disk import.

This split is appropriate because VMware does not provide an equivalent pure
REST path for complete OVF/VMDK exports and restore operations.

## 4. Backup modes

### Full backup

A full backup creates a complete restore point:

```text
/var/backups/vsphere/<VM>/full_0001/
  backup_manifest.json
  *.ovf
  *.vmdk
  *-flat.vmdk or other extents
  *.nvram, if present
```

For powered-off VMs, `VirtualMachine.ExportVm()` is used. For running VMs, the
system creates a temporary snapshot and exports from that snapshot. The
snapshot is removed after completion.

### Local delta

`--backup-mode local-delta` first creates a normal export and then packs large
files into local chunk/deduplication storage. This saves disk space but does
not reduce the initial vSphere download.

Typical artifacts:

```text
/var/backups/vsphere/<VM>/delta_0002/
  backup_manifest.json
  delta_manifest.json
  README.txt
/var/backups/vsphere/<VM>/.delta_store/
  chunks/...
```

### CBT delta

`--backup-mode cbt` uses VMware Changed Block Tracking through
VDDK/VixDiskLib. After a valid CBT baseline with a `changeId`, only changed
sectors are stored as patch files.

Typical artifacts:

```text
/var/backups/vsphere/<VM>/delta_0003/
  backup_manifest.json
  cbt_manifest.json
  *.cbtpatch
```

The CBT path is the most efficient delta mode, but requires a working VDDK
installation and correct vSphere CBT state.

## 5. Security model

The system is deliberately conservative:

- No implicit “back up all VMs” operation.
- The protected single-VM path requires `--confirm-vm`.
- Interactive selection requires a deliberate choice.
- List runs use an explicit `vm_backup_selection.txt`.
- Existing snapshots can block backups.
- Running VMs are backed up through temporary snapshots.
- Restore creates a new VM and does not overwrite an existing VM.
- Restore leaves the new VM powered off by default.
- Restore does not connect the network adapter automatically by default.
- Failed runs remain visible as `.failed`.
- Incomplete runs remain visible as `.inprogress`.

## 6. Configuration

Create a local configuration file from the template:

```bash
cp credentials.example.env credentials.env
chmod 600 credentials.env
nano credentials.env
```

Required values:

```bash
VSPHERE_SERVER=vcsa.example.invalid
VSPHERE_PORT=443
VSPHERE_USER=backup-user@example.invalid
VSPHERE_PASSWORD=CHANGE_ME
VSPHERE_TARGET_VM=CHANGE_ME_VM_NAME
BACKUP_OUTPUT_DIR=/var/backups/vsphere
```

Recommendations:

- Never commit `credentials.env`.
- Use `VSPHERE_SSL_VERIFY=true` in production once the vCenter certificate is
  configured as trusted.
- Set hardware locks when the protected single-VM path should accept only a
  specific VM.
- Optionally set `EXPECTED_INSTANCE_UUID` after the first inventory check.

## 7. Prepare VDDK

VDDK is not committed to the repository. The archive must be available
separately because it is a VMware/Broadcom artifact.

Installation:

```bash
./install_vddk_local.sh ./VMware-vix-disklib-8.0.3-23950268.x86_64.tar.gz
```

Verification:

```bash
python3 - <<'PY'
import safe_vsphere_backup as core
print(core.vddk_backend_status()["detail"])
PY
```

The expected message confirms that `libvixDiskLib.so` was loaded and that
transport modes such as `file`, `nbdssl`, or `nbd` are visible.

## 8. Operation

Python environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

If `python3 -m venv .venv` fails on Debian/Ubuntu because `ensurepip` is
missing, install `python3-venv` or the venv package matching the Python
version first.

Inventory:

```bash
python3 inventory_discovery.py --show-all
```

Preflight for the configured target VM:

```bash
python3 backup_vsphere.py preflight
```

Protected single-VM backup:

```bash
python3 backup_vsphere.py backup --confirm-vm '<VSPHERE_TARGET_VM>'
```

Interactive VM selection:

```bash
./start_select_vm_backup.sh
```

Create a VM selection file:

```bash
./start_vm_liste.sh
```

Automated list run:

```bash
./run_weekly_vm_backup.sh
```

Optional Screen session:

```bash
./start_backup_screen.sh
```

## 9. Verification

An individual restore point can be verified:

```bash
python3 backup_vsphere.py verify /var/backups/vsphere/<VM>/<run>
```

Or through the selection wrapper:

```bash
./start_select_vm_backup.sh --verify /var/backups/vsphere/<VM>/<run>
```

For delta/CBT backups, verification also checks the required metadata and
references.

## 10. Restore

List restore points:

```bash
./start_restore_vm_backup.sh --list-backups
```

The normal restore is started exclusively with the short command. The
remaining values are requested interactively:

```bash
./start_restore_vm_backup.sh
```

Dry run:

```bash
./start_restore_vm_backup.sh \
  --backup-dir /var/backups/vsphere/<VM>/<run> \
  --new-name <new-vm-name> \
  --dry-run
```

Restore:

```bash
./start_restore_vm_backup.sh \
  --backup-dir /var/backups/vsphere/<VM>/<run> \
  --new-name <new-vm-name> \
  --yes
```

Important restore properties:

- Existing VM names are rejected.
- Existing datastore target directories are not overwritten.
- The NIC is disconnected by default.
- The VM is not powered on by default.
- `--connect-network` and `--power-on` must be set deliberately.

## 11. Maintenance

Local syntax and unit tests:

```bash
python3 -m py_compile *.py tests/test_safety.py
python3 -m unittest discover -s tests -v
```

Unreferenced delta chunks can be cleaned through the function in
`delta_storage.py`. Before automated cleanup, always run a current restore test
or at least `verify` for the affected chains.

Logs and backups are operational data. They are not versioned and must be
managed through backup retention, monitoring, or external log rotation.

## 12. GitLab initial import

Recommended project name:

```text
vSphere API Backup
```

Recommended slug:

```text
vsphere-api-backup
```

Remote:

```text
https://git2.securium.ch/infrastructure/backup.git
```

Check before the first commit:

```bash
git status --short
git check-ignore -v credentials.env backups logs vendor/vddk vm_backup_selection.txt
```

Initial import:

```bash
git init
git branch -M main
git add .
git status --short
git commit -m "Initial import of vSphere API backup system"
git remote add origin https://git2.securium.ch/infrastructure/backup.git
git push -u origin main
```

After cloning on another system:

```bash
git clone https://git2.securium.ch/infrastructure/backup.git
cd vsphere-api-backup
# Debian/Ubuntu minimal: install python3-venv first if ensurepip is missing.
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp credentials.example.env credentials.env
chmod 600 credentials.env
nano credentials.env
./install_vddk_local.sh /path/to/VMware-vix-disklib-*.tar.gz
python3 -m unittest discover -s tests -v
python3 backup_vsphere.py preflight
```

## 13. Risks and recommendations

- Keep live passwords only in `credentials.env` or environment variables.
- Rotate passwords that have already been shared or pasted into tickets/chats.
- Do not commit `logs/`; logs may contain internal VM names, hosts, and error
  messages.
- Do not commit `vm_backup_selection.txt`; it contains real VM IDs in a live
  environment.
- Do not commit VDDK; download and install it separately.
- Perform regular restore dry runs and at least occasional real restores.
- Recheck CBT backups carefully after vSphere or VDDK updates.
- Resolve existing snapshots before backup windows.
- For running VMs, deliberately decide whether crash consistency is sufficient
  or whether `LIVE_SNAPSHOT_QUIESCE=true` has been tested for the workload.

## 14. Limitation on shrinking VM disks

The backup project can back up and restore vSphere data. It does not shrink an
existing vSphere VMDK in place. A genuine reduction of virtual disk size
requires a separate migration/converter path to a smaller virtual disk or VM.
