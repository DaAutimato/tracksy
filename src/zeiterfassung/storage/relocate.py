"""Speicherort der Daten ändern (Einstellungen → Daten → Speicherort) – GUI-frei.

Ablauf (:func:`relocate`), alles unter der Sperre des Service:

1. :func:`check_target` – Ziel ist ein eigenes, beschreibbares, lokales
   Verzeichnis (kein Symlink, keine Steuerzeichen, nicht im alten
   Datenverzeichnis) und enthält **keine** Tracksy-Daten (nichts wird
   überschrieben). Cloud-Sync-Ordner werden erkannt (:attr:`TargetCheck.cloud`)
   – die Oberfläche warnt, blockiert aber nicht.
2. Kopieren: Datenbank über die SQLite-Backup-API (konsistent inkl. WAL-Inhalt,
   ohne ``-wal``/``-shm``), CSV-Protokoll, CSV-``.bak`` und interne Sicherungen
   (``backups/``) Byte für Byte; Ordner 0700, Dateien 0600, ``O_EXCL`` (nie
   überschreiben), Symlinks werden nicht verfolgt.
3. Prüfen: ``PRAGMA integrity_check`` der Kopie, gleiche Zeilenzahl aller
   Tabellen, SHA-256 aller kopierten Dateien.
4. ``commit()`` – speichert den neuen Ort in der Konfiguration. Schlägt einer
   der Schritte 1–4 fehl, werden nur die eben angelegten Kopien entfernt und
   die ursprünglichen Rechte des Zielordners wiederhergestellt;
   die Originale bleiben unverändert.
5. ``before_archive()`` (Service schließt die Datenbank), danach werden die
   bisherigen Dateien im alten Datenverzeichnis in ``_alt-<zeitstempel>/``
   verschoben (umbenannt, nicht gelöscht). Modelle, ``lib/``, venv und
   ``config.toml`` bleiben, wo sie sind.

Die App muss danach neu starten (die alte Datenbankverbindung ist geschlossen).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from ..config import (
    CSV_FILENAME,
    DB_FILENAME,
    DIR_MODE,
    FILE_MODE,
    has_control_chars,
)
from .maintenance import BACKUP_DIR_NAME, PENDING_PURGE_NAME, _copy_private

log = logging.getLogger(__name__)

__all__ = [
    "RelocateError",
    "TargetCheck",
    "RelocateResult",
    "check_target",
    "relocate",
    "tracksy_items",
    "is_cloud_path",
    "filesystem_type",
    "NETWORK_FS",
    "ARCHIVE_PREFIX",
]

ARCHIVE_PREFIX = "_alt-"

# Dateisysteme, auf denen Daten das Gerät verlassen (Netzlaufwerke, entfernte FUSE)
NETWORK_FS = frozenset({
    "nfs", "nfs4", "cifs", "smb3", "smbfs", "ncpfs", "afs", "ceph", "glusterfs",
    "davfs", "fuse.davfs2", "fuse.sshfs", "sshfs", "fuse.rclone", "fuse.s3fs",
    "fuse.gvfsd-fuse", "fuse.gcsfuse", "fuse.onedriver", "fuse.google-drive-ocamlfuse",
    "fuse.dropbox", "fuse.megafuse", "fuse.curlftpfs", "fuse.goofys", "lustre", "9p.net",
})

# Ordnernamen typischer Sync-Clients (Komponente des Pfads, Groß-/Kleinschreibung egal)
_CLOUD_RE = re.compile(
    r"^(dropbox|nextcloud|owncloud|onedrive|google[ _-]?drive|gdrive|mydrive|"
    r"mega|megasync|icloud[ _-]?drive|icloud|pcloud[ _-]?drive|pcloud|seafile|"
    r"syncthing|tresorit|sync\.com|box[ _-]?sync|proton[ _-]?drive|kdrive|"
    r"magentacloud|hidrive|strato[ _-]?hidrive|web\.de[ _-]?onlinespeicher|"
    r"gmx[ _-]?mediacenter|ionos[ _-]?hidrive)\b",
    re.IGNORECASE,
)

_BAK_RE = re.compile(re.escape(CSV_FILENAME) + r"\.\d{8}-\d{6}(?:-\d{1,6})?\.bak")
_PENDING_ASIDE_RE = re.compile(re.escape(PENDING_PURGE_NAME) + r"\.defekt-\d{8}-\d{6}(?:-\d{1,6})?")


class RelocateError(RuntimeError):
    """Umzug nicht möglich – Meldung ist für die Oberfläche formuliert (Deutsch)."""


@dataclass
class TargetCheck:
    path: Path                 # aufgelöster Zielpfad
    cloud: bool = False        # sieht nach Cloud-Sync-Ordner aus → Datenschutz-Warnung
    not_empty: bool = False    # enthält andere (fremde) Dateien
    same_as_default: bool = False  # Ziel = Standardort → general.data_dir wird geleert
    needed_bytes: int = 0


@dataclass
class RelocateResult:
    new_dir: Path
    archive_dir: Path | None = None       # alte Dateien hierhin verschoben
    copied: list[str] = field(default_factory=list)  # relative Pfade
    archive_failed: list[str] = field(default_factory=list)  # nicht verschobene Altdateien
    # Rechte des Zielordners vor dem Umzug, falls Tracksy sie auf 0700 geändert hat
    # (transparent für die Oberfläche; bei Fehler wird dieser Modus zurückgesetzt)
    previous_mode: int | None = None


# --------------------------------------------------------------------------
# Hilfen
# --------------------------------------------------------------------------
def is_cloud_path(path: Path | str) -> bool:
    """Liegt ``path`` (oder ein Elternordner) offensichtlich in einem Cloud-Sync-Ordner?"""
    return any(_CLOUD_RE.match(part) for part in Path(path).parts)


def _unescape_mount(text: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def filesystem_type(path: Path | str, mountinfo: str | None = None) -> str | None:
    """Dateisystemtyp des Mountpoints, der ``path`` enthält (``/proc/self/mountinfo``)."""
    if mountinfo is None:
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        except OSError:
            return None
    target = os.path.normpath(str(path))
    best, best_len = None, -1
    for line in mountinfo.splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        cols = left.split()
        if len(cols) < 5 or not right.split():
            continue
        mnt = _unescape_mount(cols[4])
        inside = target == mnt or target.startswith(mnt.rstrip("/") + "/") or mnt == "/"
        if inside and len(mnt) > best_len:
            best, best_len = right.split()[0], len(mnt)
    return best


def tracksy_items(directory: Path) -> list[str]:
    """Namen der Tracksy-Daten direkt in ``directory`` (DB samt -wal/-shm, CSV, .bak, backups)."""
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    fixed = {DB_FILENAME, DB_FILENAME + "-wal", DB_FILENAME + "-shm",
             DB_FILENAME + "-journal", CSV_FILENAME, BACKUP_DIR_NAME, PENDING_PURGE_NAME}
    return sorted(n for n in names
                  if n in fixed or _BAK_RE.fullmatch(n) or _PENDING_ASIDE_RE.fullmatch(n))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _source_files(src_dir: Path) -> list[Path]:
    """Zu kopierende Dateien relativ zu ``src_dir`` (ohne Datenbank; ohne Symlinks)."""
    out: list[Path] = []
    for name in tracksy_items(src_dir):
        if name.startswith(DB_FILENAME):
            continue
        p = src_dir / name
        st = os.lstat(p)
        if stat.S_ISREG(st.st_mode):
            out.append(Path(name))
        elif stat.S_ISDIR(st.st_mode) and name == BACKUP_DIR_NAME:
            for root, dirs, files in os.walk(p, followlinks=False):
                rel = Path(root).relative_to(src_dir)
                for d in list(dirs):
                    if os.path.islink(os.path.join(root, d)):
                        log.warning("Symlink in Sicherungen übersprungen")
                        dirs.remove(d)
                for f in sorted(files):
                    fp = Path(root) / f
                    if stat.S_ISREG(os.lstat(fp).st_mode):
                        out.append(rel / f)
                    else:
                        log.warning("Keine reguläre Datei in Sicherungen übersprungen")
        else:
            log.warning("%s ist keine reguläre Datei – nicht übernommen", name)
    return out


def _size(src_dir: Path, db_path: Path, files: Iterable[Path]) -> int:
    total = 0
    for p in [db_path, db_path.with_name(db_path.name + "-wal")]:
        try:
            total += p.stat().st_size
        except OSError:
            pass
    for rel in files:
        total += (src_dir / rel).stat().st_size
    return total


# --------------------------------------------------------------------------
# Prüfung des Ziels
# --------------------------------------------------------------------------
def check_target(target: Path | str, current_dir: Path, *, default_dir: Path | None = None,
                 db_path: Path | None = None, mountinfo: str | None = None) -> TargetCheck:
    """Ziel prüfen; :class:`RelocateError` mit verständlicher Meldung, wenn ungeeignet."""
    raw = str(target)
    if has_control_chars(raw):
        raise RelocateError("Der Pfad enthält Steuerzeichen und kann nicht verwendet werden.")
    if not os.path.isabs(raw):
        raise RelocateError("Bitte einen vollständigen (absoluten) Pfad wählen.")
    path = Path(os.path.normpath(raw))
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise RelocateError(f"Der Ordner {path} existiert nicht.") from None
    if stat.S_ISLNK(st.st_mode):
        raise RelocateError("Der gewählte Ordner ist eine symbolische Verknüpfung. "
                            "Bitte den eigentlichen Ordner wählen.")
    if not stat.S_ISDIR(st.st_mode):
        raise RelocateError(f"{path} ist kein Ordner.")
    real = Path(os.path.realpath(path))
    if st.st_uid != os.getuid():
        raise RelocateError("Der Ordner gehört einem anderen Benutzer.")
    cur = Path(os.path.realpath(current_dir))
    if real == cur:
        raise RelocateError("Das ist bereits der aktuelle Speicherort.")
    if _is_within(real, cur):
        raise RelocateError("Der neue Ort darf nicht im bisherigen Datenverzeichnis liegen.")
    if _is_within(cur, real):
        raise RelocateError("Der neue Ort darf das bisherige Datenverzeichnis nicht enthalten. "
                            "Bitte einen eigenen Ordner wählen.")
    if not os.access(real, os.W_OK | os.X_OK):
        raise RelocateError("In diesen Ordner darf nicht geschrieben werden.")
    fs = filesystem_type(real, mountinfo)
    if fs is not None and (fs in NETWORK_FS or fs.startswith("fuse.sshfs")):
        raise RelocateError(f"Der Ordner liegt auf einem Netz- oder Cloud-Laufwerk ({fs}). "
                            "Bitte einen lokalen Ordner wählen.")
    found = tracksy_items(real)
    if found:
        raise RelocateError("Der Ordner enthält bereits Tracksy-Daten "
                            f"({', '.join(found)}). Es wird nichts überschrieben – bitte "
                            "einen anderen oder leeren Ordner wählen.")
    # Schreibprobe (eigene Temp-Datei, sofort wieder entfernt)
    try:
        fd, probe = tempfile.mkstemp(prefix=".tracksy-probe-", dir=real)
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        raise RelocateError(f"In diesen Ordner kann nicht geschrieben werden ({exc.strerror}).") from None
    check = TargetCheck(path=real, cloud=is_cloud_path(real))
    check.not_empty = any(not n.startswith(".tracksy-probe-") for n in os.listdir(real))
    if default_dir is not None:
        check.same_as_default = real == Path(os.path.realpath(default_dir))
    if db_path is not None:
        check.needed_bytes = _size(current_dir, db_path, _source_files(current_dir))
        try:
            free = os.statvfs(real).f_bavail * os.statvfs(real).f_frsize
        except OSError:  # pragma: no cover
            free = None
        if free is not None and free < check.needed_bytes + (1 << 20):
            raise RelocateError("Im Zielordner ist nicht genug freier Speicherplatz.")
    return check


# --------------------------------------------------------------------------
# Umzug
# --------------------------------------------------------------------------
def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    # Tabellennamen stammen aus sqlite_master (nicht vom Nutzer); quotiert
    return {n: conn.execute('SELECT COUNT(*) FROM "' + n.replace('"', '""') + '"').fetchone()[0]
            for n in names}


def _mkdir_private(path: Path, created: list[Path]) -> None:
    os.mkdir(path, DIR_MODE)  # FileExistsError, wenn vorhanden → nie mischen
    os.chmod(path, DIR_MODE)
    created.append(path)


def _cleanup(created: list[Path]) -> None:
    """Nur die eben selbst angelegten Kopien entfernen (umgekehrte Reihenfolge)."""
    for p in reversed(created):
        try:
            if p.is_dir() and not p.is_symlink():
                os.rmdir(p)
            else:
                os.unlink(p)
        except OSError as exc:  # pragma: no cover - Restbestand wird gemeldet
            log.warning("Kopie nicht entfernt (%s): %s", type(exc).__name__, p.name)


def _create_tracked(dest: Path, created: list[Path], create: Callable[[Path], object]) -> None:
    """``create(dest)`` ausführen und ``dest`` für das Aufräumen merken.

    Existiert ``dest`` schon (``O_EXCL`` → ``FileExistsError``, z. B. zwischen
    Prüfung und Kopie von einem anderen Prozess angelegt), ist die Datei **nicht**
    unsere und darf beim Aufräumen nie entfernt werden.
    """
    created.append(dest)
    try:
        create(dest)
    except FileExistsError:
        created.remove(dest)
        raise


def _restore_mode(path: Path, mode: int | None) -> None:
    """Ursprüngliche Rechte des Zielordners nach einem Fehler wiederherstellen."""
    if mode is None:
        return
    try:
        os.chmod(path, mode)
    except OSError as exc:  # pragma: no cover - Restbestand wird gemeldet
        log.warning("Rechte des Zielordners nicht zurückgesetzt (%s)", type(exc).__name__)


def relocate(
    db,  # storage.db.Database (offene, beschreibbare Verbindung)
    src_dir: Path,
    target: Path | str,
    *,
    commit: Callable[[Path], None],
    before_archive: Callable[[], None] | None = None,
    default_dir: Path | None = None,
    now: datetime | None = None,
    mountinfo: str | None = None,
) -> RelocateResult:
    """Daten von ``src_dir`` nach ``target`` kopieren, prüfen, umstellen, Altbestand archivieren.

    ``commit(neuer_ort)`` speichert die Konfiguration (vor dem Archivieren);
    ``before_archive()`` schließt die alte Datenbankverbindung.
    """
    src_dir = Path(src_dir)
    check = check_target(target, src_dir, default_dir=default_dir, db_path=db.path,
                         mountinfo=mountinfo)
    dest = check.path
    created: list[Path] = []
    result = RelocateResult(new_dir=dest)
    try:
        old_mode = stat.S_IMODE(os.stat(dest, follow_symlinks=False).st_mode)
        if old_mode != DIR_MODE:
            os.chmod(dest, DIR_MODE)
            result.previous_mode = old_mode
            log.info("Rechte des Zielordners von %04o auf %04o gesetzt", old_mode, DIR_MODE)
        files = _source_files(src_dir)
        # 1. Datenbank (Backup-API → konsistent, ohne -wal)
        new_db = dest / DB_FILENAME
        _create_tracked(new_db, created, db.backup_to)
        result.copied.append(DB_FILENAME)
        # 2. übrige Dateien
        for rel in files:
            for parent in reversed(rel.parents[:-1]):
                if not (dest / parent).exists():
                    _mkdir_private(dest / parent, created)
            _create_tracked(dest / rel, created,
                            lambda d, s=src_dir / rel: _copy_private(s, d))
            result.copied.append(str(rel))
        # 3. prüfen
        src_conn = db._conn  # gleiche Sperre wie backup_to (Aufrufer hält die Service-Sperre)
        with db._lock:
            want = _table_counts(src_conn)
        chk = sqlite3.connect(f"file:{new_db}?mode=ro", uri=True)
        try:
            ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
            got = _table_counts(chk)
        finally:
            chk.close()
        if ok != "ok" or got != want:
            raise RelocateError("Die kopierte Datenbank weicht vom Original ab – Umzug abgebrochen.")
        for rel in files:
            if _sha256(src_dir / rel) != _sha256(dest / rel):
                raise RelocateError(f"Kopie von {rel} weicht ab – Umzug abgebrochen.")
            if ((dest / rel).stat().st_mode & 0o777) != FILE_MODE:  # pragma: no cover
                os.chmod(dest / rel, FILE_MODE)
        # 4. Konfiguration umstellen
        commit(dest)
    except RelocateError:
        _cleanup(created)
        _restore_mode(dest, result.previous_mode)
        raise
    except Exception as exc:
        _cleanup(created)
        _restore_mode(dest, result.previous_mode)
        log.warning("Umzug fehlgeschlagen (%s)", type(exc).__name__)
        raise RelocateError(f"Umzug fehlgeschlagen: {exc}") from exc

    # 5. Altbestand archivieren (umbenennen, nicht löschen)
    if before_archive is not None:
        before_archive()
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    archive = src_dir / f"{ARCHIVE_PREFIX}{stamp}"
    n = 1
    while archive.exists():
        n += 1
        archive = src_dir / f"{ARCHIVE_PREFIX}{stamp}-{n}"
    try:
        os.mkdir(archive, DIR_MODE)
        result.archive_dir = archive
    except OSError as exc:
        log.warning("Archivordner nicht anlegbar (%s)", type(exc).__name__)
        result.archive_failed = tracksy_items(src_dir)
        return result
    for name in tracksy_items(src_dir):
        try:
            os.rename(src_dir / name, archive / name)
        except OSError as exc:
            log.warning("Altdatei nicht verschoben (%s): %s", type(exc).__name__, name)
            result.archive_failed.append(name)
    return result
