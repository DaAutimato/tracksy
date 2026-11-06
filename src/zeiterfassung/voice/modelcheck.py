"""Kopfprüfung einer ggml-Modelldatei vor dem Laden durch whisper.cpp.

whisper.cpp v1.9.5 bricht bei einem unbekannten ``ftype`` im Modellkopf per
``GGML_ASSERT`` → ``GGML_ABORT`` ab (CVE-2026-17513, ``ggml_ftype_to_ggml_type``)
– das beendet den gesamten App-Prozess. Deshalb prüft Python den Kopf vorher
und meldet eine manipulierte oder beschädigte Datei als :class:`InvalidModelError`.

Aufbau (``whisper_model_load`` in ``third_party/whisper.cpp/src/whisper.cpp``),
alles little-endian:

- Byte 0: Magic ``0x67676d6c`` („ggml“, uint32)
- Byte 4–47: 11 × int32 ``n_vocab, n_audio_ctx, n_audio_state, n_audio_head,
  n_audio_layer, n_text_ctx, n_text_state, n_text_head, n_text_layer, n_mels,
  ftype`` (``ftype`` an Byte 44; ``ftype % 1000`` = Typ, ``ftype // 1000`` =
  Quantisierungsversion)
- Byte 48–55: Mel-Filter ``n_mel, n_fft`` (int32), danach ``n_mel·n_fft`` float32

Die Grenzen sind bewusst großzügig (alle offiziellen Modelle tiny … large-v3,
quantisierte und destillierte Varianten passen), schließen aber Werte aus, die
im Loader zu Abbruch oder riesigen Speicheranforderungen führen.

Zusätzlich geprüft, weil whisper.cpp es nicht selbst abfängt (Release-Build:
``assert`` ist ausgeschaltet, ``GGML_ASSERT`` bricht ab):

- ``n_*_state % n_*_head == 0`` – sonst ``GGML_ASSERT`` in ``ggml_reshape_3d``
  beim ersten Encode/Decode (Testbericht 2026-10-31, F-01).
- Mel-Filter ``n_mel == n_mels`` – sonst schreibt ``whisper_encode_internal``
  über das Ende des Eingabepuffers (nur ``assert``).
- Mel-Filter ``n_fft == 1 + WHISPER_N_FFT/2 = 201`` – sonst liest
  ``log_mel_spectrogram`` über das FFT-Puffer-Ende (nur ``assert``).
- ``n_vocab >= 51864`` – darunter liegen die festen Sondertoken (``token_beg`` =
  50363 plus 1501 Zeitstempel) außerhalb des Logit-Arrays.

Felder, die Tensorformen bestimmen (``n_audio_ctx``, ``n_text_ctx``, Schichtzahl
usw.), gleicht der Loader selbst mit den Tensoren der Datei ab und meldet
Abweichungen sauber.
"""

from __future__ import annotations

import os
import stat
import struct
from pathlib import Path

from .errors import ModelNotFoundError

__all__ = ["InvalidModelError", "GGML_MAGIC", "VALID_FTYPES", "HEADER_SIZE", "validate_model_file"]

GGML_MAGIC = 0x67676D6C
QNT_VERSION_FACTOR = 1000  # GGML_QNT_VERSION_FACTOR
# Werte von ggml_ftype, für die ggml_ftype_to_ggml_type (v1.9.5) nicht abbricht:
# 0 F32, 1 F16, 2 Q4_0, 3 Q4_1, 7 Q8_0 … 28 Q2_0. Nicht: -1 UNKNOWN, 4 Q4_1_SOME_F16, 5, 6.
# Bei einem whisper.cpp-Update mit ggml/include/ggml.h (enum ggml_ftype) abgleichen.
VALID_FTYPES = frozenset({0, 1, 2, 3, *range(7, 29)})
# Kleinstes offizielles Vokabular (*.en-Modelle); darunter Sondertoken außerhalb.
MIN_VOCAB = 51864
WHISPER_N_FFT_BINS = 1 + 400 // 2  # 1 + WHISPER_N_FFT/2
_HEADER = struct.Struct("<I11i2i")
HEADER_SIZE = _HEADER.size  # 56

# (Name, Minimum, Maximum) der 10 hparams vor ftype
_LIMITS = (
    ("n_vocab", MIN_VOCAB, 1 << 20),
    ("n_audio_ctx", 1, 1 << 16),
    ("n_audio_state", 1, 1 << 14),
    ("n_audio_head", 1, 1 << 10),
    ("n_audio_layer", 1, 1 << 10),
    ("n_text_ctx", 1, 1 << 16),
    ("n_text_state", 1, 1 << 14),
    ("n_text_head", 1, 1 << 10),
    ("n_text_layer", 1, 1 << 10),
    ("n_mels", 1, 1 << 10),
)
_MAX_MEL = 1 << 10
_MAX_FFT = 1 << 14


class InvalidModelError(ModelNotFoundError):
    """Modelldatei vorhanden, aber kein gültiges/unterstütztes ggml-Whisper-Modell.

    Unterklasse von :class:`ModelNotFoundError`, damit bestehende Aufrufer, die
    „kein brauchbares Modell“ behandeln, unverändert funktionieren.
    """


def _invalid(path: Path, reason: str) -> InvalidModelError:
    return InvalidModelError(f"Sprachmodell ungültig: {path.name} ({reason})")


def validate_model_file(path: Path | str) -> Path:
    """Prüft Existenz und Kopf von ``path``; Rückgabe: der (expandierte) Pfad.

    Fehlt die Datei → :class:`ModelNotFoundError`; ungültiger Kopf →
    :class:`InvalidModelError`. Liest nur die ersten 56 Bytes.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise ModelNotFoundError(f"Whisper-Modell nicht gefunden: {p}")
    try:
        with open(p, "rb") as fh:
            st = os.fstat(fh.fileno())
            head = fh.read(HEADER_SIZE)
    except OSError as exc:
        raise _invalid(p, f"nicht lesbar: {exc.strerror or type(exc).__name__}") from exc
    if not stat.S_ISREG(st.st_mode):  # pragma: no cover - is_file() oben, Wettlauf
        raise _invalid(p, "keine reguläre Datei")
    if len(head) < HEADER_SIZE:
        raise _invalid(p, "Datei zu kurz")
    magic, *hp, n_mel, n_fft = _HEADER.unpack(head)
    if magic != GGML_MAGIC:
        raise _invalid(p, "kein ggml-Format")
    *hparams, ftype = hp
    for (name, lo, hi), value in zip(_LIMITS, hparams):
        if not lo <= value <= hi:
            raise _invalid(p, f"{name}={value} außerhalb {lo}…{hi}")
    values = dict(zip((n for n, _lo, _hi in _LIMITS), hparams))
    if values["n_text_state"] != values["n_audio_state"]:
        raise _invalid(p, "n_text_state ≠ n_audio_state")
    # Kopfzahl muss die Zustandsgröße teilen (ggml_reshape_3d, GGML_ASSERT).
    for part in ("audio", "text"):
        state, heads = values[f"n_{part}_state"], values[f"n_{part}_head"]
        if state % heads:
            raise _invalid(p, f"n_{part}_state={state} nicht durch n_{part}_head={heads} teilbar")
    # Negative Werte nie zulassen: C rechnet ftype % 1000 mit Vorzeichen.
    if ftype < 0 or ftype % QNT_VERSION_FACTOR not in VALID_FTYPES:
        raise _invalid(p, f"ftype={ftype} nicht unterstützt")
    if not (1 <= n_mel <= _MAX_MEL and 1 <= n_fft <= _MAX_FFT):
        raise _invalid(p, f"Mel-Filter {n_mel}×{n_fft} unplausibel")
    if n_mel != values["n_mels"]:
        raise _invalid(p, f"Mel-Filter n_mel={n_mel} ≠ n_mels={values['n_mels']}")
    if n_fft != WHISPER_N_FFT_BINS:
        raise _invalid(p, f"Mel-Filter n_fft={n_fft} ≠ {WHISPER_N_FFT_BINS}")
    if st.st_size < HEADER_SIZE + n_mel * n_fft * 4:
        raise _invalid(p, "Datei zu kurz für die Mel-Filter")
    return p

