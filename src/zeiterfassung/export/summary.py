"""Monatsbericht: alle Einträge eines Tages zu einer Zeile zusammenfassen.

Gruppiert wird nach Kalendertag (Startdatum), Kunde und Ort: Kunden werden
nie vermischt (Gesamtexport aller Kunden), Remote- und Vor-Ort-Stunden bleiben im
Leistungsnachweis getrennt (ein Tag mit beidem ergibt zwei Zeilen). Die
Tageszeile verhält sich für die Export-Renderer wie ein Eintrag:

- Beginn = frühester Beginn, Ende = spätestes Ende, Pause = Summe der Pausen;
- Netto- und abgerechnete Dauer = Summe der einzelnen Einträge (jeder Eintrag
  wird wie im Tätigkeitsbericht einzeln gerundet – die Monatssumme ist in beiden
  Berichten gleich);
- Themen, Tickets, Beschreibungen, Ergänzungen und Tätigkeiten werden
  zusammengeführt (Doppelte nur einmal).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from .common import items_of, location_of, text_of

__all__ = ["DaySummary", "summarize_by_day"]


def _unique(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


@dataclass
class DaySummary:
    """Zusammengefasster Tag (gleiche Schnittstelle wie ein Eintrag beim Export)."""

    members: list[Any]
    start_at: datetime
    end_at: datetime
    pause_seconds: int
    location: str
    customer: str
    topic: str
    ticket: str
    description: str
    addendum: str
    items: list[str] = field(default_factory=list)
    status: str = "stopped"
    uuid: str = ""

    @property
    def day(self) -> date:
        return self.start_at.date()


def summarize_by_day(entries: Iterable[Any]) -> list[DaySummary]:
    """Einträge je (Tag, Kunde, Ort) zusammenfassen; Eingabe wie von ``filter_entries``."""
    groups: dict[tuple[date, str, str], list[Any]] = {}
    for e in entries:
        key = (e.start_at.date(), text_of(e, "customer").casefold(), location_of(e))
        groups.setdefault(key, []).append(e)
    out: list[DaySummary] = []
    for (day, _cust, loc), members in sorted(
            groups.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] != "remote")):
        members = sorted(members, key=lambda e: e.start_at)
        out.append(DaySummary(
            members=members,
            start_at=min(m.start_at for m in members),
            end_at=max(m.end_at for m in members),
            pause_seconds=sum(int(getattr(m, "pause_seconds", 0) or 0) for m in members),
            location=loc,
            customer=text_of(members[0], "customer"),
            topic="; ".join(_unique(text_of(m, "topic") for m in members)),
            ticket=", ".join(_unique(text_of(m, "ticket") for m in members)),
            description=" | ".join(_unique(text_of(m, "description") for m in members)),
            addendum=" | ".join(_unique(text_of(m, "addendum") for m in members)),
            items=_unique(it for m in members for it in items_of(m)),
        ))
    return out
