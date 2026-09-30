# Runbook

[German version](RUNBOOK.md)

> **Developed by:** Alpein Software Swiss AG<br>
> **Programmed by:** [Samuel Werner (Cyberwerner)](https://github.com/Cyberwerner4444)<br>
> **Supported by:** AI<br>
> **Attention:** WIPCODING content

The standard backup start command is exclusively:

```bash
./start_select_vm_backup.sh
```

## 1. Enter credentials

```bash
cd /path/to/vsphere-api-backup
cp credentials.example.env credentials.env
chmod 600 credentials.env
nano credentials.env
```

Required values:

```bash
VSPHERE_SERVER=<vcenter-host-or-url>
VSPHERE_USER=<backup-user>
VSPHERE_PASSWORD=<password>
VSPHERE_TARGET_VM=<target-vm-for-protected-path>
BACKUP_OUTPUT_DIR=/var/backups/vsphere
```

## 2. Prepare dependencies

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

If `python3 -m venv .venv` fails on Debian/Ubuntu because `ensurepip` is
missing, install `python3-venv` or the venv package matching the Python
version first.

The starter scripts create the virtual environment automatically when needed.

Install VDDK for real VMware CBT deltas:

```bash
./install_vddk_local.sh ./VMware-vix-disklib-8.0.3-23950268.x86_64.tar.gz
python3 - <<'PY'
import safe_vsphere_backup as core
print(core.vddk_backend_status()["detail"])
PY
```

## 3. Check inventory

```bash
python3 inventory_discovery.py --show-all
```

To show only the configured target VM:

```bash
python3 inventory_discovery.py
```

## 4. Check the protected target VM

```bash
python3 backup_vsphere.py preflight
```

Start a backup only after all fatal checks report `PASS`:

```bash
python3 backup_vsphere.py backup --confirm-vm '<VSPHERE_TARGET_VM>'
```

## 5. Interactive VM selection

Choose an individual VM from the inventory:

```bash
./start_select_vm_backup.sh
```

The starter runs directly in the current terminal by default, so selections
and error messages remain visible. For intentionally detached runs, use
`./start_backup_screen.sh` or
`VSPHERE_USE_SCREEN=1 ./start_select_vm_backup.sh`.

If `vm_backup_selection.txt` exists, the starter first asks whether to use it.
Enter or `n` starts normal VM selection without the list; `j` starts the list
run. Both paths use CBT mode without additional parameters. During a list run,
VMs with a fatal preflight error are skipped so the remaining selected VMs can
still be backed up.

Create a selection file and then back up several deliberately marked VMs:

```bash
./start_vm_liste.sh
./start_select_vm_backup.sh
```

The file `vm_backup_selection.txt` contains disabled VM lines. Remove the
leading `# ` for the VMs that should be included.

## 6. Delta storage and CBT

The interactive delta path maintains one chain per VM:

```text
/var/backups/vsphere/<VM>/full_0001
/var/backups/vsphere/<VM>/delta_0002
/var/backups/vsphere/<VM>/delta_0003
```

`--backup-mode local-delta` performs local chunk deduplication after a normal
vSphere export. This saves local storage, but downloads the complete VMDK
first.

`--backup-mode cbt` requests real VMware Changed Block Tracking. It uses
VDDK/VixDiskLib, reads only the sectors reported as changed by vSphere, and
does not fall back to the old full download.

If backups already exist but the last backup does not contain a valid CBT
baseline with a `changeId`, `--backup-mode cbt` first plans a new `full_000N` as
the CBT baseline. If CBT is not yet enabled on the VM, this baseline run
enables `changeTrackingEnabled` before the snapshot. Further runs are created
as `delta_000N` with CBT patches.

Manual first CBT baseline run:

```bash
./start_select_vm_backup.sh
```

Automation after a baseline exists:

```bash
./start_select_vm_backup.sh --selection-file vm_backup_selection.txt --yes --skip-blocked --backup-mode cbt
```

Optionally, `--require-existing-backup` deliberately blocks new full baselines.
The default allows them so a new CBT baseline is automatically created after
backups have been deleted.

Manual delta pack of an existing backup:

```bash
python3 backup_vsphere.py delta-pack /var/backups/vsphere/<VM>/<run> --remove-originals
```

## 7. Verification

```bash
python3 backup_vsphere.py verify /var/backups/vsphere/<backup-directory>
./start_select_vm_backup.sh --verify /var/backups/vsphere/<VM>/<run>
```

## 8. Restore

Interactive:

```bash
./start_restore_vm_backup.sh
```

This short command is the normal, user-friendly path. The script then asks for
the VM/restore point, new VM name, target datacenter, datastore, resource pool,
network, NVRAM, and power-on behavior. Confirmation of the new VM name remains
enabled.

Non-interactive:

```bash
./start_restore_vm_backup.sh \
  --backup-dir /var/backups/vsphere/<VM>/<run> \
  --new-name <new-vm-name> \
  --yes
```

Dry run:

```bash
./start_restore_vm_backup.sh \
  --backup-dir /var/backups/vsphere/<VM>/<run> \
  --new-name <new-vm-name> \
  --dry-run
```

Restore automatically creates its temporary workspace under `.restore-tmp` on
the backup disk. This prevents a small `/tmp` filesystem from being used by
mistake to materialize large CBT disk chains. Set another location if needed:

```bash
VSPHERE_RESTORE_TMPDIR=/path/with/enough/free/space ./start_restore_vm_backup.sh
```

After the start message, restore regularly displays VMDK materialization
progress in GiB and percent in the terminal and in `logs/vsphere_backup.log`.

## 9. Recurring list run

`run_weekly_vm_backup.sh` uses `vm_backup_selection.txt`, starts the list path
in CBT mode, and writes to `logs/weekly_vm_backup.log`.

```bash
./run_weekly_vm_backup.sh
```

## 10. Error handling

- `credentials.env` is missing or empty: enter the credentials.
- Certificate error: import the vCenter certificate and use
  `VSPHERE_SSL_VERIFY=true`, or deliberately set it to `false` for a lab.
- `Target VM not found`: adjust `VSPHERE_TARGET_VM` to exactly match the
  vCenter name.
- Existing snapshots: resolve them deliberately, then run preflight again.
- Insufficient local storage: point `BACKUP_OUTPUT_DIR` to a larger target or
  clean up old backups/chunks.
