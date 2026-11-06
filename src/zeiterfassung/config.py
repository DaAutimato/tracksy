"""Konfiguration (TOML) und Pfadlogik.

Auflösung der Pfade (Reihenfolge):
  1. ``ZEIT_DATA_DIR`` gesetzt → Daten dort, Config ``<data>/config.toml``,
     Modelle ``<repo>/models`` (Repo-Checkout) bzw. ``<data>/models``.
  2. Aus Repo-Checkout gestartet (``<repo>/pyproject.toml`` + ``<repo>/.git``
     + ``<repo>/src/zeiterfassung``) → Daten ``<repo>/data``,
     Config ``<repo>/data/config.toml``, Modelle ``<repo>/models``.
  3. Sonst XDG → Daten ``$XDG_DATA_HOME/zeiterfassung`` (Default
     ``~/.local/share/zeiterfassung``), Config
     ``$XDG_CONFIG_HOME/zeiterfassung/config.toml`` (Default ``~/.config/...``),
     Modelle ``<data>/models``.

Zusätzliche Overrides: ``ZEIT_CONFIG`` (Config-Datei), ``ZEIT_MODELS_DIR``.
Die Config-Datei wird nur durch :func:`save_config` geschrieben (atomar, 0600).

Eigener Speicherort (Einstellungen → Daten → Speicherort, ``general.data_dir``):
Ohne ``ZEIT_DATA_DIR`` gilt ``ZEIT_DATA_DIR`` > ``general.data_dir`` > Repo/XDG.
Die Config-Datei selbst zieht dabei **nicht** mit um: sie bleibt am Standardort
(Repo: ``<repo>/data/config.toml``, XDG: ``~/.config/zeiterfassung/config.toml``),
nur so bleibt der Eintrag ohne Henne-Ei-Problem auffindbar. Ebenso bleiben
Modelle (``models``), Spracherkennungs-Bibliothek (``lib``) und die venv des
Installers am Standardort; umgezogen werden nur die Tracksy-Daten
(``storage.relocate.DATA_ITEMS``).
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field, fields
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from .core.holidays import REGIONS as HOLIDAY_REGIONS
from .core.holidays import expand_periods, parse_period_iso
from .core.worktime import WEEKDAY_KEYS, WEEKDAY_NAMES_DE, DayRule, WorkSchedule, parse_hhmm

log = logging.getLogger(__name__)

APP_ID = "de.local.Zeiterfassung"
APP_DIRNAME = "zeiterfassung"

ENV_DATA_DIR = "ZEIT_DATA_DIR"
ENV_CONFIG = "ZEIT_CONFIG"
ENV_MODELS_DIR = "ZEIT_MODELS_DIR"

DB_FILENAME = "zeiterfassung.sqlite3"
CSV_FILENAME = "protokoll.csv"
CONFIG_FILENAME = "config.toml"

DIR_MODE = 0o700
FILE_MODE = 0o600

LOCATIONS = ("remote", "onsite")
COLOR_SCHEMES = ("system", "light", "dark")
ROUNDING_MODES = ("up",)
MODEL_SIZES = ("tiny", "base", "small", "medium")
# Obergrenzen: Aufnahme bleibt kurz und im RAM, Threads plausibel
MAX_CPU_THREADS = 64
MAX_RECORD_SECONDS = 120
MAX_EXTRA_HOLIDAYS = 1000


def package_root() -> Path:
    """Kandidat für die Repo-Wurzel (``src/zeiterfassung/config.py`` → drei Ebenen hoch)."""
    return Path(__file__).resolve().parents[2]


def is_repo_checkout(root: Path | None = None) -> bool:
    root = package_root() if root is None else root
    return (
        (root / "pyproject.toml").is_file()
        and (root / ".git").exists()
        and (root / "src" / "zeiterfassung").is_dir()
    )


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or str(Path.home()))


def xdg_data_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_DATA_HOME") or str(_home(env) / ".local" / "share")
    return Path(base) / APP_DIRNAME


def xdg_config_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or str(_home(env) / ".config")
    return Path(base) / APP_DIRNAME


def ensure_private_dir(path: Path) -> Path:
    """Verzeichnis anlegen (0700) bzw. Rechte auf 0700 setzen."""
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    try:
        if (path.stat().st_mode & 0o777) != DIR_MODE:
            os.chmod(path, DIR_MODE)
    except PermissionError:  # pragma: no cover - fremder Besitzer
        log.warning("Konnte Rechte von %s nicht setzen", path)
    return path


def ensure_private_file(path: Path) -> Path:
    """Datei anlegen falls nötig (0600) bzw. Rechte auf 0600 setzen."""
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, FILE_MODE)
    os.close(fd)
    if (path.stat().st_mode & 0o777) != FILE_MODE:
        os.chmod(path, FILE_MODE)
    return path


@dataclass
class GeneralConfig:
    employee_name: str = "Max Mustermann"
    default_location: str = "remote"
    first_run_done: bool = False  # Ersteinrichtungs-Assistent abgeschlossen
    # Aufbewahrungsfrist (Löschkonzept): Einträge mit Start älter als N Monate
    # werden beim App-Start gelöscht. 0 = unbegrenzt (Default, keine Löschung).
    retention_months: int = 0
    color_scheme: str = "system"  # Erscheinungsbild: system | light | dark
    # Eigener Speicherort der Daten (absoluter Pfad); leer = Standard (Repo/XDG).
    # ZEIT_DATA_DIR hat Vorrang. Ändern nur über Einstellungen → Daten (Umzug).
    data_dir: str = ""


@dataclass
class ExportConfig:
    rounding_minutes: int = 15
    rounding_mode: str = "up"
    template_path: str = ""  # leer = eingebautes Layout; sonst eigene XLSX-Vorlage


def _day_periods(name: str, items: object) -> frozenset[date]:
    """Liste gespeicherter Tage/Zeiträume prüfen und aufklappen (``ValueError``)."""
    if not isinstance(items, list):
        raise ValueError(f"worktime.{name}: Liste von Tagen/Zeiträumen erwartet")
    if len(items) > MAX_EXTRA_HOLIDAYS:
        raise ValueError(f"worktime.{name}: höchstens {MAX_EXTRA_HOLIDAYS} Einträge")
    try:
        return expand_periods([parse_period_iso(item) for item in items])
    except ValueError as exc:
        raise ValueError(f"worktime.{name}: {exc}") from None


@dataclass
class WorktimeConfig:
    """Regelarbeitszeit je Wochentag (``<tag>_active/_start/_end``) und Faktoren.

    Arbeit außerhalb erscheint im Export in den Spalten „nach reg. GZ“.
    Fehlt der Abschnitt ``[worktime]`` in einer älteren config.toml, gelten
    diese Vorgaben (Mo–Fr 08:00–17:00, Sa/So frei).
    """

    mon_active: bool = True
    mon_start: str = "08:00"
    mon_end: str = "17:00"
    tue_active: bool = True
    tue_start: str = "08:00"
    tue_end: str = "17:00"
    wed_active: bool = True
    wed_start: str = "08:00"
    wed_end: str = "17:00"
    thu_active: bool = True
    thu_start: str = "08:00"
    thu_end: str = "17:00"
    fri_active: bool = True
    fri_start: str = "08:00"
    fri_end: str = "17:00"
    sat_active: bool = False
    sat_start: str = "08:00"
    sat_end: str = "17:00"
    sun_active: bool = False
    sun_start: str = "08:00"
    sun_end: str = "17:00"
    weekday_overtime_factor: float = 1.25  # Minimum 1,25 (MIN_WEEKDAY_FACTOR)
    saturday_factor: float = 1.5
    sunday_factor: float = 2.0  # gilt auch für Feiertage
    # Feiertage: "" = nur bundesweite (Vorgabe), sonst Bundesland-Kürzel (core.holidays.REGIONS);
    # mit Bundesland gelten bundesweite und Landesfeiertage zusammen
    holiday_region: str = ""
    # eigene freie Tage (ganze Tage): "JJJJ-MM-TT" oder Zeitraum "JJJJ-MM-TT/JJJJ-MM-TT"
    # (beide Enden inklusive), z. B. Heiligabend, Urlaub, Gemeinde-Feiertage
    extra_holidays: list[str] = field(default_factory=list)
    # Urlaub (Tage/Zeiträume wie oben): zählt wie ein normaler Werktag, wirkt nicht auf
    # Berechnung oder Export – nur gespeichert und angezeigt
    vacation: list[str] = field(default_factory=list)

    def day(self, index: int) -> tuple[bool, str, str]:
        """``(aktiv, beginn, ende)`` für Wochentag 0 (Mo) … 6 (So)."""
        k = WEEKDAY_KEYS[index]
        return getattr(self, f"{k}_active"), getattr(self, f"{k}_start"), getattr(self, f"{k}_end")

    def set_day(self, index: int, active: bool, start: str, end: str) -> None:
        k = WEEKDAY_KEYS[index]
        setattr(self, f"{k}_active", bool(active))
        setattr(self, f"{k}_start", start)
        setattr(self, f"{k}_end", end)

    def schedule(self) -> WorkSchedule:
        """Als :class:`~zeiterfassung.core.worktime.WorkSchedule` (wirft ``ValueError``)."""
        days = []
        for i, name in enumerate(WEEKDAY_NAMES_DE):
            active, start, end = self.day(i)
            try:
                days.append(DayRule(active=active, start=parse_hhmm(start),
                                    end=parse_hhmm(end, allow_24=True)))
            except ValueError as exc:
                raise ValueError(f"worktime ({name}): {exc}") from None
        extra = _day_periods("extra_holidays", self.extra_holidays)
        _day_periods("vacation", self.vacation)  # nur prüfen, wirkt nicht auf die Berechnung
        if self.holiday_region not in HOLIDAY_REGIONS:
            raise ValueError("worktime.holiday_region: unbekanntes Bundesland-Kürzel "
                             f"(erlaubt: leer, {', '.join(k for k in HOLIDAY_REGIONS if k)})")
        return WorkSchedule(
            days=tuple(days),
            weekday_factor=float(self.weekday_overtime_factor),
            saturday_factor=float(self.saturday_factor),
            sunday_factor=float(self.sunday_factor),
            holiday_region=self.holiday_region,
            extra_holidays=extra,
        )


@dataclass
class AudioConfig:
    input_device: str = ""  # leer = Standardgerät; Zahl = Geräteindex, sonst Gerätename
    max_record_seconds: int = 30
    # Sprach-Module/PortAudio kurz nach dem Start im Hintergrund laden (öffnet kein
    # Mikrofon, lädt kein Modell). ZEIT_VOICE_WARMUP=0 hat Vorrang.
    warmup: bool = True

    def resolved_input_device(self) -> int | str | None:
        dev = self.input_device.strip()
        if not dev:
            return None
        return int(dev) if dev.isdigit() else dev


@dataclass
class WhisperConfig:
    model_size: str = "base"  # tiny | base | small | medium
    model_path: str = ""  # leer = <models_dir>/whisper-cpp/ggml-<model_size>.bin
    cpu_threads: int = 0  # 0 = automatisch
    language: str = "de"
    unload_after_seconds: int = 300  # Modell nach Inaktivität entladen; 0 = nie

    def resolved_model_path(self, models_dir: Path) -> Path:
        if self.model_path:
            return Path(self.model_path).expanduser()
        from .voice.models import model_file  # noqa: PLC0415

        return model_file(models_dir, self.model_size)


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    worktime: WorktimeConfig = field(default_factory=WorktimeConfig)

    def validate(self) -> None:
        if self.general.default_location not in LOCATIONS:
            raise ValueError(f"general.default_location muss {LOCATIONS} sein")
        if self.general.color_scheme not in COLOR_SCHEMES:
            raise ValueError(f"general.color_scheme muss {COLOR_SCHEMES} sein")
        if (
            not isinstance(self.general.retention_months, int)
            or isinstance(self.general.retention_months, bool)
            or not 0 <= self.general.retention_months <= 1200
        ):
            raise ValueError("general.retention_months muss eine ganze Zahl 0–1200 sein")
        if not isinstance(self.export.rounding_minutes, int) or self.export.rounding_minutes < 0:
            raise ValueError("export.rounding_minutes muss eine ganze Zahl ≥ 0 sein")
        if self.export.rounding_mode not in ROUNDING_MODES:
            raise ValueError(f"export.rounding_mode muss {ROUNDING_MODES} sein")
        if self.whisper.model_size not in MODEL_SIZES:
            raise ValueError(f"whisper.model_size muss {MODEL_SIZES} sein")
        if not isinstance(self.whisper.cpu_threads, int) or not 0 <= self.whisper.cpu_threads <= MAX_CPU_THREADS:
            raise ValueError(f"whisper.cpu_threads muss eine ganze Zahl 0–{MAX_CPU_THREADS} sein")
        if not re.fullmatch(r"[a-z]{2,3}", self.whisper.language):
            raise ValueError("whisper.language muss ein Sprachkürzel sein (z. B. de)")
        if (
            not isinstance(self.whisper.unload_after_seconds, int)
            or self.whisper.unload_after_seconds < 0
        ):
            raise ValueError("whisper.unload_after_seconds muss eine ganze Zahl ≥ 0 sein")
        if self.general.data_dir and not valid_data_dir_setting(self.general.data_dir):
            raise ValueError("general.data_dir muss ein absoluter Pfad ohne Steuerzeichen sein")
        if not 0 < self.audio.max_record_seconds <= MAX_RECORD_SECONDS:
            raise ValueError(f"audio.max_record_seconds muss 1–{MAX_RECORD_SECONDS} sein")
        self.worktime.schedule()  # Format, Ende > Beginn, Faktoren (ValueError)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        cfg = cls()
        for sect in fields(cls):
            raw = data.get(sect.name, {})
            if not isinstance(raw, dict):
                raise ValueError(f"Abschnitt [{sect.name}] ungültig")
            target = getattr(cfg, sect.name)
            known = {f.name: f for f in fields(target)}
            for key, value in raw.items():
                if key not in known:
                    log.warning("Unbekannter Konfigurationsschlüssel %s.%s ignoriert", sect.name, key)
                    continue
                default = getattr(target, key)
                if isinstance(default, bool):
                    ok = isinstance(value, bool)
                elif isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool):
                    value, ok = float(value), True  # TOML: "sunday_factor = 2" ist erlaubt
                else:
                    ok = isinstance(value, type(default)) and not isinstance(value, bool)
                if not ok:
                    raise ValueError(
                        f"{sect.name}.{key}: Typ {type(value).__name__} statt {type(default).__name__}"
                    )
                setattr(target, key, value)
        for key in data:
            if key not in {f.name for f in fields(cls)}:
                log.warning("Unbekannter Konfigurationsabschnitt [%s] ignoriert", key)
        cfg.validate()
        return cfg


MAX_PATH_LEN = 4096


def has_control_chars(text: str) -> bool:
    """Steuerzeichen (C0, DEL, C1) im Pfad – nie als Speicherort akzeptieren."""
    return any(ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F for ch in text)


def valid_data_dir_setting(value: object) -> bool:
    """``general.data_dir``: absoluter Pfad, ohne Steuerzeichen, plausible Länge."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_PATH_LEN
        and not has_control_chars(value)
        and os.path.isabs(value)
    )


class DataDirUnavailable(RuntimeError):
    """Eingestellter Speicherort (``general.data_dir``) fehlt, z. B. Laufwerk nicht eingehängt."""


def configured_data_dir(config_file: Path) -> Path | None:
    """``general.data_dir`` aus der Config-Datei lesen (nur lesend, fehlertolerant).

    Ungültige/unlesbare Werte werden ignoriert (Warnung) – die App meldet eine
    fehlerhafte Konfiguration ohnehin beim Laden.
    """
    try:
        with open(config_file, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        log.warning("Speicherort aus %s nicht lesbar (%s)", config_file, type(exc).__name__)
        return None
    general = data.get("general")
    value = general.get("data_dir", "") if isinstance(general, dict) else ""
    if not value:
        return None
    if not valid_data_dir_setting(value):
        log.warning("general.data_dir ungültig – Standard-Speicherort wird verwendet")
        return None
    return Path(os.path.normpath(value))


@dataclass
class Paths:
    data_dir: Path
    config_file: Path
    models_dir: Path
    mode: str  # "env" | "repo" | "xdg"
    # Herkunft des Datenverzeichnisses: "env" (ZEIT_DATA_DIR) | "config"
    # (general.data_dir) | "default" (Repo/XDG)
    data_source: str = "default"
    default_data_dir: Path | None = None  # Standardort ohne general.data_dir

    @property
    def db_file(self) -> Path:
        return self.data_dir / DB_FILENAME

    @property
    def csv_file(self) -> Path:
        return self.data_dir / CSV_FILENAME

    @classmethod
    def resolve(
        cls, env: Mapping[str, str] | None = None, repo_root: Path | None = None
    ) -> "Paths":
        env = os.environ if env is None else env
        root = package_root() if repo_root is None else repo_root
        repo = is_repo_checkout(root)
        explicit = env.get(ENV_DATA_DIR)
        if explicit:
            mode = "env"
            data_dir = Path(explicit).expanduser().resolve()
            config_file = data_dir / CONFIG_FILENAME
            models_dir = root / "models" if repo else data_dir / "models"
        elif repo:
            mode = "repo"
            data_dir = root / "data"
            config_file = data_dir / CONFIG_FILENAME
            models_dir = root / "models"
        else:
            mode = "xdg"
            data_dir = xdg_data_dir(env)
            config_file = xdg_config_dir(env) / CONFIG_FILENAME
            models_dir = data_dir / "models"
        if env.get(ENV_CONFIG):
            config_file = Path(env[ENV_CONFIG]).expanduser()
        if env.get(ENV_MODELS_DIR):
            models_dir = Path(env[ENV_MODELS_DIR]).expanduser()
        source = "env" if mode == "env" else "default"
        default_data = data_dir
        if mode != "env":
            custom = configured_data_dir(config_file)
            if custom is not None:
                data_dir, source = custom, "config"
        return cls(data_dir=data_dir, config_file=config_file, models_dir=models_dir, mode=mode,
                   data_source=source, default_data_dir=default_data)

    def ensure(self) -> "Paths":
        """Datenverzeichnis anlegen (0700). Config-/Modellverzeichnis erst bei Bedarf.

        Ein eigener Speicherort (``general.data_dir``) wird nie neu angelegt: fehlt
        er (z. B. Laufwerk nicht eingehängt), → :class:`DataDirUnavailable` statt
        still eine leere Datenbank an anderer Stelle zu beginnen.
        """
        if self.data_source == "config":
            self.check_available()
        ensure_private_dir(self.data_dir)
        return self

    def check_available(self) -> None:
        """Eigener Speicherort vorhanden und ein echtes Verzeichnis (kein Symlink)?"""
        if self.data_source != "config":
            return
        if self.data_dir.is_symlink() or not self.data_dir.is_dir():
            raise DataDirUnavailable(
                f"Speicherort {self.data_dir} ist nicht verfügbar (Laufwerk nicht eingehängt "
                f"oder Ordner entfernt?). Eingestellt in {self.config_file} (general.data_dir).")


def voice_settings(cfg: Config, paths: "Paths | None" = None) -> Any:
    """``VoiceSettings`` für die Sprach-Pipeline aus der Konfiguration bilden.

    Modellpfad: ``whisper.model_path`` falls gesetzt, sonst
    ``voice.models.model_file(paths.models_dir, whisper.model_size)``.
    Import von ``voice`` erfolgt lazy (keine Audio-/Whisper-Abhängigkeit beim Laden).
    """
    from .voice.settings import VoiceSettings  # noqa: PLC0415

    paths = paths or Paths.resolve()
    w = cfg.whisper
    return VoiceSettings(
        model_path=w.resolved_model_path(paths.models_dir),
        cpu_threads=w.cpu_threads,
        language=w.language,
        idle_unload_seconds=float(w.unload_after_seconds) if w.unload_after_seconds else 0.0,
        input_device=cfg.audio.resolved_input_device(),
        max_record_seconds=float(cfg.audio.max_record_seconds),
    )


def load_config(path: Path) -> Config:
    if not path.exists():
        return Config()
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return Config.from_dict(data)


INVALID_SUFFIX = ".ungueltig-"
MAX_INVALID_COPY_BYTES = 1_048_576  # größere „Einstellungsdateien“ werden nicht kopiert


def preserve_invalid_config(path: Path) -> Path | None:
    """Ungültige Einstellungsdatei sichern, bevor Standardwerte sie überschreiben können.

    Kopie ``<name>.ungueltig-JJJJMMTT-HHMMSS`` im selben Ordner, Rechte 0600; das
    Original bleibt unverändert. Gibt es schon eine Kopie mit identischem Inhalt, wird
    keine weitere angelegt (deren Pfad wird zurückgegeben). ``None``, wenn die Datei
    fehlt, zu groß ist oder nicht kopiert werden konnte.
    """
    from datetime import datetime

    try:
        if path.stat().st_size > MAX_INVALID_COPY_BYTES:
            log.warning("Ungültige Einstellungsdatei zu groß – keine Kopie angelegt")
            return None
        data = path.read_bytes()
        for old in sorted(path.parent.glob(path.name + INVALID_SUFFIX + "*")):
            try:
                if old.is_file() and not old.is_symlink() and old.read_bytes() == data:
                    return old
            except OSError:
                continue
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = path.with_name(path.name + INVALID_SUFFIX + stamp)
        n = 1
        while target.exists():
            n += 1
            target = path.with_name(f"{path.name}{INVALID_SUFFIX}{stamp}-{n}")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        return target
    except OSError as exc:
        log.warning("Ungültige Einstellungsdatei nicht gesichert: %s", exc.strerror or exc)
        return None


def save_config(cfg: Config, path: Path) -> None:
    """Atomar schreiben (temp + rename im selben Verzeichnis), Rechte 0600."""
    import tomli_w

    cfg.validate()
    ensure_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".toml", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            tomli_w.dump(cfg.to_dict(), fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)  # eigene, unvollständige Temp-Datei
        raise
