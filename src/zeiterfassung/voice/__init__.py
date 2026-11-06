"""Sprach-Pipeline: Aufnahme, Transkription (whisper.cpp), Befehls-Parser.

Schwere Abhängigkeiten (numpy, sounddevice, native Bibliothek) werden lazy
geladen: ``import zeiterfassung.voice.models`` (App-Start, Einstellungen) darf
numpy nicht nach sich ziehen (~25 MB RAM, ~100 ms). Die Namen aus ``__all__``
werden deshalb erst beim ersten Zugriff aufgelöst (PEP 562).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from . import ortguard as _ortguard

_ortguard.install()  # onnxruntime nie laden, auch nicht über Fremdpakete

if TYPE_CHECKING:  # pragma: no cover - nur für Typprüfer/IDE
    from .controller import Mode, VoiceController, VoiceState
    from .errors import (
        AudioDeviceError,
        CoreDumpProtectionError,
        ModelDownloadError,
        ModelNotFoundError,
        TranscriptionError,
        VoiceError,
    )
    from .modelcheck import InvalidModelError
    from .parser import Command, Intent, parse_command
    from .settings import DEFAULT_COMMAND_PROMPT, VoiceSettings

_LAZY = {
    "Mode": "controller", "VoiceController": "controller", "VoiceState": "controller",
    "AudioDeviceError": "errors", "CoreDumpProtectionError": "errors",
    "ModelDownloadError": "errors",
    "ModelNotFoundError": "errors", "TranscriptionError": "errors", "VoiceError": "errors",
    "InvalidModelError": "modelcheck",
    "Command": "parser", "Intent": "parser", "parse_command": "parser",
    "DEFAULT_COMMAND_PROMPT": "settings", "VoiceSettings": "settings",
}

__all__ = sorted(_LAZY)


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY])
