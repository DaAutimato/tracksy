"""Rückfrage nach dem Ruhezustand: Task fortsetzen, beenden oder pausiert lassen.

Beim Einschlafen pausiert die App den laufenden Task rückwirkend zum Zeitpunkt des
Einschlafens (``TrackerService.pause_at``). Nach dem Aufwachen/Entsperren fragt sie:

- „Fortsetzen“ → Pause endet jetzt.
- „Beenden“ → Stopp mit Ende = Einschlafen, danach der übliche Ergänzungsdialog.
- „Pausiert lassen“ → nichts ändern. **Enter und Esc** wählen diese Antwort: direkt
  nach dem Entsperren landet leicht ein Enter (Passwort, Tastenwiederholung) im
  Fenster; es soll weder Zeit zählen lassen noch einen Task abschließen.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

import gi

gi.require_version("Adw", "1")
from gi.repository import Adw  # noqa: E402

from .gap_dialog import gap_period, task_name  # noqa: E402
from .viewmodel import format_date_short, format_hhmm  # noqa: E402
from .widgets import focus_response  # noqa: E402

RESP_LATER = "later"
RESP_END = "end"
RESP_RESUME = "resume"
LABEL_LATER = "Pausiert lassen"
LABEL_END = "Beenden"
LABEL_RESUME = "Fortsetzen"


def _clock_at(at: datetime, now: datetime) -> str:
    a = at.astimezone()
    if a.date() == now.astimezone().date():
        return f"{format_hhmm(a)} Uhr"
    return f"{format_date_short(a.date())} {format_hhmm(a)} Uhr"


def sleep_texts(status: dict[str, Any], at: datetime, now: datetime) -> tuple[str, str]:
    seconds = max(0, int((now - at).total_seconds()))
    period = gap_period(at, seconds)
    body = (f"Der Rechner war {period} im Ruhezustand. „{task_name(status)}“ ist seitdem "
            f"pausiert.\n\nBeenden setzt das Ende auf {_clock_at(at, now)}.")
    return "Task fortsetzen?", body


def result_toast(response: str) -> str | None:
    return {RESP_RESUME: "Weiter geht’s", RESP_LATER: "Task bleibt pausiert"}.get(response)


SLEEP_HINT = "Der Rechner war im Ruhezustand – der Task bleibt pausiert."


def build_sleep_dialog(status: dict[str, Any], at: datetime, now: datetime,
                       on_response: Callable[[str], None]) -> Adw.AlertDialog:
    heading, body = sleep_texts(status, at, now)
    dlg = Adw.AlertDialog(heading=heading, body=body)
    dlg.add_response(RESP_LATER, LABEL_LATER)
    dlg.add_response(RESP_END, LABEL_END)
    dlg.add_response(RESP_RESUME, LABEL_RESUME)
    dlg.set_default_response(RESP_LATER)
    dlg.set_close_response(RESP_LATER)
    focus_response(dlg, LABEL_LATER)  # Enter nach dem Entsperren: nichts ändern
    dlg.zeit_kind = "sleep"  # type: ignore[attr-defined]  # für Tests
    dlg.connect("response", lambda _d, resp: on_response(resp))
    return dlg
