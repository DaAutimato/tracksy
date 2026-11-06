"""Energiebasierte Stille-Trimmung (Ersatz für den Silero-VAD).

Reines numpy, kein Modell, kein onnxruntime. Schneidet Stille am Anfang und
Ende einer Push-to-talk-Aufnahme ab; Pausen innerhalb der Äußerung bleiben
erhalten. Bewusst einfach: Rahmen-RMS gegen eine Schwelle aus Rauschboden und
Spitzenpegel.
"""

from __future__ import annotations

import numpy as np

__all__ = ["trim_silence"]


def trim_silence(
    audio: np.ndarray,
    sample_rate: int = 16000,
    *,
    frame_ms: float = 20.0,
    pad_ms: float = 250.0,
    min_seconds: float = 0.5,
    min_rms: float = 0.002,
    noise_factor: float = 3.0,
    peak_ratio: float = 0.5,
) -> np.ndarray:
    """Stille an Anfang/Ende entfernen.

    - Rahmen von ``frame_ms``; Schwelle = ``max(min_rms, min(noise*noise_factor,
      peak*peak_ratio))`` mit ``noise`` = 10-%-Quantil, ``peak`` = Maximum der
      Rahmen-RMS. Die Deckelung bei ``peak*peak_ratio`` sorgt dafür, dass leise
      Sprache bei hohem Grundrauschen nicht komplett abgeschnitten wird.
    - Um den ersten/letzten Sprachrahmen bleibt ``pad_ms`` Rand stehen.
    - Ergebnis ist mindestens ``min_seconds`` lang (soweit die Aufnahme reicht),
      um den Sprachbereich zentriert.
    - Kein Rahmen über der Schwelle → leeres Array (reine Stille).
    """
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    n = audio.size
    frame = max(1, int(sample_rate * frame_ms / 1000))
    if n == 0:
        return audio
    if n < frame:
        rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
        return audio if rms >= min_rms else audio[:0]
    count = n // frame
    frames = audio[: count * frame].reshape(count, frame).astype(np.float64)
    rms = np.sqrt(np.mean(np.square(frames), axis=1))
    peak = float(rms.max())
    noise = float(np.quantile(rms, 0.1))
    threshold = max(min_rms, min(noise * noise_factor, peak * peak_ratio))
    voiced = np.flatnonzero(rms >= threshold)
    if voiced.size == 0:
        return audio[:0]
    pad = int(sample_rate * pad_ms / 1000)
    start = max(0, int(voiced[0]) * frame - pad)
    end = min(n, (int(voiced[-1]) + 1) * frame + pad)
    if n - count * frame and end >= count * frame:
        end = n  # Restsamples hinter dem letzten vollen Rahmen behalten
    min_len = min(n, int(sample_rate * min_seconds))
    if end - start < min_len:
        missing = min_len - (end - start)
        start = max(0, start - missing // 2)
        end = min(n, start + min_len)
        start = max(0, end - min_len)
    return audio[start:end]
