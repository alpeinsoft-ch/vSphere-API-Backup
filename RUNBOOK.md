# Runbook

## 1. Zugangsdaten eintragen

```bash
cd /srv/shares/Backup-f/Backup-system/vSphere-API-Bakup
chmod 600 credentials.env
nano credentials.env
```

Pflichtwerte:

```bash
VSPHERE_SERVER=<vcenter-host-oder-url>
VSPHERE_USER=<backup-user>
VSPHERE_PASSWORD=<passwort>
VSPHERE_TARGET_VM=<ziel-vm-fuer-den-geschuetzten-pfad>
```

## 2. Abhaengigkeiten vorbereiten

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Die Starter-Skripte legen die venv bei Bedarf selbst an.

VDDK fuer echte VMware-CBT-Deltas installieren:

```bash
./install_vddk_local.sh ./VMware-vix-disklib-8.0.3-23950268.x86_64.tar.gz
python3 - <<'PY'
import safe_vsphere_backup as core
print(core.vddk_backend_status()["detail"])
PY
```

## 3. Inventory pruefen

```bash
python3 inventory_discovery.py --show-all
```

Wenn nur die konfigurierte Ziel-VM angezeigt werden soll:

```bash
python3 inventory_discovery.py
```

## 4. Geschuetzte Ziel-VM pruefen

```bash
python3 backup_vsphere.py preflight
```

Erst wenn alle fatalen Checks `PASS` sind, ein Backup starten:

```bash
python3 backup_vsphere.py backup --confirm-vm '<VSPHERE_TARGET_VM>'
```

## 5. Interaktive VM-Auswahl

Einzelne VM aus dem Inventory waehlen:

```bash
./start_select_vm_backup.sh
```

Der Starter laeuft standardmaessig direkt im aktuellen Terminal, damit
Auswahl und Fehlermeldungen sichtbar bleiben. Fuer bewusst abgekoppelte Laeufe
kann `./start_backup_screen.sh` oder `VSPHERE_USE_SCREEN=1 ./start_select_vm_backup.sh`
genutzt werden.

Wenn `vm_backup_selection.txt` existiert, fragt der Starter zuerst, ob diese
Liste genutzt werden soll. Enter oder `n` startet die normale VM-Auswahl ohne
Liste, `j` startet den Listenlauf. Beide Wege laufen ohne weitere Parameter im
CBT-Modus. Im Listenlauf werden VMs mit fatalem Preflight-Fehler uebersprungen,
damit die restlichen ausgewaehlten VMs trotzdem gesichert werden.

Auswahldatei erzeugen und danach mehrere bewusst markierte VMs sichern:

```bash
./start_vm_liste.sh
./start_select_vm_backup.sh
```

Die Datei `vm_backup_selection.txt` enthaelt deaktivierte VM-Zeilen. Entferne
bei gewuenschten VMs das fuehrende `# `.

## 6. Delta-Speicher und CBT

Der bisherige interaktive Delta-Pfad legt pro VM eine Kette an:

```text
backups/<VM>/full_0001
backups/<VM>/delta_0002
backups/<VM>/delta_0003
```

`--backup-mode local-delta` ist lokaler Chunk-Dedupe nach einem normalen
vSphere-Export. Dieser Modus spart lokalen Speicher, laedt aber die VMDK zuerst
vollstaendig herunter.

`--backup-mode cbt` fordert echtes VMware Changed Block Tracking an. Dieser
Modus nutzt VDDK/VixDiskLib, liest nur die von vSphere gemeldeten geaenderten
Sektoren und faellt nicht auf den alten Voll-Download zurueck.

Wenn bereits Backups existieren, aber das letzte Backup keine gueltige
CBT-Baseline mit `changeId` enthaelt, plant `--backup-mode cbt` zuerst einen
neuen `full_000N` als CBT-Baseline. Wenn CBT auf der VM noch deaktiviert ist,
aktiviert dieser Baseline-Lauf `changeTrackingEnabled` vor dem Snapshot. Danach
werden weitere Laeufe als `delta_000N` mit CBT-Patches erstellt.

Manueller erster CBT-Baseline-Lauf:

```bash
./start_select_vm_backup.sh
```

Automatisierung nach vorhandener Baseline:

```bash
./start_select_vm_backup.sh --selection-file vm_backup_selection.txt --yes --skip-blocked --backup-mode cbt
```

Optional blockiert `--require-existing-backup` bewusst neue Full-Baselines.
Der Standard erlaubt sie, damit nach geloeschten Backups automatisch wieder
eine CBT-Baseline entsteht.

Manuelles Delta-Pack eines vorhandenen Backups:

```bash
python3 backup_vsphere.py delta-pack backups/<VM>/<lauf> --remove-originals
```

## 7. Verify

```bash
python3 backup_vsphere.py verify backups/<backup-ordner>
./start_select_vm_backup.sh --verify backups/<VM>/<lauf>
```

## 8. Restore

Interaktiv:

```bash
./start_restore_vm_backup.sh
```

Nicht-interaktiv:

```bash
./start_restore_vm_backup.sh \
  --backup-dir backups/<VM>/<lauf> \
  --new-name <neuer-vm-name> \
  --yes
```

Dry-Run:

```bash
./start_restore_vm_backup.sh \
  --backup-dir backups/<VM>/<lauf> \
  --new-name <neuer-vm-name> \
  --dry-run
```

## 9. Wiederkehrender Listenlauf

`run_weekly_vm_backup.sh` nutzt `vm_backup_selection.txt`, startet den
Listenpfad im CBT-Modus und schreibt nach `logs/weekly_vm_backup.log`.

```bash
./run_weekly_vm_backup.sh
```

## 10. Fehlerbehandlung

- `credentials.env` fehlt oder ist leer: Zugangsdaten eintragen.
- Zertifikatsfehler: vCenter-Zertifikat importieren und
  `VSPHERE_SSL_VERIFY=true` nutzen, oder fuer ein Lab bewusst `false` setzen.
- `Target VM not found`: `VSPHERE_TARGET_VM` exakt an den vCenter-Namen
  anpassen.
- Bestehende Snapshots: bewusst manuell klaeren, danach Preflight erneut
  ausfuehren.
- Zu wenig lokaler Speicher: `BACKUP_OUTPUT_DIR` auf ein groesseres Ziel legen
  oder alte Backups/Chunks bereinigen.
