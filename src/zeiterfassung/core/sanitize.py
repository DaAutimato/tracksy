"""Zentrale Eingabegrenzen und CSV-Formelschutz.

GUI-frei. Alle Oberflächen (GUI, Tray, CLI, D-Bus, Sprache, Editor) gehen über
``TrackerService``; dort werden diese Prüfungen angewendet. Fehlermeldungen
enthalten nie den Eingabewert selbst (Logs/Toasts).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

__all__ = [
    "MAX_FIELD_LEN",
    "MAX_TEXT_LEN",
    "MAX_ITEMS",
    "MAX_ITEM_LEN",
    "FIELD_LABELS",
    "InputLimitError",
    "clean_text",
    "strip_control",
    "clean_items",
    "protect_formula",
]

#: Kurzfelder: Kunde, Thema, Ticket, Ort, Quelle
MAX_FIELD_LEN = 500
#: Freitexte: Ergänzung, Beschreibung
MAX_TEXT_LEN = 10_000
#: weitere Tätigkeiten: Anzahl und Länge je Eintrag
MAX_ITEMS = 100
MAX_ITEM_LEN = 500

FIELD_LABELS = {
    "customer": "Kunde",
    "topic": "Thema",
    "ticket": "Ticket",
    "location": "Ort",
    "source": "Quelle",
    "description": "Beschreibung",
    "addendum": "Ergänzung",
    "items": "Weitere Tätigkeiten",
}


class InputLimitError(ValueError):
    """Eingabe hat einen unzulässigen Typ oder überschreitet ein Limit.

    Attribute: ``field`` (interner Feldname), ``limit`` (int oder ``None``),
    ``reason`` (``"type"`` | ``"length"`` | ``"count"``). Die Meldung ist
    deutsch und enthält den Eingabewert NICHT.
    """

    def __init__(self, field: str, reason: str, limit: int | None = None):
        self.field = field
        self.reason = reason
        self.limit = limit
        label = FIELD_LABELS.get(field, field)
        if reason == "length":
            msg = f"{label} ist zu lang (höchstens {limit} Zeichen)"
        elif reason == "count":
            msg = f"{label}: zu viele Einträge (höchstens {limit})"
        else:
            msg = f"{label}: ungültiger Datentyp"
        super().__init__(msg)


# C0-Steuerzeichen außer \t \n \r sowie DEL
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LINEBREAK_RE = re.compile(r"[\t\r\n]+")


def strip_control(text: str, multiline: bool = False) -> str:
    """C0-Steuerzeichen (NUL, BEL, ESC …) und DEL entfernen.

    ``multiline=True`` (Freitexte): ``\n`` und ``\t`` bleiben, ``\r\n``/``\r``
    werden zu ``\n``. Sonst (Kurzfelder): Tab/Zeilenumbrüche → ein Leerzeichen.
    """
    text = _CTRL_RE.sub("", text)
    if multiline:
        return text.replace("\r\n", "\n").replace("\r", "\n")
    return _LINEBREAK_RE.sub(" ", text)


def clean_text(
    value: Any, field: str, limit: int = MAX_FIELD_LEN, *, multiline: bool = False
) -> str:
    """Textfeld normalisieren (``None`` → ``""``, Steuerzeichen raus, getrimmt) und prüfen.

    Erlaubt: ``str``, ``None`` sowie ``int`` (z. B. Ticketnummer, wird zu
    ``str``). Alles andere (Listen, Dicts, Bytes, bool, float …) →
    :class:`InputLimitError`. C0-Steuerzeichen werden entfernt
    (:func:`strip_control`, ``multiline`` für Freitexte). Die Länge wird nach
    dem Bereinigen geprüft.
    """
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise InputLimitError(field, "type")
    text = strip_control(str(value), multiline).strip()
    if len(text) > limit:
        raise InputLimitError(field, "length", limit)
    return text


def clean_items(
    items: Iterable[Any] | None,
    field: str = "items",
    max_items: int = MAX_ITEMS,
    max_len: int = MAX_ITEM_LEN,
) -> list[str]:
    """Liste weiterer Tätigkeiten prüfen: leere Einträge entfernen, getrimmt.

    ``items`` muss ``None``, ``list`` oder ``tuple`` sein (ein einzelner String
    wird abgelehnt, damit er nicht zeichenweise zerfällt).
    """
    if items is None:
        return []
    if not isinstance(items, (list, tuple)):
        raise InputLimitError(field, "type")
    out: list[str] = []
    for raw in items:
        text = clean_text(raw, field, max_len)
        if text:
            out.append(text)
    if len(out) > max_items:
        raise InputLimitError(field, "count", max_items)
    return out


_FORMULA_CHARS = ("=", "+", "-", "@")


def protect_formula(text: str | None) -> str:
    """Schutz vor CSV-/Formel-Injektion beim Öffnen in Excel/Calc/Sheets.

    Ein ``'`` wird vorangestellt, wenn der Text mit Leerraum/Steuerzeichen
    (Tab, CR, LF …) beginnt oder wenn er nach Entfernen führenden Leerraums
    (``lstrip()``) mit ``= + - @`` beginnt.
    """
    text = text or ""
    if text and (text[0] in "\t\r\n" or text.lstrip().startswith(_FORMULA_CHARS)):
        return "'" + text
    return text
