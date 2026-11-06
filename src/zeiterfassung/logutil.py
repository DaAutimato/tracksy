"""Datensparsames Fehler-Logging.

Exception-Texte können Nutzereingaben enthalten (Kunde, Transkript, Pfade).
Deshalb wird standardmäßig nur der Fehlertyp mit festem Kontexttext geloggt;
Meldung und Traceback nur, wenn der Logger auf DEBUG steht (``ZEIT_LOG=DEBUG``).
"""

from __future__ import annotations

import logging
import sys

__all__ = ["log_failure"]


def log_failure(
    logger: logging.Logger,
    context: str,
    exc: BaseException | None = None,
    *,
    level: int = logging.WARNING,
) -> None:
    """``context: Fehlertyp`` loggen; Traceback (mit Meldung) nur bei DEBUG.

    ``context`` muss ein fester Text ohne Nutzereingaben sein. ``exc`` fehlt →
    aktuell behandelte Exception (innerhalb eines ``except``-Blocks).
    """
    if exc is None:
        exc = sys.exc_info()[1]
    name = type(exc).__name__ if exc is not None else "unbekannt"
    if logger.isEnabledFor(logging.DEBUG) and exc is not None:
        logger.log(level, "%s: %s", context, name, exc_info=exc, stacklevel=2)
    else:
        logger.log(level, "%s: %s", context, name, stacklevel=2)
