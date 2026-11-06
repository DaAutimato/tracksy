"""Brücke zwischen ``voice.VoiceController`` (Worker-Threads) und GTK-Main-Loop.

Der Controller wird erst bei der ersten Nutzung erzeugt (App-Start < 1 s,
kein numpy/Modell beim Start). Alle Callbacks werden per ``GLib.idle_add`` in
den Main-Loop geholt und als GObject-Signale weitergegeben.

Vorwärmen (:meth:`VoiceBridge.warm_up`): Die erste Aufnahme importierte bisher
numpy, die Sprach-Module und sounddevice (= ``Pa_Initialize`` mit Abfrage aller
Audiogeräte) synchron im Main-Loop – die Oberfläche stand dabei still, gehaltene
Tasten liefen in die Warteschlange. Kurz nach dem App-Start erledigt das jetzt ein
Hintergrund-Thread. Das Mikrofon wird dabei NICHT geöffnet und das Modell nicht
geladen (RAM; das Modell lädt weiterhin parallel zur ersten Aufnahme).
``ZEIT_VOICE_WARMUP=0`` schaltet das Vorwärmen ab (Tests).
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from gi.repository import GLib, GObject

from ..config import Config, Paths
from ..logutil import log_failure

log = logging.getLogger(__name__)

ENV_WARMUP = "ZEIT_VOICE_WARMUP"


def friendly_error(exc: BaseException) -> str:
    name = type(exc).__name__
    if name == "AudioDeviceError":
        return f"Mikrofon nicht verfügbar: {exc}"
    if name == "CoreDumpProtectionError":
        return str(exc)
    if name == "InvalidModelError":  # manipulierte/beschädigte Datei, App läuft weiter
        return ("Sprachmodell ungültig oder beschädigt. Bitte unter Einstellungen → Sprache "
                "„Erneut herunterladen …“ wählen (die alte Datei wird archiviert, nicht gelöscht).")
    if name == "ModelNotFoundError":
        return "Sprachmodell nicht gefunden. Bitte in den Einstellungen herunterladen."
    if name == "TranscriptionError":
        return f"Spracherkennung fehlgeschlagen: {exc}"
    return f"Spracheingabe fehlgeschlagen: {exc}"


class VoiceBridge(GObject.Object):
    __gtype_name__ = "ZeitVoiceBridge"
    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "level": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "result": (GObject.SignalFlags.RUN_FIRST, None, (str, object, str)),
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, config: Config, paths: Paths) -> None:
        super().__init__()
        self.config = config
        self.paths = paths
        audio = getattr(config, "audio", None)
        self.input_device: int | str | None = audio.resolved_input_device() if audio else None
        self._ctrl: Any = None
        self._state = "idle"
        self._mode = "command"
        self._warm_thread: threading.Thread | None = None

    # -- Verfügbarkeit ---------------------------------------------------------
    def model_path(self):
        return self.config.whisper.resolved_model_path(self.paths.models_dir)

    def available(self) -> tuple[bool, str]:
        from ..voice.whispercpp import find_library  # noqa: PLC0415

        if find_library() is None:
            return False, ("Die Spracherkennung ist nicht installiert (libtracksy_whisper.so fehlt). "
                           "Die App funktioniert ohne Sprache; zum Nachrüsten "
                           "scripts/build-whisper.sh ausführen.")
        p = self.model_path()
        if not p.is_file():
            return False, (f"Sprachmodell „{self.config.whisper.model_size}“ ist nicht installiert. "
                           "Die App funktioniert ohne Sprache; das Modell kann in den "
                           "Einstellungen geladen werden.")
        return True, ""

    @property
    def state(self) -> str:
        return self._state

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def warming(self) -> bool:
        """Vorwärmen läuft noch (Beginnen würde den Main-Loop bis zum Ende blockieren)."""
        t = self._warm_thread
        return t is not None and t.is_alive()

    def warm_up(self) -> bool:
        """Sprach-Module und PortAudio im Hintergrund laden (ohne Mikrofon, ohne Modell).

        Nur einmal und nur, wenn die Spracheingabe eingerichtet ist. ``True`` = gestartet."""
        if os.environ.get(ENV_WARMUP, "1").strip() == "0":
            return False
        if self._warm_thread is not None or self._ctrl is not None:
            return False
        try:
            if not self.available()[0]:
                return False
        except Exception as exc:  # pragma: no cover - defensiv
            log_failure(log, "Sprachprüfung fehlgeschlagen", exc)
            return False
        self._warm_thread = threading.Thread(target=self._warm, name="voice-warmup",
                                             daemon=True)
        self._warm_thread.start()
        return True

    @staticmethod
    def _warm() -> None:
        try:
            from ..voice import controller  # noqa: F401, PLC0415 (numpy, Parser, Transcriber)
            from ..voice.recorder import _default_backend  # noqa: PLC0415

            _default_backend()  # import sounddevice → Pa_Initialize (öffnet keinen Stream)
        except Exception as exc:  # Fehler meldet die erste Aufnahme verständlich
            log_failure(log, "Vorwärmen der Spracheingabe fehlgeschlagen", exc,
                        level=logging.INFO)

    # -- Controller ------------------------------------------------------------
    def _controller(self):
        if self._ctrl is not None:
            return self._ctrl
        from ..voice.controller import VoiceController  # noqa: PLC0415 (lazy, numpy)
        from ..voice.settings import VoiceSettings  # noqa: PLC0415

        w = self.config.whisper
        settings = VoiceSettings(
            model_path=self.model_path(),
            cpu_threads=w.cpu_threads,
            language=w.language,
            idle_unload_seconds=float(w.unload_after_seconds),
            input_device=self.input_device,
            **({"max_record_seconds": float(self.config.audio.max_record_seconds)}
               if getattr(self.config, "audio", None) else {}),
        )
        self._ctrl = VoiceController(
            settings,
            on_state=lambda s: GLib.idle_add(self._on_state, getattr(s, "value", str(s))),
            on_level=lambda lv: GLib.idle_add(self._on_level, float(lv)),
            on_result=lambda t, c: GLib.idle_add(self._on_result, t, c),
            on_error=lambda e: GLib.idle_add(self._on_error, e),
        )
        return self._ctrl

    def reconfigure(self, config: Config | None = None, input_device: Any = "keep") -> None:
        """Nach Einstellungsänderung: Controller neu erzeugen (beim nächsten Gebrauch)."""
        if config is not None:
            self.config = config
        if input_device != "keep":
            self.input_device = input_device
        if self._ctrl is not None and self._state == "idle":
            try:
                self._ctrl.shutdown()
            except Exception:  # pragma: no cover
                log_failure(log, "Voice-Shutdown fehlgeschlagen", level=logging.ERROR)
            self._ctrl = None

    # -- Steuerung -------------------------------------------------------------
    def begin(self, mode: str = "command") -> bool:
        ok, msg = self.available()
        if not ok:
            self.emit("error", msg)
            return False
        if self._state != "idle":
            return False
        self._mode = mode
        try:
            started = bool(self._controller().begin_listening(mode))
        except Exception as exc:  # Import-/Gerätefehler
            log_failure(log, "Spracheingabe konnte nicht starten", exc, level=logging.ERROR)
            self.emit("error", friendly_error(exc))
            return False
        if started and self._state != "listening":
            self._on_state("listening")
        return started

    def end(self) -> bool:
        if self._ctrl is None:
            return False
        return bool(self._ctrl.end_listening())

    def toggle(self, mode: str = "command") -> bool:
        if self._state == "listening":
            return self.end()
        return self.begin(mode)

    def cancel(self) -> None:
        if self._ctrl is not None:
            self._ctrl.cancel()

    def shutdown(self) -> None:
        if self._ctrl is not None:
            try:
                self._ctrl.shutdown()
            except Exception:  # pragma: no cover
                log_failure(log, "Voice-Shutdown fehlgeschlagen", level=logging.ERROR)
            self._ctrl = None

    # -- Callbacks (Main-Loop) -------------------------------------------------
    def _on_state(self, state: str) -> bool:
        state = "idle" if state == "error" else state
        if state != self._state:
            self._state = state
            self.emit("state-changed", state)
        return False

    def _on_level(self, level: float) -> bool:
        if self._state == "listening":
            self.emit("level", level)
        return False

    def _on_result(self, text: str, cmd: Any) -> bool:
        self.emit("result", text or "", cmd, self._mode)
        return False

    def _on_error(self, exc: BaseException) -> bool:
        self.emit("error", friendly_error(exc))
        return False
