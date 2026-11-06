"""Transkription mit whisper.cpp (offline, lokales ggml-Modell).

- Kein Netzwerkcode: die native Bibliothek (``voice/whispercpp.py``) liest nur
  die Modelldatei; Audio bleibt im RAM.
- Lazy Load beim ersten Gebrauch, Entladen nach ``idle_unload_seconds``
  Inaktivität (Timer-Thread). Alle Methoden sind threadsicher.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .errors import ModelNotFoundError, TranscriptionError
from .modelcheck import InvalidModelError, validate_model_file
from .postprocess import collapse_repeats
from .settings import VoiceSettings
from .trim import trim_silence

log = logging.getLogger(__name__)

_UNSET: Any = object()
MIN_AUDIO_SECONDS = 0.3
SILENCE_RMS = 1e-4


@dataclass
class TranscriberStats:
    load_seconds: float | None = None        # letzte Ladezeit des Modells
    transcribe_seconds: float | None = None  # letzte Transkriptionszeit
    audio_seconds: float | None = None       # Länge des letzten Audios
    loads: int = 0


def check_model_file(path: Path | str) -> Path:
    """Prüft, ob ``path`` eine gültige ggml-Modelldatei ist (Existenz + Kopf).

    Fehlt sie → :class:`ModelNotFoundError`; manipulierter/beschädigter Kopf →
    :class:`InvalidModelError` (statt ``GGML_ABORT`` in whisper.cpp).
    """
    return validate_model_file(path)


__all__ = [
    "InvalidModelError", "Transcriber", "TranscriberStats", "check_model_file",
    "dictation_token_budget",
]


def dictation_token_budget(seconds: float, settings: VoiceSettings,
                           cap: int | None = _UNSET) -> int | None:
    """Tokenlimit fürs Diktat aus der Audiolänge (nach Stille-Trimmung).

    ``dictation_min_tokens + seconds * dictation_tokens_per_second``, höchstens
    ``cap`` (Default ``dictation_max_new_tokens``). ``tokens_per_second <= 0``
    schaltet die Kopplung ab (dann nur ``cap``).
    """
    if cap is _UNSET:
        cap = settings.dictation_max_new_tokens
    tps = settings.dictation_tokens_per_second
    if not tps or tps <= 0:
        return cap or None
    budget = settings.dictation_min_tokens + math.ceil(max(0.0, seconds) * tps)
    return min(budget, cap) if cap else budget


def _default_model_factory(settings: VoiceSettings) -> Any:
    from .whispercpp import WhisperCppModel  # noqa: PLC0415 (lädt native Bibliothek)

    return WhisperCppModel(check_model_file(settings.model_path), n_threads=settings.cpu_threads)


class Transcriber:
    def __init__(self, settings: VoiceSettings, model_factory: Any = None) -> None:
        self.settings = settings
        self._factory = model_factory or _default_model_factory
        self._lock = threading.RLock()
        self._model: Any = None
        self._timer: threading.Timer | None = None
        self._last_used = 0.0
        self.stats = TranscriberStats()

    # -- Modell-Lebenszyklus ------------------------------------------------
    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Modell laden (falls noch nicht geladen). Blockiert."""
        with self._lock:
            if self._model is not None:
                return
            if self._factory is _default_model_factory:
                check_model_file(self.settings.model_path)
            t0 = time.perf_counter()
            try:
                self._model = self._factory(self.settings)
            except ModelNotFoundError:
                raise
            except Exception as exc:
                raise TranscriptionError(f"Modell konnte nicht geladen werden: {exc}") from exc
            self.stats.load_seconds = time.perf_counter() - t0
            self.stats.loads += 1
            log.info("Whisper-Modell geladen in %.2f s", self.stats.load_seconds)
            self._last_used = time.monotonic()
            self._schedule_unload()

    def unload(self) -> None:
        with self._lock:
            self._cancel_timer()
            if self._model is None:
                return
            model, self._model = self._model, None
        close = getattr(model, "close", None)
        if close is not None:
            close()
        log.info("Whisper-Modell entladen")

    def close(self) -> None:
        self.unload()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _schedule_unload(self) -> None:
        self._cancel_timer()
        delay = self.settings.idle_unload_seconds
        if delay is None or delay <= 0:
            return
        t = threading.Timer(delay, self._idle_check)
        t.daemon = True
        t.name = "whisper-idle-unload"
        self._timer = t
        t.start()

    def _idle_check(self) -> None:
        with self._lock:
            if self._model is None:
                return
            idle = time.monotonic() - self._last_used
            if idle + 0.05 < self.settings.idle_unload_seconds:
                # zwischenzeitlich benutzt -> neu planen
                self._schedule_unload()
                return
        self.unload()

    # -- Transkription --------------------------------------------------------
    def transcribe(
        self,
        audio: np.ndarray,
        *,
        initial_prompt: str | None = _UNSET,
        max_new_tokens: int | None = _UNSET,
        dictation: bool = False,
    ) -> str:
        """Transkribiert 16-kHz-Mono-float32-Audio und gibt den Text zurück.

        Leeres/zu kurzes/stilles Audio ergibt "" ohne Modell-Load.
        ``initial_prompt``/``max_new_tokens`` Default: Werte des Modus aus den
        Settings. ``dictation=True``: Zeitstempel an, Tokenlimit an die
        Audiolänge gekoppelt (``max_new_tokens`` ist dann die Obergrenze) und
        Wiederholungen werden entfernt (Schutz gegen Whisper-Schleifen).
        """
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        self.stats.audio_seconds = audio.size / self.settings.sample_rate
        if audio.size < MIN_AUDIO_SECONDS * self.settings.sample_rate:
            return ""
        if float(np.sqrt(np.mean(np.square(audio, dtype=np.float64)))) < SILENCE_RMS:
            return ""
        if self.settings.trim_silence:
            audio = trim_silence(audio, self.settings.sample_rate)
            if audio.size < MIN_AUDIO_SECONDS * self.settings.sample_rate:
                return ""
        s = self.settings
        if initial_prompt is _UNSET:
            initial_prompt = s.dictation_prompt if dictation else s.command_prompt
        prompt = initial_prompt
        if dictation:
            max_new_tokens = dictation_token_budget(
                audio.size / s.sample_rate, s, max_new_tokens)
        elif max_new_tokens is _UNSET:
            max_new_tokens = s.command_max_new_tokens
        extra = {"timestamps": True} if dictation and s.dictation_timestamps else {}
        with self._lock:
            self.load()
            self._cancel_timer()
            t0 = time.perf_counter()
            try:
                text = self._model.transcribe(
                    audio,
                    language=self.settings.language,
                    beam_size=self.settings.beam_size,
                    initial_prompt=prompt or None,
                    max_new_tokens=max_new_tokens or None,
                    temperature=tuple(self.settings.temperature),
                    **extra,
                ).strip()
            except Exception as exc:
                raise TranscriptionError(f"Transkription fehlgeschlagen: {exc}") from exc
            finally:
                self.stats.transcribe_seconds = time.perf_counter() - t0
                self._last_used = time.monotonic()
                self._schedule_unload()
        log.info(
            "Transkription: %.2f s für %.1f s Audio",
            self.stats.transcribe_seconds, self.stats.audio_seconds,
        )
        if dictation and s.dictation_collapse_repeats:
            cleaned = collapse_repeats(text)
            if cleaned != text:  # nur Längen, nie Inhalte loggen
                log.info("Diktat: Wiederholungen entfernt (%d → %d Zeichen)",
                         len(text), len(cleaned))
            text = cleaned
        return text
