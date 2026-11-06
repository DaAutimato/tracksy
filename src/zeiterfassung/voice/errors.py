"""Fehlertypen der Sprach-Pipeline."""


class VoiceError(Exception):
    """Basisklasse aller Fehler im Paket ``zeiterfassung.voice``."""


class AudioDeviceError(VoiceError):
    """Kein/ungültiges Eingabegerät oder PortAudio nicht verfügbar."""


class CoreDumpProtectionError(VoiceError):
    """Schutz vor Absturzabbildern nicht setzbar – Aufnahme wird verweigert."""


class ModelNotFoundError(VoiceError):
    """Whisper-Modell fehlt im angegebenen lokalen Pfad."""


class TranscriptionError(VoiceError):
    """Fehler beim Laden des Modells oder bei der Transkription."""


class ModelDownloadError(VoiceError):
    """Fehler beim (ausdrücklich freigegebenen) Modell-Download."""
