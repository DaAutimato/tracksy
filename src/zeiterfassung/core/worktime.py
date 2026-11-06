"""Regelarbeitszeit: Arbeitszeit in „innerhalb“ und „nach regulärer Geschäftszeit“ zerlegen.

GUI-frei. Ein Eintrag (Start, Ende, Pausen) wird in Segmente zerlegt:

- ``regular``  – innerhalb der eingestellten Regelarbeitszeit des Wochentags,
- ``weekday``  – Montag–Freitag außerhalb der Regelarbeitszeit,
- ``saturday`` – Samstag außerhalb der Regelarbeitszeit (ohne Regelzeit: 0–24 Uhr),
- ``sunday``   – Sonn- und Feiertag (ganzer Tag; auch ein Feiertag an einem
  Werktag oder Samstag zählt wie Sonntag, eine Regelarbeitszeit gilt dort nicht).

Ist für Samstag/Sonntag ausdrücklich eine Regelarbeitszeit eingerichtet, gilt
diese als regulär (Faktor 1) – nur die Zeit außerhalb wird mit dem
Wochenendfaktor bewertet (an Feiertagen gilt keine Regelarbeitszeit).
Feiertage: :mod:`.holidays` (Bundesland) plus eigene freie Tage.

Zeitgrenzen werden in der **Wanduhrzeit** des gespeicherten Zeitpunkts geprüft
(Tagesgrenzen, Mitternacht, Fr 22:00–Sa 02:00 …). Pausen: Liegen die
einzelnen Pausenintervalle vor und passen sie zur gespeicherten Pausensumme,
werden sie zeitgenau abgezogen; sonst (manuell eingetragen/korrigiert) wird die
Pause anteilig über alle Segmente verteilt. Die Summe aller Kategorien ist
immer exakt die Netto-Dauer (Ende − Start − Pause), auch bei
Sommer-/Winterzeitwechsel.

Rundung (nur beim Export): :meth:`WorkSplit.rounded` – jede angefangene
Rundungseinheit zählt, **getrennt je Anteil** (regulär, werktags, Samstag,
Sonn-/Feiertag). Die abrechenbare Zeit eines Eintrags ist die Summe der
gerundeten Anteile (:func:`billable_seconds`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from typing import Any

from .holidays import REGIONS, is_holiday
from .timeutil import round_up_seconds

__all__ = [
    "Category",
    "OUTSIDE",
    "WEEKDAY_KEYS",
    "WEEKDAY_NAMES_DE",
    "MIN_WEEKDAY_FACTOR",
    "MAX_FACTOR",
    "DayRule",
    "WorkSchedule",
    "Segment",
    "WorkSplit",
    "parse_hhmm",
    "format_hhmm",
    "check_day",
    "split_interval",
    "split_entry",
    "billable_seconds",
]

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_NAMES_DE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")

#: Mindestfaktor für Arbeit werktags außerhalb der Regelarbeitszeit
MIN_WEEKDAY_FACTOR = 1.25
MAX_FACTOR = 10.0
#: Toleranz beim Abgleich Pausenintervalle ↔ gespeicherte Pausensumme (Sekunden)
PAUSE_TOLERANCE = 60


class Category(StrEnum):
    REGULAR = "regular"
    WEEKDAY = "weekday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"


OUTSIDE = (Category.WEEKDAY, Category.SATURDAY, Category.SUNDAY)

_HHMM_RE = re.compile(r"^(\d{1,2}):([0-5]\d)$")


def parse_hhmm(text: str, *, allow_24: bool = False) -> int:
    """``'HH:MM'`` → Minuten seit Mitternacht. ``24:00`` nur mit ``allow_24`` (als Ende)."""
    m = _HHMM_RE.match(str(text).strip()) if isinstance(text, str) else None
    if not m:
        raise ValueError(f"Uhrzeit {text!r} ungültig (erwartet HH:MM)")
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 24 or (h == 24 and (mi != 0 or not allow_24)):
        raise ValueError(f"Uhrzeit {text!r} ungültig (erwartet HH:MM)")
    return h * 60 + mi


def format_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def check_day(active: bool, start: str, end: str) -> str | None:
    """Eingabe eines Wochentags prüfen (für Oberflächen): Fehlertext oder ``None``."""
    try:
        s = parse_hhmm(start)
    except ValueError:
        return "Beginn bitte als HH:MM eingeben (z. B. 08:00)"
    try:
        e = parse_hhmm(end, allow_24=True)
    except ValueError:
        return "Ende bitte als HH:MM eingeben (z. B. 17:00)"
    if active and e <= s:
        return "Das Ende muss nach dem Beginn liegen"
    return None


@dataclass(frozen=True)
class DayRule:
    """Regelarbeitszeit eines Wochentags (Minuten seit Mitternacht, Ende exklusiv)."""

    active: bool = False
    start: int = 8 * 60
    end: int = 17 * 60

    def __post_init__(self) -> None:
        if not 0 <= self.start < 24 * 60 or not 0 < self.end <= 24 * 60:
            raise ValueError("Regelarbeitszeit außerhalb 00:00–24:00")
        if self.active and self.end <= self.start:
            raise ValueError("Ende der Regelarbeitszeit muss nach dem Beginn liegen")


def _default_days() -> tuple[DayRule, ...]:
    return tuple(DayRule(active=i < 5) for i in range(7))


@dataclass(frozen=True)
class WorkSchedule:
    """Regelarbeitszeit je Wochentag (Mo=0 … So=6) und Bewertungsfaktoren."""

    days: tuple[DayRule, ...] = field(default_factory=_default_days)
    weekday_factor: float = 1.25
    saturday_factor: float = 1.5
    sunday_factor: float = 2.0
    holiday_region: str = ""  # "" = nur bundesweite Feiertage (siehe holidays.REGIONS)
    extra_holidays: frozenset[date] = frozenset()  # eigene freie Tage (ganze Tage)

    def __post_init__(self) -> None:
        if self.holiday_region not in REGIONS:
            raise ValueError(f"Unbekannte Feiertagsregion {self.holiday_region!r}")
        if len(self.days) != 7:
            raise ValueError("Regelarbeitszeit braucht genau 7 Wochentage")
        if not MIN_WEEKDAY_FACTOR <= self.weekday_factor <= MAX_FACTOR:
            raise ValueError(f"Faktor werktags muss {MIN_WEEKDAY_FACTOR}–{MAX_FACTOR} sein")
        for f in (self.saturday_factor, self.sunday_factor):
            if not 1.0 <= f <= MAX_FACTOR:
                raise ValueError(f"Faktor Wochenende muss 1–{MAX_FACTOR} sein")

    @classmethod
    def default(cls) -> "WorkSchedule":
        """Mo–Fr 08:00–17:00, Sa/So frei; Faktoren 1,25 / 1,5 / 2,0."""
        return cls()

    def factor(self, category: Category | str) -> float:
        return {
            Category.REGULAR: 1.0,
            Category.WEEKDAY: self.weekday_factor,
            Category.SATURDAY: self.saturday_factor,
            Category.SUNDAY: self.sunday_factor,
        }[Category(category)]

    def is_holiday(self, day: date) -> bool:
        return is_holiday(day, self.holiday_region, self.extra_holidays)

    def outside_category(self, day: date) -> Category:
        """Kategorie außerhalb der Regelzeit: Feiertag/Sonntag → ``sunday``."""
        wd = day.weekday()
        if wd == 6 or self.is_holiday(day):
            return Category.SUNDAY
        return Category.SATURDAY if wd == 5 else Category.WEEKDAY

    def regular_window(self, day: date) -> tuple[datetime, datetime] | None:
        rule = self.days[day.weekday()]
        if not rule.active or self.is_holiday(day):
            return None
        base = datetime.combine(day, time())
        return base + timedelta(minutes=rule.start), base + timedelta(minutes=rule.end)


@dataclass(frozen=True)
class Segment:
    """Zeitabschnitt (Wanduhrzeit, ohne tzinfo) einer Kategorie."""

    start: datetime
    end: datetime
    category: Category

    @property
    def seconds(self) -> int:
        return int((self.end - self.start).total_seconds())


def split_interval(start: datetime, end: datetime, schedule: WorkSchedule) -> list[Segment]:
    """Intervall (naive Wanduhrzeit) an Tages- und Regelzeitgrenzen zerlegen."""
    out: list[Segment] = []

    def add(a: datetime, b: datetime, cat: Category) -> None:
        if b <= a:
            return
        if out and out[-1].category == cat and out[-1].end == a:
            out[-1] = Segment(out[-1].start, b, cat)
        else:
            out.append(Segment(a, b, cat))

    cur = start
    while cur < end:
        day = cur.date()
        stop = min(end, datetime.combine(day + timedelta(days=1), time()))
        outside = schedule.outside_category(day)
        win = schedule.regular_window(day)
        if win is None:
            add(cur, stop, outside)
        else:
            rs, re_ = win
            add(cur, min(stop, rs), outside)
            add(max(cur, rs), min(stop, re_), Category.REGULAR)
            add(max(cur, re_), stop, outside)
        cur = stop
    return out


def _empty_seconds() -> dict[Category, int]:
    return {c: 0 for c in Category}


@dataclass(frozen=True)
class WorkSplit:
    """Ergebnis der Zerlegung: Sekunden je Kategorie, Beginn/Ende der Arbeit außerhalb."""

    seconds: Mapping[Category, int] = field(default_factory=_empty_seconds)
    outside_start: datetime | None = None
    outside_end: datetime | None = None

    def get(self, category: Category | str) -> int:
        return int(self.seconds.get(Category(category), 0))

    @property
    def total_seconds(self) -> int:
        return sum(self.get(c) for c in Category)

    @property
    def regular_seconds(self) -> int:
        return self.get(Category.REGULAR)

    @property
    def outside_seconds(self) -> int:
        return sum(self.get(c) for c in OUTSIDE)

    def weighted_seconds(self, schedule: WorkSchedule) -> float:
        return sum(self.get(c) * schedule.factor(c) for c in Category)

    def rounded(self, step_minutes: int) -> "WorkSplit":
        """Export-Rundung: jeder Anteil (regulär, werktags, Sa, So/Feiertag) wird
        für sich auf ``step_minutes`` aufgerundet – jede angefangene Einheit zählt.

        ``total_seconds`` des Ergebnisses ist die abrechenbare Zeit des Eintrags
        (kann größer sein als die aufgerundete Gesamtdauer).
        """
        res = {c: round_up_seconds(self.get(c), step_minutes) for c in Category}
        return WorkSplit(res, self.outside_start, self.outside_end)

    @classmethod
    def combine(cls, splits: Iterable["WorkSplit"]) -> "WorkSplit":
        """Mehrere Einträge (Monatsbericht-Tageszeile) zusammenfassen."""
        res = _empty_seconds()
        starts: list[datetime] = []
        ends: list[datetime] = []
        for sp in splits:
            for c in Category:
                res[c] += sp.get(c)
            if sp.outside_start is not None:
                starts.append(sp.outside_start)
            if sp.outside_end is not None:
                ends.append(sp.outside_end)
        return cls(res, min(starts) if starts else None, max(ends) if ends else None)


UTC_FIXED = timezone.utc


def _subtract(intervals: list[tuple[datetime, datetime]],
              cut: Sequence[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    for cs, ce in cut:
        nxt: list[tuple[datetime, datetime]] = []
        for a, b in intervals:
            if ce <= a or cs >= b:
                nxt.append((a, b))
                continue
            if a < cs:
                nxt.append((a, cs))
            if ce < b:
                nxt.append((ce, b))
        intervals = nxt
    return intervals


def _pause_intervals(entry: Any, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Abgeschlossene Pausen, auf [Start, Ende] begrenzt (reale Zeitpunkte)."""
    out = []
    for p in getattr(entry, "pauses", None) or []:
        ps, pe = getattr(p, "start_at", None), getattr(p, "end_at", None)
        if ps is None or pe is None:
            continue
        try:
            a, b = max(ps, start), min(pe, end)
        except TypeError:  # naive/aware gemischt → Pausen nicht zeitgenau nutzbar
            return []
        if b > a:
            out.append((a, b))
    out.sort()
    merged: list[tuple[datetime, datetime]] = []
    for a, b in out:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _largest_remainder(weights: Mapping[Category, int], target: int) -> dict[Category, int]:
    """``target`` ganzzahlig im Verhältnis von ``weights`` verteilen (Summe exakt)."""
    res = _empty_seconds()
    total = sum(weights.values())
    if total <= 0 or target <= 0:
        return res
    exact = {c: w * target / total for c, w in weights.items()}
    for c, v in exact.items():
        res[c] = int(v)
    rest = target - sum(res.values())
    for c in sorted(exact, key=lambda c: (-(exact[c] - res[c]), list(Category).index(c))):
        if rest <= 0:
            break
        res[c] += 1
        rest -= 1
    return res


def _wall_clock(start: datetime, end: datetime):
    """Umrechnung realer Zeitpunkte (aware) in die Wanduhrzeit des Eintrags.

    Gleicher Offset an Beginn und Ende → fester Offset. Unterschiedlich
    (Zeitumstellung im Eintrag) → lokale Systemzeitzone, sofern sie an beiden
    Enden den gespeicherten Offset liefert; sonst fester Offset des Beginns.
    Rückgabe: ``(to_wall(aware) -> naive, to_real(naive) -> aware)``.
    """
    off = start.utcoffset()
    use_local = False
    if off != end.utcoffset():
        try:
            use_local = (start.astimezone().utcoffset() == off
                         and end.astimezone().utcoffset() == end.utcoffset())
        except (OverflowError, OSError, ValueError):  # pragma: no cover - exotische Daten
            use_local = False
    if use_local:
        return (lambda dt: dt.astimezone().replace(tzinfo=None),
                lambda w: w.astimezone())
    tz = start.tzinfo
    return (lambda dt: dt.astimezone(tz).replace(tzinfo=None),
            lambda w: w.replace(tzinfo=tz))


def _cut_points(a: datetime, b: datetime, schedule: WorkSchedule, to_wall, to_real) -> list[datetime]:
    """Reale Schnittpunkte (Tageswechsel, Beginn/Ende Regelzeit) in ``(a, b)``."""
    cuts: set[datetime] = set()
    day = to_wall(a).date() - timedelta(days=1)
    last = to_wall(b).date() + timedelta(days=1)
    while day <= last:
        walls = [datetime.combine(day, time())]
        win = schedule.regular_window(day)
        if win is not None:
            walls.extend(win)
        for w in walls:
            r = to_real(w)
            if a < r < b:
                cuts.add(r)
        day += timedelta(days=1)
    return sorted(cuts)


def _category_at(wall: datetime, schedule: WorkSchedule) -> Category:
    win = schedule.regular_window(wall.date())
    if win is not None and win[0] <= wall < win[1]:
        return Category.REGULAR
    return schedule.outside_category(wall.date())


def split_entry(entry: Any, schedule: WorkSchedule) -> WorkSplit:
    """Eintrag (``start_at``, ``end_at``, ``pause_seconds``, optional ``pauses``) zerlegen.

    Geschnitten wird in **realer Zeit** (Sekunden zählen wie gemessen, auch in der
    Nacht der Zeitumstellung); die Kategorie eines Teilstücks bestimmt dessen
    Wanduhrzeit. Summe der Kategorien = Netto-Dauer (Ende − Start −
    ``pause_seconds``, ≥ 0).
    """
    start, end = entry.start_at, entry.end_at
    if end is None or end <= start:
        return WorkSplit()
    pause_s = max(0, int(getattr(entry, "pause_seconds", 0) or 0))
    gross = int((end - start).total_seconds())
    net = max(0, gross - pause_s)
    if start.tzinfo is None or end.tzinfo is None:  # Altdaten ohne Zone: Wanduhr = Zeit
        start, end = start.replace(tzinfo=UTC_FIXED), end.replace(tzinfo=UTC_FIXED)
    to_wall, to_real = _wall_clock(start, end)

    pauses = _pause_intervals(entry, start, end)
    known = sum(int((b - a).total_seconds()) for a, b in pauses)
    use_exact = pauses and abs(known - pause_s) <= PAUSE_TOLERANCE
    work = [(start, end)]
    if use_exact:
        work = _subtract(work, pauses)

    raw = _empty_seconds()
    outside: list[tuple[datetime, datetime]] = []
    for a, b in work:
        if b <= a:
            continue
        points = [a, *_cut_points(a, b, schedule, to_wall, to_real), b]
        for x, y in zip(points, points[1:]):
            cat = _category_at(to_wall(x + (y - x) / 2), schedule)
            raw[cat] += int((y - x).total_seconds())
            if cat in OUTSIDE:
                outside.append((x, y))
    # Abgleich auf die Netto-Dauer: Pause ohne passende Intervalle anteilig
    seconds = raw if sum(raw.values()) == net else _largest_remainder(raw, net)
    if not outside or not any(seconds[c] for c in OUTSIDE):
        return WorkSplit(seconds)
    return WorkSplit(seconds, to_wall(outside[0][0]), to_wall(outside[-1][1]))


def billable_seconds(entry: Any, schedule: WorkSchedule | None = None, step_minutes: int = 15) -> int:
    """Abrechenbare Sekunden eines Eintrags: Summe der je Anteil aufgerundeten Zeiten."""
    sched = WorkSchedule.default() if schedule is None else schedule
    return split_entry(entry, sched).rounded(step_minutes).total_seconds
