"""CSV-Export (Semikolon, UTF-8 mit BOM, Dezimalkomma).

Gedacht als Import-Grundlage für Tabellenkalkulation bzw. später
Lexware/sevDesk/Google Sheets. Eine Zeile pro Eintrag; ``Datum`` ist das
Startdatum (Mitternachtsregel, siehe :mod:`.common`).
"""

from __future__ import annotations

import csv
import io
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..core.sanitize import protect_formula
from ..core.worktime import Category, WorkSchedule
from .common import (
    DEFAULT_ROUND_MINUTES,
    items_of,
    local_naive,
    location_of,
    net_seconds,
    resolve_schedule,
    secure_write,
    text_of,
    work_split,
)

__all__ = ["CSV_COLUMNS", "export_csv", "render_csv", "format_decimal"]

CSV_COLUMNS = (
    "Datum",
    "Kunde",
    "Thema",
    "Ticket",
    "Ort",
    "Beginn",
    "Ende",
    "Pause_min",
    "Dauer_netto_h",
    "Dauer_abgerechnet_h",
    "Beschreibung",
    "Ergänzung",
    "Weitere_Tätigkeiten",
    # Regelarbeitszeit (gerundet wie Dauer_abgerechnet_h; reg. + nach reg. = abgerechnet)
    "Dauer_reg_GZ_h",
    "Beginn_nach_reg_GZ",
    "Ende_nach_reg_GZ",
    "Dauer_nach_reg_GZ_h",
    "davon_werktags_h",
    "davon_Samstag_h",
    "davon_Sonn_Feiertag_h",
    "Stunden_gewichtet",
)
# Neue Spalten bewusst am Ende: bestehende Importe nach Spaltenposition bleiben gültig.
# Keine UUID im Leistungsnachweis: interne Kennung, für den Kunden ohne Nutzen
# (bleibt im internen CSV-Protokoll ``protokoll.csv`` erhalten).

_LOCATION_LABEL = {"remote": "Remote", "onsite": "Vor Ort"}  # wie in der Oberfläche


def format_decimal(value: float, ndigits: int = 2) -> str:
    """Zahl mit deutschem Dezimalkomma, ohne Tausendertrennzeichen."""
    return f"{value:.{ndigits}f}".replace(".", ",")


def _safe_text(value: str, protect_formulas: bool) -> str:
    """Schutz vor CSV-/Formel-Injection beim Öffnen in Tabellenkalkulationen."""
    return protect_formula(value) if protect_formulas else value


def _hhmm(dt: Any) -> str:
    return local_naive(dt).strftime("%H:%M") if dt is not None else ""


def _row(entry: Any, round_minutes: int, protect: bool,
         schedule: WorkSchedule | None = None) -> list[str]:
    schedule = resolve_schedule(schedule)
    sp = work_split(entry, schedule, round_minutes)
    h = lambda sec: format_decimal(sec / 3600)  # noqa: E731
    start = entry.start_at
    end = entry.end_at
    pause_s = int(getattr(entry, "pause_seconds", 0) or 0)
    t = lambda name: _safe_text(text_of(entry, name), protect)  # noqa: E731
    return [
        start.strftime("%d.%m.%Y"),
        t("customer"),
        t("topic"),
        t("ticket"),
        _LOCATION_LABEL[location_of(entry)],
        start.strftime("%H:%M"),
        end.strftime("%H:%M") if end is not None else "",
        str(int(round(pause_s / 60))),
        format_decimal(net_seconds(entry) / 3600),
        h(sp.total_seconds),  # = billable_seconds(entry, round_minutes, schedule)
        t("description"),
        t("addendum"),
        _safe_text(" | ".join(items_of(entry)), protect),
        h(sp.regular_seconds),
        _hhmm(sp.outside_start),
        _hhmm(sp.outside_end),
        h(sp.outside_seconds),
        h(sp.get(Category.WEEKDAY)),
        h(sp.get(Category.SATURDAY)),
        h(sp.get(Category.SUNDAY)),
        h(sp.weighted_seconds(schedule)),
    ]


def render_csv(
    entries: Iterable[Any],
    *,
    round_minutes: int = DEFAULT_ROUND_MINUTES,
    protect_formulas: bool = True,
    schedule: WorkSchedule | None = None,
) -> str:
    """CSV-Inhalt als Text (ohne BOM). Zeilenende CRLF.

    Alle Felder stehen in Anführungszeichen: So bleibt ``"0,25"`` auch dann eine
    Zahl, wenn beim Import versehentlich zusätzlich das Komma als Trenner aktiv ist.
    """
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quotechar='"', quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for e in entries:
        w.writerow(_row(e, round_minutes, protect_formulas, schedule))
    return buf.getvalue()


def export_csv(
    entries: Iterable[Any],
    path: str | os.PathLike[str],
    *,
    round_minutes: int = DEFAULT_ROUND_MINUTES,
    protect_formulas: bool = True,
    schedule: WorkSchedule | None = None,
) -> Path:
    """Einträge als CSV schreiben (UTF-8 mit BOM, Rechte 0600).

    ``entries`` wird unverändert übernommen – vorher ggf. mit
    :func:`zeiterfassung.export.filter_entries` filtern.
    ``protect_formulas`` stellt Texten, die (auch nach führendem Leerraum) mit
    ``= + - @`` bzw. mit Tab/CR/LF beginnen, ein Apostroph voran (Schutz vor Formel-Injection in Excel/Calc/Sheets).
    ``schedule``: Regelarbeitszeit für die Spalten „nach reg. GZ“ (``None`` = Vorgabe).
    """
    data = render_csv(entries, round_minutes=round_minutes, protect_formulas=protect_formulas,
                      schedule=schedule)
    payload = data.encode("utf-8-sig")
    return secure_write(path, lambda fh: fh.write(payload))
