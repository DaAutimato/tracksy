"""Export: XLSX-Leistungsnachweis und CSV, Rundung beim Export.

Typischer Ablauf (UI/CLI)::

    from zeiterfassung.export import export_customer_month
    path = export_customer_month(all_entries, export_dir, customer="Müller GmbH",
                                 month="2026-10", employee_name="Max", fmt="xlsx")

Oder einzeln: :func:`filter_entries` → :func:`suggest_filename` →
:func:`export_xlsx` / :func:`export_csv`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..core.worktime import WorkSchedule
from .common import (
    DEFAULT_ROUND_MINUTES,
    ExportEntry,
    billable_seconds,
    filter_entries,
    month_label,
    net_seconds,
    parse_month,
    suggest_filename,
    work_split,
)
from .csv_export import CSV_COLUMNS, export_csv, render_csv
from .summary import DaySummary, summarize_by_day

#: Anzeige- und Dateiname für den Gesamtexport aller Kunden
ALL_CUSTOMERS = "Alle Kunden"
from .xlsx import TemplateError, check_template, describe_entry, export_xlsx

__all__ = [
    "ALL_CUSTOMERS",
    "CSV_COLUMNS",
    "DEFAULT_ROUND_MINUTES",
    "DaySummary",
    "ExportEntry",
    "TemplateError",
    "billable_seconds",
    "check_template",
    "describe_entry",
    "export_csv",
    "export_customer_month",
    "export_xlsx",
    "filter_entries",
    "month_label",
    "net_seconds",
    "parse_month",
    "render_csv",
    "suggest_filename",
    "summarize_by_day",
    "work_split",
    "WorkSchedule",
]


def export_customer_month(
    entries: Iterable[Any],
    directory: str | os.PathLike[str],
    *,
    customer: str | None,
    month: date | datetime | str,
    employee_name: str = "",
    fmt: str = "xlsx",
    round_minutes: int = DEFAULT_ROUND_MINUTES,
    template_path: str | os.PathLike[str] | None = None,
    summary: bool = False,
    schedule: WorkSchedule | None = None,
) -> Path:
    """Filtern (Kunde, Monat, nur gespeicherte) und in ``directory`` exportieren.

    ``customer=None``: Gesamtexport aller Kunden (Datei „…_Alle_Kunden_…“, im XLSX
    steht der Kunde in jeder Zeile).
    ``summary=True``: Monatsbericht – je Tag, Kunde und Ort eine Zeile
    (:func:`summarize_by_day`); sonst Tätigkeitsbericht mit jedem Eintrag einzeln.
    ``schedule``: Regelarbeitszeit für „nach reg. GZ“ (``None`` = Mo–Fr 08:00–17:00).

    Der Dateiname wird mit :func:`suggest_filename` sicher gebildet; eine
    vorhandene Datei gleichen Namens wird ersetzt. Rückgabe: Pfad der Datei.
    """
    selected = filter_entries(entries, customer=customer, month=month)
    fmt = fmt.lower().lstrip(".")
    if fmt not in ("xlsx", "csv"):
        raise ValueError(f"Unbekanntes Exportformat: {fmt!r}")
    all_customers = customer is None
    if all_customers:
        customer = ALL_CUSTOMERS
    elif selected:  # Schreibweise aus den Daten übernehmen (Filter ist case-insensitiv)
        customer = str(getattr(selected[0], "customer", customer) or customer).strip()
    target = Path(directory) / suggest_filename(customer, month, fmt, summary=summary)
    if summary:
        selected = summarize_by_day(selected)
    if fmt == "csv":
        return export_csv(selected, target, round_minutes=round_minutes, schedule=schedule)
    return export_xlsx(
        selected, target, customer=customer, month=month, employee_name=employee_name,
        round_minutes=round_minutes, template_path=template_path, show_customer=all_customers,
        schedule=schedule,
    )
