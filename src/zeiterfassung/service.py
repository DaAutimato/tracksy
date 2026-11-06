"""Fassade ``TrackerService`` – einzige Schnittstelle der UI/IPC zum Kern.

GUI-frei (kein GLib/Gtk-Import). Alle Methoden sind thread-sicher (RLock);
Observer werden synchron im aufrufenden Thread benachrichtigt – die GTK-UI
muss Aufrufe aus Fremd-Threads selbst per ``GLib.idle_add`` in den
Main-Loop holen.

"""

from __future__ import annotations

import copy
import json
import logging
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import Config, Paths, load_config
from .core import state as sm
from .core.models import (
    Entry,
    EntryStatus,
    Location,
    Pause,
    StartRequest,
    State,
    normalize_location,
    normalize_source,
)
from .core.sanitize import (
    MAX_FIELD_LEN,
    MAX_ITEM_LEN,
    MAX_ITEMS,
    MAX_TEXT_LEN,
    InputLimitError,
    clean_items,
    clean_text,
)
from .core.status import build_status, state_of, status_to_json
from .core.timeutil import ensure_aware, from_iso, now as _now, to_iso
from .storage.csvlog import CsvProtocol
from .storage.db import Database
from .storage.maintenance import (
    BackupCleanup,
    InternalBackup,
    PurgeResult,
    create_backup,
    find_internal_backups,
    purge,
    remove_internal_backups,
    resume_pending_purge,
    retention_cutoff,
)
from .logutil import log_failure

log = logging.getLogger(__name__)

__all__ = [
    "TrackerService",
    "EntryNotFound",
    "InvalidTimeError",
    "PendingStartError",
    "InvalidTransition",
    "FUTURE_TOLERANCE",
    "HEARTBEAT_INTERVAL_SECONDS",
    "GAP_PROMPT_THRESHOLD_SECONDS",
    "Observer",
    "START_STARTED",
    "START_PENDING",
    "InputLimitError",
    "PurgeResult",
    "MAX_FIELD_LEN",
    "MAX_TEXT_LEN",
    "MAX_ITEMS",
    "MAX_ITEM_LEN",
]

InvalidTransition = sm.InvalidTransition
Observer = Callable[[str, dict[str, Any]], None]

START_STARTED = "started"
START_PENDING = "pending"

_PENDING_KEY = "pending_start"
_HEARTBEAT_KEY = "heartbeat"
#: Offene, noch nicht beantwortete Ausfallzeit (JSON: uuid + Segmente)
_GAP_KEY = "open_gap"
#: Merker „App wurde bewusst beendet“ (Beenden-Aktion/-Dialog, ``zeit quit``)
_CLEAN_KEY = "clean_shutdown"
#: Abstand des App-Timers für ``TrackerService.heartbeat()``
HEARTBEAT_INTERVAL_SECONDS = 60
#: Ab dieser Ausfallzeit (strikt größer) fragt die App nach dem Neustart, ob die
#: Zeit abgezogen werden soll; darunter zählt sie still mit. Bewusst
#: keine Nutzer-Einstellung.
GAP_PROMPT_THRESHOLD_SECONDS = 30 * 60
_EDITABLE = (
    "customer",
    "topic",
    "ticket",
    "location",
    "description",
    "addendum",
    "items",
    "start_at",
    "end_at",
    "pause_seconds",
)
_EDITABLE_ACTIVE = ("customer", "topic", "ticket", "location", "description", "start_at")


class EntryNotFound(LookupError):
    pass


class PendingStartError(RuntimeError):
    """Stopp/Verwerfen ist gespeichert, der vorgemerkte Start (pending_start)
    aber gescheitert. Ursache in ``__cause__``; Meldung deutsch."""


class InvalidTimeError(ValueError):
    """Unplausible Zeitangaben: Ende vor Beginn, Zeitpunkt in der Zukunft,
    Pause länger als Dauer. Meldung deutsch, für Toast/Editor geeignet."""


#: Toleranz für Zeitpunkte in der Zukunft (Uhrabweichung, Eingabe "gleich")
FUTURE_TOLERANCE = timedelta(minutes=5)


_LONG_TEXT_FIELDS = ("description", "addendum")


def _text(v: Any, field: str) -> str:
    """Textfeld prüfen (Typ + Länge) und trimmen."""
    if field in _LONG_TEXT_FIELDS:
        return clean_text(v, field, MAX_TEXT_LEN, multiline=True)
    return clean_text(v, field, MAX_FIELD_LEN)


def _location(v: Any, default: str) -> str:
    return normalize_location(clean_text(v, "location"), default)


def _source(v: Any) -> str:
    return normalize_source(clean_text(v, "source"))


def _dt(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return ensure_aware(v)
    return from_iso(str(v))


class TrackerService:
    def __init__(
        self,
        db: Database,
        csv: CsvProtocol,
        config: Config | None = None,
        clock: Callable[[], datetime] = _now,
        paths: Paths | None = None,
    ):
        self.db = db
        self.csv = csv
        self.config = config or Config()
        self.paths = paths  # nur für backup() (config.toml); None in Tests
        self._clock = clock
        self._lock = threading.RLock()
        self._observers: list[Observer] = []
        self._last_event = "init"
        self._active: Entry | None = None
        self._pending: StartRequest | None = None
        self._recovered = False
        self.closed = False  # True nach relocate_data: Datenbank geschlossen, Neustart nötig
        # Ausfallzeit nach Absturz: (Eintrag-UUID, [(Beginn, Sekunden), …]);
        # in ``app_state`` gespiegelt, bis beantwortet oder der Task endet
        self._gap: tuple[str, list[tuple[datetime, int]]] | None = None
        self.clean_shutdown_detected = False
        self._recover()

    # --- Konstruktion -------------------------------------------------
    @classmethod
    def open(cls, paths: Paths | None = None, config: Config | None = None) -> "TrackerService":
        paths = (paths or Paths.resolve()).ensure()
        cfg = config or load_config(paths.config_file)
        return cls(Database(paths.db_file), CsvProtocol(paths.csv_file), cfg, paths=paths)

    def close(self) -> None:
        self.db.close()

    def _now(self) -> datetime:
        return ensure_aware(self._clock())

    def _recover(self) -> None:
        active = self.db.active_entries()
        if active:
            self._active = active[0]
            self._recovered = True
            if len(active) > 1:
                log.warning(
                    "%d offene Einträge gefunden, verwende den neuesten (%s)",
                    len(active),
                    self._active.uuid,
                )
            self._load_gap(self._active)
            clean = self._take_clean_marker()
            new = None if clean else self._compute_gap(self._active)
            if new is not None:
                self._gap = (self._active.uuid, (self._gap[1] if self._gap else []) + [new])
            self._save_gap()
            gap = self._pending_gap()
            self.db.add_audit(self._active.uuid, "recover", {
                "status": str(self._active.status),
                "gap_seconds": new[1] if new else 0,
                "open_gap_seconds": gap[2] if gap else 0,
                "clean_shutdown": clean,
            })
            self._last_event = "recovered"
        else:
            self._take_clean_marker()
            if self.db.get_state(_GAP_KEY) is not None:
                self.db.set_state(_GAP_KEY, None)
        self.heartbeat()
        raw = self.db.get_state(_PENDING_KEY)
        if raw:
            try:
                self._pending = StartRequest.from_dict(json.loads(raw))
            except (ValueError, TypeError):
                log.warning("Ungültiger pending_start verworfen")
                self.db.set_state(_PENDING_KEY, None)
        if self._pending is not None and (
            self._active is None or self._active.status != EntryStatus.STOPPING
        ):
            # pending_start ohne offenen Stopp ist bedeutungslos
            self._pending = None
            self.db.set_state(_PENDING_KEY, None)

    def _take_clean_marker(self) -> bool:
        """Merker „bewusst beendet“ lesen und löschen (gilt nur für einen Start)."""
        raw = self.db.get_state(_CLEAN_KEY)
        if raw is None:
            return False
        self.db.set_state(_CLEAN_KEY, None)
        self.clean_shutdown_detected = True
        return True

    def _load_gap(self, entry: Entry) -> None:
        """Offene Ausfallzeit aus einem früheren Start übernehmen (nur derselbe Eintrag)."""
        raw = self.db.get_state(_GAP_KEY)
        if not raw:
            return
        try:
            data = json.loads(raw)
            segs = [(ensure_aware(from_iso(a)), int(b)) for a, b in data["segments"]]
            if data["uuid"] == entry.uuid and segs and all(b > 0 for _a, b in segs):
                self._gap = (entry.uuid, segs)
        except (ValueError, TypeError, KeyError):
            log.warning("Ungültige offene Ausfallzeit verworfen")

    def _save_gap(self, conn=None) -> None:
        value = None
        if self._gap is not None:
            value = json.dumps({"uuid": self._gap[0],
                                "segments": [[to_iso(a), b] for a, b in self._gap[1]]})
        if value != self.db.get_state(_GAP_KEY):
            self.db.set_state(_GAP_KEY, value, conn=conn)

    def _compute_gap(self, entry: Entry) -> tuple[datetime, int] | None:
        """Neue Ausfallzeit = jetzt − letztes Lebenszeichen (Heartbeat bzw. letzte
        Änderung des Eintrags). Nur im Zustand running relevant: pausierte
        Zeit zählt ohnehin nicht, bei stopping ist das Ende eingefroren."""
        if entry.status != EntryStatus.RUNNING:
            return None
        candidates = [entry.start_at, entry.updated_at]
        raw = self.db.get_state(_HEARTBEAT_KEY)
        if raw:
            try:
                candidates.append(from_iso(raw))
            except (ValueError, TypeError):
                log.warning("Ungültiger Heartbeat-Wert ignoriert")
        candidates += [p.end_at for p in entry.pauses]
        if self._gap is not None:  # Ende einer früheren offenen Lücke
            candidates += [a + timedelta(seconds=b) for a, b in self._gap[1]]
        last_alive = max(ensure_aware(c) for c in candidates if c is not None)
        seconds = int((self._now() - last_alive).total_seconds())
        return (last_alive, seconds) if seconds > 0 else None

    def _pending_gap(self) -> tuple[str, datetime, int] | None:
        """(UUID, Beginn der ersten Lücke, Summe Sekunden) oder ``None``."""
        segs = self._pending_gap_segments()
        if not segs:
            return None
        return (self._gap[0], segs[0][0], sum(b for _a, b in segs))  # type: ignore[index]

    def _pending_gap_segments(self) -> list[tuple[datetime, int]]:
        if self._gap is None or self._active is None or self._active.uuid != self._gap[0]:
            return []
        return list(self._gap[1])

    # --- Lebenszeichen / Ausfallzeit ---------------------------
    def heartbeat(self) -> None:
        """Lebenszeichen in ``app_state`` schreiben (App ruft das per Timer alle
        ``HEARTBEAT_INTERVAL_SECONDS`` auf). Grundlage für ``recovered_gap_seconds``
        nach einem Absturz. Kein Observer-Ereignis.

        Schreibt nur, solange ein Task läuft (``running``): Pause und ``stopping``
        zählen ohnehin nicht weiter, im Leerlauf kein Schreibzugriff."""
        with self._lock:
            if (self.closed or self._active is None
                    or self._active.status != EntryStatus.RUNNING):
                return
            try:
                self.db.set_state(_HEARTBEAT_KEY, to_iso(self._now()))
            except Exception:  # Lebenszeichen darf nie die App stören
                log_failure(log, "Heartbeat konnte nicht geschrieben werden")

    def subtract_gap(self, seconds: int | None = None) -> Entry:
        """Ausfallzeit nach Absturz als Pause verbuchen (Ereignis ``gap_subtracted``).

        ``seconds``: ``None`` = gesamte Ausfallzeit, sonst 1..``recovered_gap_seconds``.
        Die Pause beginnt beim letzten Lebenszeichen. Ohne offene Ausfallzeit →
        ``ValueError``; ungültige Sekunden → ``InvalidTimeError``.
        """
        with self._lock:
            with self._rollback_on_error():
                gap = self._pending_gap()
                if gap is None:
                    raise ValueError("Keine Ausfallzeit zum Abziehen vorhanden")
                uuid, since, total = gap
                segments = self._pending_gap_segments()
                if seconds is None:
                    secs = total
                else:
                    if isinstance(seconds, bool) or not isinstance(seconds, int):
                        raise InvalidTimeError("Ausfallzeit: ungültiger Wert")
                    secs = seconds
                if not 0 < secs <= total:
                    raise InvalidTimeError(f"Ausfallzeit muss zwischen 1 und {total} Sekunden liegen")
                e = self._active
                assert e is not None
                rest = secs
                for seg_start, seg_secs in segments:  # bei mehreren Ausfällen je Lücke eine Pause
                    take = min(rest, seg_secs)
                    e.pauses.append(Pause(start_at=seg_start, end_at=seg_start + timedelta(seconds=take)))
                    rest -= take
                    if rest <= 0:
                        break
                e.pauses.sort(key=lambda p: p.start_at)
                e.pause_seconds = sum(p.seconds() for p in e.pauses if p.end_at is not None)
                e.updated_at = self._now()
                self._gap = None
                self._persist(e, "gap_subtracted", {"seconds": secs, "since": to_iso(since)})
        self._notify("gap_subtracted")
        return e

    def keep_gap(self, automatic: bool = False) -> None:
        """Ausfallzeit mitzählen lassen (Audit ``gap_kept``).

        ``automatic=True``: ohne Rückfrage übernommen (Ausfall unter
        ``GAP_PROMPT_THRESHOLD_SECONDS``), im Audit als ``automatic`` vermerkt."""
        with self._lock:
            with self._rollback_on_error():
                gap = self._pending_gap()
                if gap is None:
                    return
                self._gap = None
                diff: dict[str, Any] = {"seconds": gap[2]}
                if automatic:
                    diff["automatic"] = True
                with self.db.transaction() as conn:
                    self.db.add_audit(gap[0], "gap_kept", diff, at=self._now(), conn=conn)
                    self._save_gap(conn)
        self._notify("gap_kept")

    def mark_clean_shutdown(self) -> None:
        """Bewusstes Beenden (Beenden-Aktion/-Dialog, ``zeit quit``): letztes
        Lebenszeichen und Merker schreiben. Beim nächsten Start gilt die Zeit bis
        dahin wie angekündigt als Arbeitszeit, ohne Ausfallzeit-Rückfrage.
        Schreibt nur, wenn ein Task läuft."""
        with self._lock:
            if (self.closed or self._active is None
                    or self._active.status != EntryStatus.RUNNING):
                return
            try:
                now = to_iso(self._now())
                with self.db.transaction() as conn:
                    self.db.set_state(_HEARTBEAT_KEY, now, conn=conn)
                    self.db.set_state(_CLEAN_KEY, now, conn=conn)
            except Exception:
                log_failure(log, "Merker „sauber beendet“ konnte nicht geschrieben werden")

    # --- Ruhezustand (Standby) -----------------------------------------
    def pause_at(self, at: datetime, reason: str = "sleep") -> bool:
        """Laufenden Task rückwirkend ab ``at`` pausieren (z. B. Zeitpunkt des
        Einschlafens). ``at`` wird auf [letzter Beginn/Pausenende, jetzt] begrenzt.
        Rückgabe ``False``, wenn kein Task läuft (nichts geändert)."""
        with self._lock:
            with self._rollback_on_error():
                e = self._active
                if self.closed or e is None or e.status != EntryStatus.RUNNING:
                    return False
                now = self._now()
                floor = max([ensure_aware(e.start_at)]
                            + [ensure_aware(p.end_at) for p in e.pauses if p.end_at is not None])
                when = min(max(ensure_aware(at), floor), now)
                self._clear_recovered()
                sm.pause(e, when)
                e.updated_at = now
                self._persist(e, "pause", {"at": to_iso(when), "reason": reason})
        self._notify("paused")
        return True

    def request_stop_at_pause_start(self) -> Entry:
        """Pausierten Task mit Ende = Beginn der offenen Pause stoppen (Ergänzungsdialog
        folgt wie bei ``request_stop``). Für „Beenden“ nach dem Ruhezustand."""
        with self._lock:
            with self._rollback_on_error():
                self._clear_recovered()
                e = self._require_active("request_stop")
                op = e.open_pause
                if e.status != EntryStatus.PAUSED or op is None:
                    raise sm.InvalidTransition("request_stop_at_pause_start", e.status)
                sm.request_stop(e, op.start_at)
                e.updated_at = self._now()
                self._persist(e, "stop_requested", {"end_at": to_iso(e.end_at),
                                                    "reason": "pause_start"})
        self._notify("stop_requested")
        return e

    # --- Observer -----------------------------------------------------
    def subscribe(self, callback: Observer) -> Callable[[], None]:
        """``callback(event, status_dict)`` registrieren; liefert Abmeldefunktion."""
        with self._lock:
            self._observers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._observers:
                    self._observers.remove(callback)

        return unsubscribe

    def _notify(self, event: str) -> None:
        with self._lock:
            self._last_event = event
            observers = list(self._observers)
            status = self.status()
        for cb in observers:
            try:
                cb(event, status)
            except Exception:  # Observer-Fehler dürfen den Kern nicht stören
                log_failure(log, f"Observer-Fehler bei Ereignis {event}")

    # --- Status -------------------------------------------------------
    @property
    def state(self) -> State:
        with self._lock:
            return state_of(self._active)

    @property
    def pending_start(self) -> StartRequest | None:
        return self._pending

    def status(self) -> dict[str, Any]:
        with self._lock:
            st = build_status(
                self._active,
                self._now(),
                pending_start=self._pending,
                last_stopped=self.db.last_stopped(),
                recovered=self._recovered,
            )
            gap = self._pending_gap()
            st["recovered_gap_seconds"] = gap[2] if gap else 0
            st["recovered_gap_since"] = to_iso(gap[1]) if gap else None
            st["recovered_gaps"] = [[to_iso(a), b] for a, b in self._pending_gap_segments()]
            st["event"] = self._last_event
            return st

    def status_json(self) -> str:
        return status_to_json(self.status())

    def active_entry(self) -> Entry | None:
        with self._lock:
            return self._active

    # --- Persistenz-Helfer --------------------------------------------
    def _persist(
        self,
        entry: Entry,
        action: str,
        diff: dict[str, Any] | None = None,
        csv_action: str | None = None,
    ) -> None:
        with self.db.transaction() as conn:
            self.db.save_entry(entry, conn)
            self.db.add_audit(entry.uuid, action, diff, at=self._now(), conn=conn)
            if self._gap is not None and self._gap[0] == entry.uuid and not entry.is_active:
                self._gap = None  # Task beendet/verworfen: offene Lücke erledigt
            self._save_gap(conn)  # schreibt nur bei Änderung
            self.db.set_state(
                _PENDING_KEY,
                json.dumps(self._pending.to_dict()) if self._pending else None,
                conn=conn,
            )
        if csv_action:
            # Nach dem Commit darf nichts mehr scheitern: sonst setzt _rollback_on_error
            # den Speicherzustand zurück, obwohl die DB den neuen Stand hat
            try:
                self.csv.append(csv_action, entry, at=self._now())
            except Exception:
                log_failure(log, f"CSV-Protokoll konnte nicht geschrieben werden ({entry.uuid})",
                            level=logging.ERROR)

    def _snapshot(self) -> tuple:
        e = self._active
        gap = (self._gap[0], list(self._gap[1])) if self._gap is not None else None
        return (e, copy.deepcopy(e.__dict__) if e is not None else None,
                self._pending, self._recovered, gap)

    def _restore(self, snap: tuple) -> None:
        e, fields, self._pending, self._recovered, self._gap = snap
        if e is not None:  # auf demselben Objekt: die UI hält Referenzen
            e.__dict__.clear()
            e.__dict__.update(fields)
        self._active = e

    @contextmanager
    def _rollback_on_error(self) -> Iterator[None]:
        """Speicherzustand zurücksetzen, wenn die Aktion scheitert (z. B. SQLite-
        Fehler im Commit). Die DB-Transaktion rollt ``Database.transaction`` zurück;
        Observer werden nur nach Erfolg benachrichtigt. Nur unter ``self._lock``."""
        snap = self._snapshot()
        try:
            yield
        except BaseException:
            self._restore(snap)
            raise

    def _start_pending(self, pending: StartRequest) -> None:
        """Vorgemerkten Start nach gespeichertem Stopp/Verwerfen ausführen."""
        with self._lock:
            try:
                with self._rollback_on_error():
                    self._start_now(pending)
            except Exception as exc:
                log_failure(log, "Vorgemerkter Start nach dem Stopp fehlgeschlagen",
                            level=logging.ERROR)
                raise PendingStartError(
                    "Der Task wurde gespeichert, der neue Task konnte aber nicht "
                    f"gestartet werden: {exc}") from exc
        self._notify("started")

    def _require_active(self, action: str) -> Entry:
        if self._active is None:
            raise sm.InvalidTransition(action, None)
        return self._active

    def _clear_recovered(self) -> None:
        self._recovered = False

    # --- Aktionen -----------------------------------------------------
    def start(
        self,
        customer: str | None = "",
        topic: str | None = "",
        ticket: str | None = "",
        location: str | None = None,
        source: str | None = "gui",
    ) -> str:
        """Task starten.

        Rückgabe ``"started"`` oder ``"pending"``: Lief bereits ein Task, wird
        dessen Stopp ausgelöst (Zustand ``stopping``, UI zeigt Ergänzungsdialog)
        und der neue Start als ``pending_start`` vorgemerkt; er wird nach
        ``confirm_stop()``/``discard()`` automatisch gestartet und bei
        ``cancel_stop()`` verworfen.
        """
        now = self._now()
        req = StartRequest(
            customer=_text(customer, "customer"),
            topic=_text(topic, "topic"),
            ticket=_text(ticket, "ticket"),
            location=_location(location, self.config.general.default_location),
            source=_source(source),
            requested_at=now,
        )
        with self._lock:
            with self._rollback_on_error():
                self._clear_recovered()
                if self._active is None:
                    self._start_now(req)
                    event, result = "started", START_STARTED
                else:
                    self._pending = req
                    if self._active.status != EntryStatus.STOPPING:
                        sm.request_stop(self._active, now)
                        self._persist(self._active, "stop_requested", {"reason": "start"})
                    else:
                        self.db.set_state(_PENDING_KEY, json.dumps(req.to_dict()))
                    event, result = "stop_requested", START_PENDING
        self._notify(event)
        return result

    def _start_now(self, req: StartRequest) -> Entry:
        entry = sm.new_entry(req, self._now())
        self._active = entry
        self._pending = None
        self._persist(entry, "start", {"customer": entry.customer, "topic": entry.topic,
                                       "ticket": entry.ticket, "location": str(entry.location),
                                       "source": str(entry.source)})
        return entry

    def pause(self) -> None:
        with self._lock:
            with self._rollback_on_error():
                self._clear_recovered()
                e = self._require_active("pause")
                sm.pause(e, self._now())
                self._persist(e, "pause")
        self._notify("paused")

    def resume(self) -> None:
        with self._lock:
            with self._rollback_on_error():
                self._clear_recovered()
                e = self._require_active("resume")
                sm.resume(e, self._now())
                self._persist(e, "resume")
        self._notify("resumed")

    def toggle_pause(self) -> str:
        """Pause ⇄ Weiter. Rückgabe ``"paused"`` oder ``"resumed"``."""
        with self._lock:
            paused = self._require_active("toggle_pause").status == EntryStatus.PAUSED
        if paused:
            self.resume()
            return "resumed"
        self.pause()
        return "paused"

    def request_stop(self) -> Entry:
        """Stopp auslösen (Stoppzeit = jetzt). Idempotent im Zustand stopping."""
        with self._lock:
            with self._rollback_on_error():
                self._clear_recovered()
                e = self._require_active("request_stop")
                if e.status == EntryStatus.STOPPING:
                    return e
                sm.request_stop(e, self._now())
                self._persist(e, "stop_requested")
        self._notify("stop_requested")
        return e

    def cancel_stop(self) -> Entry:
        """„Weiterlaufen lassen“: Stopp abbrechen; ein pending_start entfällt."""
        with self._lock:
            with self._rollback_on_error():
                self._clear_recovered()
                e = self._require_active("cancel_stop")
                sm.cancel_stop(e, self._now())
                self._pending = None
                self._persist(e, "stop_cancelled")
        self._notify("stop_cancelled")
        return e

    def confirm_stop(self, addendum: str = "", items: list[str] | None = None) -> Entry:
        """Speichern: Eintrag abschließen, CSV ``neu``; danach ggf. pending_start.

        Limits: ``addendum`` ≤ ``MAX_TEXT_LEN``, ``items`` ≤ ``MAX_ITEMS`` × ``MAX_ITEM_LEN``
        (sonst ``InputLimitError``, Zustand bleibt stopping).
        """
        addendum = _text(addendum, "addendum")
        items = clean_items(items)
        with self._lock:
            with self._rollback_on_error():
                self._clear_recovered()
                e = self._require_active("confirm_stop")
                sm.confirm_stop(e, self._now(), addendum, items)
                pending = self._pending
                self._pending = None
                self._active = None
                self._persist(
                    e, "stop",
                    {"end_at": e.end_at.isoformat() if e.end_at else None,
                     "pause_seconds": e.pause_seconds, "addendum": e.addendum, "items": e.items},
                    csv_action="neu",
                )
        self._notify("stopped")
        if pending is not None:
            self._start_pending(pending)
        return e

    def discard(self) -> Entry:
        """Verwerfen (Rückfrage macht die UI). CSV ``verworfen``; danach ggf. pending_start."""
        with self._lock:
            with self._rollback_on_error():
                self._clear_recovered()
                e = self._require_active("discard")
                sm.discard(e, self._now())
                pending = self._pending
                self._pending = None
                self._active = None
                self._persist(e, "discard", csv_action="verworfen")
        self._notify("discarded")
        if pending is not None:
            self._start_pending(pending)
        return e

    def toggle(self) -> str:
        """Hotkey-Toggle.

        Rückgabe: ``"stop_requested"`` (lief/pausierte), ``"stopping"`` (Dialog
        bereits offen, nichts geändert) oder ``"idle"`` (nichts läuft – die UI
        entscheidet, z. B. Schnellerfassung öffnen).
        """
        with self._lock:
            if self._active is None:
                return "idle"
            if self._active.status == EntryStatus.STOPPING:
                return "stopping"
        self.request_stop()
        return "stop_requested"

    # --- Einträge lesen/bearbeiten ------------------------------------
    def get_entry(self, entry_uuid: str) -> Entry:
        e = self.db.get_entry(entry_uuid)
        if e is None:
            raise EntryNotFound(entry_uuid)
        return e

    def list_entries(
        self,
        von: date | None = None,
        bis: date | None = None,
        *,
        customer: str | None = None,
        include_discarded: bool = False,
        include_active: bool = False,
    ) -> list[Entry]:
        statuses: list[str] = [EntryStatus.STOPPED]
        if include_discarded:
            statuses.append(EntryStatus.DISCARDED)
        if include_active:
            statuses += [EntryStatus.RUNNING, EntryStatus.PAUSED, EntryStatus.STOPPING]
        return self.db.list_entries(von, bis, statuses=tuple(statuses), customer=customer)

    def customers(self) -> list[str]:
        return self.db.customers()

    def summary(self, von: date | None = None, bis: date | None = None) -> list[dict[str, Any]]:
        """Summen je Kunde (gespeicherte Einträge, Rohzeiten, ungerundet)."""
        sums: dict[str, dict[str, Any]] = {}
        for e in self.list_entries(von, bis):
            s = sums.setdefault(e.customer, {"customer": e.customer, "seconds": 0, "count": 0,
                                             "remote_seconds": 0, "onsite_seconds": 0})
            net = e.net_seconds()
            s["seconds"] += net
            s["count"] += 1
            s[f"{e.location}_seconds"] += net
        return sorted(sums.values(), key=lambda d: d["customer"].lower())

    def _apply_changes(self, e: Entry, changes: dict[str, Any], allowed: tuple[str, ...]) -> dict:
        unknown = set(changes) - set(allowed)
        if unknown:
            raise ValueError(f"Nicht änderbare Felder: {', '.join(sorted(unknown))}")
        diff: dict[str, list[Any]] = {}
        for key, value in changes.items():
            old = getattr(e, key)
            if key in ("customer", "topic", "ticket", "description", "addendum"):
                new: Any = _text(value, key)
            elif key == "location":
                new = _location(value, self.config.general.default_location)
            elif key == "items":
                new = clean_items(value)
            elif key in ("start_at", "end_at"):
                new = _dt(value)
                if key == "start_at" and new is None:
                    raise ValueError("start_at darf nicht leer sein")
            else:  # pause_seconds
                new = int(value or 0)
                if new < 0:
                    raise ValueError("pause_seconds darf nicht negativ sein")
            if new != old:
                setattr(e, key, new)
                diff[key] = [
                    old.isoformat() if isinstance(old, datetime) else old,
                    new.isoformat() if isinstance(new, datetime) else new,
                ]
        return diff

    @staticmethod
    def _validate_times(e: Entry, now: datetime) -> None:
        """Plausibilität. Aktive Einträge: Beginn ≤ jetzt; gespeicherte:
        Beginn/Ende höchstens ``FUTURE_TOLERANCE`` in der Zukunft, Ende ≥ Beginn."""
        if e.end_at is not None:
            if e.start_at > now + FUTURE_TOLERANCE:
                raise InvalidTimeError("Beginn liegt in der Zukunft")
            if e.end_at < e.start_at:
                raise InvalidTimeError("Ende liegt vor Beginn")
            if e.end_at > now + FUTURE_TOLERANCE:
                raise InvalidTimeError("Ende liegt in der Zukunft")
            if e.total_pause_seconds() > (e.end_at - e.start_at).total_seconds():
                raise InvalidTimeError("Pause ist länger als die Gesamtdauer")
        elif e.start_at > now:
            raise InvalidTimeError("Beginn liegt in der Zukunft")

    def update_entry(self, entry_uuid: str, **changes: Any) -> Entry:
        """Felder ändern (Audit ``update``).

        Gespeicherte Einträge: alle Felder aus ``_EDITABLE``, CSV ``korrektur``.
        Aktiver Eintrag: nur customer/topic/ticket/location/description/start_at,
        kein CSV-Eintrag. Verworfene Einträge sind nicht änderbar.
        """
        with self._lock:
            now = self._now()
            active = self._active is not None and self._active.uuid == entry_uuid
            e = self._active if active else self.get_entry(entry_uuid)
            assert e is not None
            if e.status == EntryStatus.DISCARDED:
                raise ValueError("Verworfene Einträge können nicht geändert werden")
            backup = copy.deepcopy(e.__dict__)
            try:
                diff = self._apply_changes(e, changes, _EDITABLE_ACTIVE if active else _EDITABLE)
                self._validate_times(e, now)
                if not diff:
                    return e
                e.updated_at = now
                self._persist(e, "update", diff, csv_action=None if active else "korrektur")
            except BaseException:  # Validierung oder Speicherfehler: Objekt wie vorher
                e.__dict__.clear()
                e.__dict__.update(backup)
                raise
        self._notify("entry_updated")
        return e

    def create_entry(
        self,
        start_at: datetime | str,
        end_at: datetime | str,
        customer: str = "",
        topic: str = "",
        ticket: str = "",
        location: str | None = None,
        pause_seconds: int = 0,
        description: str = "",
        addendum: str = "",
        items: list[str] | None = None,
        source: str = "gui",
    ) -> Entry:
        """Manuell angelegten, abgeschlossenen Eintrag speichern (Audit ``create``, CSV ``neu``)."""
        now = self._now()
        start = _dt(start_at)
        end = _dt(end_at)
        if start is None or end is None:
            raise InvalidTimeError("Beginn und Ende sind Pflicht")
        req = StartRequest(
            customer=_text(customer, "customer"), topic=_text(topic, "topic"),
            ticket=_text(ticket, "ticket"),
            location=_location(location, self.config.general.default_location),
            source=_source(source), requested_at=start,
        )
        description = _text(description, "description")
        addendum = _text(addendum, "addendum")
        items = clean_items(items)
        e = sm.new_entry(req, now)
        e.end_at = end
        e.status = EntryStatus.STOPPED
        e.pause_seconds = int(pause_seconds or 0)
        if e.pause_seconds < 0:
            raise ValueError("pause_seconds darf nicht negativ sein")
        e.description = description
        e.addendum = addendum
        e.items = items
        self._validate_times(e, now)
        with self._lock:
            self._persist(e, "create", {"manual": True}, csv_action="neu")
        self._notify("entry_created")
        return e

    # --- Löschen / Sichern ----------------------------
    def purge_preview(self, before: date | None = None, discarded: bool = False) -> int:
        """Anzahl der Einträge, die ``purge_before``/``purge_discarded`` löschen würden."""
        with self._lock:
            return len(self.db.purgeable_uuids(before, discarded))

    def _purge(self, before: date | None, discarded: bool, keep_backup: bool) -> PurgeResult:
        with self._lock:
            result = purge(self.db, self.csv, before=before, discarded=discarded,
                           keep_backup=keep_backup, now=self._now())
        self._notify("purged")
        return result

    def purge_before(self, before: date, *, keep_backup: bool = False) -> PurgeResult:
        """Gespeicherte und verworfene Einträge mit Startdatum < ``before`` endgültig löschen.

        Inklusive Tätigkeiten, Pausen, Audit-Zeilen und CSV-Protokollzeilen;
        danach ``VACUUM``. Der aktive Task bleibt immer erhalten. Die Rückfrage
        macht die Oberfläche. Ereignis ``purged``.
        """
        if not isinstance(before, date) or isinstance(before, datetime):
            raise TypeError("before muss ein datetime.date sein")
        return self._purge(before, False, keep_backup)

    def purge_until(self, until: date, *, keep_backup: bool = False) -> PurgeResult:
        """Wie :meth:`purge_before`, aber der Stichtag zählt mit (bis einschließlich ``until``).

        Grundlage der GUI („Einträge bis einschließlich 31.10.2026 löschen“) und von
        ``zeit loeschen --bis``. Aktive Tasks (running/paused/stopping) bleiben immer.
        """
        if not isinstance(until, date) or isinstance(until, datetime):
            raise TypeError("until muss ein datetime.date sein")
        return self.purge_before(until + timedelta(days=1), keep_backup=keep_backup)

    def purge_until_preview(self, until: date) -> int:
        """Anzahl der Einträge, die :meth:`purge_until` löschen würde."""
        return self.purge_preview(before=until + timedelta(days=1))

    def purge_discarded(self, *, keep_backup: bool = False) -> PurgeResult:
        """Alle verworfenen Einträge endgültig löschen (Ereignis ``purged``)."""
        return self._purge(None, True, keep_backup)

    def apply_retention(self, today: date | None = None) -> PurgeResult | None:
        """Aufbewahrungsfrist ``general.retention_months`` anwenden (0 = nichts tun).

        Löscht Einträge mit Beginn vor dem Stichtag und entfernt interne
        Sicherungen (``<Datenverzeichnis>/backups/``, CSV-``.bak``), die vor dem
        Stichtag angelegt wurden. Die App ruft das beim Start auf.
        Rückgabe ``None``, wenn nichts zu tun war.
        """
        with self._lock:  # unterbrochenes Löschen im CSV-Protokoll zuerst nachholen
            resume_pending_purge(self.db, self.csv)
        cutoff = retention_cutoff(today or self._now().date(),
                                  self.config.general.retention_months)
        if cutoff is None:
            return None
        result = self.purge_before(cutoff) if self.db.purgeable_uuids(cutoff) else None
        cleanup = self.remove_internal_backups(before=cutoff, reason="retention")
        if result is None and not cleanup.removed:
            return None
        result = result or PurgeResult(before=cutoff)
        result.backups_removed = len(cleanup.removed)
        return result

    # --- interne Sicherungen ----------------------------------
    @property
    def data_dir(self) -> Path:
        return self.db.path.parent

    def internal_backups(self) -> list[InternalBackup]:
        """Sicherungen im Datenverzeichnis (``backups/``, CSV-``.bak``), älteste zuerst."""
        return find_internal_backups(self.data_dir, self.csv.path.name)

    def remove_internal_backups(
        self, *, before: date | None = None, reason: str = "manual",
    ) -> BackupCleanup:
        """Interne Sicherungen entfernen (alle bzw. vor ``before`` angelegte).

        Nur nach ausdrücklicher Bestätigung des Nutzers (Oberfläche) oder über
        die Aufbewahrungsfrist aufrufen. Audit-Zeile ``backups_removed``.
        """
        with self._lock:
            return remove_internal_backups(self.db, self.data_dir, self.csv.path.name,
                                           before=before, reason=reason, now=self._now())

    def backup(self, target_dir: str | Path | None = None) -> Path:
        """Sicherung (SQLite-Backup-API + CSV + config.toml) anlegen, Rückgabe: Ordner.

        Default-Ziel: ``<datenverzeichnis>/backups``. Ordner 0700, Dateien 0600.
        """
        target = Path(target_dir) if target_dir else self.data_dir / "backups"
        cfg = self.paths.config_file if self.paths is not None else None
        return create_backup(self.db, target, csv_path=self.csv.path, config_path=cfg,
                             now=self._now())

    # --- Speicherort ändern ---------------------------------------------
    def check_relocation_target(self, target: str | Path, default_dir: Path | None = None):
        """Ziel für :meth:`relocate_data` prüfen (``RelocateError`` mit Meldung)."""
        from .storage.relocate import check_target  # noqa: PLC0415

        with self._lock:
            return check_target(target, self.data_dir, default_dir=default_dir,
                                db_path=self.db.path)

    def relocate_data(self, target: str | Path, commit: Callable[[Path], None],
                      default_dir: Path | None = None):
        """Daten nach ``target`` kopieren, prüfen, ``commit`` (Konfiguration), Altbestand
        in ``_alt-<stempel>/`` verschieben. Danach ist die Datenbank geschlossen
        (``closed``) – die App muss neu starten. Audit-Zeile ``relocated`` im neuen Ort."""
        from .storage.relocate import relocate  # noqa: PLC0415

        with self._lock:
            old_dir = self.data_dir
            result = relocate(self.db, old_dir, target, commit=commit,
                              before_archive=self.db.close, default_dir=default_dir,
                              now=self._now())
            try:  # Audit im neuen Ort (nur Tatsache, keine Pfade/Inhalte)
                new_db = Database(result.new_dir / self.db.path.name)
                try:
                    new_db.add_audit("", "relocated", {"files": len(result.copied)},
                                     at=self._now())
                finally:
                    new_db.close()
            except Exception:  # pragma: no cover - Audit darf den Umzug nicht kippen
                log_failure(log, "Audit-Zeile nach Umzug nicht geschrieben")
            self.closed = True
        return result

    # --- Erweiterungspunkte (Sprachbefehle, Export) -------------------
    def apply_command(self, command: Any) -> dict[str, Any]:
        """Sprachbefehl ausführen (Verdrahtung zum Parser).

        ``command``: ``voice.parser.Command`` (Attribute ``intent``, ``customer``,
        ``topic``, ``ticket``, ``location``) oder ``dict`` mit denselben Schlüsseln.
        Intents: ``start``, ``stop``, ``pause``, ``resume``, ``toggle_pause``, ``status``.
        Liefert ``{"intent", "result", "status"}``. ``unknown``/unbekannte Intents →
        ``ValueError`` (UI zeigt Transkript und fragt nach statt zu raten);
        unpassender Zustand → ``InvalidTransition``.

        Besonderheiten: ``resume`` im Zustand stopping entspricht „Weiterlaufen
        lassen“ (``cancel_stop``); ``stop`` im Zustand stopping ist idempotent.
        """
        if isinstance(command, dict):
            get = command.get
        else:
            def get(key: str, default: Any = None) -> Any:
                return getattr(command, key, default)
        raw_intent = get("intent")
        intent = getattr(raw_intent, "value", raw_intent)
        result: Any
        if intent == "start":
            result = self.start(
                get("customer"), get("topic"), get("ticket"),
                get("location"), get("source") or "voice",
            )
        elif intent == "stop":
            self.request_stop()
            result = "stop_requested"
        elif intent == "pause":
            self.pause()
            result = "paused"
        elif intent == "resume":
            if self.state == State.STOPPING:
                self.cancel_stop()
                result = "stop_cancelled"
            else:
                self.resume()
                result = "resumed"
        elif intent == "toggle_pause":
            result = self.toggle_pause()
        elif intent == "status":
            result = "status"
        else:
            raw = get("raw", "")
            raise ValueError(f"Befehl nicht erkannt: {raw or intent!r}")
        return {"intent": intent, "result": result, "status": self.status()}

    def entries_for_export(self, customer: str, year: int, month: int) -> list[Entry]:
        """Erweiterungspunkt für Export: gespeicherte Einträge eines Kunden im Monat."""
        von = date(year, month, 1)
        bis = date(year + (month == 12), month % 12 + 1, 1)
        return [e for e in self.list_entries(von, bis, customer=customer)
                if e.start_at.date() < bis]


# Bequemer Re-Export
__all__ += ["Location", "State", "EntryStatus"]
