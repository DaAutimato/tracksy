"""Gemeinsame Hilfsfunktionen für XLSX- und CSV-Export.

Die Exportfunktionen arbeiten gegen ein schmales Protokoll (:class:`ExportEntry`)
statt gegen eine konkrete Klasse; :class:`zeiterfassung.core.models.Entry`
erfüllt es.

Mitternacht: Ein Eintrag, der über Mitternacht läuft, wird vollständig dem
**Startdatum** zugeordnet (Datum, Monatsfilter, Summen). Die Endzeit wird als
Uhrzeit ausgegeben; im XLSX steht in der Beschreibung ein Hinweis auf das
abweichende Enddatum.

Rundung: Netto-Dauer je Eintrag wird beim Export auf ``round_minutes``
aufgerundet (Default 15, 0 bleibt 0). Die Rundung selbst liegt in
:mod:`zeiterfassung.core.timeutil` und wird hier nur genutzt.
"""

from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import IO, Any, Protocol, runtime_checkable

from ..core.sanitize import strip_control
from ..core.worktime import WorkSchedule, WorkSplit, split_entry

__all__ = [
    "ExportEntry",
    "MONTH_NAMES_DE",
    "parse_month",
    "month_label",
    "net_seconds",
    "billable_seconds",
    "work_split",
    "resolve_schedule",
    "location_of",
    "items_of",
    "filter_entries",
    "suggest_filename",
    "secure_write",
    "local_naive",
]

DEFAULT_ROUND_MINUTES = 15

MONTH_NAMES_DE = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)


@runtime_checkable
class ExportEntry(Protocol):
    """Minimal benötigte Attribute eines Eintrags für den Export."""

    uuid: str
    customer: str
    topic: str
    ticket: str
    location: str
    start_at: datetime
    end_at: datetime | None
    pause_seconds: int
    description: str
    addendum: str
    items: Sequence[Any]
    status: str


# --------------------------------------------------------------------------
# Monat
# --------------------------------------------------------------------------
_MONTH_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})\s*$")


def parse_month(month: date | datetime | str | tuple[int, int]) -> tuple[int, int]:
    """``date``/``datetime``/``'JJJJ-MM'``/``(jahr, monat)`` → ``(jahr, monat)``."""
    if isinstance(month, (date, datetime)):
        return month.year, month.month
    if isinstance(month, tuple) and len(month) == 2:
        y, m = int(month[0]), int(month[1])
    elif isinstance(month, str):
        mo = _MONTH_RE.match(month)
        if not mo:
            raise ValueError(f"Ungültiger Monat {month!r}, erwartet 'JJJJ-MM'")
        y, m = int(mo.group(1)), int(mo.group(2))
    else:
        raise TypeError(f"Ungültiger Monatstyp: {type(month).__name__}")
    if not 1 <= m <= 12 or not 1 <= y <= 9999:
        raise ValueError(f"Ungültiger Monat {month!r}")
    return y, m


def month_label(month: Any) -> str:
    y, m = parse_month(month)
    return f"{MONTH_NAMES_DE[m - 1]} {y}"


# --------------------------------------------------------------------------
# Dauer / Felder
# --------------------------------------------------------------------------
def net_seconds(entry: Any) -> int:
    """Netto-Dauer (Ende − Start − Pause) in Sekunden, nie negativ.

    Zusammengefasster Tag (``members``): Summe der einzelnen Einträge.
    """
    members = getattr(entry, "members", None)
    if members is not None:
        return sum(net_seconds(m) for m in members)
    if entry.end_at is None:
        return 0
    gross = int((entry.end_at - entry.start_at).total_seconds())
    return max(0, gross - int(getattr(entry, "pause_seconds", 0) or 0))


def billable_seconds(
    entry: Any,
    round_minutes: int = DEFAULT_ROUND_MINUTES,
    schedule: WorkSchedule | None = None,
) -> int:
    """Abzurechnende Sekunden: Summe der je Anteil aufgerundeten Zeiten.

    Jede angefangene Rundungseinheit zählt, getrennt für regulär, werktags
    außerhalb, Samstag und Sonn-/Feiertag (:meth:`WorkSplit.rounded`).
    Zusammengefasster Tag: Summe der einzeln gerundeten Einträge (wie im
    Tätigkeitsbericht, damit beide Berichte dieselbe Monatssumme haben).
    """
    return work_split(entry, schedule, round_minutes).total_seconds


def resolve_schedule(schedule: WorkSchedule | None) -> WorkSchedule:
    """``None`` → Vorgabe-Regelarbeitszeit (Mo–Fr 08:00–17:00)."""
    return WorkSchedule.default() if schedule is None else schedule


def work_split(
    entry: Any,
    schedule: WorkSchedule | None = None,
    round_minutes: int | None = DEFAULT_ROUND_MINUTES,
) -> WorkSplit:
    """Netto-Dauer in Regelzeit / nach reg. GZ (werktags, Sa, So) zerlegen.

    ``round_minutes=None``: ungerundet. Sonst Export-Rundung je Eintrag
    je Anteil (:meth:`WorkSplit.rounded`): ``total_seconds`` == :func:`billable_seconds`.
    Zusammengefasster Tag (``members``): Summe der einzeln gerundeten Einträge,
    Beginn/Ende außerhalb = frühester Beginn / spätestes Ende.
    """
    schedule = resolve_schedule(schedule)
    members = getattr(entry, "members", None)
    if members is not None:
        return WorkSplit.combine(work_split(m, schedule, round_minutes) for m in members)
    sp = split_entry(entry, schedule)
    return sp if round_minutes is None else sp.rounded(round_minutes)


def location_of(entry: Any) -> str:
    """``'onsite'`` oder ``'remote'`` (Unbekanntes → remote)."""
    v = str(getattr(entry, "location", "") or "").strip().lower()
    return "onsite" if v in ("onsite", "on-site", "on site", "vor ort", "vorort") else "remote"


def items_of(entry: Any) -> list[str]:
    """Weitere Tätigkeiten als Liste von Texten (Strings oder Objekte mit ``.text``)."""
    out: list[str] = []
    for it in getattr(entry, "items", None) or []:
        text = it if isinstance(it, str) else getattr(it, "text", str(it))
        text = strip_control(str(text), multiline=True).strip()
        if text:
            out.append(text)
    return out


def text_of(entry: Any, name: str) -> str:
    v = getattr(entry, name, "")
    # Ältere Daten können Steuerzeichen enthalten → beim Export entfernen
    return "" if v is None else strip_control(str(v), multiline=True).strip()


def local_naive(dt: datetime) -> datetime:
    """Zeitpunkt in seiner gespeicherten (lokalen) Zeitzone, ohne tzinfo (für Excel)."""
    return dt.replace(tzinfo=None, microsecond=0)


def _status(entry: Any) -> str:
    return str(getattr(entry, "status", "") or "").strip().lower()


def filter_entries(
    entries: Iterable[Any],
    customer: str | None = None,
    month: Any = None,
) -> list[Any]:
    """Nur gespeicherte (``stopped``) Einträge mit Endzeit, optional je Kunde/Monat.

    - Verworfene, laufende, pausierte und im Stopp befindliche Einträge fallen raus.
    - Kundenvergleich ohne Groß-/Kleinschreibung und ohne Randleerzeichen.
    - Monat bezieht sich auf das **Startdatum** (Mitternachtsregel).
    Ergebnis ist nach Startzeit sortiert.
    """
    ym = parse_month(month) if month is not None else None
    cust = customer.strip().casefold() if customer is not None else None
    out = []
    for e in entries:
        if _status(e) != "stopped" or e.end_at is None:
            continue
        if cust is not None and text_of(e, "customer").casefold() != cust:
            continue
        if ym is not None and (e.start_at.year, e.start_at.month) != ym:
            continue
        out.append(e)
    out.sort(key=lambda e: (e.start_at, text_of(e, "uuid")))
    return out


# --------------------------------------------------------------------------
# Dateinamen / sicheres Schreiben
# --------------------------------------------------------------------------
_UNSAFE_RE = re.compile(r"[^\w.\-]+", re.UNICODE)


def _sanitize_component(value: str, fallback: str, max_len: int = 60) -> str:
    value = unicodedata.normalize("NFC", str(value or ""))
    # Steuerzeichen und Pfadtrenner raus, Leerraum -> "_"
    value = "".join(ch for ch in value if unicodedata.category(ch)[0] != "C")
    value = _UNSAFE_RE.sub("_", value)
    value = re.sub(r"\.{2,}", ".", value)  # kein ".."
    value = re.sub(r"_{2,}", "_", value)
    value = value.strip("._-")
    value = value[:max_len].rstrip("._-")
    return value or fallback


def suggest_filename(customer: str, month: Any, ext: str, summary: bool = False) -> str:
    """Sicherer Dateiname ``Leistungsnachweis_<Kunde>_<JJJJ-MM>[_Monatsbericht].<ext>``.

    Nur Buchstaben (inkl. Umlaute), Ziffern, ``_ - .``; keine Pfadtrenner,
    kein ``..``, keine führenden Punkte. Gibt nur den Namen zurück, keinen Pfad.
    """
    y, m = parse_month(month)
    cust = _sanitize_component(customer, "Kunde")
    ext_clean = re.sub(r"[^A-Za-z0-9]", "", str(ext or "").lstrip(".")).lower()[:10] or "dat"
    kind = "_Monatsbericht" if summary else ""
    return f"Leistungsnachweis_{cust}_{y:04d}-{m:02d}{kind}.{ext_clean}"


def secure_write(path: str | os.PathLike[str], writer: Callable[[IO[bytes]], None]) -> Path:
    """Datei atomar mit Rechten 0600 schreiben.

    Schreibt in eine temporäre Datei im Zielverzeichnis (``mkstemp`` → 0600)
    und ersetzt dann das Ziel. Das Zielverzeichnis muss existieren.
    """
    target = Path(path)
    parent = target.parent if str(target.parent) else Path(".")
    if not parent.is_dir():
        raise FileNotFoundError(f"Zielverzeichnis existiert nicht: {parent}")
    if target.is_dir():
        raise IsADirectoryError(str(target))
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".export-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            writer(fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)  # eigene, unvollständige Temp-Datei aufräumen
        except FileNotFoundError:
            pass
        raise
    os.chmod(target, 0o600)
    return target
