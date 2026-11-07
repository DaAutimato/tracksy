"""Parameter der Sprach-Pipeline.

Die Werte werden vom Integrator aus der App-Konfiguration befüllt; dieses
Modul liest selbst keine Konfiguration.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Domänenvokabular als Whisper-Prompt; verbessert Erkennung von Befehlswörtern.
#: Reine Wortliste, bewusst OHNE Beispielnamen und OHNE Startverb:
#: Whisper gibt Prompt-Inhalte bei undeutlicher Sprache teils als Transkript
#: aus (gemessen: erfundener Kunde "Müller"/Thema "Serverumzug" bzw.
#: "Starte Kunde, Thema, Ticket." für gesprochenes "Status"). Ein Echo dieser
#: Liste enthält mehrere Befehlswörter und wird vom Parser als UNKNOWN verworfen.
DEFAULT_COMMAND_PROMPT = "Kunde, Thema, Ticket, Stopp, Pause, weiter, Status."
# Zweiter Versuch, wenn der erste "nicht verstanden" oder "Start ohne Kunde" ergibt
# (gemessen 2026-11-07, Whisper base, 95 synthetische Aufnahmen: 55 → 71 richtig,
# 0 falsche Befehle). Nur Stichwörter des Startbefehls, weil
# - Beispielsätze/-werte als Halluzination im Transkript landen (Thema, das nie gesagt wurde),
# - Kurzbefehle als Liste im Prompt Wiederholungsschleifen auslösen ("Pause, Pause, …"),
# - der Start-Prompt allein Kurzbefehle verschlechtert ("Stopp." → "Start.") – deshalb
#   wird sein Ergebnis nur übernommen, wenn es ein Start MIT Kunde ist.
DEFAULT_START_RETRY_PROMPT = "Starte, Beginne, Neuer Task, Kunde, Thema, Ticket."


@dataclass(frozen=True)
class VoiceSettings:
    model_path: Path | str
    cpu_threads: int = 0                 # 0 = automatisch (max. 4)
    language: str = "de"
    beam_size: int = 1                   # 1 = schnell (schwache Rechner); stärkere Rechner ggf. 5
    # Energiebasierte Stille-Trimmung (voice/trim.py), nur numpy
    trim_silence: bool = True
    # Temperatur-Fallback begrenzt; lange Fallback-Ketten kosten bei
    # Halluzinationen viele Sekunden (gemessen bis 13 s).
    temperature: tuple[float, ...] = (0.0,)
    command_max_new_tokens: int | None = 48   # Befehle sind kurz
    # Diktat: Tokenbudget an die (getrimmte) Audiolänge gekoppelt, damit eine
    # Wiederholungsschleife früh endet: min + s * Token/s, höchstens max.
    # Deutsch zügig gesprochen ~4 Wörter/s à ~2 Token -> 10 Token/s mit Reserve.
    dictation_max_new_tokens: int | None = 224   # Obergrenze (whisper: n_text_ctx/2)
    dictation_tokens_per_second: float = 10.0
    dictation_min_tokens: int = 24
    # Zeitstempel im Diktat: Decoder kann das Sprachende markieren (gegen Schleifen)
    dictation_timestamps: bool = True
    # Aufeinanderfolgende identische Sätze/Phrasen im Diktat zusammenfassen
    dictation_collapse_repeats: bool = True
    idle_unload_seconds: float = 300.0   # <= 0: nie automatisch entladen
    command_prompt: str | None = DEFAULT_COMMAND_PROMPT
    start_retry_prompt: str | None = DEFAULT_START_RETRY_PROMPT  # None = kein zweiter Versuch
    dictation_prompt: str | None = None
    input_device: int | str | None = None  # None = Default-Gerät
    sample_rate: int = 16000
    max_record_seconds: float = 30.0
