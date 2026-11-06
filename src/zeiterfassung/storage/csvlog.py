"""CSV-Protokoll ``protokoll.csv`` – nur anhängend.

- Trennzeichen Semikolon, UTF-8, BOM nur beim Anlegen der Datei.
- Dezimalkomma bei Stunden.
- Spalte ``aktion``: ``neu`` | ``korrektur`` | ``verworfen``.
- Textzellen, die (auch nach führendem Leerraum) mit ``= + - @`` bzw. mit
  Tab/CR/LF beginnen, erhalten ein vorangestelltes ``'`` (Schutz vor
  Formel-Injektion beim Öffnen in Tabellen).
- Einzige Ausnahme von „nur anhängen“: :meth:`CsvProtocol.remove_rows` beim
  ausdrücklichen Löschen durch den Nutzer (Löschkonzept).
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
import threading
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from ..config import FILE_MODE, ensure_private_dir
from ..core.models import Entry
from ..core.sanitize import protect_formula
from ..core.timeutil import now as _now, to_iso

__all__ = ["CsvProtocol", "HEADER", "ACTIONS"]

HEADER = [
    "aktion",
    "protokolliert_am",
    "uuid",
    "datum",
    "kunde",
    "thema",
    "ticket",
    "ort",
    "beginn",
    "ende",
    "pause_min",
    "dauer_h",
    "beschreibung",
    "ergaenzung",
    "taetigkeiten",
    "quelle",
]
ACTIONS = ("neu", "korrektur", "verworfen")


def _safe(text: str) -> str:
    return protect_formula(text)


def _decimal(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


class CsvProtocol:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.Lock()

    def row_for(self, action: str, entry: Entry, at: datetime | None = None) -> list[str]:
        if action not in ACTIONS:
            raise ValueError(f"unbekannte Aktion {action!r}")
        net = entry.net_seconds()
        return [
            action,
            to_iso(at or _now()) or "",
            entry.uuid,
            entry.start_at.date().isoformat(),
            _safe(entry.customer),
            _safe(entry.topic),
            _safe(entry.ticket),
            str(entry.location),
            entry.start_at.strftime("%H:%M"),
            entry.end_at.strftime("%H:%M") if entry.end_at else "",
            str(round(entry.total_pause_seconds() / 60)),
            _decimal(net / 3600.0),
            _safe(entry.description),
            _safe(entry.addendum),
            _safe(" | ".join(entry.items)),
            str(entry.source),
        ]

    def append(self, action: str, entry: Entry, at: datetime | None = None) -> None:
        row = self.row_for(action, entry, at)
        with self._lock:
            ensure_private_dir(self.path.parent)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)
            with os.fdopen(fd, "a", encoding="utf-8", newline="") as fh:
                buf = io.StringIO()
                writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
                if os.fstat(fh.fileno()).st_size == 0:
                    buf.write("﻿")
                    writer.writerow(HEADER)
                writer.writerow(row)
                fh.write(buf.getvalue())
                fh.flush()
                os.fsync(fh.fileno())
            if (self.path.stat().st_mode & 0o777) != FILE_MODE:
                os.chmod(self.path, FILE_MODE)

    def read_rows(self) -> list[dict[str, str]]:
        """Hilfsfunktion (Tests/Anzeige): Protokoll als Liste von Dicts."""
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh, delimiter=";"))

    def remove_rows(
        self,
        *,
        uuids: Iterable[str] = (),
        before: date | None = None,
        discarded: bool = False,
        keep_backup: bool = False,
        now: datetime | None = None,
    ) -> tuple[int, Path | None]:
        """Zeilen endgültig entfernen (Löschkonzept) – Datei wird neu geschrieben.

        Entfernt Zeilen, deren ``uuid`` in ``uuids`` liegt, deren ``datum`` vor
        ``before`` liegt bzw. (``discarded``) mit ``aktion == "verworfen"``.
        Atomar: neue Datei (0600) im selben Verzeichnis, ``fsync``, ``os.replace``.
        Die alte Fassung verschwindet dabei (kein Rest im Datenverzeichnis);
        mit ``keep_backup=True`` bleibt sie als ``protokoll.csv.<zeitstempel>.bak``
        (0600) erhalten – dann muss der Nutzer sie selbst entsorgen.

        Rückgabe ``(entfernte_zeilen, backup_pfad_oder_None)``. Ohne Treffer
        wird nichts geschrieben.
        """
        drop = set(uuids)
        cutoff = before.isoformat() if before is not None else None
        with self._lock:
            if not self.path.exists():
                return 0, None
            with open(self.path, encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.reader(fh, delimiter=";"))
            if not rows:
                return 0, None
            header, body = rows[0], rows[1:]
            try:
                i_uuid = header.index("uuid")
                i_date = header.index("datum")
                i_action = header.index("aktion")
            except ValueError:
                raise ValueError("CSV-Protokoll hat einen unbekannten Tabellenkopf") from None

            def remove(row: list[str]) -> bool:
                if len(row) <= max(i_uuid, i_date, i_action):
                    return False
                if row[i_uuid] in drop:
                    return True
                if cutoff is not None and row[i_date] and row[i_date] < cutoff:
                    return True
                return discarded and row[i_action] == "verworfen"

            kept = [r for r in body if not remove(r)]
            removed = len(body) - len(kept)
            if removed == 0:
                return 0, None
            buf = io.StringIO()
            buf.write("\ufeff")
            writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
            writer.writerow(header)
            writer.writerows(kept)
            fd, tmp = tempfile.mkstemp(prefix=".protokoll-", suffix=".tmp", dir=self.path.parent)
            try:
                os.fchmod(fd, FILE_MODE)
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                    fh.write(buf.getvalue())
                    fh.flush()
                    os.fsync(fh.fileno())
                backup: Path | None = None
                if keep_backup:
                    stamp = (now or _now()).strftime("%Y%m%d-%H%M%S")
                    backup = self.path.with_name(f"{self.path.name}.{stamp}.bak")
                    n = 1
                    while backup.exists():
                        n += 1
                        backup = self.path.with_name(f"{self.path.name}.{stamp}-{n}.bak")
                    os.link(self.path, backup)  # gleicher Inhalt, bleibt nach replace bestehen
                    os.chmod(backup, FILE_MODE)
                os.replace(tmp, self.path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)  # eigene, unvollständige Temp-Datei
                raise
            dfd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
            return removed, backup
