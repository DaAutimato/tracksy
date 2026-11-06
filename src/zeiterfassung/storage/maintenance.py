"""Löschen (Löschkonzept) und Sichern – GUI-frei.

Wird von ``TrackerService`` (GUI, Aufbewahrungsfrist) und direkt von der CLI
(``zeit loeschen``/``zeit backup``) genutzt.

Hinweis Restrisiko: ``secure_delete`` + ``VACUUM`` entfernen die Daten aus der
SQLite-Datei; das neu geschriebene CSV ersetzt die alte Datei. Auf Journaling-
Dateisystemen/SSDs (Wear-Leveling, Snapshots) und in vorhandenen Sicherungen
können alte Blöcke physisch überdauern → Festplattenverschlüsselung empfohlen.
"""

from __future__ import annotations

import calendar
import contextlib
import errno
import fcntl
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from ..config import FILE_MODE, ensure_private_dir
from ..core.timeutil import now as _now
from .csvlog import CsvProtocol
from .db import Database

log = logging.getLogger(__name__)

__all__ = [
    "PurgeResult",
    "purge",
    "resume_pending_purge",
    "PENDING_PURGE_NAME",
    "retention_cutoff",
    "create_backup",
    "BACKUP_PREFIX",
    "BACKUP_DIR_NAME",
    "InternalBackup",
    "BackupCleanup",
    "find_internal_backups",
    "remove_internal_backups",
]

BACKUP_PREFIX = "zeiterfassung-backup-"
BACKUP_DIR_NAME = "backups"  # <Datenverzeichnis>/backups – Standardziel von create_backup
_STAMP = r"(\d{8}-\d{6})(?:-\d{1,6})?"
_BACKUP_RE = re.compile(re.escape(BACKUP_PREFIX) + _STAMP)


@dataclass
class PurgeResult:
    entries: int = 0          # gelöschte Einträge (inkl. Tätigkeiten/Pausen per Cascade)
    audit: int = 0            # gelöschte Audit-Zeilen
    csv_rows: int = 0         # entfernte Zeilen im CSV-Protokoll
    csv_backup: Path | None = None  # nur bei keep_backup=True: alte CSV-Fassung (Nutzer entsorgt)
    before: date | None = None
    discarded: bool = False
    uuids: list[str] = field(default_factory=list)
    vacuumed: bool = True     # False: VACUUM blockiert (andere Verbindung) – secure_delete greift trotzdem
    backups_removed: int = 0  # entfernte interne Sicherungen/.bak (nur Aufbewahrungsfrist)

    def to_dict(self) -> dict:
        return {
            "entries": self.entries,
            "backups_removed": self.backups_removed,
            "audit": self.audit,
            "csv_rows": self.csv_rows,
            "csv_backup": str(self.csv_backup) if self.csv_backup else None,
            "before": self.before.isoformat() if self.before else None,
            "discarded": self.discarded,
            "vacuumed": self.vacuumed,
        }


def retention_cutoff(today: date, months: int) -> date | None:
    """Stichtag für die Aufbewahrungsfrist: ``today`` minus ``months`` Monate.

    ``months <= 0`` → ``None`` (unbegrenzt). Monatsende wird begrenzt
    (31.03. − 1 Monat → 28./29.02.).
    """
    if months <= 0:
        return None
    total = today.year * 12 + (today.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    day = min(today.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


# Merkzettel für ein begonnenes Löschen: Die Datenbank wird in einer Transaktion
# bereinigt, das CSV-Protokoll danach neu geschrieben. Scheitert der zweite
# Schritt (z. B. Platte voll), blieben gelöschte Einträge im Protokoll stehen.
# Der Merkzettel enthält nur die UUIDs (keine Inhalte) und wird erst nach dem
# erfolgreichen CSV-Schritt entfernt; ``resume_pending_purge`` holt den Rest nach.
PENDING_PURGE_NAME = ".loeschen-offen.json"
_PENDING_MAX_BYTES = 8 * 1024 * 1024
_LOCK_UNSUPPORTED = {errno.ENOLCK, errno.ENOSYS, errno.EOPNOTSUPP, errno.EINVAL}


def _pending_path(csv: CsvProtocol) -> Path:
    return csv.path.parent / PENDING_PURGE_NAME


@contextmanager
def _purge_lock(directory: Path, *, blocking: bool = True) -> Iterator[bool]:
    """Löschvorgänge mehrerer Prozesse (App, CLI) gegeneinander sperren.

    ``flock`` auf das Datenverzeichnis selbst – keine zusätzliche Datei. Liefert
    ``False``, wenn ``blocking=False`` und ein anderer Prozess gerade löscht.
    """
    ensure_private_dir(directory)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        except OSError as exc:
            if exc.errno not in _LOCK_UNSUPPORTED:
                raise
            # z. B. Netzlaufwerk ohne Sperren: ohne Sperre weiter statt Löschen zu verhindern
            log.warning("Sperre beim Löschen nicht möglich (%s) – fahre ohne Sperre fort",
                        type(exc).__name__)
            yield True
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_pending(path: Path, uuids: list[str]) -> None:
    ensure_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".loeschen-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"uuids": uuids}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_pending(path: Path) -> list[str] | None:
    """UUIDs aus dem Merkzettel; ``None``, wenn keiner da ist.

    ``ValueError`` bei allem, was kein gültiger Merkzettel ist (Symlink, FIFO,
    zu groß, falsches Format, nicht lesbar).
    """
    try:
        # O_NONBLOCK: eine FIFO unter diesem Namen darf nicht hängen bleiben
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:  # ELOOP (Symlink), EACCES …
        raise ValueError(f"{path.name} nicht lesbar ({type(exc).__name__})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):  # Verzeichnis, FIFO, Gerät …
            raise ValueError(f"{path.name} ist keine reguläre Datei")
        if st.st_size > _PENDING_MAX_BYTES:
            raise ValueError(f"{path.name} ist zu groß")
        with os.fdopen(fd, "rb") as fh:
            fd = -1  # gehört jetzt fh
            raw = fh.read(_PENDING_MAX_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"{path.name} nicht lesbar ({type(exc).__name__})") from None
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, RecursionError) as exc:
        raise ValueError(f"{path.name} unlesbar ({type(exc).__name__})") from None
    uuids = data.get("uuids") if isinstance(data, dict) else None
    if not isinstance(uuids, list) or not all(isinstance(u, str) for u in uuids):
        raise ValueError(f"{path.name} hat ein unbekanntes Format")
    return uuids


def _clear_pending(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _set_aside(path: Path, why: BaseException) -> None:
    """Unbrauchbaren Merkzettel beiseitelegen statt jedes Löschen zu blockieren."""
    base = f"{path.name}.defekt-{_now().strftime('%Y%m%d-%H%M%S')}"
    aside = path.with_name(base)
    n = 1
    while os.path.lexists(aside):  # zwei Defekte in derselben Sekunde: nichts überschreiben
        n += 1
        aside = path.with_name(f"{base}-{n}")
    try:
        os.replace(path, aside)  # verschiebt bei einem Symlink den Link selbst
    except OSError as exc:
        log.error("Merkzettel für unterbrochenes Löschen unbrauchbar (%s) und nicht "
                  "verschiebbar (%s)", type(why).__name__, type(exc).__name__)
        return
    log.error("Merkzettel für unterbrochenes Löschen unbrauchbar (%s), verschoben nach %s",
              type(why).__name__, aside.name)


def _resume_locked(db: Database, csv: CsvProtocol) -> int:
    path = _pending_path(csv)
    try:
        uuids = _read_pending(path)
    except ValueError as exc:
        _set_aside(path, exc)
        return 0
    if uuids is None:
        return 0
    gone = set(uuids) - db.existing_uuids(uuids)
    rows = 0
    if gone:
        rows, _ = csv.remove_rows(uuids=gone)
        log.info("Unterbrochenes Löschen nachgeholt: %d CSV-Zeilen entfernt", rows)
    # Unter der Sperre kann kein anderer Prozess mitten im Löschen sein: existieren
    # alle Einträge noch, ist der Datenbankschritt nie erfolgt → nichts nachzuholen.
    _clear_pending(path)
    return rows


def resume_pending_purge(db: Database, csv: CsvProtocol) -> int:
    """Ein unterbrochenes Löschen im CSV-Protokoll zu Ende führen.

    Entfernt nur Zeilen von Einträgen, die in der Datenbank tatsächlich nicht
    mehr existieren – ein Merkzettel aus einem gescheiterten Datenbankschritt
    löscht also nie Protokollzeilen noch vorhandener Einträge. Löscht gerade ein
    anderer Prozess (Sperre belegt), passiert nichts. Rückgabe: Anzahl
    entfernter CSV-Zeilen. Fehler beim CSV-Schreiben werden weitergereicht; der
    Merkzettel bleibt dann für den nächsten Versuch. Ein unbrauchbarer
    Merkzettel (Symlink, FIFO, defekt) wird nach ``….defekt-<Stempel>`` verschoben.
    """
    if not _pending_path(csv).parent.is_dir():
        return 0
    with _purge_lock(csv.path.parent, blocking=False) as locked:
        if not locked:
            log.info("Nachholen übersprungen: ein anderer Prozess löscht gerade")
            return 0
        return _resume_locked(db, csv)


def purge(
    db: Database,
    csv: CsvProtocol,
    *,
    before: date | None = None,
    discarded: bool = False,
    keep_backup: bool = False,
    now: datetime | None = None,
) -> PurgeResult:
    """Einträge vor ``before`` und/oder alle verworfenen Einträge endgültig löschen.

    Reihenfolge: offenen Merkzettel nachholen → Merkzettel mit den UUIDs
    schreiben → SQLite (eine Transaktion) → CSV-Protokoll neu schreiben →
    Merkzettel entfernen → ``VACUUM``. Scheitert der CSV-Schritt, holt der
    nächste Aufruf (oder ``resume_pending_purge`` beim App-Start) ihn nach.
    Aktive Einträge werden nie gelöscht.
    """
    if before is None and not discarded:
        raise ValueError("Löschen braucht ein Datum oder 'verworfene'")
    now = now or _now()
    with _purge_lock(csv.path.parent):
        _resume_locked(db, csv)
        pending = _pending_path(csv)
        planned = db.purgeable_uuids(before, discarded)
        if planned:
            _write_pending(pending, planned)
        try:
            stats = db.purge(before, discarded, at=now)
        except BaseException:
            _clear_pending(pending)  # nichts gelöscht → kein Nachholen nötig
            raise
        rows, backup = csv.remove_rows(
            uuids=set(planned) | set(stats["uuids"]), before=before, discarded=discarded,
            keep_backup=keep_backup, now=now,
        )
        _clear_pending(pending)
    vacuumed = True
    try:
        db.vacuum()
    except sqlite3.OperationalError:  # z. B. offene Lesetransaktion eines anderen Prozesses
        log.warning("VACUUM nach dem Löschen nicht möglich (Datenbank belegt)")
        vacuumed = False
    return PurgeResult(
        entries=stats["entries"], audit=stats["audit"], csv_rows=rows, csv_backup=backup,
        before=before, discarded=discarded, uuids=list(stats["uuids"]), vacuumed=vacuumed,
    )


def _copy_private(src: Path, dest: Path) -> None:
    fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE)
    with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
        shutil.copyfileobj(inp, out)
        out.flush()
        os.fsync(out.fileno())


def create_backup(
    db: Database,
    target_dir: Path | str,
    *,
    csv_path: Path | None = None,
    config_path: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Sicherung in ``<target_dir>/zeiterfassung-backup-JJJJMMTT-HHMMSS/`` (0700).

    Inhalt (je 0600): ``zeiterfassung.sqlite3`` (SQLite-Backup-API, konsistent
    auch bei laufender App, ohne ``-wal``), ``protokoll.csv`` und
    ``config.toml`` (falls vorhanden). ``target_dir`` wird bei Bedarf angelegt.
    Rückgabe: Pfad des Sicherungsordners.
    """
    target = Path(target_dir).expanduser()
    if not target.exists():
        ensure_private_dir(target)
    if not target.is_dir():
        raise NotADirectoryError(f"Kein Verzeichnis: {target}")
    stamp = (now or _now()).strftime("%Y%m%d-%H%M%S")
    out = target / f"{BACKUP_PREFIX}{stamp}"
    n = 1
    while True:
        try:
            out.mkdir(mode=0o700)
            break
        except FileExistsError:
            n += 1
            out = target / f"{BACKUP_PREFIX}{stamp}-{n}"
    os.chmod(out, 0o700)
    db.backup_to(out / db.path.name)
    if csv_path is not None and Path(csv_path).is_file():
        _copy_private(Path(csv_path), out / Path(csv_path).name)
    if config_path is not None and Path(config_path).is_file():
        _copy_private(Path(config_path), out / Path(config_path).name)
    return out


# --------------------------------------------------------------------------
# Interne Sicherungen: <Datenverzeichnis>/backups/zeiterfassung-backup-*
# und <Datenverzeichnis>/protokoll.csv.<stempel>.bak. Sicherungen außerhalb des
# Datenverzeichnisses bleiben Verantwortung des Nutzers und werden nie berührt.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class InternalBackup:
    path: Path
    kind: str           # "backup" (Ordner) | "csv_bak" (Datei)
    created: datetime   # aus dem Namen (lokale Zeit, ohne Zone)


@dataclass
class BackupCleanup:
    removed: list[InternalBackup] = field(default_factory=list)
    failed: list[Path] = field(default_factory=list)

    @property
    def backups(self) -> int:
        return sum(1 for b in self.removed if b.kind == "backup")

    @property
    def csv_baks(self) -> int:
        return sum(1 for b in self.removed if b.kind == "csv_bak")

    def to_dict(self) -> dict:
        return {"backups": self.backups, "csv_bak": self.csv_baks, "failed": len(self.failed)}


def _csv_bak_re(csv_name: str) -> re.Pattern[str]:
    return re.compile(re.escape(csv_name) + r"\." + _STAMP + r"\.bak")


def _stamp(m: re.Match[str]) -> datetime | None:
    try:
        return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def _scan(dir_path: Path, pattern: re.Pattern[str], want_dir: bool) -> list[tuple[str, datetime]]:
    """Einträge in ``dir_path`` (kein Symlink) mit exakt passendem Namen, ohne Symlinks."""
    try:
        st = os.lstat(dir_path)
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(st.st_mode):  # auch: Symlink auf einen Ordner → ignorieren
        return []
    found = []
    with os.scandir(dir_path) as it:
        for e in it:
            m = pattern.fullmatch(e.name)
            if m is None or e.is_symlink():
                continue
            if (e.is_dir(follow_symlinks=False) if want_dir else e.is_file(follow_symlinks=False)):
                created = _stamp(m)
                if created is not None:
                    found.append((e.name, created))
    return found


def find_internal_backups(data_dir: Path | str, csv_name: str) -> list[InternalBackup]:
    """Sicherungen im eigenen Datenverzeichnis, älteste zuerst (folgt keinen Symlinks)."""
    data = Path(data_dir)
    out = [InternalBackup(data / BACKUP_DIR_NAME / n, "backup", c)
           for n, c in _scan(data / BACKUP_DIR_NAME, _BACKUP_RE, want_dir=True)]
    out += [InternalBackup(data / n, "csv_bak", c)
            for n, c in _scan(data, _csv_bak_re(csv_name), want_dir=False)]
    return sorted(out, key=lambda b: (b.created, str(b.path)))


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _remove_backup_dir(parent_fd: int, name: str) -> None:
    """Sicherungsordner ``name`` (relativ zu ``parent_fd``) entfernen.

    Öffnet mit ``O_NOFOLLOW`` und arbeitet nur über Datei-Deskriptoren: ein
    zwischenzeitlich untergeschobener Symlink führt nicht aus dem Ordner heraus.
    Unterordner sind in eigenen Sicherungen nie vorhanden → dann abbrechen.
    """
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        with os.scandir(fd) as it:
            entries = [(e.name, e.is_dir(follow_symlinks=False)) for e in it]
        if any(is_dir for _n, is_dir in entries):
            raise IsADirectoryError(f"unerwarteter Unterordner in {name}")
        for n, _d in entries:
            os.unlink(n, dir_fd=fd)  # entfernt auch Symlinks selbst, nie ihr Ziel
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def _remove_file(parent_fd: int, name: str) -> None:
    st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode):
        raise OSError(f"keine reguläre Datei: {name}")
    os.unlink(name, dir_fd=parent_fd)


def remove_internal_backups(
    db: Database | None,
    data_dir: Path | str,
    csv_name: str,
    *,
    before: date | None = None,
    reason: str = "manual",
    exclude: set[Path] | frozenset[Path] = frozenset(),
    now: datetime | None = None,
) -> BackupCleanup:
    """Interne Sicherungen entfernen – alle oder nur die vor ``before`` angelegten.

    Nur Namen nach dem eigenen Muster direkt in ``<data_dir>/backups/`` bzw.
    ``<data_dir>/`` (CSV-``.bak``), keine Symlinks. ``exclude``: Pfade, die
    bleiben sollen (z. B. die eben ausdrücklich angelegte ``.bak``). Protokolliert
    eine Audit-Zeile ``backups_removed`` (nur Anzahl, Grund, Stichtag – keine
    Inhalte oder Namen), wenn etwas entfernt wurde und ``db`` beschreibbar ist.
    """
    data = Path(data_dir)
    result = BackupCleanup()
    items = [b for b in find_internal_backups(data, csv_name)
             if b.path not in exclude and (before is None or b.created.date() < before)]
    if not items:
        return result
    fds: dict[Path, int] = {}
    try:
        for b in items:
            parent = b.path.parent
            try:
                if parent not in fds:
                    # Datenverzeichnis darf ein Symlink sein (Nutzerwahl), backups/ nicht
                    flags = _DIR_FLAGS if b.kind == "backup" else _DIR_FLAGS & ~os.O_NOFOLLOW
                    fds[parent] = os.open(parent, flags)
                if b.kind == "backup":
                    _remove_backup_dir(fds[parent], b.path.name)
                else:
                    _remove_file(fds[parent], b.path.name)
                result.removed.append(b)
            except OSError as exc:
                log.warning("Sicherung nicht entfernt (%s): %s", type(exc).__name__, b.path.name)
                result.failed.append(b.path)
    finally:
        for fd in fds.values():
            os.close(fd)
    if result.removed and db is not None and not db.read_only:
        db.add_audit("", "backups_removed", {
            "reason": reason,
            "before": before.isoformat() if before else None,
            **result.to_dict(),
        }, at=now or _now())
    return result
