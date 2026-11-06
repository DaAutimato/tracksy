"""Zeit-Hilfsfunktionen: lokale Zeit mit Zeitzone, ISO-8601, Rundung.

Alle gespeicherten Zeitpunkte sind ISO 8601 mit Offset und Sekundengenauigkeit,
z. B. ``2026-10-30T09:15:00+01:00``.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

__all__ = [
    "now",
    "to_iso",
    "from_iso",
    "ensure_aware",
    "round_up_seconds",
    "round_up_minutes",
    "seconds_to_hours",
]


def now() -> datetime:
    """Aktuelle lokale Zeit mit Zeitzone, auf Sekunden gekürzt."""
    return datetime.now().astimezone().replace(microsecond=0)


def ensure_aware(dt: datetime) -> datetime:
    """Naive Zeitpunkte als lokale Zeit interpretieren; Mikrosekunden kürzen."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        dt = dt.astimezone()  # naive -> lokale Zeitzone
    return dt.replace(microsecond=0)


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return ensure_aware(dt).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    dt = datetime.fromisoformat(value)
    return ensure_aware(dt)


def round_up_seconds(seconds: float, step_minutes: int = 15) -> int:
    """Dauer auf ein Vielfaches von ``step_minutes`` aufrunden (in Sekunden).

    0 bleibt 0, negative Werte werden als 0 behandelt. ``step_minutes <= 0``
    bedeutet: keine Rundung (nur auf ganze Sekunden).
    """
    if seconds <= 0:
        return 0
    if step_minutes <= 0:
        return int(math.ceil(seconds))
    step = step_minutes * 60
    return int(math.ceil(seconds / step) * step)


def round_up_minutes(seconds: float, step_minutes: int = 15) -> int:
    """Wie :func:`round_up_seconds`, Ergebnis in Minuten (aufgerundet)."""
    return int(math.ceil(round_up_seconds(seconds, step_minutes) / 60))


def seconds_to_hours(seconds: float, ndigits: int = 2) -> float:
    return round(seconds / 3600.0, ndigits)


# Für Tests/Typen bequem re-exportiert
UTC = timezone.utc
ZERO = timedelta(0)
