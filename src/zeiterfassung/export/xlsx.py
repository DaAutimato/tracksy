"""XLSX-Export „Leistungsnachweis“ (ein Kunde, ein Monat).

Standard: eigenes, neu aufgebautes Arbeitsblatt im Layout eines üblichen
Leistungsnachweises (Spalten A–J), Titel „Leistungsnachweis“.

Optional: ``template_path`` – eine eigene Vorlage des Nutzers wird befüllt.
Positionen werden über Beschriftungen gesucht (``Kunde:``, ``Mitarbeiter:``,
``KW/Monat:``/``Monat:``, ``Datum:``, Tabellenkopf mit ``DATUM`` …
``BESCHREIBUNG``, Zeile ``SUMME``). Reichen die Datenzeilen nicht, werden
Zeilen vor ``SUMME`` eingefügt (Formeln, verbundene Zellen, Bilder und
Zeilenhöhen darunter werden mitverschoben).

Spaltenbelegung (wie Vorlage):
  A Datum · B Beginn · C Ende · D Pause (h:mm) · E Stunden Remote ·
  F Stunden Onsite (nur innerhalb der Regelarbeitszeit; Dezimalstunden, gerundet) ·
  G Beginn / H Ende der Arbeit nach reg. GZ (erstes/letztes Segment außerhalb) ·
  I Stunden nach reg. GZ · J Beschreibung (Thema/Ticket, Beschreibung, Ergänzung,
  weitere Tätigkeiten) · K/L/M davon werktags / Samstag / Sonn-/Feiertag (eigenes Layout;
  in Vorlagen optional, erkannt an „werktag“/„samstag“/„sonntag“/„feiertag“ im Kopf).
E + F + I = abgerechnete (gerundete) Stunden des Eintrags. Summen und die
gewichtete Gesamtstundenzahl (Faktoren aus der Regelarbeitszeit) werden als
Excel-Formeln geschrieben.
"""

from __future__ import annotations

import math
import os
import re
import zipfile
from collections.abc import Iterable
from copy import copy
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.formula.translate import Translator
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from ..core.worktime import Category, WorkSchedule
from .common import (
    DEFAULT_ROUND_MINUTES,
    MONTH_NAMES_DE,
    items_of,
    local_naive,
    location_of,
    parse_month,
    resolve_schedule,
    secure_write,
    text_of,
    work_split,
)

__all__ = [
    "export_xlsx", "describe_entry", "TemplateError", "check_template",
    "MAX_TEMPLATE_BYTES", "MAX_TEMPLATE_UNPACKED_BYTES", "MAX_TEMPLATE_MEMBERS",
]

#: Grenzen für Nutzer-Vorlagen (Schutz vor Zip-Bomben/Speicherfressern)
MAX_TEMPLATE_BYTES = 10 * 1024 * 1024
MAX_TEMPLATE_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_TEMPLATE_MEMBERS = 10_000


class TemplateError(ValueError):
    """Vorlage hat nicht die erwartete Struktur bzw. überschreitet Grenzen."""


def check_template(path: str | os.PathLike[str]) -> Path:
    """Vorlage vor ``openpyxl.load_workbook`` prüfen.

    Datei ≤ ``MAX_TEMPLATE_BYTES``, gültiges ZIP, ≤ ``MAX_TEMPLATE_MEMBERS``
    Einträge, Summe der entpackten Größen ≤ ``MAX_TEMPLATE_UNPACKED_BYTES``.
    (``zipfile`` liest nie mehr als die deklarierte Größe je Eintrag.)
    Fehler → :class:`TemplateError`.
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise TemplateError("Vorlage nicht lesbar") from exc
    if not p.is_file():
        raise TemplateError("Vorlage ist keine Datei")
    if size > MAX_TEMPLATE_BYTES:
        raise TemplateError(
            f"Vorlage zu groß ({size // (1024 * 1024)} MB, höchstens "
            f"{MAX_TEMPLATE_BYTES // (1024 * 1024)} MB)"
        )
    try:
        with zipfile.ZipFile(p) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise TemplateError("Vorlage ist keine gültige XLSX-Datei") from exc
    if len(infos) > MAX_TEMPLATE_MEMBERS:
        raise TemplateError("Vorlage enthält zu viele Bestandteile")
    unpacked = sum(i.file_size for i in infos)
    if unpacked > MAX_TEMPLATE_UNPACKED_BYTES:
        raise TemplateError(
            f"Vorlage entpackt zu groß (höchstens {MAX_TEMPLATE_UNPACKED_BYTES // (1024 * 1024)} MB)"
        )
    return p


# --------------------------------------------------------------------------
# Zeilendaten
# --------------------------------------------------------------------------
@dataclass
class _Row:
    day: date
    start: time
    end: time | None
    pause: time | float
    remote_h: float | None
    onsite_h: float | None
    text: str
    after_start: time | None = None
    after_end: time | None = None
    after_h: float | None = None
    weekday_h: float | None = None
    saturday_h: float | None = None
    sunday_h: float | None = None


def _clean(value: str) -> str:
    return ILLEGAL_CHARACTERS_RE.sub("", value)


def describe_entry(entry: Any) -> str:
    """Beschreibungstext für Spalte J (mehrzeilig).

    Zeile 1: Thema und Ticket; dann Beschreibung, Ergänzung, weitere
    Tätigkeiten (je ``• …``) und ggf. Hinweis auf Ende am Folgetag.
    Zusammengefasster Tag (Monatsbericht): je Eintrag ein Block.
    """
    members = getattr(entry, "members", None)
    if members is not None:
        return _clean("\n".join(describe_entry(m) for m in members))
    topic = text_of(entry, "topic")
    ticket = text_of(entry, "ticket")
    head = topic
    if ticket:
        head = f"{topic} (Ticket {ticket})" if topic else f"Ticket {ticket}"
    lines = [head] if head else []
    for name in ("description", "addendum"):
        val = text_of(entry, name)
        if val and val not in lines:
            lines.append(val)
    lines.extend(f"• {it}" for it in items_of(entry))
    end = entry.end_at
    if end is not None and end.date() != entry.start_at.date():
        lines.append(f"(Ende am {end.strftime('%d.%m.%Y')})")
    return _clean("\n".join(lines))


def _as_time(seconds: int) -> time | float:
    if 0 <= seconds < 86400:
        return time(seconds // 3600, (seconds % 3600) // 60, seconds % 60)
    return seconds / 86400  # Excel-Tagesbruchteil (≥ 24 h)


def _hours(seconds: int) -> float | None:
    return seconds / 3600 if seconds else None


def _rows(entries: Iterable[Any], round_minutes: int, show_customer: bool = False,
          schedule: WorkSchedule | None = None) -> list[_Row]:
    schedule = resolve_schedule(schedule)
    rows = []
    for e in entries:
        start = local_naive(e.start_at)
        end = local_naive(e.end_at) if e.end_at is not None else None
        sp = work_split(e, schedule, round_minutes)
        hours = sp.regular_seconds / 3600  # E/F: nur Anteil innerhalb der Regelarbeitszeit
        onsite = location_of(e) == "onsite"
        rows.append(
            _Row(
                day=start.date(),
                start=start.time(),
                end=end.time() if end else None,
                pause=_as_time(int(getattr(e, "pause_seconds", 0) or 0)),
                remote_h=None if onsite else hours,
                onsite_h=hours if onsite else None,
                text=_with_customer(e, describe_entry(e)) if show_customer else describe_entry(e),
                after_start=sp.outside_start.time() if sp.outside_seconds and sp.outside_start else None,
                after_end=sp.outside_end.time() if sp.outside_seconds and sp.outside_end else None,
                after_h=_hours(sp.outside_seconds),
                weekday_h=_hours(sp.get(Category.WEEKDAY)),
                saturday_h=_hours(sp.get(Category.SATURDAY)),
                sunday_h=_hours(sp.get(Category.SUNDAY)),
            )
        )
    return rows


def factor_label(value: float) -> str:
    """Faktor für Beschriftungen: 1.25 → ``1,25``, 2.0 → ``2``."""
    return f"{value:g}".replace(".", ",")


def _with_customer(entry: Any, text: str) -> str:
    cust = _clean(text_of(entry, "customer"))
    return f"{cust}\n{text}" if cust else text


def _set_text(cell: Any, value: str) -> None:
    """Text immer als String schreiben (kein ungewollter Formel-Typ bei '=…')."""
    cell.value = _clean(value) if value else None
    if value:
        cell.data_type = "s"


def _row_height(text: str, col_width: float, base: float = 15.75) -> float:
    chars = max(10, int(col_width * 1.15))
    lines = sum(max(1, math.ceil(len(line) / chars)) for line in (text.split("\n") or [""]))
    return max(base, round(lines * 13.0 + 3, 2))


# --------------------------------------------------------------------------
# Eigenes Layout
# --------------------------------------------------------------------------
_HEADERS = (
    "DATUM",
    "BEGINN",
    "ENDE",
    "PAUSE",
    "# STUNDEN\nreg. GZ / Remote",
    "# STUNDEN\nreg. GZ / Onsite",
    "BEGINN\nnach reg. GZ",
    "ENDE\nnach reg. GZ",
    "# STUNDEN\nnach reg. GZ",
    "BESCHREIBUNG TÄTIGKEIT",
)
_WIDTHS = (14, 8, 8, 8, 12, 12, 11, 11, 11, 60, 11, 11, 11)
NCOLS = len(_WIDTHS)  # A–M
TEXT_COL = 10  # J


def _category_headers(schedule: WorkSchedule) -> tuple[str, str, str]:
    return (
        f"davon\nwerktags\n(×{factor_label(schedule.weekday_factor)})",
        f"davon\nSamstag\n(×{factor_label(schedule.saturday_factor)})",
        f"davon\nSonn-/\nFeiertag\n(×{factor_label(schedule.sunday_factor)})",
    )

FMT_DATE = "DD.MM.YYYY"
FMT_DATE_ROW = "[$-407]DD.MM. DDD"
FMT_TIME = "hh:mm"
FMT_DURATION = "[h]:mm"
FMT_HOURS = "0.00"

_FONT = "Liberation Sans"
_HEAD_FILL = PatternFill("solid", fgColor="FF3A4A5C")
_SUM_FILL = PatternFill("solid", fgColor="FFDDE3EA")
_LIGHT_FILL = PatternFill("solid", fgColor="FFF3F3F3")
_HAIR = Side(style="hair", color="FF808080")
_THIN = Side(style="thin", color="FF000000")
_GRID = Border(left=_HAIR, right=_HAIR, top=_HAIR, bottom=_HAIR)


def _build_sheet(
    ws: Worksheet,
    rows: list[_Row],
    *,
    customer: str,
    employee_name: str,
    year: int,
    month: int,
    created: date,
    schedule: WorkSchedule | None = None,
) -> None:
    schedule = resolve_schedule(schedule)
    f = lambda **kw: Font(name=_FONT, size=kw.pop("size", 10), **kw)  # noqa: E731
    for i, w in enumerate(_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws["A1"].value = "Leistungsnachweis"
    ws["A1"].font = f(size=18, bold=True)
    ws.row_dimensions[1].height = 28

    head = (
        ("Kunde:", customer),
        ("Mitarbeiter:", employee_name),
        ("Monat:", f"{MONTH_NAMES_DE[month - 1]} {year}"),
        ("Erstellt am:", None),
    )
    for offset, (label, value) in enumerate(head):
        r = 3 + offset
        ws.cell(r, 1, label).font = f(size=11, bold=True)
        c = ws.cell(r, 3)
        if value is None:
            c.value = created
            c.number_format = FMT_DATE
            c.alignment = Alignment(horizontal="left")
        else:
            _set_text(c, value)
        c.font = f(size=11)
        ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=6)

    hdr = 8
    for col, text in enumerate(_HEADERS + _category_headers(schedule), start=1):
        c = ws.cell(hdr, col, text)
        c.font = f(bold=True, color="FFFFFFFF")
        c.fill = _HEAD_FILL
        c.alignment = Alignment(wrap_text=True, vertical="center",
                                horizontal="left" if col == TEXT_COL else "center")
        c.border = _GRID
    ws.row_dimensions[hdr].height = 56

    first = hdr + 1
    data = rows or [None]  # mindestens eine (leere) Zeile, damit SUM-Bereiche gültig sind
    for i, row in enumerate(data):
        r = first + i
        for col in range(1, NCOLS + 1):
            c = ws.cell(r, col)
            c.border = _GRID
            c.font = f()
            c.alignment = Alignment(vertical="top", wrap_text=(col == TEXT_COL))
        ws.cell(r, 1).number_format = FMT_DATE_ROW
        for col in (2, 3, 7, 8):
            ws.cell(r, col).number_format = FMT_TIME
        ws.cell(r, 4).number_format = FMT_DURATION
        for col in (5, 6, 9, 11, 12, 13):
            ws.cell(r, col).number_format = FMT_HOURS
        if row is None:
            continue
        ws.cell(r, 1).value = row.day
        ws.cell(r, 2).value = row.start
        ws.cell(r, 3).value = row.end
        ws.cell(r, 4).value = row.pause
        ws.cell(r, 5).value = row.remote_h
        ws.cell(r, 6).value = row.onsite_h
        ws.cell(r, 7).value = row.after_start
        ws.cell(r, 8).value = row.after_end
        ws.cell(r, 9).value = row.after_h
        ws.cell(r, 11).value = row.weekday_h
        ws.cell(r, 12).value = row.saturday_h
        ws.cell(r, 13).value = row.sunday_h
        _set_text(ws.cell(r, TEXT_COL), row.text)
        ws.row_dimensions[r].height = _row_height(row.text, _WIDTHS[9])
    last = first + len(data) - 1

    s = last + 1
    for col in range(1, NCOLS + 1):
        c = ws.cell(s, col)
        c.fill = _SUM_FILL
        c.font = f(bold=True)
        c.border = _GRID
    ws.cell(s, 1, "SUMME")
    ws.cell(s, 4, f"=SUM(D{first}:D{last})").number_format = FMT_DURATION
    for col in ("E", "F", "I", "K", "L", "M"):
        c = ws[f"{col}{s}"]
        c.value = f"=SUM({col}{first}:{col}{last})"
        c.number_format = FMT_HOURS

    # Auswertung: Stunden × Faktor = gewichtete Stunden (Faktoren als Zahl in Spalte F,
    # damit sie im Blatt sichtbar und nachrechenbar sind)
    fl = factor_label
    summary = (
        ("STUNDEN REMOTE reg. GZ", f"=E{s}", 1.0),
        ("STUNDEN ONSITE reg. GZ", f"=F{s}", 1.0),
        (f"STUNDEN nach reg. GZ werktags (×{fl(schedule.weekday_factor)})", f"=K{s}",
         schedule.weekday_factor),
        (f"STUNDEN nach reg. GZ Samstag (×{fl(schedule.saturday_factor)})", f"=L{s}",
         schedule.saturday_factor),
        (f"STUNDEN nach reg. GZ Sonn-/Feiertag (×{fl(schedule.sunday_factor)})", f"=M{s}",
         schedule.sunday_factor),
    )
    head = s + 2
    for col, label in ((5, "STUNDEN"), (6, "FAKTOR"), (7, "GEWICHTET")):
        c = ws.cell(head, col, label)
        c.font = f(bold=True, size=9)
        c.alignment = Alignment(horizontal="center")
    top = head + 1
    for i, (label, formula, factor) in enumerate(summary):
        r = top + i
        ws.cell(r, 1, label).font = f(bold=True)
        ws.cell(r, 1).fill = _LIGHT_FILL
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
        for col, value in ((5, formula), (6, factor), (7, f"=E{r}*F{r}")):
            c = ws.cell(r, col, value)
            c.number_format = FMT_HOURS
            c.font = f(bold=col != 6)
            c.fill = _LIGHT_FILL
    total = top + len(summary)
    ws.cell(total, 1, "STUNDEN GESAMT").font = f(bold=True, color="FFFFFFFF")
    ws.merge_cells(start_row=total, start_column=1, end_row=total, end_column=4)
    for col, value in ((1, None), (5, f"=SUM(E{top}:E{total - 1})"), (6, None),
                       (7, f"=SUM(G{top}:G{total - 1})")):
        c = ws.cell(total, col)
        if value is not None:
            c.value = value
            c.number_format = FMT_HOURS
            c.font = f(bold=True, color="FFFFFFFF")
        c.fill = _HEAD_FILL
    note = ws.cell(total + 1, 5, "Spalte G: gewichtete Stunden (Stunden × Faktor)")
    note.font = f(size=8, italic=True)

    sig = total + 5
    for start_col, end_col, label in ((1, 4, "Ort, Datum, Unterschrift Auftragnehmer"),
                                      (8, 10, "Ort, Datum, Name, Unterschrift Kunde")):
        for col in range(start_col, end_col + 1):
            ws.cell(sig, col).border = Border(top=_THIN)
        ws.cell(sig, start_col, label).font = f(size=9)
        ws.merge_cells(start_row=sig, start_column=start_col, end_row=sig, end_column=end_col)

    # Druck
    ws.freeze_panes = ws.cell(first, 1)
    ws.print_title_rows = f"{hdr}:{hdr}"
    ws.print_area = f"A1:{get_column_letter(NCOLS)}{sig}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.oddFooter.center.text = "Seite &P von &N"
    ws.oddFooter.center.size = 8
    ws.sheet_view.showGridLines = False


# --------------------------------------------------------------------------
# Vorlage befüllen
# --------------------------------------------------------------------------
def _norm(value: Any) -> str:
    return " ".join(str(value).split()).casefold() if isinstance(value, str) else ""


@dataclass
class _TemplateMap:
    customer: tuple[int, int] | None
    employee: tuple[int, int] | None
    month: tuple[int, int] | None
    year: tuple[int, int] | None
    header_row: int
    sum_row: int
    cols: dict[str, int]
    max_col: int


def _value_cell(ws: Worksheet, row: int, col: int) -> tuple[int, int]:
    """Wertzelle rechts neben einer Beschriftung: erste belegte Zelle (Platzhalter)
    in den nächsten 4 Spalten, sonst Spalte + 2 (wie Vorlage: A→C)."""
    for c in range(col + 1, col + 5):
        if ws.cell(row, c).value not in (None, ""):
            return row, c
    return row, col + 2


def _map_template(ws: Worksheet) -> _TemplateMap:
    labels: dict[str, tuple[int, int]] = {}
    header_row = None
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 80), max_col=min(ws.max_column, 30)):
        for c in row:
            n = _norm(c.value)
            if n in ("kunde:", "mitarbeiter:", "kw/monat:", "monat:", "datum:") and n not in labels:
                labels[n] = (c.row, c.column)
            if n == "datum" and header_row is None:
                header_row = c.row
    if header_row is None:
        raise TemplateError("Tabellenkopf mit 'DATUM' nicht gefunden")

    cols: dict[str, int] = {}
    for c in ws[header_row]:
        n = _norm(c.value)
        if not n:
            continue
        after = "nach" in n
        if "werktag" in n:
            cols.setdefault("weekday", c.column)
        elif "samstag" in n:
            cols.setdefault("saturday", c.column)
        elif "sonntag" in n or "feiertag" in n:
            cols.setdefault("sunday", c.column)
        elif n == "datum":
            cols.setdefault("date", c.column)
        elif n.startswith("beginn"):
            cols.setdefault("start_after" if after else "start", c.column)
        elif n.startswith("ende"):
            cols.setdefault("end_after" if after else "end", c.column)
        elif n.startswith("pause"):
            cols.setdefault("pause", c.column)
        elif "remote" in n:
            cols.setdefault("remote", c.column)
        elif "onsite" in n or "vor ort" in n:
            cols.setdefault("onsite", c.column)
        elif "stunden" in n and after:
            cols.setdefault("hours_after", c.column)
        elif "beschreibung" in n or "tätigkeit" in n:
            cols.setdefault("text", c.column)
    missing = {"date", "start", "end", "pause", "remote", "onsite", "text"} - cols.keys()
    if missing:
        raise TemplateError(f"Spalten im Tabellenkopf fehlen: {', '.join(sorted(missing))}")

    sum_row = None
    for r in range(header_row + 1, ws.max_row + 1):
        if any(_norm(ws.cell(r, c).value) == "summe" for c in range(1, 4)):
            sum_row = r
            break
    if sum_row is None:
        raise TemplateError("Zeile 'SUMME' unter dem Tabellenkopf nicht gefunden")

    def lv(*keys: str) -> tuple[int, int] | None:
        for k in keys:
            if k in labels:
                return _value_cell(ws, *labels[k])
        return None

    return _TemplateMap(
        customer=lv("kunde:"),
        employee=lv("mitarbeiter:"),
        month=lv("kw/monat:", "monat:"),
        year=lv("datum:"),
        header_row=header_row,
        sum_row=sum_row,
        cols=cols,
        max_col=max(max(cols.values()), ws.max_column),
    )


def _insert_rows(ws: Worksheet, at: int, amount: int, style_row: int | None, max_col: int) -> None:
    """``amount`` Zeilen vor ``at`` einfügen und Abhängiges darunter mitschieben."""
    formulas = [
        (c.row, c.column, c.value)
        for row in ws.iter_rows(min_row=at)
        for c in row
        if isinstance(c.value, str) and c.value.startswith("=")
    ]
    merged = [mr for mr in ws.merged_cells.ranges if mr.min_row >= at]
    for mr in merged:
        ws.merged_cells.remove(mr)
    heights = {r: d.height for r, d in ws.row_dimensions.items() if r >= at and d.height is not None}

    ws.insert_rows(at, amount)

    for r, col, formula in formulas:
        origin = f"{get_column_letter(col)}{r}"
        dest = f"{get_column_letter(col)}{r + amount}"
        ws.cell(r + amount, col).value = Translator(formula, origin=origin).translate_formula(dest)
    for mr in merged:
        mr.shift(0, amount)
        ws.merged_cells.add(mr)
    for r in sorted(heights, reverse=True):
        ws.row_dimensions[r + amount].height = heights[r]
    for r in range(at, at + amount):
        ws.row_dimensions[r].height = ws.row_dimensions[style_row].height if style_row else None
        if style_row:
            for col in range(1, max_col + 1):
                src = ws.cell(style_row, col)
                if src.has_style:
                    ws.cell(r, col)._style = copy(src._style)
    for img in getattr(ws, "_images", []):
        anchor = img.anchor
        frm = getattr(anchor, "_from", None)
        if frm is not None and frm.row >= at - 1:
            frm.row += amount
            to = getattr(anchor, "to", None)
            if to is not None:
                to.row += amount


def _fill_template(
    ws: Worksheet,
    rows: list[_Row],
    *,
    customer: str,
    employee_name: str,
    year: int,
    month: int,
    schedule: WorkSchedule | None = None,
) -> None:
    schedule = resolve_schedule(schedule)
    m = _map_template(ws)
    if m.customer:
        _set_text(ws.cell(*m.customer), customer)
    if m.employee:
        _set_text(ws.cell(*m.employee), employee_name)
    if m.month:
        _set_text(ws.cell(*m.month), MONTH_NAMES_DE[month - 1])
    if m.year:
        ws.cell(*m.year).value = year  # Vorlage: "Datum:" enthält das Jahr

    first = m.header_row + 1
    capacity = m.sum_row - first
    if len(rows) > capacity:
        extra = len(rows) - capacity
        _insert_rows(ws, m.sum_row, extra, m.sum_row - 1 if capacity > 0 else None, m.max_col)
        m.sum_row += extra
    last = max(first, m.sum_row - 1)

    # Datenbereich leeren (Beispielwerte der Vorlage), Formatierung bleibt.
    for r in range(first, m.sum_row):
        for col in range(1, m.max_col + 1):
            ws.cell(r, col).value = None

    c = m.cols
    text_width = ws.column_dimensions[get_column_letter(c["text"])].width or 45
    for i, row in enumerate(rows):
        r = first + i
        ws.cell(r, c["date"]).value = row.day
        for key, value, fmt in (("start", row.start, FMT_TIME), ("end", row.end, FMT_TIME),
                                ("pause", row.pause, FMT_DURATION)):
            cell = ws.cell(r, c[key])
            cell.value = value
            cell.number_format = fmt  # Vorlage ist hier uneinheitlich (h:mm:ss)
        for key, value in (("start_after", row.after_start), ("end_after", row.after_end)):
            if key in c:
                cell = ws.cell(r, c[key])
                cell.value = value
                cell.number_format = FMT_TIME
        for key, value in (("remote", row.remote_h), ("onsite", row.onsite_h),
                           ("hours_after", row.after_h), ("weekday", row.weekday_h),
                           ("saturday", row.saturday_h), ("sunday", row.sunday_h)):
            if key not in c:
                continue  # Spalten „nach reg. GZ“ sind in Vorlagen optional
            cell = ws.cell(r, c[key])
            cell.value = value
            cell.number_format = FMT_HOURS
        _set_text(ws.cell(r, c["text"]), row.text)
        for col in range(1, m.max_col + 1):
            cell = ws.cell(r, col)
            al = copy(cell.alignment)
            al.vertical = "top"
            if col == c["text"]:
                al.wrap_text = True
                al.horizontal = "left"
            cell.alignment = al
        ws.row_dimensions[r].height = _row_height(row.text, text_width)

    for r in range(first + len(rows), m.sum_row):  # ungenutzte Zeilen: Standardhöhe
        ws.row_dimensions[r].height = None

    for key in ("remote", "onsite", "hours_after", "weekday", "saturday", "sunday"):
        if key in c:
            col = get_column_letter(c[key])
            ws.cell(m.sum_row, c[key]).value = f"=SUM({col}{first}:{col}{last})"
    _fix_overtime_formula(ws, m, rows, schedule)


_OVERTIME_RE = re.compile(r"^=\s*\$?([A-Z]{1,3})\$?(\d+)\s*\*\s*\d+(?:[.,]\d+)?\s*$", re.IGNORECASE)


def _fix_overtime_formula(ws: Worksheet, m: _TemplateMap, rows: list[_Row],
                          schedule: WorkSchedule) -> None:
    """Vorlagenformel „Stunden nach reg. GZ × 1,25“ (z. B. ``=I32*1.25``) ersetzen.

    Mit Wochenendfaktoren ist ein fester Faktor falsch. Ersetzt wird nur eine
    Formel der Form ``=<Spalte nach reg. GZ><SUMME-Zeile>*<Zahl>`` unterhalb
    der Summenzeile: Bei Spalten werktags/Samstag/Sonntag in der Vorlage durch
    Bezüge auf deren Summen, sonst durch die Summen als Zahlen
    (z. B. ``=12.5*1.25+4*1.5+0*2``). Andere Formeln bleiben unverändert.
    """
    c = m.cols
    if "hours_after" not in c:
        return
    target_col = get_column_letter(c["hours_after"])
    fw, fs, fu = (schedule.weekday_factor, schedule.saturday_factor, schedule.sunday_factor)
    if all(k in c for k in ("weekday", "saturday", "sunday")):
        refs = [f"{get_column_letter(c[k])}{m.sum_row}" for k in ("weekday", "saturday", "sunday")]
    else:
        sums = [sum(getattr(r, k) or 0.0 for r in rows) for k in ("weekday_h", "saturday_h", "sunday_h")]
        refs = [f"{round(v, 4):g}" for v in sums]
    formula = f"={refs[0]}*{fw:g}+{refs[1]}*{fs:g}+{refs[2]}*{fu:g}"
    for row in ws.iter_rows(min_row=m.sum_row + 1, max_row=min(ws.max_row, m.sum_row + 30)):
        for cell in row:
            v = cell.value
            if not isinstance(v, str):
                continue
            mo = _OVERTIME_RE.match(v)
            if mo and mo.group(1).upper() == target_col and int(mo.group(2)) == m.sum_row:
                cell.value = formula
                for lc in ws[cell.row][: cell.column - 1]:  # Beschriftung links daneben
                    if "nach reg" in _norm(lc.value):
                        _set_text(lc, f"STUNDEN nach reg. GZ gewichtet (werktags ×{factor_label(fw)}, "
                                      f"Sa ×{factor_label(fs)}, So/Feiertag ×{factor_label(fu)})")


# --------------------------------------------------------------------------
# Öffentliche Funktion
# --------------------------------------------------------------------------
def export_xlsx(
    entries: Iterable[Any],
    path: str | os.PathLike[str],
    *,
    customer: str,
    month: date | datetime | str,
    employee_name: str,
    round_minutes: int = DEFAULT_ROUND_MINUTES,
    template_path: str | os.PathLike[str] | None = None,
    created: date | None = None,
    show_customer: bool = False,
    schedule: WorkSchedule | None = None,
) -> Path:
    """Leistungsnachweis als XLSX schreiben (Rechte 0600, atomar).

    ``show_customer``: Kundenname als erste Zeile jeder Beschreibung (Gesamtexport
    aller Kunden).

    ``entries`` wird unverändert übernommen (Reihenfolge = Zeilenreihenfolge) –
    vorher mit :func:`zeiterfassung.export.filter_entries` auf Kunde/Monat
    filtern. ``month``: ``date``/``datetime`` oder ``'JJJJ-MM'``.
    ``created``: Datum für „Erstellt am“ (Default: heute).
    ``schedule``: Regelarbeitszeit und Faktoren (``None`` = Mo–Fr 08:00–17:00,
    1,25/1,5/2,0).
    """
    year, mon = parse_month(month)
    rows = _rows(entries, round_minutes, show_customer, schedule)
    if template_path is not None:
        wb = load_workbook(check_template(template_path))
        ws = wb.active
        _fill_template(ws, rows, customer=customer, employee_name=employee_name, year=year,
                       month=mon, schedule=schedule)
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = f"{MONTH_NAMES_DE[mon - 1]} {year}"
        _build_sheet(
            ws, rows, customer=customer, employee_name=employee_name,
            year=year, month=mon, created=created or date.today(), schedule=schedule,
        )
        wb.properties.title = "Leistungsnachweis"
    # Datenminimierung: keine Autorennamen aus Vorlage/System in den Metadaten
    wb.properties.creator = "Zeiterfassung"
    wb.properties.lastModifiedBy = "Zeiterfassung"
    return secure_write(path, wb.save)
