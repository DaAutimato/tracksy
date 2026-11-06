"""Gesetzliche Feiertage in Deutschland (lokal berechnet, ohne Zusatzpaket).

Ostersonntag nach dem anonymen gregorianischen Algorithmus (Meeus/Jones/Butcher,
Variante der Gaußschen Osterformel); bewegliche Feiertage relativ dazu, feste
Feiertage je Bundesland. Gemeinde- oder stadtbezogene Feiertage (z. B.
Augsburger Friedensfest, Fronleichnam nur in Teilen Sachsens/Thüringens) sind
nicht enthalten – sie werden über „eigene freie Tage“ eingetragen (einzelne Tage
oder Zeiträume, z. B. Urlaub).

Regionen (``REGIONS``): ``""`` = nur bundesweite Feiertage, sonst Kürzel des
Bundeslands – dann gelten bundesweite **und** Landesfeiertage zusammen. Bayern
gibt es zweimal: ``BY`` ohne und ``BY-MH`` mit Mariä Himmelfahrt (15.08., gilt in Gemeinden mit überwiegend katholischer Bevölkerung).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from functools import lru_cache

__all__ = [
    "MAX_FREE_DAYS", "MAX_PERIOD_DAYS", "REGIONS", "easter_sunday", "expand_periods",
    "format_period", "holidays", "is_holiday", "merge_periods", "parse_day", "parse_period_input",
    "parse_period_iso", "period_days", "period_to_iso",
]

REGIONS: dict[str, str] = {
    "": "Nur bundesweite Feiertage",
    "BW": "Baden-Württemberg",
    "BY": "Bayern (ohne Mariä Himmelfahrt)",
    "BY-MH": "Bayern (mit Mariä Himmelfahrt)",
    "BE": "Berlin",
    "BB": "Brandenburg",
    "HB": "Bremen",
    "HH": "Hamburg",
    "HE": "Hessen",
    "MV": "Mecklenburg-Vorpommern",
    "NI": "Niedersachsen",
    "NW": "Nordrhein-Westfalen",
    "RP": "Rheinland-Pfalz",
    "SL": "Saarland",
    "SN": "Sachsen",
    "ST": "Sachsen-Anhalt",
    "SH": "Schleswig-Holstein",
    "TH": "Thüringen",
}


def easter_sunday(year: int) -> date:
    """Ostersonntag (gregorianischer Kalender)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _penance_day(year: int) -> date:
    """Buß- und Bettag: Mittwoch vor dem 23. November (16.–22.11.)."""
    d = date(year, 11, 22)
    return d - timedelta(days=(d.weekday() - 2) % 7)


@lru_cache(maxsize=256)
def holidays(year: int, region: str = "") -> dict[date, str]:
    """Feiertage eines Jahres: ``{Datum: Name}`` (bundesweit + Region)."""
    if region not in REGIONS:
        raise ValueError(f"Unbekannte Feiertagsregion {region!r}")
    st = "BY" if region == "BY-MH" else region
    e = easter_sunday(year)
    out: dict[date, str] = {
        date(year, 1, 1): "Neujahr",
        e - timedelta(days=2): "Karfreitag",
        e + timedelta(days=1): "Ostermontag",
        date(year, 5, 1): "Tag der Arbeit",
        e + timedelta(days=39): "Christi Himmelfahrt",
        e + timedelta(days=50): "Pfingstmontag",
        date(year, 10, 3): "Tag der Deutschen Einheit",
        date(year, 12, 25): "1. Weihnachtstag",
        date(year, 12, 26): "2. Weihnachtstag",
    }
    if year == 2017:
        out[date(2017, 10, 31)] = "Reformationstag (500 Jahre)"

    def add(states: set[str], day: date, name: str, since: int = 0) -> None:
        if st in states and year >= since:
            out[day] = name

    add({"BW", "BY", "ST"}, date(year, 1, 6), "Heilige Drei Könige")
    add({"BE"}, date(year, 3, 8), "Internationaler Frauentag", 2019)
    add({"MV"}, date(year, 3, 8), "Internationaler Frauentag", 2023)
    add({"BB"}, e, "Ostersonntag")
    add({"BB"}, e + timedelta(days=49), "Pfingstsonntag")
    add({"BW", "BY", "HE", "NW", "RP", "SL"}, e + timedelta(days=60), "Fronleichnam")
    if region == "BY-MH" or st == "SL":
        out[date(year, 8, 15)] = "Mariä Himmelfahrt"
    add({"TH"}, date(year, 9, 20), "Weltkindertag", 2019)
    add({"BB", "MV", "SN", "ST", "TH"}, date(year, 10, 31), "Reformationstag")
    add({"HB", "HH", "NI", "SH"}, date(year, 10, 31), "Reformationstag", 2018)
    add({"BW", "BY", "NW", "RP", "SL"}, date(year, 11, 1), "Allerheiligen")
    add({"SN"}, _penance_day(year), "Buß- und Bettag")
    if st == "BE" and year in (2020, 2025):
        out[date(year, 5, 8)] = "Tag der Befreiung"
    return out


def is_holiday(day: date, region: str = "", extra: frozenset[date] | set[date] = frozenset()) -> bool:
    return day in extra or day in holidays(day.year, region)


def parse_day(text: str) -> date:
    """``JJJJ-MM-TT`` oder ``TT.MM.JJJJ`` → ``date`` (``ValueError`` bei Fehlern)."""
    t = str(text).strip()
    try:
        if "." in t:
            d, m, y = t.split(".")
            if len(y) != 4:
                raise ValueError
            return date(int(y), int(m), int(d))
        if len(t) != 10:
            raise ValueError
        return date.fromisoformat(t)
    except ValueError:
        raise ValueError(f"Datum {text!r} ungültig (erwartet JJJJ-MM-TT oder TT.MM.JJJJ)") from None


# ------------------------------------------------------------ eigene freie Tage
# Einträge sind einzelne Tage oder Zeiträume (z. B. Urlaub). Gespeichert wird
# ISO-8601: ``"JJJJ-MM-TT"`` bzw. ``"JJJJ-MM-TT/JJJJ-MM-TT"`` (beide Enden inklusive).

MAX_PERIOD_DAYS = 366  # je Zeitraum
MAX_FREE_DAYS = 10_000  # insgesamt nach dem Aufklappen (Schutz vor übergroßen Mengen)
MIN_YEAR, MAX_YEAR = 1900, 2200  # plausibler Bereich (Tippfehler wie 0026 oder 9999 abfangen)

# re.ASCII: nur Ziffern 0–9 (keine Unicode-Ziffern wie „２“)
_ISO_PERIOD_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:/(\d{4}-\d{2}-\d{2}))?", re.ASCII)
_INPUT_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})|(\d{1,2})\.(\d{1,2})\.(\d{4})?", re.ASCII)
_INPUT_SEP_RE = re.compile(r"\s*(?:-|–|—|/|\.\.|bis)\s*", re.IGNORECASE)

Period = tuple[date, date]


def _check_period(start: date, end: date) -> Period:
    for d in (start, end):
        if not MIN_YEAR <= d.year <= MAX_YEAR:
            raise ValueError(f"Jahr {d.year} außerhalb {MIN_YEAR}–{MAX_YEAR}")
    if end < start:
        raise ValueError("Ende liegt vor dem Beginn")
    if (end - start).days + 1 > MAX_PERIOD_DAYS:
        raise ValueError(f"Zeitraum zu lang (höchstens {MAX_PERIOD_DAYS} Tage)")
    return start, end


def parse_period_input(text: str) -> Period:
    """Eingabe → ``(beginn, ende)``.

    Erlaubt: ``24.12.2026``, ``2026-12-24`` sowie Zeiträume ``A–B`` mit den Trennern
    ``-``, ``–``, ``—``, ``/``, ``..`` oder ``bis``. Fehlt beim Beginn das Jahr
    (``27.07.–07.08.2026``), gilt das Jahr des Endes, über den Jahreswechsel das Vorjahr.
    """
    t = str(text).strip()
    matches = list(_INPUT_DATE_RE.finditer(t))
    if not 1 <= len(matches) <= 2 or t[:matches[0].start()].strip() or t[matches[-1].end():].strip():
        raise ValueError(f"„{t[:30]}“ ist kein Datum oder Zeitraum")
    if len(matches) == 2 and not _INPUT_SEP_RE.fullmatch(t[matches[0].end():matches[1].start()]):
        raise ValueError(f"„{t[:30]}“ ist kein Datum oder Zeitraum")

    def to_date(m: re.Match, year: int | None) -> date:
        try:
            if m.group(1):
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            y = int(m.group(6)) if m.group(6) else year
            if y is None:
                raise ValueError
            return date(y, int(m.group(5)), int(m.group(4)))
        except ValueError:
            raise ValueError(f"„{m.group(0)}“ ist kein gültiges Datum") from None

    end_m = matches[-1]
    if end_m.group(4) and not end_m.group(6):
        raise ValueError(f"„{t[:30]}“: Jahr fehlt")
    end = to_date(end_m, None)
    if len(matches) == 1:
        return _check_period(end, end)
    start = to_date(matches[0], end.year)
    if matches[0].group(4) and not matches[0].group(6) and start > end:
        start = to_date(matches[0], end.year - 1)  # 28.12.–02.01.2027
    return _check_period(start, end)


def parse_period_iso(item: object) -> Period:
    """Gespeicherter Eintrag (streng ISO) → ``(beginn, ende)``; sonst ``ValueError``."""
    msg = 'Datum als "JJJJ-MM-TT" oder Zeitraum als "JJJJ-MM-TT/JJJJ-MM-TT" angeben'
    m = _ISO_PERIOD_RE.fullmatch(item) if isinstance(item, str) else None
    if m is None:
        raise ValueError(msg)
    try:
        start = date.fromisoformat(m.group(1))
        end = date.fromisoformat(m.group(2)) if m.group(2) else start
    except ValueError:
        raise ValueError(f"{item!r} ist kein gültiges Datum – {msg}") from None
    return _check_period(start, end)


def period_to_iso(period: Period) -> str:
    start, end = period
    return start.isoformat() if start == end else f"{start.isoformat()}/{end.isoformat()}"


def format_period(period: Period) -> str:
    """Anzeige: ``24.12.2026`` bzw. ``27.07.2026–07.08.2026``."""
    start, end = period
    a = start.strftime("%d.%m.%Y")
    return a if start == end else f"{a}–{end.strftime('%d.%m.%Y')}"


def merge_periods(periods: list[Period]) -> list[Period]:
    """Sortieren, Doppelte und Überlappungen zusammenfassen (angrenzende bleiben getrennt)."""
    out: list[Period] = []
    for start, end in sorted(periods):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def period_days(periods: list[Period]) -> int:
    """Anzahl Tage (Überlappungen einfach gezählt)."""
    return sum((e - s).days + 1 for s, e in merge_periods(periods))


def expand_periods(periods: list[Period]) -> frozenset[date]:
    """Alle Tage der Zeiträume; ``ValueError`` über :data:`MAX_FREE_DAYS`."""
    merged = merge_periods(periods)  # Doppelte nur einmal aufklappen
    if period_days(merged) > MAX_FREE_DAYS:
        raise ValueError(f"höchstens {MAX_FREE_DAYS} eigene freie Tage insgesamt")
    return frozenset(s + timedelta(days=i) for s, e in merged for i in range((e - s).days + 1))
