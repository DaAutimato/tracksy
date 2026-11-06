"""Datenmodelle (GUI-frei)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from .timeutil import from_iso, to_iso

__all__ = [
    "Location",
    "Source",
    "EntryStatus",
    "State",
    "ACTIVE_STATUSES",
    "Pause",
    "Entry",
    "StartRequest",
]


class Location(StrEnum):
    REMOTE = "remote"
    ONSITE = "onsite"


class Source(StrEnum):
    VOICE = "voice"
    HOTKEY = "hotkey"
    GUI = "gui"
    CLI = "cli"
    TRAY = "tray"


class EntryStatus(StrEnum):
    """Status eines Eintrags in der Datenbank (Spalte ``entries.status``)."""

    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"  # Ergänzungsdialog offen, Stoppzeit steht fest
    STOPPED = "stopped"  # gespeichert
    DISCARDED = "discarded"  # verworfen


class State(StrEnum):
    """Zustand des Trackers (Service-Sicht)."""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"


ACTIVE_STATUSES = (EntryStatus.RUNNING, EntryStatus.PAUSED, EntryStatus.STOPPING)


def _norm_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_location(value: Any, default: str = Location.REMOTE) -> str:
    v = _norm_text(value).lower()
    if v in ("", "none"):
        return str(Location(default))
    aliases = {"vor ort": "onsite", "vorort": "onsite", "on-site": "onsite", "on site": "onsite"}
    v = aliases.get(v, v)
    try:
        return str(Location(v))
    except ValueError:  # Meldung ohne Eingabewert
        raise ValueError("Ungültiger Ort (erlaubt: remote, onsite)") from None


def normalize_source(value: Any, default: str = Source.GUI) -> str:
    v = _norm_text(value).lower() or str(default)
    try:
        return str(Source(v))
    except ValueError:  # Meldung ohne Eingabewert
        raise ValueError(
            "Ungültige Quelle (erlaubt: " + ", ".join(s.value for s in Source) + ")"
        ) from None


@dataclass
class Pause:
    start_at: datetime
    end_at: datetime | None = None
    id: int | None = None

    def seconds(self, until: datetime | None = None) -> int:
        end = self.end_at or until
        if end is None:
            return 0
        return max(0, int((end - self.start_at).total_seconds()))


@dataclass
class Entry:
    uuid: str
    start_at: datetime
    customer: str = ""
    topic: str = ""
    ticket: str = ""
    location: str = Location.REMOTE
    end_at: datetime | None = None
    pause_seconds: int = 0
    description: str = ""
    addendum: str = ""
    source: str = Source.GUI
    status: str = EntryStatus.RUNNING
    created_at: datetime | None = None
    updated_at: datetime | None = None
    stop_requested_at: datetime | None = None
    prev_status: str | None = None
    exported_at: datetime | None = None
    invoice_ref: str | None = None
    items: list[str] = field(default_factory=list)
    pauses: list[Pause] = field(default_factory=list)
    id: int | None = None

    # --- Berechnungen -------------------------------------------------
    @property
    def open_pause(self) -> Pause | None:
        for p in self.pauses:
            if p.end_at is None:
                return p
        return None

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def closed_pause_seconds(self) -> int:
        """Summe abgeschlossener Pausen.

        Bei gespeicherten Einträgen ist ``pause_seconds`` führend (kann manuell
        korrigiert sein); bei aktiven Einträgen wird aus der Pausenliste gerechnet.
        """
        if not self.is_active:
            return int(self.pause_seconds)
        return sum(p.seconds() for p in self.pauses if p.end_at is not None)

    def total_pause_seconds(self, now: datetime | None = None) -> int:
        """Pausen inkl. einer offenen Pause (bis ``now`` bzw. Stoppzeit)."""
        if not self.is_active:
            return int(self.pause_seconds)
        until = self.end_at or now
        total = self.closed_pause_seconds()
        op = self.open_pause
        if op is not None and until is not None:
            total += op.seconds(until)
        return total

    def net_seconds(self, now: datetime | None = None) -> int:
        """Netto-Dauer = Ende − Start − Pausen (Ende: end_at oder ``now``)."""
        end = self.end_at or now
        if end is None:
            return 0
        gross = int((end - self.start_at).total_seconds())
        return max(0, gross - self.total_pause_seconds(now))

    # --- Serialisierung -----------------------------------------------
    def to_dict(self, now: datetime | None = None) -> dict[str, Any]:
        op = self.open_pause
        return {
            "uuid": self.uuid,
            "customer": self.customer,
            "topic": self.topic,
            "ticket": self.ticket,
            "location": str(self.location),
            "source": str(self.source),
            "status": str(self.status),
            "start_at": to_iso(self.start_at),
            "end_at": to_iso(self.end_at),
            "pause_seconds": self.closed_pause_seconds(),
            "paused_since": to_iso(op.start_at) if op is not None else None,
            "stop_requested_at": to_iso(self.stop_requested_at),
            "prev_status": self.prev_status,
            "elapsed_seconds": self.net_seconds(now),
            "description": self.description,
            "addendum": self.addendum,
            "items": list(self.items),
            "created_at": to_iso(self.created_at),
            "updated_at": to_iso(self.updated_at),
        }


@dataclass
class StartRequest:
    """Gewünschter Start (u. a. für ``pending_start`` während eines Stopps)."""

    customer: str = ""
    topic: str = ""
    ticket: str = ""
    location: str = Location.REMOTE
    source: str = Source.GUI
    requested_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "customer": self.customer,
            "topic": self.topic,
            "ticket": self.ticket,
            "location": str(self.location),
            "source": str(self.source),
            "requested_at": to_iso(self.requested_at),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StartRequest":
        return cls(
            customer=_norm_text(d.get("customer")),
            topic=_norm_text(d.get("topic")),
            ticket=_norm_text(d.get("ticket")),
            location=normalize_location(d.get("location")),
            source=normalize_source(d.get("source")),
            requested_at=from_iso(d.get("requested_at")),
        )
