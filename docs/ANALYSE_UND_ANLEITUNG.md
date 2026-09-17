# Analyse und Anleitung: vSphere API Backup

Stand: 2026-08-31 UTC

Dieses Dokument beschreibt den aktuellen Zustand des Projekts
`vSphere-API-Bakup`, den technischen Aufbau, den sicheren Betrieb und den
GitLab-Erstimport.

## 1. Kurzfazit

Das Projekt ist ein konservatives vSphere/vCenter-Backup-System fuer virtuelle
Maschinen. Es nutzt vCenter REST fuer Session/Inventory und die vSphere
Managed-Object API fuer Snapshot, Export, Datastore-Zugriff und Restore.

Der Code ist lokal syntaktisch gueltig und die vorhandenen Unit-Tests laufen
erfolgreich:

```bash
python3 -m py_compile *.py tests/test_safety.py
python3 -m unittest discover -s tests -v
```

Ergebnis am 2026-08-31: 69 Tests bestanden.

Produktive Runtime-Daten gehoeren nicht ins GitLab-Repository:

- `credentials.env`
- `/srv/samba/Backup-Alpein/Backup/` (separates 5-TB-Backup-Dateisystem)
- `logs/`
- `vendor/vddk/`
- VMware VDDK Archive
- lokale VM-Auswahldateien

## 2. Aktueller lokaler Bestand

Projektpfad:

```text
/srv/shares/Backup-f/Backup-system/vSphere-API-Bakup
```

Wichtige Dateien:

```text
backup_vsphere.py              CLI-Einstieg fuer geschuetzten Einzel-VM-Pfad
safe_vsphere_backup.py         Kernlogik fuer vSphere Session, Backup, Verify
select_vm_backup.py            Interaktive VM-Auswahl und Listen-Backups
vm_backup_selection.py         Generator fuer editierbare VM-Auswahlliste
inventory_discovery.py         Read-only Inventory
restore_vm_backup.py           Restore unter neuem VM-Namen
delta_storage.py               Lokaler Chunk-/Dedupe-Speicher
vddk_cbt.py                    VDDK/CBT-Patchspeicher
start_*.sh                     Wrapper fuer Betrieb
run_weekly_vm_backup.sh        Automatisierter Listenlauf mit Lockfile
requirements.txt               Python-Abhaengigkeiten
README.md                      Kurzanleitung
RUNBOOK.md                     Betriebs-Runbook
tests/test_safety.py           Unit-Tests fuer Sicherheits-/Restore-Logik
```

Aktuelle Groessen bei der Analyse:

```text
Projekt gesamt:       ca. 173 MB
backups/:             4 KB, aktuell keine Restore-Punkte sichtbar
logs/:                ca. 328 KB, historische Laufzeitlogs
vendor/vddk/:         ca. 132 MB, lokale VDDK-Installation
VDDK-Archiv:          ca. 40 MB
```

Hinweis: In einem frueheren Lauf war `backups/` mit ca. 147 GB belegt. Zum
Analysezeitpunkt ist `backups/` leer bzw. enthaelt keine sichtbaren
Backup-Laeufe.

## 3. Architektur

Das System kombiniert zwei vSphere-Zugriffswege:

1. vCenter REST API
   - Login ueber `/api/session`
   - Fallback ueber `/rest/com/vmware/cis/session`
   - Inventory ueber `/api/vcenter/vm`
   - Fallback ueber `/rest/vcenter/vm`

2. vSphere Managed-Object API ueber pyVmomi
   - VM-Objekte und Hardwaredetails
   - Snapshot erstellen/entfernen
   - HttpNfcLease fuer OVF/VMDK-Transfer
   - Datastore-Dateizugriff
   - VM-Restore und Disk-Import

Diese Aufteilung ist sinnvoll, weil VMware fuer vollstaendige OVF/VMDK-Exports
und Restore-Operationen keinen gleichwertigen reinen REST-Pfad bereitstellt.

## 4. Backup-Modi

### Full Backup

Ein Full Backup erzeugt einen vollstaendigen Wiederherstellungspunkt:

```text
/srv/samba/Backup-Alpein/Backup/<VM>/full_0001/
  backup_manifest.json
  *.ovf
  *.vmdk
  *-flat.vmdk oder andere Extents
  *.nvram, falls vorhanden
```

Bei ausgeschalteten VMs wird `VirtualMachine.ExportVm()` genutzt. Bei laufenden
VMs erstellt das System einen temporaeren Snapshot und exportiert aus diesem
Snapshot. Nach Abschluss wird der Snapshot wieder entfernt.

### Local Delta

`--backup-mode local-delta` erstellt erst einen normalen Export und packt grosse
Dateien danach in einen lokalen Chunk-/Dedupe-Speicher. Dieser Modus spart
lokalen Plattenplatz, reduziert aber nicht den initialen vSphere-Download.

Typische Artefakte:

```text
/srv/samba/Backup-Alpein/Backup/<VM>/delta_0002/
  backup_manifest.json
  delta_manifest.json
  README.txt
/srv/samba/Backup-Alpein/Backup/<VM>/.delta_store/
  chunks/...
```

### CBT Delta

`--backup-mode cbt` nutzt VMware Changed Block Tracking ueber VDDK/VixDiskLib.
Nach einer gueltigen CBT-Baseline mit `changeId` werden nur geaenderte Sektoren
als Patch-Dateien gespeichert.

Typische Artefakte:

```text
/srv/samba/Backup-Alpein/Backup/<VM>/delta_0003/
  backup_manifest.json
  cbt_manifest.json
  *.cbtpatch
```

Der CBT-Pfad ist der effizienteste Delta-Modus, setzt aber eine funktionierende
VDDK-Installation und korrekte vSphere CBT-Zustaende voraus.

## 5. Sicherheitsmodell

Das System ist absichtlich konservativ gebaut:

- Kein implizites "alle VMs sichern".
- Der geschuetzte Einzelpfad verlangt `--confirm-vm`.
- Die interaktive Auswahl verlangt eine bewusste Auswahl.
- Listenlaeufe nutzen eine explizite `vm_backup_selection.txt`.
- Bestehende Snapshots koennen Backups blockieren.
- Laufende VMs werden ueber temporaere Snapshots gesichert.
- Restore erzeugt eine neue VM und ueberschreibt keine vorhandene VM.
- Restore startet die neue VM standardmaessig ausgeschaltet.
- Restore verbindet die Netzwerkkarte standardmaessig nicht automatisch.
- Fehlgeschlagene Laeufe bleiben als `.failed` nachvollziehbar.
- Unfertige Laeufe bleiben als `.inprogress` sichtbar.

## 6. Konfiguration

Eine lokale Konfigurationsdatei wird aus der Vorlage erzeugt:

```bash
cp credentials.example.env credentials.env
chmod 600 credentials.env
nano credentials.env
```

Pflichtwerte:

```bash
VSPHERE_SERVER=vcsa.example.local
VSPHERE_PORT=443
VSPHERE_USER=backup-user@example.local
VSPHERE_PASSWORD=CHANGE_ME
VSPHERE_TARGET_VM=CHANGE_ME_VM_NAME
BACKUP_OUTPUT_DIR=/srv/samba/Backup-Alpein/Backup
```

Empfehlungen:

- `credentials.env` niemals committen.
- Fuer Produktion `VSPHERE_SSL_VERIFY=true` nutzen, sobald das vCenter-
  Zertifikat vertrauenswuerdig eingerichtet ist.
- Hardware-Locks setzen, wenn der geschuetzte Einzelpfad nur eine bestimmte VM
  akzeptieren soll.
- Nach dem ersten Inventory optional `EXPECTED_INSTANCE_UUID` setzen.

## 7. VDDK vorbereiten

VDDK wird nicht ins Repository committed. Das Archiv muss separat bereitliegen,
weil es ein VMware/Broadcom-Artefakt ist.

Installation:

```bash
./install_vddk_local.sh ./VMware-vix-disklib-8.0.3-23950268.x86_64.tar.gz
```

Pruefung:

```bash
python3 - <<'PY'
import safe_vsphere_backup as core
print(core.vddk_backend_status()["detail"])
PY
```

Erwartet wird eine Meldung, dass `libvixDiskLib.so` geladen werden konnte und
Transportmodi wie `file`, `nbdssl` oder `nbd` sichtbar sind.

## 8. Betrieb

Python-Umgebung:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Falls `python3 -m venv .venv` auf Debian/Ubuntu wegen fehlendem `ensurepip`
abbricht, muss vorher `python3-venv` bzw. das zur Python-Version passende
venv-Paket installiert werden.

Inventory:

```bash
python3 inventory_discovery.py --show-all
```

Preflight der konfigurierten Ziel-VM:

```bash
python3 backup_vsphere.py preflight
```

Geschuetztes Einzel-VM-Backup:

```bash
python3 backup_vsphere.py backup --confirm-vm '<VSPHERE_TARGET_VM>'
```

Interaktive VM-Auswahl:

```bash
./start_select_vm_backup.sh
```

VM-Auswahldatei erzeugen:

```bash
./start_vm_liste.sh
```

Automatisierter Listenlauf:

```bash
./run_weekly_vm_backup.sh
```

Optional mit Screen:

```bash
./start_backup_screen.sh
```

## 9. Verify

Ein einzelner Wiederherstellungspunkt kann geprueft werden:

```bash
python3 backup_vsphere.py verify /srv/samba/Backup-Alpein/Backup/<VM>/<lauf>
```

Oder ueber den Auswahl-Wrapper:

```bash
./start_select_vm_backup.sh --verify /srv/samba/Backup-Alpein/Backup/<VM>/<lauf>
```

Bei Delta-/CBT-Backups prueft Verify auch die jeweils benoetigten Metadaten und
Referenzen.

## 10. Restore

Restore-Punkte anzeigen:

```bash
./start_restore_vm_backup.sh --list-backups
```

Der normale Restore wird ausschließlich mit dem kurzen Aufruf gestartet. Die
restlichen Angaben werden interaktiv abgefragt:

```bash
./start_restore_vm_backup.sh
```

Dry-Run:

```bash
./start_restore_vm_backup.sh \
  --backup-dir /srv/samba/Backup-Alpein/Backup/<VM>/<lauf> \
  --new-name <neuer-vm-name> \
  --dry-run
```

Restore:

```bash
./start_restore_vm_backup.sh \
  --backup-dir /srv/samba/Backup-Alpein/Backup/<VM>/<lauf> \
  --new-name <neuer-vm-name> \
  --yes
```

Wichtige Restore-Eigenschaften:

- vorhandene VM-Namen werden abgelehnt
- vorhandene Datastore-Zielordner werden nicht ueberschrieben
- NIC ist standardmaessig nicht verbunden
- VM wird standardmaessig nicht eingeschaltet
- `--connect-network` und `--power-on` muessen bewusst gesetzt werden

## 11. Wartung

Lokale Syntax-/Unit-Tests:

```bash
python3 -m py_compile *.py tests/test_safety.py
python3 -m unittest discover -s tests -v
```

Nicht referenzierte Delta-Chunks koennen ueber die Funktion in
`delta_storage.py` bereinigt werden. Vor einer automatisierten Bereinigung
sollte immer ein aktueller Restore-Test oder mindestens `verify` fuer die
betroffenen Ketten laufen.

Logs und Backups sind Betriebsdaten. Sie werden nicht versioniert und muessen
ueber Backup-Retention, Monitoring oder externe Logrotation verwaltet werden.

## 12. GitLab-Erstimport

Empfohlener Projektname:

```text
vSphere API Backup
```

Empfohlener Slug:

```text
vsphere-api-backup
```

Remote:

```text
https://git.hostwerk.ch/AlpeinSW/vsphere-api-backup.git
```

Vor dem ersten Commit pruefen:

```bash
git status --short
git check-ignore -v credentials.env backups logs vendor/vddk vm_backup_selection.txt
```

Erstimport:

```bash
git init
git branch -M main
git add .
git status --short
git commit -m "Initial import of vSphere API backup system"
git remote add origin https://git.hostwerk.ch/AlpeinSW/vsphere-api-backup.git
git push -u origin main
```

Nach dem Clone auf einem anderen System:

```bash
git clone https://git.hostwerk.ch/AlpeinSW/vsphere-api-backup.git
cd vsphere-api-backup
# Debian/Ubuntu minimal: bei fehlendem ensurepip vorher python3-venv installieren.
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp credentials.example.env credentials.env
chmod 600 credentials.env
nano credentials.env
./install_vddk_local.sh /pfad/zum/VMware-vix-disklib-*.tar.gz
python3 -m unittest discover -s tests -v
python3 backup_vsphere.py preflight
```

## 13. Risiken und Empfehlungen

- Live-Passwoerter nur in `credentials.env` oder ueber Umgebungsvariablen
  verwenden.
- Bereits geteilte oder in Tickets/Chats eingefuegte Passwoerter rotieren.
- `logs/` nicht committen; Logs koennen interne VM-Namen, Hosts und Fehlertexte
  enthalten.
- `vm_backup_selection.txt` nicht committen; sie enthaelt echte VM-IDs.
- VDDK nicht committen; separat herunterladen/installieren.
- Regelmaessig Restore-Dry-Runs und mindestens stichprobenartige echte Restores
  durchfuehren.
- CBT-Backups nach vSphere-/VDDK-Updates besonders pruefen.
- Bestehende Snapshots vor Backup-Fenstern klaeren.
- Fuer laufende VMs bewusst entscheiden, ob crash-consistent reicht oder
  `LIVE_SNAPSHOT_QUIESCE=true` fachlich getestet wurde.

## 14. Einschraenkung zur VM-Festplattenverkleinerung

Das Backup-Projekt kann vSphere-Daten sichern und wiederherstellen. Es
verkleinert keine bestehende vSphere-VMDK in-place. Fuer eine echte Reduktion
der virtuellen Festplattengroesse ist ein separater Migrations-/Converter-Weg
auf eine kleinere virtuelle Disk bzw. VM erforderlich.
