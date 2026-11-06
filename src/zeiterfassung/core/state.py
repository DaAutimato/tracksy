"""Zustandsmaschine eines Tasks (reine Funktionen auf :class:`Entry`).

Übergänge::

    idle ──start──▶ running ⇄ paused
                      │  ╲       │
              request_stop ╲  request_stop
                      ▼     ╲    ▼
                    stopping ◀───┘   (Stoppzeit = Auslösezeitpunkt)
                      │  │  │
         confirm_stop │  │  └─ cancel_stop ─▶ zurück nach running/paused
                      ▼  └──── discard ─▶ discarded
                   stopped

``discard`` ist auch direkt aus running/paused erlaubt. Persistenz übernimmt
der Aufrufer (``TrackerService``).
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime

from .models import Entry, EntryStatus, Pause, StartRequest

__all__ = [
    "InvalidTransition",
    "new_entry",
    "pause",
    "resume",
    "request_stop",
    "cancel_stop",
    "confirm_stop",
    "discard",
]


class InvalidTransition(Exception):
    """Übergang im aktuellen Zustand nicht erlaubt."""

    def __init__(self, action: str, status: str | None):
        self.action = action
        self.status = status
        super().__init__(f"Aktion '{action}' im Zustand '{status or 'idle'}' nicht möglich")


def _require(entry: Entry, action: str, *allowed: EntryStatus) -> None:
    if entry.status not in allowed:
        raise InvalidTransition(action, entry.status)


def new_entry(req: StartRequest, now: datetime, entry_uuid: str | None = None) -> Entry:
    start = req.requested_at or now
    return Entry(
        uuid=entry_uuid or str(_uuid.uuid4()),
        start_at=start,
        customer=req.customer,
        topic=req.topic,
        ticket=req.ticket,
        location=req.location,
        source=req.source,
        status=EntryStatus.RUNNING,
        created_at=now,
        updated_at=now,
    )


def pause(entry: Entry, now: datetime) -> Entry:
    _require(entry, "pause", EntryStatus.RUNNING)
    entry.pauses.append(Pause(start_at=now))
    entry.status = EntryStatus.PAUSED
    entry.updated_at = now
    return entry


def _close_open_pause(entry: Entry, at: datetime) -> None:
    op = entry.open_pause
    if op is not None:
        op.end_at = max(at, op.start_at)
    entry.pause_seconds = sum(p.seconds() for p in entry.pauses if p.end_at is not None)


def resume(entry: Entry, now: datetime) -> Entry:
    _require(entry, "resume", EntryStatus.PAUSED)
    _close_open_pause(entry, now)
    entry.status = EntryStatus.RUNNING
    entry.updated_at = now
    return entry


def request_stop(entry: Entry, now: datetime) -> Entry:
    _require(entry, "request_stop", EntryStatus.RUNNING, EntryStatus.PAUSED)
    entry.prev_status = str(entry.status)
    entry.status = EntryStatus.STOPPING
    entry.end_at = max(now, entry.start_at)
    entry.stop_requested_at = now
    entry.updated_at = now
    return entry


def cancel_stop(entry: Entry, now: datetime) -> Entry:
    """„Weiterlaufen lassen“: Stopp abbrechen, Task läuft (bzw. pausiert) weiter."""
    _require(entry, "cancel_stop", EntryStatus.STOPPING)
    entry.status = EntryStatus(entry.prev_status or EntryStatus.RUNNING)
    entry.prev_status = None
    entry.end_at = None
    entry.stop_requested_at = None
    entry.updated_at = now
    return entry


def confirm_stop(
    entry: Entry, now: datetime, addendum: str = "", items: list[str] | None = None
) -> Entry:
    _require(entry, "confirm_stop", EntryStatus.STOPPING)
    assert entry.end_at is not None
    _close_open_pause(entry, entry.end_at)
    entry.addendum = (addendum or "").strip()
    entry.items = [t.strip() for t in (items or []) if t and t.strip()]
    entry.status = EntryStatus.STOPPED
    entry.prev_status = None
    entry.updated_at = now
    return entry


def discard(entry: Entry, now: datetime) -> Entry:
    _require(
        entry, "discard", EntryStatus.RUNNING, EntryStatus.PAUSED, EntryStatus.STOPPING
    )
    if entry.end_at is None:
        entry.end_at = max(now, entry.start_at)
    _close_open_pause(entry, entry.end_at)
    entry.status = EntryStatus.DISCARDED
    entry.prev_status = None
    entry.updated_at = now
    return entry
