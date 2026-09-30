# vSphere API Backup

[Deutsche Version](README.md)

> **Developed by:** Alpein Software Swiss AG<br>
> **Programmed by:** [Samuel Werner (Cyberwerner)](https://github.com/Cyberwerner4444)<br>
> **Supported by:** AI<br>
> **Attention:** WIPCODING content

The standard start command remains exactly `./start_select_vm_backup.sh`.

This directory contains a new, separate vSphere/vCenter backup system.
It follows the workflow and security model of the
`upstream_vsphere_backup_template`, but is adapted for vSphere/vCenter
configuration and the vCenter REST inventory.

A complete technical inventory, operations guide, and GitHub initial-import
guide are available in the following documents:

- [Analysis and guide in English](docs/ANALYSE_UND_ANLEITUNG.en.md)
- [Analyse und Anleitung auf Deutsch](docs/ANALYSE_UND_ANLEITUNG.md)
- [Runbook in English](RUNBOOK.en.md)
- [Runbook auf Deutsch](RUNBOOK.md)

## API access

- Login/session handling and inventory use the vCenter REST API:
  `/api/session` and `/api/vcenter/vm`.
- If an older vCenter REST variant responds, the system automatically tries
  `/rest/com/vmware/cis/session` and `/rest/vcenter/vm`.
- VM export, snapshot export, datastore file access, and restore use the
  vSphere Managed Object API/HttpNfcLease because VMware does not provide an
  equivalent pure REST endpoint for complete OVF/VMDK exports.

Official reference: Broadcom Developer Portal, vSphere Automation API.

## Files

- `credentials.env`: local vCenter/vSphere login file.
- `credentials.example.env` and `config.example.env`: templates.
- `safe_vsphere_backup.py`: core logic, vSphere session, preflight, backup,
  verification, and delta pack.
- `backup_vsphere.py`: CLI for protected target-VM backups.
- `inventory_discovery.py`: read-only inventory.
- `vm_backup_selection.py`: generator for editable VM selection lists.
- `select_vm_backup.py`: interactive single-VM and list-backup entry point.
- `restore_vm_backup.py`: restore under a new VM name.
- `delta_storage.py`: local chunk/deduplication storage for large VMDK files.
- `vddk_cbt.py`: VMware VDDK/CBT patch storage for fast, real delta backups.

## Setup

```bash
git clone https://github.com/alpeinsoft-ch/vSphere-API-Backup.git
cd vsphere-api-backup
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp credentials.example.env credentials.env
chmod 600 credentials.env
```

If `python3 -m venv .venv` fails on Debian/Ubuntu with `ensurepip is not
available`, the `python3-venv` package is missing. Use the package matching
your Python version, for example `python3.12-venv`.

Edit `credentials.env` afterwards:

```bash
VSPHERE_SERVER=vcsa.example.invalid
VSPHERE_USER=backup-user@example.invalid
VSPHERE_PASSWORD=CHANGE_ME
VSPHERE_TARGET_VM=<protected-vm>
BACKUP_OUTPUT_DIR=/var/backups/vsphere
```

`VSPHERE_SERVER` may be entered as a hostname, `host:port`, or
`https://host:port`. If the vCenter certificate is trusted, set
`VSPHERE_SSL_VERIFY=true`.

## VDDK for real CBT deltas

The `VMware-vix-disklib-8.0.3-23950268.x86_64.tar.gz` archive can be installed
locally into the project:

```bash
./install_vddk_local.sh ./VMware-vix-disklib-8.0.3-23950268.x86_64.tar.gz
python3 - <<'PY'
import safe_vsphere_backup as core
print(core.vddk_backend_status()["detail"])
PY
```

The installer places VDDK under `vendor/vddk`. The status check actually
initializes VixDiskLib and reports the available transport modes.

## Workflow

Inventory:

```bash
python3 inventory_discovery.py --show-all
```

Preflight for the VM configured in `VSPHERE_TARGET_VM`:

```bash
python3 backup_vsphere.py preflight
```

Backup of the protected target VM:

```bash
python3 backup_vsphere.py backup --confirm-vm '<protected-vm>'
```

Explicit backup mode:

```bash
python3 backup_vsphere.py backup --confirm-vm '<protected-vm>' --backup-mode full
python3 backup_vsphere.py backup --confirm-vm '<protected-vm>' --backup-mode delta
```

Note: with `--backup-mode delta`, the protected single-VM path
`backup_vsphere.py` continues to use local delta storage after a normal
vSphere export.

The interactive and list paths can use fast, real VMware CBT deltas:

```bash
./start_select_vm_backup.sh
```

Without parameters, the wrapper starts in CBT mode. If
`vm_backup_selection.txt` exists, it first asks whether that list should be
used. `j` starts the list run; Enter or `n` starts the normal interactive VM
selection without a list. During a list run, VMs with a fatal preflight error
are skipped so the remaining selected VMs can still be backed up.

CBT mode uses VDDK/VixDiskLib and `QueryChangedDiskAreas`, reads only changed
sectors, and does not fall back to a full download. If backups already exist
but there is no valid CBT baseline with a `changeId`, a new `full_000N` is
automatically planned as the CBT baseline. If CBT is not yet enabled on the
VM, the baseline run enables `changeTrackingEnabled` before the snapshot.
Later runs are saved as `delta_000N` with CBT patches and download only the
changes reported by vSphere.

The old local mode remains explicitly available:

```bash
./start_select_vm_backup.sh --backup-mode local-delta
```

Interactive VM selection:

```bash
./start_select_vm_backup.sh
```

Screen remains available when a run should intentionally be detached:

```bash
./start_backup_screen.sh
# or:
VSPHERE_USE_SCREEN=1 ./start_select_vm_backup.sh
```

Generate a selection file from the current vSphere inventory:

```bash
./start_vm_liste.sh
```

Restore:

```bash
./start_restore_vm_backup.sh
```

## Security model

- No blind “back up all VMs” operation.
- A protected single-VM backup requires `--confirm-vm` to exactly match the
  configured `VSPHERE_TARGET_VM`.
- CPU, RAM, disk count, disk size, and instance UUID can optionally be fixed
  in `credentials.env`.
- Running VMs are backed up through temporary snapshots; snapshots are
  removed after completion.
- Existing snapshots block the protected single-VM path.
- Restore creates a new VM, powered off by default and without a connected
  network adapter.

## Verification

```bash
python3 -m py_compile *.py tests/test_safety.py
python3 -m unittest discover -s tests -v
```
