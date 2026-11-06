"""SQLite-Speicher (führend): Schema, Migrationen, Einträge, Audit-Log.

- WAL, ``foreign_keys=ON``, ausschließlich parametrisierte Queries.
- Datei 0600 (inkl. ``-wal``/``-shm``), Verzeichnis 0700.
- Thread-Sicherheit: eine Verbindung, Zugriffe über ein ``RLock`` serialisiert.
- ``PRAGMA secure_delete=ON``: gelöschte Inhalte werden in der Datei mit Nullen
  überschrieben (Löschkonzept).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..config import FILE_MODE, ensure_private_dir, ensure_private_file
from ..core.models import ACTIVE_STATUSES, Entry, EntryStatus, Pause
from ..core.timeutil import from_iso, now as _now, to_iso

log = logging.getLogger(__name__)

__all__ = ["Database", "MIGRATIONS", "SCHEMA_VERSION", "PURGEABLE_STATUSES"]

#: Nur abgeschlossene Einträge sind löschbar – nie der aktive Task.
PURGEABLE_STATUSES = (EntryStatus.STOPPED, EntryStatus.DISCARDED)

# Jede Migration ist eine Liste von SQL-Anweisungen; Index+1 = Zielversion.
MIGRATIONS: list[list[str]] = [
    # v1 – Grundschema (+ Felder für stopping-Zustand, vorbereitete Felder)
    [
        """
        CREATE TABLE entries (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid              TEXT NOT NULL UNIQUE,
            customer          TEXT NOT NULL DEFAULT '',
            topic             TEXT NOT NULL DEFAULT '',
            ticket            TEXT NOT NULL DEFAULT '',
            location          TEXT NOT NULL DEFAULT 'remote'
                              CHECK (location IN ('remote', 'onsite')),
            start_at          TEXT NOT NULL,
            end_at            TEXT,
            pause_seconds     INTEGER NOT NULL DEFAULT 0 CHECK (pause_seconds >= 0),
            description       TEXT NOT NULL DEFAULT '',
            addendum          TEXT NOT NULL DEFAULT '',
            source            TEXT NOT NULL DEFAULT 'gui',
            status            TEXT NOT NULL
                              CHECK (status IN ('running','paused','stopping','stopped','discarded')),
            stop_requested_at TEXT,
            prev_status       TEXT,
            exported_at       TEXT,
            invoice_ref       TEXT,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_entries_start ON entries(start_at)",
        "CREATE INDEX idx_entries_status ON entries(status)",
        """
        CREATE TABLE entry_items (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id  INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
            position  INTEGER NOT NULL,
            text      TEXT NOT NULL,
            UNIQUE (entry_id, position)
        )
        """,
        """
        CREATE TABLE pauses (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id  INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
            start_at  TEXT NOT NULL,
            end_at    TEXT
        )
        """,
        "CREATE INDEX idx_pauses_entry ON pauses(entry_id)",
        """
        CREATE TABLE audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_uuid  TEXT NOT NULL,
            at          TEXT NOT NULL,
            action      TEXT NOT NULL,
            diff_json   TEXT NOT NULL DEFAULT '{}'
        )
        """,
        "CREATE INDEX idx_audit_entry ON audit_log(entry_uuid)",
        """
        CREATE TABLE app_state (
            key    TEXT PRIMARY KEY,
            value  TEXT NOT NULL
        )
        """,
    ],
]
SCHEMA_VERSION = len(MIGRATIONS)

_ENTRY_COLS = (
    "id, uuid, customer, topic, ticket, location, start_at, end_at, pause_seconds, "
    "description, addendum, source, status, stop_requested_at, prev_status, "
    "exported_at, invoice_ref, created_at, updated_at"
)


class Database:
    def __init__(self, path: Path | str, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self._lock = threading.RLock()
        if read_only:
            uri = f"file:{self.path}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            ensure_private_dir(self.path.parent)
            ensure_private_file(self.path)
            self._conn = sqlite3.connect(
                str(self.path), check_same_thread=False, isolation_level=None
            )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if not read_only:
            self._conn.execute("PRAGMA secure_delete = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = FULL")
            self.migrate()
            self._fix_permissions()

    # --- Infrastruktur -------------------------------------------------
    def _fix_permissions(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            try:
                if p.exists() and (p.stat().st_mode & 0o777) != FILE_MODE:
                    os.chmod(p, FILE_MODE)
            except OSError:  # pragma: no cover
                log.warning("Konnte Rechte von %s nicht setzen", p)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if row is None:
                return 0
            row = self._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            return int(row["v"] or 0)

    def migrate(self, migrations: list[list[str]] | None = None) -> int:
        migrations = MIGRATIONS if migrations is None else migrations
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            current = self.schema_version()
            if current > len(migrations):
                raise RuntimeError(
                    f"Datenbank-Schema v{current} ist neuer als die App (v{len(migrations)})"
                )
            for idx in range(current, len(migrations)):
                with self.transaction() as conn:
                    for stmt in migrations[idx]:
                        conn.execute(stmt)
                    conn.execute(
                        "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                        (idx + 1, to_iso(_now())),
                    )
                log.info("Datenbank migriert auf v%d", idx + 1)
            return self.schema_version()

    # --- Einträge ------------------------------------------------------
    def save_entry(self, entry: Entry, conn: sqlite3.Connection | None = None) -> Entry:
        """Eintrag inkl. Pausen und Tätigkeiten speichern (Insert/Update)."""
        if conn is None:
            with self.transaction() as c:
                return self.save_entry(entry, c)
        values = (
            entry.uuid,
            entry.customer,
            entry.topic,
            entry.ticket,
            str(entry.location),
            to_iso(entry.start_at),
            to_iso(entry.end_at),
            int(entry.pause_seconds),
            entry.description,
            entry.addendum,
            str(entry.source),
            str(entry.status),
            to_iso(entry.stop_requested_at),
            entry.prev_status,
            to_iso(entry.exported_at),
            entry.invoice_ref,
            to_iso(entry.created_at),
            to_iso(entry.updated_at),
        )
        if entry.id is None:
            cur = conn.execute(
                "INSERT INTO entries (uuid, customer, topic, ticket, location, start_at, end_at,"
                " pause_seconds, description, addendum, source, status, stop_requested_at,"
                " prev_status, exported_at, invoice_ref, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            entry.id = int(cur.lastrowid)
        else:
            conn.execute(
                "UPDATE entries SET uuid=?, customer=?, topic=?, ticket=?, location=?, start_at=?,"
                " end_at=?, pause_seconds=?, description=?, addendum=?, source=?, status=?,"
                " stop_requested_at=?, prev_status=?, exported_at=?, invoice_ref=?, created_at=?,"
                " updated_at=? WHERE id=?",
                values + (entry.id,),
            )
        for p in entry.pauses:
            if p.id is None:
                cur = conn.execute(
                    "INSERT INTO pauses (entry_id, start_at, end_at) VALUES (?,?,?)",
                    (entry.id, to_iso(p.start_at), to_iso(p.end_at)),
                )
                p.id = int(cur.lastrowid)
            else:
                conn.execute(
                    "UPDATE pauses SET start_at=?, end_at=? WHERE id=? AND entry_id=?",
                    (to_iso(p.start_at), to_iso(p.end_at), p.id, entry.id),
                )
        conn.execute("DELETE FROM entry_items WHERE entry_id=?", (entry.id,))
        conn.executemany(
            "INSERT INTO entry_items (entry_id, position, text) VALUES (?,?,?)",
            [(entry.id, i, t) for i, t in enumerate(entry.items)],
        )
        return entry

    def _row_to_entry(self, row: sqlite3.Row) -> Entry:
        e = Entry(
            id=row["id"],
            uuid=row["uuid"],
            customer=row["customer"],
            topic=row["topic"],
            ticket=row["ticket"],
            location=row["location"],
            start_at=from_iso(row["start_at"]),  # type: ignore[arg-type]
            end_at=from_iso(row["end_at"]),
            pause_seconds=row["pause_seconds"],
            description=row["description"],
            addendum=row["addendum"],
            source=row["source"],
            status=EntryStatus(row["status"]),
            stop_requested_at=from_iso(row["stop_requested_at"]),
            prev_status=row["prev_status"],
            exported_at=from_iso(row["exported_at"]),
            invoice_ref=row["invoice_ref"],
            created_at=from_iso(row["created_at"]),
            updated_at=from_iso(row["updated_at"]),
        )
        e.items = [
            r["text"]
            for r in self._conn.execute(
                "SELECT text FROM entry_items WHERE entry_id=? ORDER BY position", (e.id,)
            )
        ]
        e.pauses = [
            Pause(id=r["id"], start_at=from_iso(r["start_at"]), end_at=from_iso(r["end_at"]))  # type: ignore[arg-type]
            for r in self._conn.execute(
                "SELECT id, start_at, end_at FROM pauses WHERE entry_id=? ORDER BY start_at, id",
                (e.id,),
            )
        ]
        return e

    def get_entry(self, entry_uuid: str) -> Entry | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_ENTRY_COLS} FROM entries WHERE uuid=?", (entry_uuid,)
            ).fetchone()
            return self._row_to_entry(row) if row else None

    def active_entries(self) -> list[Entry]:
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_ENTRY_COLS} FROM entries WHERE status IN ({marks})"
                " ORDER BY start_at DESC, id DESC",
                tuple(str(s) for s in ACTIVE_STATUSES),
            ).fetchall()
            return [self._row_to_entry(r) for r in rows]

    def last_stopped(self) -> Entry | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_ENTRY_COLS} FROM entries WHERE status='stopped'"
                " ORDER BY end_at DESC, id DESC LIMIT 1"
            ).fetchone()
            return self._row_to_entry(row) if row else None

    def list_entries(
        self,
        von: date | None = None,
        bis: date | None = None,
        *,
        statuses: tuple[str, ...] | None = (EntryStatus.STOPPED,),
        customer: str | None = None,
    ) -> list[Entry]:
        """Einträge nach lokalem Startdatum (``von``/``bis`` inklusive)."""
        sql = f"SELECT {_ENTRY_COLS} FROM entries WHERE 1=1"
        params: list[Any] = []
        if von is not None:
            sql += " AND substr(start_at, 1, 10) >= ?"
            params.append(von.isoformat())
        if bis is not None:
            sql += " AND substr(start_at, 1, 10) <= ?"
            params.append(bis.isoformat())
        if statuses:
            sql += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
            params.extend(str(s) for s in statuses)
        if customer is not None:
            sql += " AND customer = ?"
            params.append(customer)
        sql += " ORDER BY start_at, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [self._row_to_entry(r) for r in rows]

    def customers(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT customer FROM entries WHERE customer <> ''"
                " AND status <> 'discarded' ORDER BY customer COLLATE NOCASE"
            ).fetchall()
            return [r["customer"] for r in rows]

    # --- Audit ---------------------------------------------------------
    def add_audit(
        self,
        entry_uuid: str,
        action: str,
        diff: dict[str, Any] | None = None,
        at: datetime | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        c = conn or self._conn
        with self._lock:
            c.execute(
                "INSERT INTO audit_log (entry_uuid, at, action, diff_json) VALUES (?,?,?,?)",
                (
                    entry_uuid,
                    to_iso(at or _now()),
                    action,
                    json.dumps(diff or {}, ensure_ascii=False, sort_keys=True, default=str),
                ),
            )

    def audit_for(self, entry_uuid: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT at, action, diff_json FROM audit_log WHERE entry_uuid=? ORDER BY id",
                (entry_uuid,),
            ).fetchall()
            return [
                {"at": r["at"], "action": r["action"], "diff": json.loads(r["diff_json"])}
                for r in rows
            ]

    # --- App-Zustand (Key/Value) --------------------------------------
    def get_state(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def set_state(
        self, key: str, value: str | None, conn: sqlite3.Connection | None = None
    ) -> None:
        c = conn or self._conn
        with self._lock:
            if value is None:
                c.execute("DELETE FROM app_state WHERE key=?", (key,))
            else:
                c.execute(
                    "INSERT INTO app_state(key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )

    # --- Löschen ----------------------------------------------
    @staticmethod
    def _purge_where(before: date | None, discarded: bool) -> tuple[str, list[Any]]:
        if before is None and not discarded:
            raise ValueError("Löschen braucht ein Datum oder 'verworfene'")
        conds: list[str] = []
        params: list[Any] = []
        if before is not None:
            conds.append("substr(start_at, 1, 10) < ?")
            params.append(before.isoformat())
        if discarded:
            conds.append("status = ?")
            params.append(str(EntryStatus.DISCARDED))
        marks = ",".join("?" for _ in PURGEABLE_STATUSES)
        sql = f"status IN ({marks}) AND (" + " OR ".join(conds) + ")"
        return sql, [str(x) for x in PURGEABLE_STATUSES] + params

    def purgeable_uuids(self, before: date | None = None, discarded: bool = False) -> list[str]:
        """UUIDs der Einträge, die :meth:`purge` löschen würde (Vorschau)."""
        where, params = self._purge_where(before, discarded)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT uuid FROM entries WHERE {where} ORDER BY start_at, id", params
            ).fetchall()
            return [r["uuid"] for r in rows]

    def existing_uuids(self, uuids: Iterable[str]) -> set[str]:
        """Teilmenge von ``uuids``, zu der noch ein Eintrag existiert."""
        wanted = list(dict.fromkeys(uuids))
        found: set[str] = set()
        with self._lock:
            for i in range(0, len(wanted), 500):  # SQLite-Grenze für Platzhalter
                chunk = wanted[i:i + 500]
                marks = ",".join("?" * len(chunk))
                found.update(r["uuid"] for r in self._conn.execute(
                    f"SELECT uuid FROM entries WHERE uuid IN ({marks})", chunk))
        return found

    def purge(
        self, before: date | None = None, discarded: bool = False, at: datetime | None = None
    ) -> dict[str, Any]:
        """Abgeschlossene Einträge endgültig löschen (eine Transaktion).

        - ``before``: Einträge mit lokalem Startdatum < ``before`` (gespeichert + verworfen)
        - ``discarded``: zusätzlich alle verworfenen Einträge
        - aktive Einträge (running/paused/stopping) werden nie gelöscht
        - ``entry_items``/``pauses`` per ``ON DELETE CASCADE``; ``audit_log`` der
          gelöschten Einträge sowie verwaiste Audit-Zeilen vor ``before``
        - protokolliert selbst eine Audit-Zeile ``purge`` (nur Anzahl/Datum,
          keine Inhalte)

        Rückgabe ``{"uuids": [...], "entries": n, "audit": m}``. Danach
        :meth:`vacuum` aufrufen, damit Reste aus Datei und WAL verschwinden.
        """
        where, params = self._purge_where(before, discarded)
        with self.transaction() as conn:
            uuids = [
                r["uuid"]
                for r in conn.execute(f"SELECT uuid FROM entries WHERE {where}", params)
            ]
            audit = 0
            for u in uuids:
                audit += conn.execute("DELETE FROM audit_log WHERE entry_uuid=?", (u,)).rowcount
            cur = conn.execute(f"DELETE FROM entries WHERE {where}", params)
            entries = cur.rowcount
            if before is not None:
                audit += conn.execute(
                    "DELETE FROM audit_log WHERE substr(at, 1, 10) < ?"
                    " AND entry_uuid NOT IN (SELECT uuid FROM entries)",
                    (before.isoformat(),),
                ).rowcount
            self.add_audit(
                "",
                "purge",
                {
                    "before": before.isoformat() if before else None,
                    "discarded": bool(discarded),
                    "entries": entries,
                    "audit": audit,
                },
                at=at,
                conn=conn,
            )
        return {"uuids": uuids, "entries": entries, "audit": audit}

    def vacuum(self) -> None:
        """WAL zurückschreiben, Datei neu aufbauen (``VACUUM``), WAL leeren."""
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.execute("VACUUM")
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._fix_permissions()

    # --- Sicherung --------------------------------------------
    def backup_to(self, dest: Path | str) -> Path:
        """Konsistente Kopie über die SQLite-Backup-API (auch bei laufender App).

        Ziel darf nicht existieren; wird mit 0600 angelegt und als eigenständige
        Datei (``journal_mode=DELETE``, ohne ``-wal``) geschrieben.
        """
        dest = Path(dest)
        fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE)
        os.close(fd)
        target = sqlite3.connect(str(dest), isolation_level=None)
        try:
            with self._lock:
                self._conn.backup(target)
            target.execute("PRAGMA journal_mode = DELETE")
            ok = target.execute("PRAGMA quick_check").fetchone()[0]
            if ok != "ok":
                raise sqlite3.DatabaseError("Sicherung fehlerhaft (quick_check)")
        finally:
            target.close()
        if (dest.stat().st_mode & 0o777) != FILE_MODE:
            os.chmod(dest, FILE_MODE)
        return dest
