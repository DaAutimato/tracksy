"""ctypes-Anbindung an whisper.cpp (``libtracksy_whisper.so``).

Die Bibliothek wird mit ``scripts/build-whisper.sh`` gebaut: whisper.cpp (MIT)
plus die schmale C-Brücke ``native/tracksy_whisper.c``. Sie enthält keinen
Netzwerkcode (beim Build per ``nm`` geprüft) und keine proprietären Teile;
Python braucht dafür keine weiteren Pakete außer numpy.

Suchreihenfolge der Bibliothek: ``ZEIT_WHISPER_LIB`` → Repo-Checkout
(``native/build/``) → Installation (``$XDG_DATA_HOME/zeiterfassung/lib/``).
"""

from __future__ import annotations

import ctypes
import math
import os
import threading
from pathlib import Path

import numpy as np

from .errors import ModelNotFoundError, TranscriptionError
from .modelcheck import validate_model_file

ENV_LIB = "ZEIT_WHISPER_LIB"
LIB_NAME = "libtracksy_whisper.so"
ABI_VERSION = 3  # 3: Parameter timestamps (Diktat ohne Wiederholungsschleife)
SAMPLE_RATE = 16000
# Encoder-Fenster (audio_ctx) = Audiolänge + Reserve; kleiner als 256 führte
# in Messungen zu Wiederholungsschleifen ("Pause Pause Pause …").
AUDIO_CTX_PER_SECOND = 50
AUDIO_CTX_RESERVE = 256
AUDIO_CTX_MAX = 1500
_OUT_BYTES = 16384

# ggml/whisper.cpp startet bei Abstürzen sonst gdb/lldb für einen Backtrace
os.environ.setdefault("GGML_NO_BACKTRACE", "1")

_lib: ctypes.CDLL | None = None
_lib_lock = threading.Lock()


def library_candidates(env: dict[str, str] | None = None) -> list[Path]:
    env = os.environ if env is None else env
    out: list[Path] = []
    if env.get(ENV_LIB):
        out.append(Path(env[ENV_LIB]).expanduser())
    repo = Path(__file__).resolve().parents[3]
    # Nur aus einem echten Repo-Checkout; installiert zeigt parents[3]
    # sonst in die venv (…/lib/python3.12/native/build/).
    from ..config import is_repo_checkout  # noqa: PLC0415

    if is_repo_checkout(repo):
        out.append(repo / "native" / "build" / LIB_NAME)
    xdg = Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share").expanduser()
    out.append(xdg / "zeiterfassung" / "lib" / LIB_NAME)
    return out


def find_library() -> Path | None:
    for p in library_candidates():
        if p.is_file():
            return p
    return None


def load_library() -> ctypes.CDLL:
    """Bibliothek einmalig laden und Signaturen setzen."""
    global _lib
    with _lib_lock:
        if _lib is not None:
            return _lib
        path = find_library()
        if path is None:
            raise ModelNotFoundError(
                "Spracherkennung nicht gebaut (libtracksy_whisper.so fehlt) – "
                "scripts/build-whisper.sh ausführen"
            )
        lib = ctypes.CDLL(str(path))
        lib.tw_abi_version.restype = ctypes.c_int
        if lib.tw_abi_version() != ABI_VERSION:
            raise TranscriptionError(
                f"{path} passt nicht zu dieser App-Version – scripts/build-whisper.sh erneut ausführen"
            )
        lib.tw_whisper_version.restype = ctypes.c_char_p
        lib.tw_init.restype = ctypes.c_void_p
        lib.tw_init.argtypes = [ctypes.c_char_p]
        lib.tw_free.restype = None
        lib.tw_free.argtypes = [ctypes.c_void_p]
        lib.tw_transcribe.restype = ctypes.c_int
        lib.tw_transcribe.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_int,
            ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_float, ctypes.c_float, ctypes.c_int,
            ctypes.c_int,
            ctypes.c_char_p, ctypes.c_int,
        ]
        _lib = lib
        return lib


def audio_ctx_for(n_samples: int) -> int:
    ctx = math.ceil(n_samples / SAMPLE_RATE * AUDIO_CTX_PER_SECOND) + AUDIO_CTX_RESERVE
    return 0 if ctx >= AUDIO_CTX_MAX else ctx


class WhisperCppModel:
    """Geladenes ggml-Modell. Nicht threadsicher (Transcriber sperrt)."""

    def __init__(self, model_file: Path | str, n_threads: int = 0) -> None:
        self._lib = load_library()
        # Kopf vor dem nativen Laden prüfen: ein ungültiger ftype bricht sonst per
        # GGML_ABORT den ganzen Prozess ab (CVE-2026-17513)
        path = validate_model_file(model_file)
        self._ctx = self._lib.tw_init(os.fsencode(path))
        if not self._ctx:
            raise TranscriptionError(f"Modell konnte nicht geladen werden: {path}")
        self.n_threads = n_threads if n_threads > 0 else min(4, os.cpu_count() or 1)
        self._out = ctypes.create_string_buffer(_OUT_BYTES)

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str = "de",
        beam_size: int = 1,
        initial_prompt: str | None = None,
        max_new_tokens: int | None = None,
        temperature: tuple[float, ...] = (0.0,),
        timestamps: bool = False,
    ) -> str:
        """``timestamps=True`` (Diktat): Zeitstempel-Token und mehrere Segmente.

        Ohne Zeitstempel kann der Decoder das Sprachende nicht markieren und
        wiederholt längere Sätze bis ``max_new_tokens`` (Wiederholungsschleife).
        """
        if not self._ctx:
            raise TranscriptionError("Modell ist nicht geladen")
        samples = np.ascontiguousarray(audio, dtype=np.float32)
        temp = float(temperature[0]) if temperature else 0.0
        inc = float(temperature[1] - temperature[0]) if len(temperature) > 1 else 0.0
        n = self._lib.tw_transcribe(
            self._ctx, samples.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), int(samples.size),
            language.encode(), (initial_prompt or "").encode(),
            self.n_threads, int(beam_size), int(max_new_tokens or 0),
            temp, inc, audio_ctx_for(samples.size),
            1 if timestamps else 0,
            self._out, _OUT_BYTES,
        )
        if n < 0:
            raise TranscriptionError(f"whisper.cpp meldet Fehler {n}")
        return self._out.value.decode("utf-8", "ignore").strip()

    def close(self) -> None:
        if self._ctx:
            self._lib.tw_free(self._ctx)
            self._ctx = None

    def __del__(self) -> None:  # pragma: no cover - Sicherheitsnetz
        try:
            self.close()
        except Exception:
            pass
