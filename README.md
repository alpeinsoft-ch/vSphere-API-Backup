# vSphere API Backup

Vor jedem Backup zuerst lesen: [`VOR_BACKUP_LESEN.md`](../VOR_BACKUP_LESEN.md).
Der normale Startbefehl bleibt exakt `./start_select_vm_backup.sh`.

Dieses Verzeichnis enthaelt ein neues, getrenntes vSphere/vCenter-Backup-System.
Es uebernimmt Ablauf und Sicherheitsmodell der Vorlage `docsign_esxi_api_backup`,
ist aber auf vSphere/vCenter-Konfiguration und vCenter-REST-Inventory
umgestellt.

Eine vollstaendige technische Bestandsaufnahme, Betriebsanleitung und
GitLab-Erstimport-Anleitung liegt unter
[`docs/ANALYSE_UND_ANLEITUNG.md`](docs/ANALYSE_UND_ANLEITUNG.md).

## API-Ansteuerung

- Login/Session und Inventory laufen ueber die vCenter REST API:
  `/api/session` und `/api/vcenter/vm`.
- Falls eine aeltere vCenter-REST-Variante antwortet, wird automatisch
  `/rest/com/vmware/cis/session` und `/rest/vcenter/vm` versucht.
- VM-Export, Snapshot-Export, Datastore-Dateizugriff und Restore verwenden die
  vSphere Managed-Object API/HttpNfcLease, weil VMware fuer komplette OVF/VMDK-
  Exporte keinen gleichwertigen reinen REST-Endpunkt bereitstellt.

Offizielle Referenz: Broadcom Developer Portal, vSphere Automation API.

## Dateien

- `credentials.env`: lokale Login-Datei fuer vCenter/vSphere.
- `credentials.example.env` und `config.example.env`: Vorlagen.
- `safe_vsphere_backup.py`: Kernlogik, vSphere-Session, Preflight, Backup,
  Verify und Delta-Pack.
- `backup_vsphere.py`: CLI fuer geschuetztes Ziel-VM-Backup.
- `inventory_discovery.py`: Read-only Inventory.
- `vm_backup_selection.py`: Generator fuer editierbare VM-Auswahllisten.
- `select_vm_backup.py`: interaktiver Ein-VM- und Listen-Backup-Einstieg.
- `restore_vm_backup.py`: Restore unter neuem VM-Namen.
- `delta_storage.py`: lokaler Chunk-/Dedupe-Speicher fuer grosse VMDK-Dateien.
- `vddk_cbt.py`: VMware-VDDK/CBT-Patchspeicher fuer schnelle echte Delta-Backups.

## Einrichtung

```bash
git clone https://git.hostwerk.ch/AlpeinSW/vsphere-api-backup.git
cd vsphere-api-backup
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp credentials.example.env credentials.env
chmod 600 credentials.env
```

Falls `python3 -m venv .venv` auf Debian/Ubuntu mit `ensurepip is not
available` abbricht, fehlt das Paket `python3-venv` bzw. passend zur
Python-Version z. B. `python3.12-venv`.

Danach `credentials.env` bearbeiten:

```bash
VSPHERE_SERVER=vcsa.example.local
VSPHERE_USER=backup-user@example.local
VSPHERE_PASSWORD=CHANGE_ME
VSPHERE_TARGET_VM=<geschuetzte-vm>
BACKUP_OUTPUT_DIR=/srv/samba/Backup-Alpein/Backup
```

`VSPHERE_SERVER` darf als Hostname, `host:port` oder `https://host:port`
eingetragen werden. Wenn das vCenter-Zertifikat vertrauenswuerdig ist,
`VSPHERE_SSL_VERIFY=true` setzen.

## VDDK fuer echte CBT-Deltas

Das Archiv `VMware-vix-disklib-8.0.3-23950268.x86_64.tar.gz` kann lokal in
das Projekt installiert werden:

```bash
./install_vddk_local.sh ./VMware-vix-disklib-8.0.3-23950268.x86_64.tar.gz
python3 - <<'PY'
import safe_vsphere_backup as core
print(core.vddk_backend_status()["detail"])
PY
```

Der Installer legt VDDK unter `vendor/vddk` ab. Der Statuscheck initialisiert
VixDiskLib wirklich und meldet die verfuegbaren Transportmodi.

## Ablauf

Inventory:

```bash
python3 inventory_discovery.py --show-all
```

Preflight fuer die in `VSPHERE_TARGET_VM` konfigurierte VM:

```bash
python3 backup_vsphere.py preflight
```

Backup der geschuetzten Ziel-VM:

```bash
python3 backup_vsphere.py backup --confirm-vm '<geschuetzte-vm>'
```

Explizite Backup-Art:

```bash
python3 backup_vsphere.py backup --confirm-vm '<geschuetzte-vm>' --backup-mode full
python3 backup_vsphere.py backup --confirm-vm '<geschuetzte-vm>' --backup-mode delta
```

Hinweis: Der geschuetzte Einzelpfad `backup_vsphere.py` verwendet bei
`--backup-mode delta` weiterhin den lokalen Delta-Speicher nach einem normalen
vSphere-Export.

Der interaktive und der Listenpfad koennen echte schnelle VMware-CBT-Deltas
verwenden:

```bash
./start_select_vm_backup.sh
```

Ohne Parameter startet der Wrapper im CBT-Modus. Wenn
`vm_backup_selection.txt` existiert, fragt er zuerst, ob diese Liste genutzt
werden soll. `j` startet den Listenlauf, Enter oder `n` startet die normale
interaktive VM-Auswahl ohne Liste. Im Listenlauf werden VMs mit fatalem
Preflight-Fehler uebersprungen, damit die restlichen ausgewaehlten VMs trotzdem
gesichert werden.

Der CBT-Modus nutzt VDDK/VixDiskLib und `QueryChangedDiskAreas`, liest nur
geaenderte Sektoren und faellt nicht auf einen Voll-Download zurueck. Wenn
bereits Backups existieren, aber keine gueltige CBT-Baseline mit `changeId`,
wird automatisch ein neuer `full_000N` als CBT-Baseline geplant. Wenn CBT auf
der VM noch deaktiviert ist, aktiviert der Baseline-Lauf `changeTrackingEnabled`
vor dem Snapshot. Danach werden die folgenden Laeufe als `delta_000N` mit
CBT-Patches gespeichert und laden nur die von vSphere gemeldeten Aenderungen.

Der alte lokale Modus bleibt explizit verfuegbar:

```bash
./start_select_vm_backup.sh --backup-mode local-delta
```

Interaktive VM-Auswahl:

```bash
./start_select_vm_backup.sh
```

Screen bleibt verfuegbar, falls ein Lauf bewusst abgekoppelt werden soll:

```bash
./start_backup_screen.sh
# oder:
VSPHERE_USE_SCREEN=1 ./start_select_vm_backup.sh
```

Auswahldatei aus dem aktuellen vSphere-Inventar erzeugen:

```bash
./start_vm_liste.sh
```

Restore:

```bash
./start_restore_vm_backup.sh
```

## Sicherheitsmodell

- Kein blindes "alle VMs sichern".
- Geschuetztes Einzel-Backup verlangt `--confirm-vm` mit exakt dem konfigurierten
  `VSPHERE_TARGET_VM`.
- Optional koennen CPU, RAM, Disk-Anzahl, Disk-Groesse und Instance-UUID in
  `credentials.env` fixiert werden.
- Laufende VMs werden ueber temporaere Snapshots gesichert; Snapshots werden
  nach Abschluss entfernt.
- Vorhandene Snapshots blockieren den geschuetzten Einzelpfad.
- Restore erzeugt eine neue VM, standardmaessig ausgeschaltet und ohne
  verbundene Netzwerkkarte.

## Pruefung

```bash
python3 -m py_compile *.py tests/test_safety.py
python3 -m unittest discover -s tests -v
```
