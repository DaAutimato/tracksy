"""GUI-freie Steuerung der Push-to-talk-Pipeline.

Ablauf: ``begin_listening()`` öffnet das Mikrofon (und lädt das Modell parallel
vor), ``end_listening()`` schließt es und startet die Transkription im
Worker-Thread. Ergebnis kommt per Callback.

THREADING / GUI-HINWEIS
-----------------------
Alle Callbacks (``on_state``, ``on_level``, ``on_result``, ``on_error``) werden
aus Worker- bzw. PortAudio-Threads aufgerufen, NIE aus dem GTK-Main-Loop.
Die GUI muss sie marshallen, z. B.::

    VoiceController(settings,
        on_state=lambda s: GLib.idle_add(win.set_voice_state, s),
        on_result=lambda t, c: GLib.idle_add(app.handle_voice, t, c),
        ...)

Callbacks müssen schnell zurückkehren (``on_level`` kommt ca. alle 50 ms).
"""

from __future__ import annotations

import dataclasses
import logging
import re
import threading
from collections.abc import Callable
from enum import Enum
from typing import Any

import numpy as np

from .parser import Command, parse_command
from .recorder import Recorder
from .settings import VoiceSettings
from .transcriber import Transcriber
from ..logutil import log_failure

log = logging.getLogger(__name__)


def _intent(cmd: Any) -> str | None:
    intent = getattr(cmd, "intent", None)
    return getattr(intent, "value", intent)


# Wortanfänge, die nach Startbefehl klingen ("Startekunde", "Statte", "Beginn", "Neuer Task")
_RETRY_HINT = re.compile(r"sta|beg|neu|task|kund|them|tick|für", re.IGNORECASE)


def _needs_start_retry(cmd: Any, text: str) -> bool:
    """Start ohne Kunde, oder „nicht verstanden“ bei einem Text, der nach Start klingt.

    Stille/leerer Text und klar fremde Sätze lösen keinen zweiten Lauf aus
    (der kostet auf schwachen Rechnern mehrere Sekunden)."""
    if not text.strip():
        return False
    intent = _intent(cmd)
    if intent == "start":
        return not (getattr(cmd, "customer", None) or "").strip()
    return intent == "unknown" and bool(_RETRY_HINT.search(text))


class VoiceState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    ERROR = "error"


class Mode(str, Enum):
    COMMAND = "command"      # Transkript + geparster Command
    DICTATION = "dictation"  # nur Transkript (Ergänzungsdialog), Command ist None


class VoiceController:
    def __init__(
        self,
        settings: VoiceSettings,
        *,
        on_state: Callable[[VoiceState], None] | None = None,
        on_level: Callable[[float], None] | None = None,
        on_result: Callable[[str, Command | None], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        transcriber: Transcriber | None = None,
        recorder_factory: Callable[..., Recorder] | None = None,
        parser: Callable[[str], Command] = parse_command,
        preload_on_listen: bool = True,
    ) -> None:
        self.settings = settings
        self.on_state = on_state
        self.on_level = on_level
        self.on_result = on_result
        self.on_error = on_error
        self.transcriber = transcriber or Transcriber(settings)
        self._recorder_factory = recorder_factory or Recorder
        self._parser = parser
        self._preload = preload_on_listen
        self._lock = threading.Lock()
        self._state = VoiceState.IDLE
        self._mode = Mode.COMMAND
        self._recorder: Recorder | None = None
        self._worker: threading.Thread | None = None

    # -- Zustand ---------------------------------------------------------------
    @property
    def state(self) -> VoiceState:
        return self._state

    @property
    def mode(self) -> Mode:
        return self._mode

    def _set_state(self, state: VoiceState) -> None:
        self._state = state
        self._emit(self.on_state, state)

    def _emit(self, cb: Callable[..., Any] | None, *args: Any) -> None:
        if cb is None:
            return
        try:
            cb(*args)
        except Exception:
            log_failure(log, "Voice-Callback fehlgeschlagen")

    def _fail(self, exc: Exception) -> None:
        log_failure(log, "Sprachsteuerung", exc)
        self._set_state(VoiceState.ERROR)
        self._emit(self.on_error, exc)
        self._set_state(VoiceState.IDLE)

    # -- Steuerung -------------------------------------------------------------
    def begin_listening(self, mode: Mode | str = Mode.COMMAND) -> bool:
        """Mikrofon öffnen. False, wenn bereits aktiv/beschäftigt oder Fehler."""
        with self._lock:
            if self._state is not VoiceState.IDLE:
                return False
            self._mode = Mode(mode)
            rec = self._recorder_factory(
                device=self.settings.input_device,
                sample_rate=self.settings.sample_rate,
                max_seconds=self.settings.max_record_seconds,
                on_level=self._on_level,
                on_limit=self._on_limit,
            )
            try:
                rec.start()
            except Exception as exc:
                self._recorder = None
                err = exc
            else:
                self._recorder = rec
                self._state = VoiceState.LISTENING
                err = None
        if err is not None:
            self._fail(err)
            return False
        self._emit(self.on_state, VoiceState.LISTENING)
        if self._preload and not self.transcriber.is_loaded:
            threading.Thread(target=self._safe_preload, name="whisper-preload", daemon=True).start()
        return True

    def end_listening(self) -> bool:
        """Mikrofon schließen, Transkription im Worker starten."""
        with self._lock:
            if self._state is not VoiceState.LISTENING or self._recorder is None:
                return False
            rec, self._recorder = self._recorder, None
            self._state = VoiceState.TRANSCRIBING
            mode = self._mode
        try:
            audio = rec.stop()
        except Exception as exc:
            self._fail(exc)
            return False
        self._emit(self.on_state, VoiceState.TRANSCRIBING)
        # Audio in einem leerbaren Behälter übergeben: Thread-Argumente bleiben sonst bis
        # zum Thread-Ende (im Fehlerfall auch im Traceback) im Speicher
        box = [audio]
        del audio
        self._worker = threading.Thread(
            target=self._run, args=(box, mode), name="voice-worker", daemon=True
        )
        self._worker.start()
        return True

    def toggle(self, mode: Mode | str = Mode.COMMAND) -> bool:
        """Push-to-talk per Einzel-Hotkey: startet bzw. beendet die Aufnahme."""
        if self._state is VoiceState.LISTENING:
            return self.end_listening()
        return self.begin_listening(mode)

    def cancel(self) -> None:
        """Laufende Aufnahme verwerfen (keine Transkription)."""
        with self._lock:
            rec, self._recorder = self._recorder, None
            was_listening = self._state is VoiceState.LISTENING
            if was_listening:
                self._state = VoiceState.IDLE
        if rec is not None:
            rec.cancel()
        if was_listening:
            self._emit(self.on_state, VoiceState.IDLE)

    def preload(self) -> None:
        """Modell im Hintergrund laden (z. B. beim App-Start, optional)."""
        threading.Thread(target=self._safe_preload, name="whisper-preload", daemon=True).start()

    def wait(self, timeout: float | None = None) -> None:
        """Auf laufende Transkription warten (Tests/Beenden)."""
        w = self._worker
        if w is not None:
            w.join(timeout)

    def shutdown(self) -> None:
        self.cancel()
        self.wait(5.0)
        self.transcriber.close()

    # -- intern ----------------------------------------------------------------
    def _safe_preload(self) -> None:
        try:
            self.transcriber.load()
        except Exception as exc:  # Fehler wird bei der Transkription erneut gemeldet
            log_failure(log, "Vorladen des Modells fehlgeschlagen", exc)

    def _on_level(self, level: float) -> None:
        self._emit(self.on_level, level)

    def _on_limit(self) -> None:
        # aus PortAudio-Thread: Stream nicht hier schließen -> eigener Thread
        threading.Thread(target=self.end_listening, name="voice-limit", daemon=True).start()

    def _retry_start(self, audio: np.ndarray, text: str, cmd: Command) -> tuple[str, Command]:
        """Zweiter Versuch mit Start-Prompt; Ergebnis nur bei „Start mit Kunde“ übernehmen."""
        s = self.settings
        try:
            text2 = self.transcriber.transcribe(
                audio, initial_prompt=s.start_retry_prompt, max_new_tokens=s.command_max_new_tokens
            )
            cmd2 = self._parser(text2)
        except Exception as exc:  # nur eine Hilfe – erstes Ergebnis nicht verlieren
            log_failure(log, "Zweiter Sprachversuch", exc)
            return text, cmd
        if _intent(cmd2) == "start" and (getattr(cmd2, "customer", None) or "").strip():
            log.info("Sprachbefehl im zweiten Versuch erkannt")  # keine Inhalte loggen
            if dataclasses.is_dataclass(cmd2):
                cmd2 = dataclasses.replace(cmd2, second_pass=True)
            return text2, cmd2
        return text, cmd

    def _run(self, box: list[np.ndarray], mode: Mode) -> None:
        audio = box.pop()
        try:
            s = self.settings
            if mode is Mode.COMMAND:
                text = self.transcriber.transcribe(
                    audio, initial_prompt=s.command_prompt, max_new_tokens=s.command_max_new_tokens
                )
            else:
                # Tokenbudget/Zeitstempel/Wiederholungsschutz setzt der Transcriber
                text = self.transcriber.transcribe(
                    audio, initial_prompt=s.dictation_prompt,
                    max_new_tokens=s.dictation_max_new_tokens, dictation=True,
                )
            cmd = self._parser(text) if mode is Mode.COMMAND else None
            if mode is Mode.COMMAND and s.start_retry_prompt and _needs_start_retry(cmd, text):
                text, cmd = self._retry_start(audio, text, cmd)
            audio = None  # Audio sofort freigeben (einzige Referenz)
        except Exception as exc:
            audio = None  # nicht über den Fehlerpfad festhalten
            self._fail(exc)
            return
        self._set_state(VoiceState.IDLE)
        self._emit(self.on_result, text, cmd)
