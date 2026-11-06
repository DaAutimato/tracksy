"""Sperre für onnxruntime.

onnxruntime (offizielle Builds) sendet Telemetrie an Microsoft
(``mobile.events.data.microsoft.com``) und legt eine Geräte-ID unter
``~/.cache/Microsoft/DeveloperTools/.onnxruntime`` an. Die App nutzt seit dem
Wechsel auf whisper.cpp kein onnxruntime mehr, und es wird auch nicht
installiert (automatisch geprüft). Diese Sperre bleibt
als zweite Verteidigungslinie, falls es über ein Fremdpaket doch in die
Umgebung gelangt:

- ``ORT_DISABLE_TELEMETRY=1`` (laut ``onnxruntime/Privacy.md``: verhindert
  Uploader, Events und Geräte-ID, wenn vor der Initialisierung gesetzt);
- ein ``sys.meta_path``-Finder, der ``import onnxruntime`` mit ``ImportError``
  beantwortet.
"""

from __future__ import annotations

import importlib.abc
import logging
import os
import sys

__all__ = ["BLOCKED", "install", "is_installed"]

log = logging.getLogger(__name__)

BLOCKED = "onnxruntime"


class _OnnxRuntimeBlocker(importlib.abc.MetaPathFinder):
    """Lehnt den Import von ``onnxruntime`` und seinen Untermodulen ab."""

    def find_spec(self, fullname, path=None, target=None):  # noqa: D401, ANN001
        if fullname == BLOCKED or fullname.startswith(BLOCKED + "."):
            raise ImportError(
                "onnxruntime ist in dieser App gesperrt (Telemetrie)",
                name=fullname,
            )
        return None


def is_installed() -> bool:
    return any(isinstance(f, _OnnxRuntimeBlocker) for f in sys.meta_path)


def install() -> None:
    """Sperre aktivieren (idempotent)."""
    os.environ["ORT_DISABLE_TELEMETRY"] = "1"
    if not is_installed():
        sys.meta_path.insert(0, _OnnxRuntimeBlocker())
    if sys.modules.get(BLOCKED) is not None:  # pragma: no cover - nur bei Fremdimport
        log.warning("onnxruntime wurde bereits vor der Sperre geladen")
