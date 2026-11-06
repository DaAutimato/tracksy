"""Push-to-talk-Aufnahme über sounddevice.

Audio bleibt ausschließlich im RAM (Liste von numpy-Blöcken) und wird nach
``stop()`` an den Aufrufer übergeben; es wird nie etwas auf Platte geschrieben.
Das Mikrofon ist nur zwischen ``start()`` und ``stop()``/``cancel()`` geöffnet.

Absturzabbilder: systemd-coredump (Mint/Ubuntu) speichert bei einem Absturz den
ganzen Prozessspeicher unter ``/var/lib/systemd/coredump`` – inklusive Audio im
RAM – und ignoriert dabei ``RLIMIT_CORE``. Vor der ersten Aufnahme wird der
Prozess deshalb per ``prctl(PR_SET_DUMPABLE, 0)`` vom Core-Dump ausgenommen.
Lässt sich dieser Schutz nicht setzen, verweigert ``start()`` die Aufnahme
(``CoreDumpProtectionError``) – das Mikrofon wird dann gar nicht geöffnet.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable
from typing import Any

import numpy as np

from .errors import AudioDeviceError, CoreDumpProtectionError
from ..logutil import log_failure

log = logging.getLogger(__name__)

_PR_SET_DUMPABLE = 4
_COREDUMP_FILTER = "/proc/self/coredump_filter"


def _clear_coredump_filter(path: str = _COREDUMP_FILTER) -> bool:
    """Speicherbereiche vom Core-Dump ausschließen.

    ``PR_SET_DUMPABLE=0`` allein reicht NICHT: systemd-coredump setzt
    ``fs.suid_dumpable=2`` (``/usr/lib/sysctl.d/50-coredump.conf``); dann
    erstellt der Kernel auch für nicht-dumpbare Prozesse ein Abbild (als root)
    und reicht es an die Pipe in ``core_pattern`` weiter. Filter 0 bewirkt,
    dass weder anonymer Speicher (Heap, Stacks, Audio-Puffer) noch
    Datei-Mappings in das Abbild geschrieben werden (proc(5)). Muss VOR
    ``PR_SET_DUMPABLE=0`` geschehen: danach gehört die Datei root.
    """
    try:
        with open(path, encoding="ascii") as fh:
            if int(fh.read().strip() or "1", 16) == 0:
                return True
        with open(path, "w", encoding="ascii") as fh:
            fh.write("0")
        return True
    except (OSError, ValueError) as exc:  # pragma: no cover - kein Linux/proc
        log_failure(log, "Core-Dump-Filter nicht setzbar", exc)
        return False


def disable_core_dumps() -> bool:
    """Prozess vom Core-Dump ausnehmen (idempotent). ``True`` bei Erfolg."""
    ok = _clear_coredump_filter()
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) == 0:
            return ok
        log.warning("Core-Dumps nicht abschaltbar (errno %d)", ctypes.get_errno())
    except (OSError, AttributeError) as exc:  # pragma: no cover - kein Linux/libc
        log_failure(log, "Core-Dumps nicht abschaltbar", exc)
    return False

LevelCallback = Callable[[float], None]
LimitCallback = Callable[[], None]


def _default_backend() -> Any:
    try:
        import sounddevice as sd  # noqa: PLC0415
    except (ImportError, OSError) as exc:  # PortAudio fehlt
        raise AudioDeviceError(f"Audio-System nicht verfügbar: {exc}") from exc
    return sd


def rms_level(block: np.ndarray) -> float:
    """Pegel 0..1 (RMS, logarithmisch auf -60..0 dBFS abgebildet)."""
    if block.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))
    if rms <= 1e-6:
        return 0.0
    db = 20.0 * np.log10(rms)
    return float(min(1.0, max(0.0, (db + 60.0) / 60.0)))


def resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Einfaches lineares Resampling (für Sprache ausreichend)."""
    if src_rate == dst_rate or audio.size == 0:
        return audio.astype(np.float32, copy=False)
    n_out = int(round(audio.size * dst_rate / src_rate))
    x_old = np.linspace(0.0, 1.0, num=audio.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


class Recorder:
    """Aufnahme 16 kHz mono float32.

    ``on_level`` und ``on_limit`` werden aus dem PortAudio-Thread aufgerufen
    und müssen schnell zurückkehren (GUI: per ``GLib.idle_add`` weiterreichen).
    ``on_limit`` meldet, dass ``max_seconds`` erreicht wurde; weitere Daten
    werden dann verworfen, bis ``stop()`` aufgerufen wird.
    """

    def __init__(
        self,
        device: int | str | None = None,
        sample_rate: int = 16000,
        max_seconds: float = 30.0,
        on_level: LevelCallback | None = None,
        on_limit: LimitCallback | None = None,
        backend: Any = None,
    ) -> None:
        self.device = device
        self.sample_rate = sample_rate
        self.max_seconds = max_seconds
        self.on_level = on_level
        self.on_limit = on_limit
        self._backend = backend
        self._lock = threading.Lock()
        self._stream: Any = None
        self._chunks: list[np.ndarray] = []
        self._frames = 0
        self._max_frames = 0
        self._stream_rate = sample_rate
        self._limit_hit = False

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    @property
    def limit_reached(self) -> bool:
        return self._limit_hit

    def _sd(self) -> Any:
        if self._backend is None:
            self._backend = _default_backend()
        return self._backend

    def _callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        if status:
            log.debug("Audio-Status: %s", status)
        with self._lock:
            if self._limit_hit:
                return
            block = np.array(indata[:, 0] if indata.ndim == 2 else indata, dtype=np.float32, copy=True)
            room = self._max_frames - self._frames
            if block.size >= room:
                block = block[:room]
                self._limit_hit = True
            self._chunks.append(block)
            self._frames += block.size
            limit_hit = self._limit_hit
        if self.on_level is not None:
            try:
                self.on_level(rms_level(block))
            except Exception:  # Callback-Fehler dürfen die Aufnahme nicht stoppen
                log_failure(log, "on_level-Callback fehlgeschlagen")
        if limit_hit and self.on_limit is not None:
            try:
                self.on_limit()
            except Exception:
                log_failure(log, "on_limit-Callback fehlgeschlagen")

    def _open_stream(self, rate: int) -> Any:
        sd = self._sd()
        stream = sd.InputStream(
            samplerate=rate,
            channels=1,
            dtype="float32",
            device=self.device,
            callback=self._callback,
            blocksize=int(rate * 0.05),  # 50 ms -> flüssiger Pegel
        )
        try:
            stream.start()
        except Exception:
            # Handle nicht lecken lassen (z. B. vor dem Fallback auf die native Rate)
            try:
                stream.close()
            except Exception as close_exc:
                log_failure(log, "Audio-Stream nach Startfehler nicht schließbar", close_exc)
            raise
        return stream

    def start(self) -> None:
        """Mikrofon öffnen. Wirft ``AudioDeviceError`` bei fehlendem Gerät."""
        if self._stream is not None:
            raise RuntimeError("Aufnahme läuft bereits")
        # Audio im RAM darf nie in einem Absturzabbild landen: ohne Schutz keine Aufnahme
        if not disable_core_dumps():
            raise CoreDumpProtectionError(
                "Sprachaufnahme nicht möglich: Der Schutz vor Absturzabbildern ließ sich "
                "nicht einschalten. Ohne ihn könnte Audio bei einem Absturz auf der "
                "Festplatte landen."
            )
        sd = self._sd()
        with self._lock:
            self._chunks = []
            self._frames = 0
            self._limit_hit = False
        try:
            info = sd.query_devices(self.device, kind="input")
        except Exception as exc:
            raise AudioDeviceError(f"Kein Eingabegerät verfügbar ({self.device!r}): {exc}") from exc
        try:
            self._stream_rate = self.sample_rate
            self._max_frames = int(self.max_seconds * self._stream_rate)
            self._stream = self._open_stream(self._stream_rate)
        except Exception as first_exc:
            # Gerät kann 16 kHz evtl. nicht -> native Rate, danach resamplen
            native = int(info.get("default_samplerate") or 0) if isinstance(info, dict) else 0
            if not native or native == self.sample_rate:
                raise AudioDeviceError(f"Mikrofon konnte nicht geöffnet werden: {first_exc}") from first_exc
            try:
                self._stream_rate = native
                self._max_frames = int(self.max_seconds * native)
                self._stream = self._open_stream(native)
                log.info("Aufnahme mit %d Hz, wird auf %d Hz umgerechnet", native, self.sample_rate)
            except Exception as exc:
                self._stream = None
                raise AudioDeviceError(f"Mikrofon konnte nicht geöffnet werden: {exc}") from exc

    def _close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
        finally:
            stream.close()

    def stop(self) -> np.ndarray:
        """Mikrofon schließen und Aufnahme als 1-D float32 (16 kHz) zurückgeben."""
        self._close()
        with self._lock:
            chunks, self._chunks = self._chunks, []
            self._frames = 0
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        return resample(audio, self._stream_rate, self.sample_rate)

    def cancel(self) -> None:
        """Mikrofon schließen und Aufnahme verwerfen."""
        self._close()
        with self._lock:
            self._chunks = []
            self._frames = 0
