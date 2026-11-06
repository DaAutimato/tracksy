"""CLI ``zeit`` – steuert die Haupt-App über D-Bus (Grundlage für Hotkeys).

Exit-Codes: 0 ok · 1 Fehler · 2 Aufruffehler · 3 App nicht startbar/erreichbar.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from . import __version__

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_APP = 3

ENV_START_TIMEOUT = "ZEIT_START_TIMEOUT"

# CLI-Befehl → GAction
_SIMPLE = {
    "stop": "stop",
    "toggle": "toggle",
    "pause": "pause",
    "voice": "voice",
    "show": "show",
    "schnell": "quick-entry",
}

_STATE_DE = {
    "idle": "kein Task aktiv",
    "running": "läuft",
    "paused": "pausiert",
    "stopping": "wird gestoppt (Ergänzungsdialog offen)",
}


def _ipc():
    from . import ipc  # lazy: Gio-Import nur wenn nötig

    return ipc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zeit", description="Tracksy steuern")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="BEFEHL")

    s = sub.add_parser("start", help="Task starten (ohne Angaben: Startformular)")
    s.add_argument("--kunde", default=None)
    s.add_argument("--thema", default=None)
    s.add_argument("--ticket", default=None)
    s.add_argument("--ort", choices=("remote", "onsite"), default=None)

    sub.add_parser("stop", help="Laufenden Task stoppen (Ergänzungsdialog)")
    sub.add_parser("toggle", help="Start/Stopp umschalten (Hotkey)")
    sub.add_parser("pause", help="Pause ⇄ Weiter")
    sub.add_parser("voice", help="Sprachbefehl: unter X11 Kürzel halten, sonst Start/Stopp")
    sub.add_parser("show", help="Hauptfenster zeigen")
    sub.add_parser("schnell", help="Schnellerfassung öffnen")
    sub.add_parser("quit", help="App beenden (falls sie läuft)")

    st = sub.add_parser("status", help="Status anzeigen")
    st.add_argument("--json", action="store_true", help="Status als JSON ausgeben")

    ex = sub.add_parser("export", help="Leistungsnachweis je Kunde und Monat exportieren")
    who = ex.add_mutually_exclusive_group(required=True)
    who.add_argument("--kunde", help="Kundenname (Groß-/Kleinschreibung egal)")
    who.add_argument("--alle-kunden", action="store_true", dest="alle_kunden",
                     help="Gesamtexport aller Kunden in einer Datei")
    ex.add_argument("--monat", required=True, help="Monat als JJJJ-MM")
    ex.add_argument("--format", choices=("xlsx", "csv"), default="xlsx", dest="fmt")
    ex.add_argument("--ziel", default=".", help="Zielordner (muss existieren), Default: aktuelles Verzeichnis")
    ex.add_argument("--vorlage", default=None, help="eigene XLSX-Vorlage (sonst Konfiguration/eingebaut)")
    ex.add_argument("--bericht", choices=("einzeln", "monat"), default="einzeln",
                    help="einzeln = jede Tätigkeit einzeln (Standard); "
                         "monat = Monatsbericht, Tätigkeiten je Tag zusammengefasst")

    sub.add_parser("kunden", help="Bisherige Kunden auflisten")

    lo = sub.add_parser("loeschen", help="Einträge endgültig löschen (Löschkonzept/DSGVO)")
    when = lo.add_mutually_exclusive_group()
    when.add_argument("--vor", default=None, metavar="JJJJ-MM-TT",
                      help="Einträge mit Beginn VOR diesem Datum löschen (Tag selbst bleibt; "
                           "gespeichert + verworfen)")
    when.add_argument("--bis", default=None, metavar="JJJJ-MM-TT",
                      help="Einträge mit Beginn BIS EINSCHLIESSLICH diesem Datum löschen "
                           "(wie in der App; gespeichert + verworfen)")
    lo.add_argument("--verworfene", action="store_true", help="alle verworfenen Einträge löschen")
    lo.add_argument("--ja", action="store_true", help="ohne Rückfrage löschen")
    lo.add_argument("--csv-sicherung", action="store_true",
                    help="alte Fassung des CSV-Protokolls als .bak behalten (selbst entsorgen!)")
    lo.add_argument("--mit-sicherungen", action="store_true", dest="mit_sicherungen",
                    help="auch alle Sicherungen im Datenverzeichnis (backups/, CSV-.bak) "
                         "entfernen – sie enthalten die gelöschten Daten weiterhin; "
                         "allein angegeben: nur diese Sicherungen entfernen")

    bk = sub.add_parser(
        "backup",
        help="Sicherung von Datenbank, CSV-Protokoll und Einstellungen; wendet vorher die "
             "Aufbewahrungsfrist an (löscht abgelaufene Einträge endgültig)",
        description="Legt eine Sicherung von Datenbank, CSV-Protokoll und Einstellungen an. "
                    "Vorher wird die Aufbewahrungsfrist (Einstellungen, retention_months) "
                    "angewendet: Einträge mit Beginn vor dem Stichtag werden endgültig "
                    "gelöscht (nur wenn Tracksy nicht läuft; sonst erledigt das die App), "
                    "ebenso interne Sicherungen, die vor dem Stichtag angelegt wurden. "
                    "So enthält die neue Sicherung keine abgelaufenen Daten. "
                    "Bei Frist 0 (unbegrenzt) wird nichts gelöscht.")
    bk.add_argument("--ziel", default=None,
                    help="Zielordner (muss existieren), Default: <Datenverzeichnis>/backups")
    return p


def start_payload(args: argparse.Namespace) -> str:
    fields = {
        "customer": args.kunde,
        "topic": args.thema,
        "ticket": args.ticket,
        "location": args.ort,
    }
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return ""
    fields["source"] = "cli"
    return json.dumps(fields, ensure_ascii=False)


def _paths():
    """Pfade wie die App auflösen (inkl. ``general.data_dir``); fehlender eigener
    Speicherort → ``DataDirUnavailable`` statt „keine Daten“."""
    from .config import Paths

    paths = Paths.resolve()
    paths.check_available()
    return paths


def _src_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def launch_app() -> subprocess.Popen:
    """Haupt-App im Hintergrund starten (detached, ``--hidden``)."""
    env = dict(os.environ)
    src = str(_src_dir())
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.Popen(
        [sys.executable, "-m", "zeiterfassung", "--hidden"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=env,
    )


def ensure_app(timeout_s: float | None = None) -> bool:
    ipc = _ipc()
    if ipc.is_running():
        return True
    if timeout_s is None:
        try:
            timeout_s = float(os.environ.get(ENV_START_TIMEOUT, "15"))
        except ValueError:
            timeout_s = 15.0
    launch_app()
    return ipc.wait_for_name(timeout_s)


def _fmt_duration(seconds: int) -> str:
    h, rem = divmod(max(0, int(seconds)), 3600)
    return f"{h}:{rem // 60:02d}"


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "–"
    return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M")


def format_status(st: dict[str, Any]) -> str:
    state = st.get("state", "idle")
    lines = [f"Status: {_STATE_DE.get(state, state)}"]
    e = st.get("entry")
    if e:
        lines.append(f"Kunde:  {e.get('customer') or '(leer)'}")
        if e.get("topic"):
            lines.append(f"Thema:  {e['topic']}")
        if e.get("ticket"):
            lines.append(f"Ticket: {e['ticket']}")
        lines.append(f"Ort:    {e.get('location')}")
        lines.append(f"Beginn: {_fmt_time(e.get('start_at'))}")
        lines.append(f"Dauer:  {_fmt_duration(e.get('elapsed_seconds', 0))} h (netto)")
        if e.get("pause_seconds") or e.get("paused_since"):
            lines.append(f"Pausen: {_fmt_duration(e.get('pause_seconds', 0))} h"
                         + (f", pausiert seit {_fmt_time(e['paused_since'])}"
                            if e.get("paused_since") else ""))
    if st.get("pending_start"):
        ps = st["pending_start"]
        lines.append(f"Danach: Start für {ps.get('customer') or '(leer)'} vorgemerkt")
    if not e and st.get("last_stopped"):
        ls = st["last_stopped"]
        lines.append(f"Zuletzt: {ls.get('customer') or '(leer)'}"
                     + (f" – {ls['topic']}" if ls.get("topic") else "")
                     + f" (bis {_fmt_time(ls.get('end_at'))})")
    if st.get("source") == "offline":
        lines.append("(App läuft nicht – Daten direkt aus der Datenbank)")
    return "\n".join(lines)


def offline_status() -> dict[str, Any]:
    """Status direkt aus SQLite (read-only), wenn die App nicht läuft."""
    from .core.status import build_status
    from .core.timeutil import now
    from .storage.db import Database

    paths = _paths()
    entry = last = None
    if paths.db_file.exists():
        db = Database(paths.db_file, read_only=True)
        try:
            active = db.active_entries()
            entry = active[0] if active else None
            last = db.last_stopped()
        finally:
            db.close()
    st = build_status(entry, now(), last_stopped=last)
    st["event"] = "offline"
    st["source"] = "offline"
    return st


def _open_db_ro():
    """(Paths, Database|None) – Datenbank read-only, None wenn noch keine existiert."""
    from .storage.db import Database

    paths = _paths()
    if not paths.db_file.exists():
        return paths, None
    return paths, Database(paths.db_file, read_only=True)


def cmd_kunden() -> int:
    try:
        _paths, db = _open_db_ro()
        names = []
        if db is not None:
            try:
                names = db.customers()
            finally:
                db.close()
    except Exception as exc:
        print(f"Fehler: Kunden nicht lesbar: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not names:
        print("Noch keine Kunden erfasst.")
    for n in names:
        print(n)
    return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    from .config import load_config
    from .export import TemplateError, export_customer_month, filter_entries, parse_month

    try:
        parse_month(args.monat)
    except (ValueError, TypeError):
        print(f"Fehler: Monat '{args.monat}' ungültig (erwartet JJJJ-MM).", file=sys.stderr)
        return EXIT_USAGE
    target = Path(args.ziel).expanduser()
    if not target.is_dir():
        print(f"Fehler: Zielordner existiert nicht: {target}", file=sys.stderr)
        return EXIT_ERROR
    try:
        paths, db = _open_db_ro()
        cfg = load_config(paths.config_file)
        entries = []
        if db is not None:
            try:
                entries = db.list_entries(statuses=("stopped",))
            finally:
                db.close()
    except Exception as exc:
        print(f"Fehler: Daten/Konfiguration nicht lesbar: {exc}", file=sys.stderr)
        return EXIT_ERROR
    customer = None if args.alle_kunden else args.kunde
    if not filter_entries(entries, customer=customer, month=args.monat):
        who = "alle Kunden" if customer is None else f"'{customer}'"
        print(f"Hinweis: keine gespeicherten Einträge für {who} im {args.monat}.",
              file=sys.stderr)
    template = args.vorlage or cfg.export.template_path or None
    if template and not Path(template).expanduser().is_file():
        print(f"Fehler: Vorlage nicht gefunden: {template}", file=sys.stderr)
        return EXIT_ERROR
    try:
        out = export_customer_month(
            entries, target, customer=customer, month=args.monat,
            employee_name=cfg.general.employee_name, fmt=args.fmt,
            round_minutes=cfg.export.rounding_minutes,
            template_path=Path(template).expanduser() if template else None,
            summary=args.bericht == "monat",
            schedule=cfg.worktime.schedule(),
        )
    except TemplateError as exc:
        print(f"Fehler in der Vorlage: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # z. B. OSError, ValueError, ungültige XLSX (BadZipFile)
        print(f"Fehler beim Export: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(out)
    return EXIT_OK


def _app_running() -> bool:
    try:
        return bool(_ipc().is_running())
    except Exception:  # kein PyGObject/kein Bus → App kann nicht laufen
        return False


def _confirm(prompt: str) -> bool:
    if not sys.stdin or not sys.stdin.isatty():
        return False
    try:
        return input(prompt).strip().lower() in ("ja", "j")
    except EOFError:
        return False


def _count(n: int, singular: str, plural: str) -> str:
    """„1 Eintrag“ / „2 Einträge“ / „0 Einträge“."""
    return f"{n} {singular if n == 1 else plural}"


def _backup_summary(items) -> str:
    n_dirs = sum(1 for b in items if b.kind == "backup")
    n_bak = len(items) - n_dirs
    parts = []
    if n_dirs:
        parts.append(_count(n_dirs, "Sicherung", "Sicherungen"))
    if n_bak:
        parts.append(_count(n_bak, "CSV-Sicherung", "CSV-Sicherungen") + " (.bak)")
    return " und ".join(parts)


def _retention_settings(paths, *, quiet: bool = False) -> tuple[int, date] | None:
    """``(Monate, Stichtag)`` der Aufbewahrungsfrist oder ``None`` (unbegrenzt/unlesbar)."""
    from .config import load_config
    from .storage.maintenance import retention_cutoff

    try:
        months = load_config(paths.config_file).general.retention_months
    except (ValueError, OSError) as exc:
        if not quiet:
            print(f"Warnung: Konfiguration nicht lesbar ({type(exc).__name__}) – "
                  "Aufbewahrungsfrist nicht angewendet.", file=sys.stderr)
        return None
    cutoff = retention_cutoff(date.today(), months)
    return None if cutoff is None else (months, cutoff)


def _cli_retention(db, paths, *, entries: bool) -> None:
    """Aufbewahrungsfrist auch ohne laufende App anwenden.

    ``entries=False`` (App läuft): nur interne Sicherungen – die Einträge
    bereinigt die App selbst beim Start. Fehler werden gemeldet, brechen den
    eigentlichen Befehl aber nicht ab.
    """
    from .storage.csvlog import CsvProtocol
    from .storage.maintenance import purge, remove_internal_backups, resume_pending_purge

    if entries:
        try:
            resume_pending_purge(db, CsvProtocol(paths.csv_file))
        except Exception as exc:
            print(f"Warnung: Unterbrochenes Löschen im CSV-Protokoll nicht nachgeholt "
                  f"({type(exc).__name__}).", file=sys.stderr)
    retention = _retention_settings(paths)
    if retention is None:
        return
    months, cutoff = retention
    try:
        n = 0
        if entries and db.purgeable_uuids(cutoff):
            n = purge(db, CsvProtocol(paths.csv_file), before=cutoff).entries
        cleanup = remove_internal_backups(db, paths.data_dir, paths.csv_file.name,
                                          before=cutoff, reason="retention")
    except Exception as exc:
        print(f"Warnung: Aufbewahrungsfrist nicht vollständig angewendet "
              f"({type(exc).__name__}).", file=sys.stderr)
        return
    if n or cleanup.removed:
        what = [_count(n, "Eintrag", "Einträge")] if n else []
        if cleanup.removed:
            what.append(f"{_backup_summary(cleanup.removed)} im Datenverzeichnis")
        print(f"Aufbewahrungsfrist ({_count(months, 'Monat', 'Monate')}, Stichtag "
              f"{cutoff.strftime('%d.%m.%Y')}): {' und '.join(what)} entfernt.", file=sys.stderr)
    if cleanup.failed:
        n_failed = len(cleanup.failed)
        print(f"Warnung: {_count(n_failed, 'Sicherung konnte', 'Sicherungen konnten')} "
              "nicht entfernt werden.", file=sys.stderr)


def cmd_loeschen(args: argparse.Namespace) -> int:
    from .storage.csvlog import CsvProtocol
    from .storage.db import Database
    from .storage.maintenance import (
        BACKUP_DIR_NAME,
        find_internal_backups,
        purge,
        remove_internal_backups,
    )

    before: date | None = None
    until: date | None = None
    raw = args.vor or getattr(args, "bis", None)
    if raw:
        try:
            parsed = date.fromisoformat(raw)
        except ValueError:
            print(f"Fehler: Datum '{raw}' ungültig (erwartet JJJJ-MM-TT).", file=sys.stderr)
            return EXIT_USAGE
        if args.vor:
            before = parsed
        else:  # --bis: Tag zählt mit → intern „vor dem Folgetag“
            until = parsed
            before = parsed + timedelta(days=1)
    entries_wanted = before is not None or args.verworfene
    if not entries_wanted and not args.mit_sicherungen:
        print("Fehler: --vor/--bis JJJJ-MM-TT und/oder --verworfene (bzw. --mit-sicherungen) "
              "angeben.", file=sys.stderr)
        return EXIT_USAGE
    if _app_running():
        print("Fehler: Tracksy läuft. Bitte zuerst beenden (zeit quit) "
              "oder in der App löschen.", file=sys.stderr)
        return EXIT_ERROR
    paths = _paths()
    if not paths.db_file.exists():
        print("Keine Daten vorhanden.")
        return EXIT_OK
    try:
        db = Database(paths.db_file)
    except Exception as exc:
        print(f"Fehler: Datenbank nicht lesbar ({type(exc).__name__}).", file=sys.stderr)
        return EXIT_ERROR
    backup_dir = paths.data_dir / BACKUP_DIR_NAME
    res = None
    try:
        # Aufbewahrungsfrist erst NACH der Bestätigung anwenden: bei „nein“
        # wird nichts gelöscht. Vorher nur ankündigen, was sie zusätzlich entfernt.
        retention = _retention_settings(paths, quiet=True)
        backups = find_internal_backups(paths.data_dir, paths.csv_file.name)
        wanted: set[str] = set()
        if entries_wanted:
            wanted = set(db.purgeable_uuids(before, args.verworfene))
            count = len(wanted)
            what = []
            if until is not None:
                what.append("alle Einträge mit Beginn bis einschließlich "
                            f"{until.strftime('%d.%m.%Y')}")
            elif before is not None:
                what.append(f"alle Einträge mit Beginn vor dem {before.strftime('%d.%m.%Y')}")
            if args.verworfene:
                what.append("alle verworfenen Einträge")
            print(f"Zu löschen: {' und '.join(what)} – {_count(count, 'Eintrag', 'Einträge')}.")
            print("Gelöscht werden auch Tätigkeiten, Pausen, Änderungsprotokoll und die Zeilen "
                  "im CSV-Protokoll. Laufende Tasks bleiben erhalten. Vorhandene Exporte und "
                  "Sicherungen außerhalb des Datenverzeichnisses sind NICHT betroffen.")
        if backups:
            if args.mit_sicherungen:
                print(f"Zusätzlich zu entfernen: {_backup_summary(backups)} im Datenverzeichnis "
                      f"({backup_dir}).")
            else:
                one = len(backups) == 1
                print(f"Hinweis: Im Datenverzeichnis {'liegt' if one else 'liegen'} "
                      f"{_backup_summary(backups)} ({backup_dir}). "
                      f"{'Sie enthält' if one else 'Sie enthalten'} die gelöschten Daten "
                      "weiterhin.")
        elif not entries_wanted:
            print("Keine Sicherungen im Datenverzeichnis vorhanden.")
            return EXIT_OK
        if retention is not None:
            months, cutoff = retention
            # Nur nennen, was nicht schon oben angekündigt ist: Einträge
            # unter --vor/--bis/--verworfene bzw. Sicherungen bei --mit-sicherungen.
            n_old_entries = len(set(db.purgeable_uuids(cutoff)) - wanted)
            n_old_backups = 0 if args.mit_sicherungen else sum(
                1 for b in backups if b.created.date() < cutoff)
            expired = [_count(n_old_entries, "Eintrag", "Einträge")] if n_old_entries else []
            if n_old_backups:
                expired.append(_count(n_old_backups, "interne Sicherung", "interne Sicherungen"))
            if expired:
                verb = "wird" if n_old_entries + n_old_backups == 1 else "werden"
                print(f"Außerdem gilt die Aufbewahrungsfrist "
                      f"({_count(months, 'Monat', 'Monate')}, Stichtag "
                      f"{cutoff.strftime('%d.%m.%Y')}): {' und '.join(expired)} vor dem "
                      f"Stichtag {verb} ebenfalls entfernt.")
        if not args.ja and not _confirm("Endgültig löschen? Eingabe 'ja': "):
            print("Abgebrochen – nichts gelöscht.", file=sys.stderr)
            return EXIT_ERROR
        _cli_retention(db, paths, entries=True)
        backups = find_internal_backups(paths.data_dir, paths.csv_file.name)
        if entries_wanted:
            res = purge(db, CsvProtocol(paths.csv_file), before=before,
                        discarded=args.verworfene, keep_backup=args.csv_sicherung)
            print(f"Gelöscht: {_count(res.entries, 'Eintrag', 'Einträge')}, "
                  f"{_count(res.audit, 'Protokolleintrag', 'Protokolleinträge')}, "
                  f"{_count(res.csv_rows, 'CSV-Zeile', 'CSV-Zeilen')}.")
        remove = args.mit_sicherungen
        if backups and not remove and not args.ja:
            remove = _confirm(f"Auch die {_backup_summary(backups)} im Datenverzeichnis "
                              "endgültig entfernen? Eingabe 'ja': ")
        if backups and remove:
            keep = {res.csv_backup} if res is not None and res.csv_backup is not None else set()
            cleanup = remove_internal_backups(db, paths.data_dir, paths.csv_file.name,
                                              reason="manual", exclude=keep)
            print(f"Entfernt: {_backup_summary(cleanup.removed) or 'keine Sicherungen'}.")
            if cleanup.failed:
                n_failed = len(cleanup.failed)
                print(f"Warnung: {_count(n_failed, 'Sicherung konnte', 'Sicherungen konnten')} "
                      "nicht entfernt werden – bitte selbst prüfen:", file=sys.stderr)
                for f in cleanup.failed:
                    print(f"  {f}", file=sys.stderr)
        elif backups:
            verb = "enthält" if len(backups) == 1 else "enthalten"
            print(f"Hinweis: Die {_backup_summary(backups)} in {paths.data_dir} {verb} die "
                  "gelöschten Daten weiterhin. Entfernen: zeit loeschen --mit-sicherungen")
    except Exception as exc:
        print(f"Fehler beim Löschen ({type(exc).__name__}). Vorgang kann wiederholt werden.",
              file=sys.stderr)
        return EXIT_ERROR
    finally:
        db.close()
    if res is not None and res.csv_backup is not None:
        print(f"Alte CSV-Fassung aufbewahrt: {res.csv_backup} – enthält die gelöschten Daten, "
              "bitte selbst sicher entsorgen.")
    return EXIT_OK


def cmd_backup(args: argparse.Namespace) -> int:
    from .config import ensure_private_dir
    from .storage.db import Database
    from .storage.maintenance import BACKUP_DIR_NAME, create_backup

    paths = _paths()
    if args.ziel:
        target = Path(args.ziel).expanduser()
        if not target.is_dir():
            print(f"Fehler: Zielordner existiert nicht: {target}", file=sys.stderr)
            return EXIT_ERROR
    else:
        target = paths.data_dir / BACKUP_DIR_NAME
    if not paths.db_file.exists():
        print("Keine Daten vorhanden – nichts zu sichern.", file=sys.stderr)
        return EXIT_ERROR
    # Aufbewahrungsfrist auch bei reiner CLI-Nutzung: vor der Sicherung,
    # damit sie keine abgelaufenen Daten enthält. Läuft die App, bereinigt sie die
    # Einträge selbst; hier dann nur alte interne Sicherungen.
    try:
        rw = Database(paths.db_file)
    except Exception as exc:
        print(f"Warnung: Aufbewahrungsfrist nicht angewendet ({type(exc).__name__}).",
              file=sys.stderr)
    else:
        try:
            _cli_retention(rw, paths, entries=not _app_running())
        finally:
            rw.close()
    try:
        if not target.exists():
            ensure_private_dir(target)
        db = Database(paths.db_file, read_only=True)
        try:
            out = create_backup(db, target, csv_path=paths.csv_file,
                                config_path=paths.config_file)
        finally:
            db.close()
    except Exception as exc:
        print(f"Fehler bei der Sicherung ({type(exc).__name__}: {exc}).", file=sys.stderr)
        return EXIT_ERROR
    print(out)
    print("Hinweis: Die Sicherung enthält personenbezogene Daten – nur verschlüsselt "
          "aufbewahren. Sicherungen im Datenverzeichnis entfernt die Aufbewahrungsfrist "
          "automatisch, alle anderen bitte nach Ablauf der Frist selbst löschen.",
          file=sys.stderr)
    return EXIT_OK


def cmd_status(as_json: bool) -> int:
    ipc = None
    try:
        ipc = _ipc()
    except (ImportError, ValueError):  # pragma: no cover - kein PyGObject
        pass
    st: dict[str, Any] | None = None
    if ipc is not None and ipc.is_running():
        try:
            st = ipc.get_status()
            st.setdefault("source", "app")
        except ipc.IpcError as exc:
            print(f"Warnung: {exc}", file=sys.stderr)
    if st is None:
        try:
            st = offline_status()
        except Exception as exc:
            print(f"Fehler: Status nicht lesbar: {exc}", file=sys.stderr)
            return EXIT_ERROR
    if as_json:
        print(json.dumps(st, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_status(st))
    return EXIT_OK


def cmd_action(action: str, param: str | None = None, *, autostart: bool = True) -> int:
    ipc = _ipc()
    if autostart:
        if not ensure_app():
            print("Fehler: Tracksy konnte nicht gestartet werden.", file=sys.stderr)
            return EXIT_APP
    elif not ipc.is_running():
        print("Tracksy läuft nicht.")
        return EXIT_OK
    try:
        ipc.activate_action(action, param)
    except ipc.IpcError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return EXIT_APP
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    from .config import DataDirUnavailable

    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except DataDirUnavailable as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _dispatch(args: argparse.Namespace) -> int:
    cmd = args.command
    if cmd == "status":
        return cmd_status(args.json)
    if cmd == "export":
        return cmd_export(args)
    if cmd == "kunden":
        return cmd_kunden()
    if cmd == "loeschen":
        return cmd_loeschen(args)
    if cmd == "backup":
        return cmd_backup(args)
    if cmd == "start":
        return cmd_action("start", start_payload(args))
    if cmd == "quit":
        return cmd_action("quit", autostart=False)
    return cmd_action(_SIMPLE[cmd])


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
