"""GUI-freie Darstellungslogik (Formatierung, ViewModels).

Kein Gtk/GLib-Import: vollständig per pytest ohne Display testbar.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable

WEEKDAYS_SHORT = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
WEEKDAYS_LONG = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
MONTHS = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)
LOCATION_LABELS = {"remote": "Remote", "onsite": "Vor Ort"}
STATE_LABELS = {
    "idle": "Bereit",
    "running": "Läuft",
    "paused": "Pausiert",
    "stopping": "Wird abgeschlossen",
}


# ---------------------------------------------------------------------------
# Dauer / Zeit
# ---------------------------------------------------------------------------
def format_clock(seconds: int | float) -> str:
    """Laufende Zeit ``H:MM:SS`` (z. B. ``0:07:05``, ``12:00:00``)."""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}"


def format_hm(seconds: int | float) -> str:
    """Kurzdauer ``H:MM h``."""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    return f"{h}:{rem // 60:02d} h"


def format_tray_label(seconds: int | float) -> str:
    """Kompaktes Panel-Label ``H:MM``."""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    return f"{h}:{rem // 60:02d}"


def format_hours_decimal(seconds: int | float, ndigits: int = 2) -> str:
    """Dezimalstunden mit deutschem Komma: ``1,50 h``."""
    value = max(0.0, float(seconds)) / 3600.0
    return f"{value:.{ndigits}f}".replace(".", ",") + " h"


def round_up(seconds: int | float, step_minutes: int) -> int:
    """Auf ``step_minutes`` aufrunden (0 = keine Rundung)."""
    if seconds <= 0:
        return 0
    if step_minutes <= 0:
        return int(math.ceil(seconds))
    step = step_minutes * 60
    return int(math.ceil(seconds / step) * step)


@dataclass(frozen=True)
class Quanta:
    """Abrechnungseinheiten (Rundungsschritte) für die Viertelstunden-Leiste."""

    step_minutes: int
    full: int  # vollständig verbrauchte Einheiten
    partial: float  # Anteil der angebrochenen Einheit (0..1)
    billed_seconds: int  # aufgerundete Dauer

    @property
    def started(self) -> int:
        return self.full + (1 if self.partial > 0 else 0)

    @property
    def minutes_to_next(self) -> int:
        """Minuten bis zur nächsten Abrechnungsstufe (0 wenn genau auf Stufe)."""
        if self.step_minutes <= 0 or self.partial <= 0:
            return 0
        return int(math.ceil(round((1.0 - self.partial) * self.step_minutes, 6)))


def quanta(seconds: int | float, step_minutes: int) -> Quanta:
    s = max(0, int(seconds))
    if step_minutes <= 0:
        return Quanta(0, 0, 0.0, s)
    step = step_minutes * 60
    full, rest = divmod(s, step)
    return Quanta(step_minutes, int(full), rest / step, round_up(s, step_minutes))


def describe_quanta(q: Quanta) -> str:
    if q.step_minutes <= 0:
        return "Keine Rundung"
    if q.started == 0:
        return f"Abrechnung in {q.step_minutes}-Minuten-Schritten"
    units = "Einheit" if q.started == 1 else "Einheiten"
    text = f"{q.started} {units} à {q.step_minutes} min = {format_hours_decimal(q.billed_seconds)}"
    return text


def format_date_short(d: date) -> str:
    return f"{WEEKDAYS_SHORT[d.weekday()]}, {d.day:02d}.{d.month:02d}."


def format_date_long(d: date) -> str:
    return f"{WEEKDAYS_LONG[d.weekday()]}, {d.day}. {MONTHS[d.month - 1]} {d.year}"


def format_date_compact(d: date) -> str:
    """Kurzform für schmale Fenster: ``Do., 24.09.2026``."""
    return f"{WEEKDAYS_SHORT[d.weekday()]}., {d.day:02d}.{d.month:02d}.{d.year}"


def format_date_de(d: date) -> str:
    return f"{d.day:02d}.{d.month:02d}.{d.year}"


def format_month(year: int, month: int) -> str:
    return f"{MONTHS[month - 1]} {year}"


def format_hhmm(dt: datetime | None) -> str:
    if dt is None:
        return "–"
    return f"{dt.hour:02d}:{dt.minute:02d}"


def day_label(d: date, today: date | None = None) -> str:
    today = today or date.today()
    if d == today:
        return f"Heute, {format_date_short(d)[4:]}"
    if d == today - timedelta(days=1):
        return f"Gestern, {format_date_short(d)[4:]}"
    return format_date_short(d)


# ---------------------------------------------------------------------------
# Eingaben parsen (Editor)
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})?\s*$")
_TIME_RE = re.compile(r"^\s*(\d{1,2})(?:[:.](\d{2}))?\s*$")


def parse_date_de(text: str, default_year: int | None = None) -> date:
    """``30.10.2026``, ``30.10.26`` oder ``30.10.`` → date. ValueError bei Fehler."""
    m = _DATE_RE.match(text or "")
    if not m:
        raise ValueError("Datum bitte als TT.MM.JJJJ eingeben")
    day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
    if year is None:
        y = default_year or date.today().year
    else:
        y = int(year) + (2000 if len(year) == 2 else 0)
    return date(y, month, day)


def parse_time_hm(text: str) -> time:
    """``9:05``, ``09.05``, ``9`` → time. ValueError bei Fehler."""
    m = _TIME_RE.match(text or "")
    if not m:
        raise ValueError("Uhrzeit bitte als HH:MM eingeben")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if h > 23 or mi > 59:
        raise ValueError("Uhrzeit ungültig")
    return time(h, mi)


def combine_local(d: date, t: time) -> datetime:
    """Lokale Zeit mit Zeitzone."""
    return datetime.combine(d, t).astimezone()


def parse_minutes(text: str) -> int:
    t = (text or "").strip()
    if not t:
        return 0
    if ":" in t:
        h, m = t.split(":", 1)
        val = int(h) * 60 + int(m)
    else:
        val = int(t)
    if val < 0:
        raise ValueError("Pause darf nicht negativ sein")
    return val


def _to_minute(dt: datetime) -> datetime:
    return dt.astimezone().replace(second=0, microsecond=0)


def resolve_end(day: date, end_t: time, start: datetime,
                orig_start: datetime | None = None,
                orig_end: datetime | None = None) -> datetime:
    """Ende aus dem Datum des Beginns und der Ende-Uhrzeit bestimmen.

    Ohne Bezug: derselbe Tag, liegt die Uhrzeit vor dem Beginn, der Folgetag.
    Ist die Ende-Minute gegenüber dem gespeicherten Eintrag unverändert, bleibt
    dessen Tagesabstand erhalten – sonst würden Einträge über 24 h beim
    Speichern ohne Änderung gekürzt."""
    if orig_start is not None and orig_end is not None:
        oe, os_ = orig_end.astimezone(), orig_start.astimezone()
        if (oe.hour, oe.minute) == (end_t.hour, end_t.minute):
            cand = combine_local(day + timedelta(days=(oe.date() - os_.date()).days), end_t)
            if cand >= start:
                return cand
    end = combine_local(day, end_t)
    if end < start:  # über Mitternacht
        end = combine_local(day + timedelta(days=1), end_t)
    return end


def spans_beyond_default(orig_start: datetime, orig_end: datetime) -> bool:
    """True, wenn das Ende nicht aus Beginn-Datum + Uhrzeit folgt (über 24 h bzw.
    mehrere Tage) – der Editor zeigt dann einen Hinweis."""
    os_, oe = orig_start.astimezone(), orig_end.astimezone()
    default = resolve_end(os_.date(), time(oe.hour, oe.minute),
                          os_.replace(second=0, microsecond=0))
    return default.date() != oe.date()


def drop_unchanged_rounded(data: dict[str, Any], entry: Any, active: bool = False) -> dict[str, Any]:
    """Minutengenau angezeigte Felder entfernen, die der Nutzer nicht geändert hat.

    Der Eintragseditor zeigt Beginn/Ende als HH:MM und die Pause in ganzen
    Minuten (abgerundet). Ohne diesen Abgleich würde jedes Speichern die
    Sekunden der Rohzeiten still verwerfen (Audit „update“, CSV „korrektur“).
    Unverändert gelten Werte, die der angezeigten Minute entsprechen; dann bleibt
    der Rohwert erhalten. Ändert ``data`` in place und gibt es zurück.
    """
    if data.get("start_at") is not None and data["start_at"] == _to_minute(entry.start_at):
        data.pop("start_at")
    if active:
        return data
    if entry.end_at is not None and data.get("end_at") is not None \
            and data["end_at"] == _to_minute(entry.end_at):
        data.pop("end_at")
    if "pause_seconds" in data \
            and data["pause_seconds"] == (int(entry.pause_seconds or 0) // 60) * 60:
        data.pop("pause_seconds")
    return data


# ---------------------------------------------------------------------------
# Status (vom Service oder D-Bus) → Anzeige
# ---------------------------------------------------------------------------
def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


@dataclass
class TaskView:
    """Was der Hero-Bereich/Tray über den aktuellen Task zeigt."""

    state: str = "idle"
    customer: str = ""
    topic: str = ""
    ticket: str = ""
    location: str = "remote"
    start_at: datetime | None = None
    elapsed_at_snapshot: int = 0
    snapshot_at: datetime | None = None
    voice: str = "idle"
    pending_customer: str = ""
    recovered: bool = False
    last_customer: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.state in ("running", "paused", "stopping")

    @property
    def ticking(self) -> bool:
        return self.state == "running"

    def elapsed(self, now: datetime | None = None) -> int:
        """Netto-Sekunden; läuft der Task, wird ab Snapshot weitergezählt."""
        if not self.active:
            return 0
        if self.ticking and self.snapshot_at is not None:
            now = now or datetime.now().astimezone()
            delta = int((now - self.snapshot_at).total_seconds())
            return max(0, self.elapsed_at_snapshot + max(0, delta))
        return self.elapsed_at_snapshot

    @property
    def title(self) -> str:
        if not self.active:
            return "Kein Task aktiv"
        return self.customer or "Ohne Kunde"

    @property
    def subtitle(self) -> str:
        if not self.active:
            if self.last_customer:
                return f"Zuletzt: {self.last_customer}"
            return "Starte einen Task per Formular, Sprache oder Hotkey."
        parts = []
        if self.topic:
            parts.append(self.topic)
        if self.ticket:
            parts.append(f"Ticket {self.ticket}")
        return " – ".join(parts) if parts else "Ohne Thema"

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def since_label(self) -> str:
        if not self.active or self.start_at is None:
            return ""
        return f"seit {format_hhmm(self.start_at)}"

    @property
    def location_label(self) -> str:
        return LOCATION_LABELS.get(self.location, self.location)

    @property
    def css_state(self) -> str:
        """CSS-Klasse für den Zustand (siehe style.css)."""
        if self.voice == "listening":
            return "state-recording"
        return f"state-{self.state}"

    @property
    def tray_icon(self) -> str:
        if self.voice in ("listening", "transcribing"):
            return "zeit-recording"
        return {
            "running": "zeit-running",
            "paused": "zeit-paused",
            "stopping": "zeit-paused",
        }.get(self.state, "zeit-idle")

    def tooltip(self, now: datetime | None = None) -> str:
        if not self.active:
            return "Tracksy – kein Task aktiv"
        return (
            f"{self.title} ({self.state_label}) – {format_clock(self.elapsed(now))}\n{self.subtitle}"
        )


def task_view_from_status(status: dict[str, Any] | None) -> TaskView:
    if not status:
        return TaskView()
    entry = status.get("entry") or {}
    pending = status.get("pending_start") or {}
    last = status.get("last_stopped") or {}
    return TaskView(
        state=str(status.get("state") or "idle"),
        customer=str(entry.get("customer") or ""),
        topic=str(entry.get("topic") or ""),
        ticket=str(entry.get("ticket") or ""),
        location=str(entry.get("location") or "remote"),
        start_at=_parse_iso(entry.get("start_at")),
        elapsed_at_snapshot=int(entry.get("elapsed_seconds") or 0),
        snapshot_at=_parse_iso(status.get("generated_at")),
        voice=str(status.get("voice") or "idle"),
        pending_customer=str(pending.get("customer") or ""),
        recovered=bool(status.get("recovered")),
        last_customer=str(last.get("customer") or ""),
    )


# ---------------------------------------------------------------------------
# Sprachbefehl → Rückmeldung
# ---------------------------------------------------------------------------
def shorten(text: str, limit: int) -> str:
    """Text auf höchstens ``limit`` Zeichen kürzen (Auslassung „…“).

    Für Stellen ohne eigenen Umbruch: Tray-Menü, Tooltips, Banner, Toasts. Auch
    zulässige Extremlängen (500 Zeichen, ohne Leerzeichen) dürfen dort weder das
    Fenster noch das Menü verbreitern.
    """
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:max(limit - 1, 0)].rstrip() + "…"


def describe_start_fields(customer: str | None, topic: str | None, ticket: str | None,
                          location: str | None) -> str:
    parts = [customer or "Ohne Kunde"]
    if topic:
        parts.append(topic)
    if ticket:
        parts.append(f"Ticket {ticket}")
    if location:
        parts.append(LOCATION_LABELS.get(location, location))
    return ", ".join(parts)


def command_to_dict(cmd: Any) -> dict[str, Any]:
    """voice.parser.Command (oder dict) → dict für ``TrackerService.apply_command``."""
    if isinstance(cmd, dict):
        return dict(cmd)
    intent = getattr(cmd, "intent", "unknown")
    intent = getattr(intent, "value", intent)
    return {
        "intent": str(intent),
        "customer": getattr(cmd, "customer", None),
        "topic": getattr(cmd, "topic", None),
        "ticket": getattr(cmd, "ticket", None),
        "location": getattr(cmd, "location", None),
        "source": "voice",
    }


# ---------------------------------------------------------------------------
# Übersicht
# ---------------------------------------------------------------------------
@dataclass
class DayGroup:
    day: date
    entries: list[Any]
    seconds: int


@dataclass
class CustomerSum:
    customer: str
    seconds: int
    billed_seconds: int
    count: int
    remote_seconds: int = 0
    onsite_seconds: int = 0


def _local_day(e: Any) -> date:
    return e.start_at.astimezone().date() if e.start_at.tzinfo else e.start_at.date()


def group_by_day(entries: Iterable[Any], newest_first: bool = True) -> list[DayGroup]:
    groups: dict[date, DayGroup] = {}
    for e in entries:
        d = _local_day(e)
        g = groups.setdefault(d, DayGroup(d, [], 0))
        g.entries.append(e)
        g.seconds += int(e.net_seconds())
    out = sorted(groups.values(), key=lambda g: g.day, reverse=newest_first)
    for g in out:
        g.entries.sort(key=lambda e: e.start_at)
    return out


def billable_of(e: Any, step_minutes: int, schedule: Any = None) -> int:
    """Abrechenbare Sekunden wie im Export (je Anteil regulär/außerhalb aufgerundet).

    Laufende/pausierte Einträge (noch ohne Ende bzw. nicht gespeichert): Netto-Dauer
    bis jetzt, als Ganzes aufgerundet – die Aufteilung steht erst beim Speichern fest.
    """
    if str(getattr(e, "status", "stopped")) != "stopped" or e.end_at is None:
        return round_up(int(e.net_seconds()), step_minutes)
    from ..core.worktime import billable_seconds  # noqa: PLC0415

    return billable_seconds(e, schedule, step_minutes)


def sums_by_customer(entries: Iterable[Any], step_minutes: int,
                     schedule: Any = None) -> list[CustomerSum]:
    sums: dict[str, CustomerSum] = {}
    for e in entries:
        net = int(e.net_seconds())
        s = sums.setdefault(e.customer, CustomerSum(e.customer, 0, 0, 0))
        s.seconds += net
        s.billed_seconds += billable_of(e, step_minutes, schedule)
        s.count += 1
        if str(e.location) == "onsite":
            s.onsite_seconds += net
        else:
            s.remote_seconds += net
    return sorted(sums.values(), key=lambda s: (-s.seconds, s.customer.lower()))


def month_range(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def entry_row_title(e: Any) -> str:
    title = e.customer or "Ohne Kunde"
    if e.topic:
        title += f" – {e.topic}"
    return title


def entry_row_subtitle(e: Any) -> str:
    parts = [f"{format_hhmm(e.start_at.astimezone())}–{format_hhmm(e.end_at.astimezone() if e.end_at else None)}"]
    if e.ticket:
        parts.append(f"Ticket {e.ticket}")
    parts.append(LOCATION_LABELS.get(str(e.location), str(e.location)))
    if e.pause_seconds:
        parts.append(f"Pause {int(e.pause_seconds) // 60} min")
    return "  ·  ".join(parts)


def filter_suggestions(candidates: Iterable[str], text: str, limit: int = 6) -> list[str]:
    """Autovervollständigung: Präfix-Treffer zuerst, dann Teilstring; ohne exakten Treffer."""
    t = (text or "").strip().lower()
    if not t:
        return []
    seen: set[str] = set()
    prefix, inner = [], []
    for c in candidates:
        cl = c.lower()
        if not c or cl in seen or cl == t:
            continue
        seen.add(cl)
        if cl.startswith(t):
            prefix.append(c)
        elif t in cl:
            inner.append(c)
    return (sorted(prefix, key=str.lower) + sorted(inner, key=str.lower))[:limit]


# ---------------------------------------------------------------------------
# Daten: Aufbewahrung, Löschen, Sicherung (Einstellungen → Daten)
# ---------------------------------------------------------------------------
RETENTION_LEGAL_NOTE = (
    "Steuer- und handelsrechtliche Pflichten (in Deutschland bis zu 10 Jahre) können "
    "verlangen, Nachweise länger aufzubewahren. Für die passende Frist bist du selbst "
    "verantwortlich."
)
BACKUP_NOTE = (
    "Sicherungen enthalten personenbezogene Daten – bewahre sie verschlüsselt auf. "
    "Sicherungen im Datenverzeichnis (Ordner „backups“) entfernt die Aufbewahrungsfrist "
    "automatisch, beim Löschen auf Wunsch. Sicherungen in anderen Ordnern löschst du "
    "selbst, wenn die Frist abläuft."
)


def count_entries(n: int, adjective: str = "") -> str:
    """``1 Eintrag``, ``3 verworfene Einträge``, ``1 verworfener Eintrag``."""
    if not n:  # „Keine verworfenen Einträge“ (schwache Deklination)
        return f"Keine {adjective}n Einträge" if adjective else "Keine Einträge"
    if adjective:
        word = f"{adjective}r Eintrag" if n == 1 else f"{adjective} Einträge"
    else:
        word = "Eintrag" if n == 1 else "Einträge"
    return f"{n} {word}"


def retention_hint(months: int, cutoff: date | None, count: int) -> str:
    """Untertitel der Aufbewahrungsfrist: Folgen beim nächsten Start benennen."""
    if months <= 0 or cutoff is None:
        return "0 = unbegrenzt – es wird nichts automatisch gelöscht"
    when = f"Beim nächsten Start werden Einträge vor dem {format_date_de(cutoff)} gelöscht"
    if count:
        return f"{when} – derzeit {count_entries(count)}"
    return f"{when} – derzeit keine"


def purge_until_preview(until: date, count: int) -> str:
    """Vorschau „Alte Einträge löschen“: Stichtag zählt mit (bis einschließlich)."""
    return f"Beginn bis einschließlich {format_date_de(until)}: {count_entries(count)}"


def purge_before_heading(count: int) -> str:
    return f"{count_entries(count)} endgültig löschen?"


def purge_until_body(until: date) -> str:
    return (f"Alle Einträge mit Beginn bis einschließlich {format_date_de(until)} werden "
            "samt Tätigkeiten, Pausen, Änderungsverlauf und Protokollzeilen gelöscht. "
            "Ein laufender Task bleibt erhalten. Das lässt sich nicht rückgängig machen.")


def purge_until_label(until: date) -> str:
    """Beschriftung des Weiter-Knopfs, z. B. „Einträge bis einschließlich 31.10.2026 löschen …“."""
    return f"Einträge bis einschließlich {format_date_de(until)} löschen …"


def purge_discarded_heading(count: int) -> str:
    return f"{count_entries(count, 'verworfene')} endgültig löschen?"


PURGE_DISCARDED_BODY = (
    "Verworfene Einträge werden samt Änderungsverlauf und Protokollzeilen gelöscht. "
    "Das lässt sich nicht rückgängig machen."
)


def count_backups(n: int) -> str:
    return "1 Sicherung" if n == 1 else f"{n} Sicherungen"


def backups_option_label(n: int) -> str:
    """Kontrollkästchen im Lösch-Dialog."""
    return f"Auch {count_backups(n)} im Datenverzeichnis löschen"


def backups_option_hint(n: int) -> str:
    return (f"{'Sie enthält' if n == 1 else 'Sie enthalten'} die gelöschten Daten weiterhin. "
            "Sicherungen in anderen Ordnern sind nicht betroffen.")


def backups_left_hint(n: int) -> str:
    """Toast-Zusatz, wenn Sicherungen bewusst behalten wurden."""
    return (f"{count_backups(n)} im Datenverzeichnis {'enthält' if n == 1 else 'enthalten'} "
            "die Daten weiterhin")


def purge_done(count: int, discarded: bool = False) -> str:
    what = count_entries(count, "verworfene" if discarded else "")
    return f"{what} gelöscht"


# ---------------------------------------------------------------------------
# Exportdialog: Gesamtexport-Hinweis, Endung + Überschreiben
# ---------------------------------------------------------------------------
ALL_CUSTOMERS_NOTE_TITLE = "Enthält die Daten aller Kunden"
ALL_CUSTOMERS_NOTE_TEXT = ("Nur für die eigene Übersicht oder Buchhaltung. "
                           "Nicht an einzelne Kunden weitergeben.")


def with_export_suffix(path: Path, fmt: str) -> Path:
    """Endung ``.<fmt>`` anhängen, falls sie fehlt (Groß-/Kleinschreibung egal)."""
    if path.suffix.lower() == f".{fmt}":
        return path
    return path.with_name(path.name + f".{fmt}")


def needs_overwrite_confirm(chosen: Path, target: Path) -> bool:
    """True, wenn die App die Endung ergänzt hat und ``target`` schon existiert.

    Für den eingegebenen Namen fragt bereits ``Gtk.FileDialog`` nach; für den
    erweiterten Namen nicht – ohne Rückfrage würde ``os.replace`` die Datei still
    ersetzen. Ein Ordner zählt nicht: daran scheitert der Export ohnehin.
    """
    if target == chosen:
        return False
    if target.is_symlink():
        return True
    return target.exists() and not target.is_dir()


def overwrite_body(target: Path, fmt: str) -> str:
    return (f"Die Endung „.{fmt}“ wurde ergänzt. „{target.name}“ gibt es in diesem "
            "Ordner schon; beim Überschreiben geht der bisherige Inhalt verloren.")
