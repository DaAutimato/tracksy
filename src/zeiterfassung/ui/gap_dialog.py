"""Rückfrage nach einem Absturz/Neustart: Ausfallzeit abziehen oder behalten.

Lief beim Absturz (oder Ausschalten des Rechners) ein Task, zählt die Zeit bis zum
Neustart zunächst mit. Ist die Lücke seit dem letzten Lebenszeichen größer als
``GAP_PROMPT_THRESHOLD_SECONDS``, fragt die App nach. Darunter wird sie still
übernommen (``keep_gap(automatic=True)``), damit der Status sauber ist.

Tastatur: Enter und Esc lösen beide „Als Arbeitszeit behalten“ aus – also das
bisherige Verhalten, der Zeiteintrag bleibt unverändert. Abziehen geht nur per
bewusster Wahl (Tab zum Knopf bzw. Klick), weil es die erfasste Zeit ändert.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

import gi

gi.require_version("Adw", "1")
from gi.repository import Adw  # noqa: E402

from ..core.timeutil import from_iso  # noqa: E402
from ..service import GAP_PROMPT_THRESHOLD_SECONDS  # noqa: E402
from .viewmodel import format_date_short, format_hhmm, shorten, task_view_from_status  # noqa: E402
from .widgets import focus_response  # noqa: E402

ASK = "ask"
KEEP = "keep"
NONE = "none"

RESP_KEEP = "keep"
RESP_SUBTRACT = "subtract"
LABEL_KEEP = "Als Arbeitszeit behalten"
LABEL_SUBTRACT = "Abziehen"

TASK_NAME_LIMIT = 80


def gap_action(status: dict[str, Any] | None,
               threshold: int = GAP_PROMPT_THRESHOLD_SECONDS) -> str:
    """Was nach dem Start mit einer Ausfallzeit geschieht: ``ask`` (Dialog),
    ``keep`` (still übernehmen) oder ``none`` (keine Ausfallzeit)."""
    if not status or status.get("state") != "running":
        return NONE
    seconds = int(status.get("recovered_gap_seconds") or 0)
    if seconds <= 0:
        return NONE
    return ASK if seconds > threshold else KEEP


def format_gap_duration(seconds: int | float) -> str:
    """``45 min``, ``1 h``, ``2 h 5 min`` (auf Minuten gerundet, mindestens 1 min)."""
    minutes = max(1, (max(0, int(seconds)) + 30) // 60)  # kaufmännisch, kein round()
    h, m = divmod(minutes, 60)
    if not h:
        return f"{m} min"
    return f"{h} h {m} min" if m else f"{h} h"


def gap_period(since: datetime, seconds: int) -> str:
    """``von 12:05 bis 13:40 Uhr``; über Tagesgrenzen mit Datum."""
    start = since.astimezone()
    end = (since + timedelta(seconds=int(seconds))).astimezone()
    if start.date() == end.date():
        return f"von {format_hhmm(start)} bis {format_hhmm(end)} Uhr"
    return (f"von {format_date_short(start.date())} {format_hhmm(start)} Uhr "
            f"bis {format_date_short(end.date())} {format_hhmm(end)} Uhr")


def task_name(status: dict[str, Any]) -> str:
    v = task_view_from_status(status)
    parts = [v.customer or "Ohne Kunde"]
    if v.topic:
        parts.append(v.topic)
    if v.ticket:
        parts.append(f"Ticket {v.ticket}")
    return shorten(" – ".join(parts), TASK_NAME_LIMIT)


def gap_texts(status: dict[str, Any]) -> tuple[str, str]:
    """Überschrift und Text des Dialogs."""
    seconds = int(status.get("recovered_gap_seconds") or 0)
    since = from_iso(status.get("recovered_gap_since"))
    duration = format_gap_duration(seconds)
    segments = status.get("recovered_gaps") or []
    if len(segments) > 1:  # mehrere Ausfälle ohne Antwort dazwischen
        periods = ", ".join(gap_period(from_iso(a), int(b)) for a, b in segments)
        when = f"mehrmals ({periods}, zusammen {duration})"
    elif since:
        when = f"{gap_period(since, seconds)} ({duration})"
    else:
        when = f"{duration} lang"
    body = (f"Tracksy war {when} nicht aktiv, während „{task_name(status)}“ lief. "
            "Diese Zeit zählt bisher als Arbeitszeit.\n\n"
            "Abziehen verbucht sie als Pause.")
    return "Ausfallzeit abziehen?", body


def result_toast(response: str, seconds: int) -> str:
    if response == RESP_SUBTRACT:
        return f"Ausfallzeit abgezogen: {format_gap_duration(seconds)}"
    return "Ausfallzeit zählt als Arbeitszeit"


def build_gap_dialog(status: dict[str, Any],
                     on_response: Callable[[str], None]) -> Adw.AlertDialog:
    """``Adw.AlertDialog`` mit „Als Arbeitszeit behalten“ (Enter/Esc) und „Abziehen“.

    ``on_response`` erhält ``RESP_KEEP`` oder ``RESP_SUBTRACT``. Beide Antworten
    sind neutral gestaltet: welche richtig ist, weiß nur der Nutzer."""
    heading, body = gap_texts(status)
    dlg = Adw.AlertDialog(heading=heading, body=body)
    dlg.add_response(RESP_KEEP, LABEL_KEEP)
    dlg.add_response(RESP_SUBTRACT, LABEL_SUBTRACT)
    dlg.set_default_response(RESP_KEEP)
    dlg.set_close_response(RESP_KEEP)
    focus_response(dlg, LABEL_KEEP)  # Enter darf nie „Abziehen“ auslösen
    dlg.zeit_kind = "gap"  # type: ignore[attr-defined]  # für Tests
    dlg.connect("response", lambda _d, resp: on_response(resp))
    return dlg
