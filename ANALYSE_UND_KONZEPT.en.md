# Analysis and concept

[German version](ANALYSE_UND_KONZEPT.md)

> **Developed by:** Alpein Software Swiss AG<br>
> **Programmed by:** [Samuel Werner (Cyberwerner)](https://github.com/Cyberwerner4444)<br>
> **Supported by:** AI<br>
> **Attention:** WIPCODING content

## Starting point

The `upstream_vsphere_backup_template` was adopted as a conservative VM backup
system: inventory, preflight, a temporary snapshot for running VMs, export,
manifest, verification, local delta/deduplication storage, and restore under a
new name.

## Adaptation for vSphere/vCenter

The new system is called `vSphere-API-Backup` and uses a new configuration
layer:

- `credentials.env` for login data.
- `VSPHERE_SERVER`, `VSPHERE_USER`, and `VSPHERE_PASSWORD` instead of `ESXI_*`.
- `VSPHERE_TARGET_VM` for the protected single-VM path.
- Optional hardware and UUID locks.

## API layers

1. vCenter REST API:
   - Login through `/api/session`.
   - Fallback for older vCenter versions through
     `/rest/com/vmware/cis/session`.
   - Inventory through `/api/vcenter/vm`, with fallback to
     `/rest/vcenter/vm`.
2. vSphere Managed Object API:
   - Create and remove snapshots.
   - `VirtualMachine.ExportVm()` for powered-off VMs.
   - `Snapshot.ExportSnapshot()` or the datastore-copy fallback for running
     VMs.
   - HttpNfcLease file transfer.
   - Restore, datastore upload, and disk import.

The Managed Object path remains necessary because complete OVF/VMDK exports are
not available through an equivalent pure REST endpoint in vSphere.

## Workflow

1. Load credentials from `credentials.env`.
2. Normalize the vSphere endpoint.
3. Open the Managed Object and REST sessions.
4. Read the inventory.
5. Run preflight checks.
6. Create the backup chain and staging directory.
7. Write OVF/metadata.
8. Create a temporary snapshot when the VM is powered on.
9. Download the export or use the datastore-copy fallback.
10. Remove the snapshot and temporary datastore files.
11. Write the manifest and finalize the staging directory.
12. Optionally pack large VMDK extents into local chunks.

## Security decisions

- No implicit backup of all VMs.
- The protected single-VM path requires an exact `--confirm-vm` confirmation.
- Interactive selection requires a deliberate operator choice.
- List runs use an explicit `vm_backup_selection.txt`.
- Existing snapshots can block backups.
- Running VMs are backed up through temporary snapshots.
- Restore creates new names and leaves network adapters disconnected by default.
- Failed runs remain visible as `.failed`.
