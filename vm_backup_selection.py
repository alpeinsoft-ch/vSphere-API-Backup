#!/usr/bin/env python3
"""Read-only vSphere inventory exporter for editable VM backup selection files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

import safe_vsphere_backup as core
from select_vm_backup import (
    inventory_records,
    parse_selection_numbers,
    print_numbered_inventory,
    write_selection_file,
)


def prompt_selected_indexes(total: int) -> list[int]:
    if not sys.stdin.isatty():
        return []
    print()
    raw = input("VM-Nummern fuer Backup eingeben (z.B. 3 oder 3,8,9; Enter = nur Datei erzeugen): ")
    return parse_selection_numbers(raw, total)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write an editable vSphere VM backup selection file")
    parser.add_argument("--config", help="Path to credentials.env/config.env")
    parser.add_argument(
        "--output",
        default="vm_backup_selection.txt",
        help="Selection file path. Default: vm_backup_selection.txt",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite an existing selection file")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    core.setup_logging(verbose=args.verbose)

    try:
        config = core.load_config(args.config)
        with core.VSphereSession(config) as session:
            records = inventory_records(session)
            print_numbered_inventory(records)
            selected_indexes = prompt_selected_indexes(len(records))
            written = write_selection_file(
                Path(args.output),
                records,
                overwrite=args.force,
                enabled_indexes=selected_indexes,
            )

        print()
        print(f"Auswahl-Datei geschrieben: {written}")
        if selected_indexes:
            selected_text = ", ".join(str(index) for index in selected_indexes)
            print(f"Ausgewaehlte VM-Nummern eingetragen: {selected_text}")
            print("Jetzt starten mit: ./start_select_vm_backup.sh")
        else:
            print("In der Datei bei jeder gewuenschten VM das fuehrende '# ' entfernen.")
        return 0
    except core.SafetyError as exc:
        core.LOGGER.error("Blocked by safety rule: %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 10
    except Exception as exc:
        core.LOGGER.error("Command failed: %s", exc, exc_info=args.verbose)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
