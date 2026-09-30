# Analyse und Konzept

[English version](ANALYSE_UND_KONZEPT.en.md)

> **Entwickelt von:** Alpein Software Swiss AG<br>
> **Programmiert von:** [Samuel Werner (Cyberwerner)](https://github.com/Cyberwerner4444)<br>
> **Unterstützt von:** KI<br>
> **Achtung:** WIPCODING-Inhalt

## Ausgangspunkt

Die Vorlage `upstream_vsphere_backup_template` wurde als konservatives VM-Backup-System
uebernommen: Inventory, Preflight, temporaerer Snapshot fuer laufende VMs,
Export, Manifest, Verify, lokaler Delta-/Dedupe-Speicher und Restore unter
neuem Namen.

## Anpassung fuer vSphere/vCenter

Das neue System heisst `vSphere-API-Backup` und nutzt eine neue
Konfigurationsschicht:

- `credentials.env` fuer Login-Daten.
- `VSPHERE_SERVER`, `VSPHERE_USER`, `VSPHERE_PASSWORD` statt `ESXI_*`.
- `VSPHERE_TARGET_VM` fuer den geschuetzten Einzel-VM-Pfad.
- optionale Hardware- und UUID-Locks.

## API-Schichten

1. vCenter REST API:
   - Login ueber `/api/session`.
   - Fallback fuer aeltere vCenter-Versionen ueber
     `/rest/com/vmware/cis/session`.
   - Inventory ueber `/api/vcenter/vm`, Fallback `/rest/vcenter/vm`.
2. vSphere Managed-Object API:
   - Snapshot-Erstellung und -Entfernung.
   - `VirtualMachine.ExportVm()` fuer ausgeschaltete VMs.
   - `Snapshot.ExportSnapshot()` oder Datastore-Copy-Fallback fuer laufende VMs.
   - HttpNfcLease-Dateitransfer.
   - Restore, Datastore-Upload und Disk-Import.

Der Managed-Object-Pfad bleibt notwendig, weil komplette OVF/VMDK-Exports in
vSphere nicht als gleichwertiger reiner REST-Endpunkt verfuegbar sind.

## Ablauf

1. Zugangsdaten aus `credentials.env` laden.
2. vSphere-Endpoint normalisieren.
3. Managed-Object-Session und REST-Session oeffnen.
4. Inventory lesen.
5. Preflight pruefen.
6. Backup-Kette und Staging-Ordner anlegen.
7. OVF/Metadaten schreiben.
8. Bei `poweredOn` temporaeren Snapshot erstellen.
9. Export herunterladen oder Datastore-Copy-Fallback nutzen.
10. Snapshot und temporaere Datastore-Dateien entfernen.
11. Manifest schreiben und Staging-Ordner finalisieren.
12. Optional grosse VMDK-Extents in lokale Chunks packen.

## Sicherheitsentscheidungen

- Keine implizite Sicherung aller VMs.
- Der geschuetzte Einzelpfad benoetigt eine exakte `--confirm-vm`-Bestaetigung.
- Die interaktive Auswahl verlangt eine bewusste Operator-Auswahl.
- Restore erzeugt neue Namen und laesst Netzwerkkarten standardmaessig getrennt.
- Fehlgeschlagene Laeufe bleiben als `.failed` nachvollziehbar.
