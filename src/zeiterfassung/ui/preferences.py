"""Einstellungen (AdwPreferencesDialog) und gemeinsame Bausteine für die Ersteinrichtung."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from ..core.sanitize import MAX_FIELD_LEN  # noqa: E402
from ..core.holidays import REGIONS as HOLIDAY_REGIONS  # noqa: E402
from ..core.holidays import (  # noqa: E402
    MAX_FREE_DAYS,
    MAX_PERIOD_DAYS,
    format_period,
    merge_periods,
    parse_period_input,
    parse_period_iso,
    period_days,
    period_to_iso,
)
from ..core.worktime import (  # noqa: E402
    MAX_FACTOR,
    MIN_WEEKDAY_FACTOR,
    WEEKDAY_NAMES_DE,
    check_day,
    format_hhmm,
    parse_hhmm,
)
from ..logutil import log_failure  # noqa: E402
from . import integration  # noqa: E402
from .data_page import DataPage  # noqa: E402
from .widgets import Keycaps, a11y, limit_length  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ..app import ZeitApp

log = logging.getLogger(__name__)

ROUNDING_CHOICES = (0, 5, 6, 10, 15, 30, 60)
LOCATION_CHOICES = (("remote", "Remote"), ("onsite", "Vor Ort"))
COLOR_SCHEME_CHOICES = (("system", "Wie im System"), ("light", "Hell"), ("dark", "Dunkel"))
MODEL_SIZES = ("tiny", "base", "small", "medium")

PRIVACY_DOWNLOAD = (
    "Das Sprachmodell wird einmalig von huggingface.co heruntergeladen. Dabei werden "
    "deine IP-Adresse und technische Verbindungsdaten an Hugging Face (USA) übertragen. "
    "Danach arbeitet die Spracherkennung vollständig offline; Audio wird nie gespeichert."
)


def model_infos() -> dict:
    try:
        from ..voice.models import MODELS  # noqa: PLC0415
        return MODELS
    except Exception:  # pragma: no cover
        return {}


def model_subtitle(size: str) -> str:
    info = model_infos().get(size)
    if info is None:
        return ""
    return f"ca. {info.approx_mb} MB, RAM {info.ram_hint} – {info.note}"


def model_installed(app: "ZeitApp", size: str) -> bool:
    try:
        from ..voice.models import is_installed  # noqa: PLC0415
        return is_installed(app.paths.models_dir, size)
    except Exception:  # pragma: no cover
        return (app.paths.models_dir / "whisper-cpp" / f"ggml-{size}.bin").is_file()


def model_status(app: "ZeitApp", size: str) -> str:
    """``missing`` / ``ok`` / ``size_mismatch`` (günstig, nur Dateigröße)."""
    try:
        from ..voice.models import model_status as _status  # noqa: PLC0415
        return _status(app.paths.models_dir, size)
    except Exception:  # pragma: no cover
        return "ok" if model_installed(app, size) else "missing"


MODEL_STATE_TEXT = {
    "missing": "Nicht installiert – Sprache ist deaktiviert",
    "ok": "Installiert",
    "size_mismatch": "Beschädigt (Dateigröße falsch) – bitte reparieren",
    "hash_mismatch": "Beschädigt (Prüfsumme falsch) – bitte reparieren",
}


def repair_done_text(size: str, archived) -> str:
    """Rückmeldung nach erfolgreicher Reparatur inkl. Ablageort der alten Datei."""
    if archived is None:
        return f"Modell „{size}“ installiert."
    return (f"Modell „{size}“ repariert. Die alte Datei liegt unter {archived} "
            "und kann dort gelöscht werden, wenn sie nicht mehr gebraucht wird.")


class ModelDownloader:
    """Startet den (freigegebenen) Download im Thread; Rückmeldungen im Main-Loop."""

    running = False
    current: "ModelDownloader | None" = None

    def __init__(self, app: "ZeitApp", size: str, on_progress: Callable[[str], None],
                 on_done: Callable[[bool, str], None], *, repair: bool = False) -> None:
        self.app, self.size, self.repair = app, size, repair
        self.on_progress, self.on_done = on_progress, on_done
        self.cancel = threading.Event()

    @classmethod
    def cancel_running(cls) -> None:
        """Laufenden Download abbrechen; ohne Download wirkungslos."""
        if cls.current is not None:
            cls.current.cancel.set()

    def start(self) -> None:
        if ModelDownloader.running:
            self.on_done(False, "Es läuft bereits ein Download.")
            return
        ModelDownloader.running = True
        ModelDownloader.current = self
        threading.Thread(target=self._run, name="model-download", daemon=True).start()

    def _run(self) -> None:
        try:
            from ..voice.models import download, repair  # noqa: PLC0415

            progress = lambda msg: GLib.idle_add(self.on_progress, msg)  # noqa: E731
            if self.repair:
                res = repair(self.app.paths.models_dir, self.size, progress, cancel=self.cancel)
                done_msg = repair_done_text(self.size, res.archived)
            else:
                download(self.app.paths.models_dir, self.size, progress, cancel=self.cancel)
                done_msg = f"Modell „{self.size}“ installiert."
        except Exception as exc:
            if self.cancel.is_set():
                GLib.idle_add(self._finish, False, "Download abgebrochen.")
                return
            GLib.idle_add(self._finish, False, str(exc))
        else:
            GLib.idle_add(self._finish, True, done_msg)

    def _finish(self, ok: bool, msg: str) -> bool:
        ModelDownloader.running = False
        ModelDownloader.current = None
        if hasattr(self.app, "update_first_run_action"):
            self.app.update_first_run_action()  # Menü „Einrichtung starten“ ausblenden
        self.on_done(ok, msg)
        return False


def confirm_download(parent: Gtk.Widget, size: str, on_yes: Callable[[], None]) -> None:
    info = model_infos().get(size)
    mb = f" (ca. {info.approx_mb} MB)" if info else ""
    dlg = Adw.AlertDialog(heading=f"Sprachmodell „{size}“ herunterladen?",
                          body=PRIVACY_DOWNLOAD + f"\n\nDownload-Größe{mb}.")
    dlg.add_response("cancel", "Abbrechen")
    dlg.add_response("download", "Herunterladen")
    dlg.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response("cancel")
    dlg.set_close_response("cancel")
    dlg.connect("response", lambda _d, r: on_yes() if r == "download" else None)
    dlg.present(parent)


def repair_body(size: str) -> str:
    """Text des Bestätigungsdialogs „Erneut herunterladen / Reparieren“."""
    info = model_infos().get(size)
    mb = f"ca. {info.approx_mb} MB" if info else "die Modelldatei"
    return (f"Das Sprachmodell „{size}“ wird erneut von huggingface.co heruntergeladen "
            f"(Datenvolumen {mb}). Dabei werden deine IP-Adresse und technische "
            "Verbindungsdaten an Hugging Face (USA) übertragen.\n\n"
            "Die neue Datei wird erst nach erfolgreicher Prüfung (Größe, SHA-1, SHA-256) "
            "aktiviert. Die vorhandene Datei wird nicht gelöscht, sondern in den Ordner "
            "„_archiv“ im Modellordner verschoben; schlägt der Download fehl, bleibt sie "
            "unverändert. Während des Downloads wird vorübergehend der doppelte "
            "Speicherplatz benötigt.")


def confirm_repair(parent: Gtk.Widget, size: str, on_yes: Callable[[], None]) -> None:
    dlg = Adw.AlertDialog(heading=f"Sprachmodell „{size}“ erneut herunterladen?",
                          body=repair_body(size))
    dlg.add_response("cancel", "Abbrechen")
    dlg.add_response("repair", "Erneut herunterladen")
    dlg.set_response_appearance("repair", Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response("cancel")
    dlg.set_close_response("cancel")
    dlg.connect("response", lambda _d, r: on_yes() if r == "repair" else None)
    dlg.present(parent)


def confirm_commands(parent: Gtk.Widget, heading: str, intro: str, text: str,
                     confirm_label: str, on_yes: Callable[[], None]) -> Adw.AlertDialog:
    """Zeigt die geplanten Befehle/Dateiinhalte und führt erst nach Klick aus."""
    dlg = Adw.AlertDialog(heading=heading, body=intro)
    lbl = Gtk.Label(label=text, xalign=0, wrap=True, selectable=True)
    lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    lbl.add_css_class("command-preview")
    sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                            max_content_height=300, propagate_natural_height=True)
    sc.set_size_request(480, -1)
    sc.set_child(lbl)
    dlg.set_extra_child(sc)
    dlg.add_response("cancel", "Abbrechen")
    dlg.add_response("ok", confirm_label)
    dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response("cancel")
    dlg.set_close_response("cancel")
    dlg.connect("response", lambda _d, r: on_yes() if r == "ok" else None)
    dlg.present(parent)
    return dlg


def show_error(parent: Gtk.Widget, heading: str, text: str) -> Adw.AlertDialog:
    """Fehler deutlich anzeigen (nicht nur als flüchtiger Toast)."""
    dlg = Adw.AlertDialog(heading=heading, body=text)
    dlg.add_response("ok", "Schließen")
    dlg.set_default_response("ok")
    dlg.set_close_response("ok")
    dlg.present(parent)
    return dlg


HOTKEYS_DISABLED_HINT = ("Systemänderungen sind in dieser Umgebung abgeschaltet "
                         "(ZEIT_NO_SYSTEM_CHANGES) – Kürzel können hier nicht eingerichtet werden.")
HOTKEYS_NOT_SET_HINT = ("Nicht eingerichtet – die Kürzel wirken erst nach „Einrichten …“")


class HotkeyGroup(Adw.PreferencesGroup):
    """Hotkeys anzeigen, einrichten, entfernen (Cinnamon, GNOME/Ubuntu) bzw. Anleitung."""

    def __init__(self, app: "ZeitApp", parent_getter: Callable[[], Gtk.Widget],
                 on_changed: Callable[[str], None]) -> None:
        super().__init__(title="Globale Tastenkürzel")
        self.app = app
        self._parent = parent_getter
        self._on_changed = on_changed
        self.desktop = integration.keybinding_desktop()
        self.state: integration.HotkeyState | None = None
        self.last_error: str | None = None  # für Tests
        self.last_dialog: Adw.AlertDialog | None = None  # für Tests
        self.set_description(
            f"Werden als {self.desktop.label}-Tastenkürzel eingetragen und rufen das Programm "
            "„zeit“ auf."
            if self.desktop else integration.manual_hotkey_description())
        for hk in integration.HOTKEYS:
            row = Adw.ActionRow(title=hk.label.replace("Tracksy: ", ""),
                                subtitle=GLib.markup_escape_text(integration.cli_command(hk.action)))
            row.set_subtitle_selectable(True)
            # Keycaps statt Gtk.ShortcutLabel: deutsch beschriftet, auch ohne GTK-Sprachpaket
            row.add_suffix(Keycaps(hk.binding))
            self.add(row)
        self.status_row = Adw.ActionRow(title="Status")
        self.status_row.set_name("hotkey-status")
        self.setup_btn = Gtk.Button(label="Einrichten …", valign=Gtk.Align.CENTER)
        self.remove_btn = Gtk.Button(label="Entfernen …", valign=Gtk.Align.CENTER)
        self.remove_btn.add_css_class("destructive-action")
        a11y(self.setup_btn, "Tastenkürzel einrichten")
        a11y(self.remove_btn, "Tastenkürzel entfernen")
        self.setup_btn.connect("clicked", lambda *_: self._setup())
        self.remove_btn.connect("clicked", lambda *_: self._remove())
        self.status_row.add_suffix(self.setup_btn)
        self.status_row.add_suffix(self.remove_btn)
        self.disabled = integration.system_changes_disabled()
        # ohne Automatik (z. B. KDE) bleibt die Zeile lesbar, sie hat dann keine Knöpfe
        self.status_row.set_sensitive(self.desktop is None or not self.disabled)
        self.add(self.status_row)
        self._refresh()

    def _read(self) -> "integration.HotkeyState | None":
        try:
            return integration.read_hotkey_state(desktop=self.desktop)
        except Exception as exc:
            log_failure(log, "Tastenkürzel nicht lesbar", exc, level=logging.INFO)
            self.last_error = str(exc)
            return None

    def _installed(self, st: "integration.HotkeyState | None") -> bool:
        return st is not None and integration.hotkeys_installed(
            st.entries, desktop=self.desktop, ours=st.ours)

    def _refresh(self) -> None:
        if self.desktop is None:  # z. B. KDE: nur Anleitung, keine Knöpfe
            self.status_row.set_subtitle(integration.HOTKEYS_MANUAL_STATUS)
            self.setup_btn.set_visible(False)
            self.remove_btn.set_visible(False)
            return
        if self.disabled:
            self.status_row.set_subtitle(HOTKEYS_DISABLED_HINT)
            self.remove_btn.set_visible(False)
            return
        self.state = self._read() if self.desktop else None
        if self.state is None:
            self.status_row.set_subtitle("Nicht ermittelbar" + (
                f": {GLib.markup_escape_text(self.last_error)}" if self.last_error else ""))
            installed = False
        else:
            installed = self._installed(self.state)
            self.status_row.set_subtitle("Eingerichtet und aktiv" if installed
                                         else HOTKEYS_NOT_SET_HINT)
        self.remove_btn.set_visible(installed)
        self.setup_btn.set_label("Erneut einrichten …" if installed else "Einrichten …")

    def _fail(self, heading: str, exc: BaseException) -> None:
        msg = str(exc) or type(exc).__name__
        if isinstance(exc, integration.SystemChangesDisabled):
            msg = HOTKEYS_DISABLED_HINT
        log_failure(log, heading, exc, level=logging.ERROR)
        self.last_error = msg
        self.last_dialog = show_error(self._parent(), heading, msg)

    def _setup(self) -> None:
        st = self._read()
        if st is None:
            self._fail("Einrichten nicht möglich",
                       RuntimeError(f"Die vorhandenen Kürzel sind nicht lesbar: {self.last_error}"))
            return
        cmds = integration.hotkey_setup_commands(st.entries, desktop=self.desktop, ours=st.ours,
                                                 duplicates=st.duplicates)
        conflicts = integration.read_conflicts(st, desktop=self.desktop)
        intro = f"Folgende Befehle ändern deine {self.desktop.label}-Tastenkürzel:"
        if conflicts:
            intro = ("Achtung: " + "; ".join(conflicts) + ". Das Kürzel funktioniert dann "
                     "ggf. nicht oder löst beides aus.\n\n" + intro)

        def run() -> None:
            try:
                integration.run_commands(cmds)
                after = integration.read_hotkey_state(desktop=self.desktop)
                if not self._installed(after):
                    raise RuntimeError("Die Kürzel wurden nicht übernommen (gsettings meldete "
                                       "keinen Fehler, die Liste enthält sie aber nicht).")
            except Exception as exc:
                self._fail("Einrichten fehlgeschlagen", exc)
            else:
                self._on_changed("Tastenkürzel eingerichtet – sofort aktiv")
            self._refresh()

        self.last_dialog = confirm_commands(
            self._parent(), "Tastenkürzel einrichten?", intro,
            integration.commands_as_text(cmds), "Ausführen", run)
        self._run_setup = run  # für Tests

    def _remove(self) -> None:
        st = self._read()
        if st is None:
            self._fail("Entfernen nicht möglich",
                       RuntimeError(f"Die vorhandenen Kürzel sind nicht lesbar: {self.last_error}"))
            return
        cmds = integration.hotkey_remove_commands(st.entries, desktop=self.desktop, ours=st.ours,
                                                  duplicates=st.duplicates)

        def run() -> None:
            try:
                integration.run_commands(cmds)
            except Exception as exc:
                self._fail("Entfernen fehlgeschlagen", exc)
            else:
                self._on_changed("Tastenkürzel entfernt")
            self._refresh()

        self.last_dialog = confirm_commands(
            self._parent(), "Tastenkürzel entfernen?",
            "Folgende Befehle entfernen die Kürzel von Tracksy:",
            integration.commands_as_text(cmds), "Ausführen", run)


WORKTIME_DESCRIPTION = (
    "Arbeit außerhalb dieser Zeiten erscheint im Export in den Spalten „nach reg. GZ“ "
    "und wird mit den Faktoren unten bewertet. An freien Tagen zählt der ganze Tag als "
    "außerhalb (Samstag/Sonntag 0–24 Uhr); Feiertage zählen wie Sonntag. Jede angefangene "
    "Rundungseinheit zählt – getrennt für Regelzeit und Zeit außerhalb."
)


MAX_EXTRA_HOLIDAYS_UI = 1000


def format_day(iso: str) -> str:
    """Gespeicherter Eintrag → Anzeige: ``24.12.2026`` bzw. ``27.07.2026–07.08.2026``."""
    try:
        return format_period(parse_period_iso(iso))
    except ValueError:
        return iso


def parse_day_list(text: str) -> tuple[list[str], str | None]:
    """Eingabe „24.12.2026, 27.07.–07.08.2026“ → sortierte ISO-Liste (Tage bzw.
    ``beginn/ende``), Doppelte und Überlappungen zusammengefasst; sonst Fehlertext."""
    periods = []
    for part in text.replace(";", ",").replace("\n", ",").split(","):
        if not part.strip():
            continue
        try:
            periods.append(parse_period_input(part))
        except ValueError as exc:
            return [], f"{exc} (z. B. 24.12.2026 oder 27.07.–07.08.2026)"
    merged = merge_periods(periods)
    # Überlappende Zeiträume können zusammen länger als erlaubt werden; so ein Eintrag
    # ließe sich nicht speichern (Config-Prüfung) → hier schon ablehnen
    if any((e - s).days + 1 > MAX_PERIOD_DAYS for s, e in merged):
        return [], (f"Zusammengefasster Zeitraum zu lang (höchstens {MAX_PERIOD_DAYS} Tage) – "
                    "Zeiträume ohne Überschneidung eintragen")
    if len(merged) > MAX_EXTRA_HOLIDAYS_UI:
        return [], f"Höchstens {MAX_EXTRA_HOLIDAYS_UI} Einträge"
    if period_days(merged) > MAX_FREE_DAYS:
        return [], f"Höchstens {MAX_FREE_DAYS} freie Tage insgesamt"
    return [period_to_iso(p) for p in merged], None


def describe_days(items: list[str], key: str = "extra_holidays") -> str:
    """„24.12.2026, 27.07.2026–07.08.2026 (13 Tage)“ für den Hinweis unter dem Feld."""
    if not items:
        return "kein Urlaub" if key == "vacation" else "keine eigenen Feiertage"
    n = period_days([parse_period_iso(i) for i in items])
    return ", ".join(format_day(i) for i in items) + (f" ({n} Tage)" if n != 1 else " (1 Tag)")


class WorktimeGroup(Adw.PreferencesGroup):
    """Regelarbeitszeit je Wochentag (Schalter + Beginn/Ende) und Faktoren.

    Schreibt gültige Eingaben sofort in ``cfg.worktime`` und ruft dann
    ``on_change`` auf (Einstellungen: speichern; Assistent: nichts – dort wird erst
    am Ende gespeichert). Ungültige Zeilen werden rot markiert und nicht übernommen.
    """

    def __init__(self, cfg, on_change: Callable[[], None] | None = None,
                 *, with_factors: bool = True) -> None:
        super().__init__(title="Regelarbeitszeit", description=WORKTIME_DESCRIPTION)
        self.cfg = cfg
        self._on_change = on_change or (lambda: None)
        self._loading = True
        self.rows: list[dict] = []
        for i, name in enumerate(WEEKDAY_NAMES_DE):
            active, start, end = cfg.worktime.day(i)
            row = Adw.ActionRow(title=name)
            sw = Gtk.Switch(active=active, valign=Gtk.Align.CENTER)
            a11y(sw, f"{name}: Regelarbeitszeit", False)
            row.add_prefix(sw)
            row.set_activatable_widget(sw)
            ent_s = Gtk.Entry(text=start, width_chars=5, max_length=5, valign=Gtk.Align.CENTER,
                              input_purpose=Gtk.InputPurpose.DIGITS, xalign=0.5)
            ent_e = Gtk.Entry(text=end, width_chars=5, max_length=5, valign=Gtk.Align.CENTER,
                              input_purpose=Gtk.InputPurpose.DIGITS, xalign=0.5)
            a11y(ent_s, f"{name}: Beginn (HH:MM)")
            a11y(ent_e, f"{name}: Ende (HH:MM)")
            dash = Gtk.Label(label="–", valign=Gtk.Align.CENTER)
            dash.set_accessible_role(Gtk.AccessibleRole.PRESENTATION)
            box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
            box.append(ent_s)
            box.append(dash)
            box.append(ent_e)
            row.add_suffix(box)
            entry = {"index": i, "row": row, "switch": sw, "start": ent_s, "end": ent_e}
            self.rows.append(entry)
            sw.connect("notify::active", lambda *_a, e=entry: self._changed(e))
            ent_s.connect("changed", lambda *_a, e=entry: self._changed(e))
            ent_e.connect("changed", lambda *_a, e=entry: self._changed(e))
            self._update_row(entry, check_day(active, start, end))
            self.add(row)
        self.factor_rows: dict[str, Adw.SpinRow] = {}
        if with_factors:
            w = cfg.worktime
            for key, title, subtitle, lo, value in (
                ("weekday_overtime_factor", "Faktor werktags außerhalb",
                 f"Montag–Freitag außerhalb der Regelarbeitszeit (mindestens "
                 f"{MIN_WEEKDAY_FACTOR:g})".replace(".", ","), MIN_WEEKDAY_FACTOR,
                 w.weekday_overtime_factor),
                ("saturday_factor", "Faktor Samstag", "Samstag außerhalb der Regelarbeitszeit",
                 1.0, w.saturday_factor),
                ("sunday_factor", "Faktor Sonn- und Feiertag", "ganzer Tag, auch Feiertage an Werktagen",
                 1.0, w.sunday_factor),
            ):
                sr = Adw.SpinRow.new_with_range(lo, MAX_FACTOR, 0.05)
                sr.set_digits(2)
                sr.set_title(title)
                sr.set_subtitle(subtitle)
                sr.set_value(value)
                a11y(sr, title, False)
                sr.connect("notify::value", lambda *_a, k=key: self._factor_changed(k))
                self.factor_rows[key] = sr
                self.add(sr)
        self._add_holiday_rows()
        self._loading = False

    # ------------------------------------------------------------ Feiertage
    def _add_holiday_rows(self) -> None:
        w = self.cfg.worktime
        keys = list(HOLIDAY_REGIONS)
        self.region = Adw.ComboRow(
            title="Feiertage",
            subtitle="Bundesweite plus die des gewählten Bundeslands; gelten wie Sonntag – "
                     "auch die Regelarbeitszeit entfällt",
            model=Gtk.StringList.new([HOLIDAY_REGIONS[k] for k in keys]))
        self.region.set_selected(keys.index(w.holiday_region) if w.holiday_region in keys else 0)
        a11y(self.region, "Feiertage: Bundesland", False)
        self.region.connect("notify::selected", lambda *_: self._region_changed())
        self.add(self.region)
        self.extra = self._add_day_field(
            "extra_holidays",
            "Eigene Feiertage (z. B. 24.12.2026, 27.07.–07.08.2026)",
            "Eigene Feiertage: Tage oder Zeiträume, durch Komma getrennt",
            "Eigene Feiertage",
            "zählen wie Feiertage (Sonn-/Feiertag). Einzelne Tage oder Zeiträume (Bindestrich "
            "oder „bis“), durch Komma getrennt; nur ganze Tage. Für Gemeinde-Feiertage (z. B. "
            "Augsburger Friedensfest, Fronleichnam in Teilen Sachsens/Thüringens), Heiligabend "
            "oder Betriebsruhe.")
        self.vacation = self._add_day_field(
            "vacation",
            "Urlaub (z. B. 27.07.–07.08.2026)",
            "Urlaub: Tage oder Zeiträume, durch Komma getrennt",
            "Urlaub",
            "zählt wie ein normaler Werktag – ändert nichts an Berechnung und Export. Nur zur "
            "eigenen Übersicht, wird nur lokal gespeichert.")

    def _add_day_field(self, key: str, title: str, label: str, name: str, hint: str) -> dict:
        """Eingabezeile für Tage/Zeiträume (``cfg.worktime.<key>``) mit Hinweis darunter."""
        row = Adw.EntryRow(title=title)
        row.set_text(", ".join(format_day(d) for d in getattr(self.cfg.worktime, key)))
        row.set_show_apply_button(True)
        limit_length(row, 4000)
        a11y(row, label, False)
        # Hinweise stehen unter der Karte → immer mit Feldnamen beginnen
        hint_label = Gtk.Label(label=f"{name}: {hint}", wrap=True, xalign=0)
        hint_label.add_css_class("caption")
        hint_label.add_css_class("dim-label")
        hint_label.set_margin_top(6)
        # Screenreader: Hinweis bzw. Fehlermeldung gehört zum Feld
        row.update_relation([Gtk.AccessibleRelation.DESCRIBED_BY], [[hint_label]])
        field_ = {"key": key, "name": name, "row": row, "hint": hint_label, "error": None}
        row.connect("apply", lambda *_: self._days_changed(field_))
        row.connect("entry-activated", lambda *_: self._days_changed(field_))
        self.add(row)
        self.add(hint_label)
        return field_

    def _region_changed(self) -> None:
        if self._loading:
            return
        key = list(HOLIDAY_REGIONS)[self.region.get_selected()]
        if self.cfg.worktime.holiday_region != key:
            self.cfg.worktime.holiday_region = key
            self._on_change()

    def _days_changed(self, f: dict) -> bool:
        """Eingabe übernehmen; ``False`` bei Fehler (Feld rot, Hinweis)."""
        days, error = parse_day_list(f["row"].get_text())
        f["error"] = error
        if error:
            f["row"].add_css_class("error")
            f["hint"].set_label(f"{f['name']}: {error}")
            return False
        f["row"].remove_css_class("error")
        f["hint"].set_label(f"{f['name']} – gespeichert: " + describe_days(days, f["key"]))
        if getattr(self.cfg.worktime, f["key"]) != days:
            setattr(self.cfg.worktime, f["key"], days)
            self._on_change()
        return True

    def _update_row(self, e: dict, error: str | None) -> None:
        active = e["switch"].get_active()
        for w in (e["start"], e["end"]):
            w.set_sensitive(active)
            if error:
                w.add_css_class("error")
            else:
                w.remove_css_class("error")
        name = WEEKDAY_NAMES_DE[e["index"]]
        if error:
            e["row"].set_subtitle(GLib.markup_escape_text(error))
        elif active:
            e["row"].set_subtitle("")
        else:
            e["row"].set_subtitle("frei – ganzer Tag zählt als außerhalb")
        e["error"] = error
        e["row"].zeit_state = (name, active, error)  # für Tests

    def _changed(self, e: dict) -> None:
        if self._loading:
            return
        active = e["switch"].get_active()
        start, end = e["start"].get_text().strip(), e["end"].get_text().strip()
        error = check_day(active, start, end)
        self._update_row(e, error)
        if error:
            return
        start_n = format_hhmm(parse_hhmm(start))
        end_n = format_hhmm(parse_hhmm(end, allow_24=True))
        if self.cfg.worktime.day(e["index"]) == (active, start_n, end_n):
            return
        self.cfg.worktime.set_day(e["index"], active, start_n, end_n)
        self._on_change()

    def _factor_changed(self, key: str) -> None:
        if self._loading:
            return
        value = round(float(self.factor_rows[key].get_value()), 2)
        if key == "weekday_overtime_factor":
            value = max(MIN_WEEKDAY_FACTOR, value)
        if getattr(self.cfg.worktime, key) == value:
            return
        setattr(self.cfg.worktime, key, value)
        self._on_change()

    @property
    def valid(self) -> bool:
        """Alle Eingaben gültig; übernimmt dabei auch nicht bestätigte freie Tage."""
        ok = all([self._days_changed(self.extra), self._days_changed(self.vacation)])
        return ok and not any(e.get("error") for e in self.rows)


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, app: "ZeitApp") -> None:
        super().__init__()
        self.app = app
        self.cfg = app.config
        self.set_title("Einstellungen")
        self.set_search_enabled(False)

        # ---------------------------------------------------------- Allgemein
        p1 = Adw.PreferencesPage(title="Allgemein", icon_name="preferences-system-symbolic")
        p1.set_name("general")
        g = Adw.PreferencesGroup(title="Person und Arbeitsweise")
        self.name = Adw.EntryRow(title="Mitarbeitername (für den Export)")
        self.name.set_text(self.cfg.general.employee_name)
        self.name.set_show_apply_button(True)
        self.name.connect("apply", lambda *_: self._set_name())
        limit_length(self.name, MAX_FIELD_LEN)
        a11y(self.name, "Mitarbeitername", False)
        g.add(self.name)
        self.loc = Adw.ComboRow(title="Standardort",
                                model=Gtk.StringList.new([l for _k, l in LOCATION_CHOICES]))
        self.loc.set_selected(0 if self.cfg.general.default_location == "remote" else 1)
        self.loc.connect("notify::selected", lambda *_: self._set_location())
        a11y(self.loc, "Standardort", False)
        g.add(self.loc)
        self.rounding = Adw.ComboRow(
            title="Rundung beim Export",
            subtitle="Je Eintrag aufrunden; gespeicherte Zeiten bleiben exakt",
            model=Gtk.StringList.new(
                ["keine" if m == 0 else f"auf {m} Minuten" for m in ROUNDING_CHOICES]))
        cur = self.cfg.export.rounding_minutes
        self.rounding.set_selected(ROUNDING_CHOICES.index(cur) if cur in ROUNDING_CHOICES else 4)
        self.rounding.connect("notify::selected", lambda *_: self._set_rounding())
        a11y(self.rounding, "Rundung beim Export", False)
        g.add(self.rounding)
        p1.add(g)
        gl = Adw.PreferencesGroup(title="Darstellung")
        self.scheme = Adw.ComboRow(
            title="Erscheinungsbild",
            subtitle="Hell oder dunkel – oder wie im System eingestellt",
            model=Gtk.StringList.new([l for _k, l in COLOR_SCHEME_CHOICES]))
        keys = [k for k, _l in COLOR_SCHEME_CHOICES]
        cur_scheme = self.cfg.general.color_scheme
        self.scheme.set_selected(keys.index(cur_scheme) if cur_scheme in keys else 0)
        self.scheme.connect("notify::selected", lambda *_: self._set_scheme())
        a11y(self.scheme, "Erscheinungsbild", False)
        gl.add(self.scheme)
        p1.add(gl)
        self.add(p1)

        # ---------------------------------------------------------- Arbeitszeit
        pw = Adw.PreferencesPage(title="Arbeitszeit", icon_name="x-office-calendar-symbolic")
        pw.set_name("worktime")
        self.worktime = WorktimeGroup(self.cfg, self._worktime_changed)
        pw.add(self.worktime)
        self.add(pw)

        # ---------------------------------------------------------- Sprache
        p2 = Adw.PreferencesPage(title="Sprache", icon_name="audio-input-microphone-symbolic")
        p2.set_name("voice")
        gm = Adw.PreferencesGroup(title="Spracherkennung (offline)")
        self.model = Adw.ComboRow(title="Modellgröße", model=Gtk.StringList.new(list(MODEL_SIZES)))
        self.model.set_selected(MODEL_SIZES.index(self.cfg.whisper.model_size))
        self.model.connect("notify::selected", lambda *_: self._set_model())
        a11y(self.model, "Whisper-Modellgröße", False)
        gm.add(self.model)
        self.model_state = Adw.ActionRow(title="Modell")
        self.dl_btn = Gtk.Button(label="Herunterladen …", valign=Gtk.Align.CENTER)
        a11y(self.dl_btn, "Sprachmodell herunterladen")
        self.dl_btn.connect("clicked", lambda *_: self._download())
        self.model_state.add_suffix(self.dl_btn)
        self.check_btn = Gtk.Button(label="Prüfen", valign=Gtk.Align.CENTER, visible=False)
        self.check_btn.set_tooltip_text("Prüfsummen der Modelldatei prüfen (liest die ganze Datei)")
        a11y(self.check_btn, "Sprachmodell auf Beschädigung prüfen")
        self.check_btn.connect("clicked", lambda *_: self._verify_model())
        self.model_state.add_suffix(self.check_btn)
        self.repair_btn = Gtk.Button(label="Erneut herunterladen …", valign=Gtk.Align.CENTER,
                                     visible=False)
        a11y(self.repair_btn, "Sprachmodell erneut herunterladen und reparieren")
        self.repair_btn.connect("clicked", lambda *_: self._download(repair=True))
        self.model_state.add_suffix(self.repair_btn)
        self._hash_bad: set[str] = set()
        self._verifying = False
        gm.add(self.model_state)
        self.progress = Gtk.ProgressBar(show_text=True)
        self.progress.set_visible(False)
        self.progress.set_margin_top(8)
        gm.add(self.progress)
        self.cancel_btn = Gtk.Button(label="Download abbrechen", halign=Gtk.Align.START, visible=False)
        self.cancel_btn.set_margin_top(6)
        self.cancel_btn.connect("clicked", lambda *_: ModelDownloader.cancel_running())
        a11y(self.cancel_btn, "Modell-Download abbrechen")
        gm.add(self.cancel_btn)
        note = Gtk.Label(label=PRIVACY_DOWNLOAD, wrap=True, xalign=0)
        note.add_css_class("privacy-note")
        note.add_css_class("caption")
        note.set_margin_top(10)
        gm.add(note)
        p2.add(gm)

        gd = Adw.PreferencesGroup(title="Mikrofon")
        self.devices: list[tuple[int | str | None, str]] = [(None, "Standardgerät des Systems")]
        self.mic = Adw.ComboRow(title="Eingabegerät")
        a11y(self.mic, "Mikrofon", False)
        self._load_devices()
        self.mic.connect("notify::selected", lambda *_: self._set_device())
        gd.add(self.mic)
        if not hasattr(self.cfg, "audio"):
            gd.set_description("Die Auswahl gilt bis zum Beenden der App.")
        else:
            self.warmup = Adw.SwitchRow(
                title="Spracheingabe beim Start vorbereiten",
                subtitle="Schnellere erste Aufnahme; fragt die Audiogeräte ab, nimmt nichts auf")
            self.warmup.set_active(self.cfg.audio.warmup)
            a11y(self.warmup, "Spracheingabe beim Start vorbereiten", False)
            self.warmup.connect("notify::active", lambda *_: self._set_warmup())
            gd.add(self.warmup)
        p2.add(gd)
        self.add(p2)

        # ---------------------------------------------------------- System
        p3 = Adw.PreferencesPage(title="System", icon_name="preferences-desktop-keyboard-symbolic")
        p3.set_name("system")
        p3.add(HotkeyGroup(app, lambda: self, self._toast))
        tray = app.tray_status() if hasattr(app, "tray_status") else "ok"
        if tray != "ok":  # Panel ohne Tray-Bereich (Vanilla-GNOME) oder --no-tray
            gt = Adw.PreferencesGroup(title="Tray-Symbol")
            self.tray_row = Adw.ActionRow(
                title="Kein Tray-Symbol sichtbar",
                subtitle=GLib.markup_escape_text(
                    integration.no_tray_hint(disabled=tray == "disabled")))
            self.tray_row.set_name("tray-status")
            self.tray_row.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
            gt.add(self.tray_row)
            p3.add(gt)
        ga = Adw.PreferencesGroup(title="Autostart")
        self.autostart = Adw.SwitchRow(
            title="Bei Anmeldung im Hintergrund starten",
            subtitle=GLib.markup_escape_text(str(integration.autostart_path())))
        self.autostart.set_active(integration.autostart_enabled())
        self._autostart_guard = False
        self.autostart.connect("notify::active", lambda *_: self._toggle_autostart())
        a11y(self.autostart, "Autostart", False)
        ga.add(self.autostart)
        p3.add(ga)
        self.add(p3)

        # ---------------------------------------------------------- Daten
        self.data_page = DataPage(app, lambda: self, self._toast)
        self.data_page.set_name("data")
        self.add(self.data_page)

        self._refresh_model_state()
        self.connect("closed", lambda *_: self.data_page.flush())

    # ------------------------------------------------------------- Helfer
    def _toast(self, text: str, button: str | None = None,
               on_button: Callable[[], None] | None = None, timeout: int = 4) -> None:
        t = Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout)
        if button and on_button:
            t.set_button_label(button)
            t.connect("button-clicked", lambda *_: on_button())
        self.add_toast(t)

    def _set_scheme(self) -> None:
        self.cfg.general.color_scheme = COLOR_SCHEME_CHOICES[self.scheme.get_selected()][0]
        self._save()
        self.app.apply_color_scheme()

    def _save(self) -> None:
        try:
            self.app.save_config()
        except Exception as exc:
            self._toast(f"Speichern fehlgeschlagen: {exc}")

    def _set_name(self) -> None:
        self.cfg.general.employee_name = self.name.get_text().strip()
        self._save()
        self._toast("Name gespeichert")

    def _set_location(self) -> None:
        self.cfg.general.default_location = LOCATION_CHOICES[self.loc.get_selected()][0]
        self._save()

    def _set_warmup(self) -> None:
        self.cfg.audio.warmup = self.warmup.get_active()
        self._save()

    def _worktime_changed(self) -> None:
        self._save()
        self.app.refresh_all()

    def _set_rounding(self) -> None:
        self.cfg.export.rounding_minutes = ROUNDING_CHOICES[self.rounding.get_selected()]
        self._save()
        self.app.refresh_all()

    def _set_model(self) -> None:
        self.cfg.whisper.model_size = MODEL_SIZES[self.model.get_selected()]
        self._save()
        self.app.voice.reconfigure(self.cfg)
        self._refresh_model_state()
        self.app.refresh_voice_state()

    def _refresh_model_state(self) -> None:
        size = self.cfg.whisper.model_size
        state = model_status(self.app, size)
        if state == "ok" and size in self._hash_bad:
            state = "hash_mismatch"
        busy = ModelDownloader.running or self._verifying
        self.model.set_subtitle(model_subtitle(size))
        self.model_state.set_subtitle(MODEL_STATE_TEXT[state])
        self.dl_btn.set_visible(state == "missing" and not busy)
        self.repair_btn.set_visible(state != "missing" and not busy)
        if state in ("size_mismatch", "hash_mismatch"):
            self.repair_btn.add_css_class("suggested-action")
        else:
            self.repair_btn.remove_css_class("suggested-action")
        # Prüfsummen nur auf Anforderung (liest die ganze Datei, langsam auf schwacher Hardware)
        self.check_btn.set_visible(state == "ok" and not busy)

    def _verify_model(self) -> None:
        size = self.cfg.whisper.model_size
        self._verifying = True
        self._refresh_model_state()
        self.model_state.set_subtitle("Prüfe Modelldatei …")

        def done(ok: bool) -> bool:
            self._verifying = False
            if ok:
                self._hash_bad.discard(size)
            else:
                self._hash_bad.add(size)
            self._toast("Modelldatei ist in Ordnung." if ok else
                        "Modelldatei ist beschädigt – bitte „Erneut herunterladen“ wählen.")
            self._refresh_model_state()
            return False

        def run() -> None:
            try:
                from ..voice.models import verify  # noqa: PLC0415
                ok = verify(self.app.paths.models_dir, size)
            except Exception as exc:  # pragma: no cover - defensiv
                log_failure(log, "Modellprüfung fehlgeschlagen", exc)
                ok = False
            GLib.idle_add(done, ok)

        threading.Thread(target=run, name="model-verify", daemon=True).start()

    def _download(self, repair: bool = False) -> None:
        size = self.cfg.whisper.model_size

        def start() -> None:
            self.progress.set_visible(True)
            self.progress.set_text("Starte Download …")
            self.dl_btn.set_visible(False)
            self.repair_btn.set_visible(False)
            self.check_btn.set_visible(False)
            self.cancel_btn.set_visible(True)
            pulse = GLib.timeout_add(150, lambda: (self.progress.pulse(), True)[1])

            def done(ok: bool, msg: str) -> None:
                GLib.source_remove(pulse)
                self.progress.set_visible(False)
                self.cancel_btn.set_visible(False)
                if ok:
                    self._hash_bad.discard(size)
                self._toast(msg if ok or msg == "Download abgebrochen." else f"Download fehlgeschlagen: {msg}",
                            timeout=10 if ok and repair else 4)
                self.app.voice.reconfigure(self.cfg)
                self._refresh_model_state()
                self.app.refresh_voice_state()

            ModelDownloader(self.app, size, self.progress.set_text, done, repair=repair).start()

        (confirm_repair if repair else confirm_download)(self, size, start)

    def _load_devices(self) -> None:
        try:
            import sounddevice as sd  # noqa: PLC0415

            for idx, dev in enumerate(sd.query_devices()):
                if int(dev.get("max_input_channels", 0)) > 0:
                    self.devices.append((idx, str(dev.get("name", f"Gerät {idx}"))))
        except Exception as exc:
            log_failure(log, "Audiogeräte nicht abfragbar", exc, level=logging.INFO)
        self.mic.set_model(Gtk.StringList.new([n for _i, n in self.devices]))
        cur = self.app.voice.input_device
        for i, (dev, name) in enumerate(self.devices):
            if dev == cur or (isinstance(cur, str) and cur == name):
                self.mic.set_selected(i)
                break

    def _set_device(self) -> None:
        dev, name = self.devices[self.mic.get_selected()]
        if hasattr(self.cfg, "audio"):
            # Name statt Index speichern: Indizes ändern sich beim Umstecken
            self.cfg.audio.input_device = "" if dev is None else name
            self._save()
        self.app.voice.reconfigure(input_device=None if dev is None else name)

    def _toggle_autostart(self) -> None:
        if self._autostart_guard:
            return
        want = self.autostart.get_active()
        path = integration.autostart_path()

        def revert() -> None:
            self._autostart_guard = True
            self.autostart.set_active(not want)
            self._autostart_guard = False

        if integration.autostart_is_foreign(path):
            # fremde Datei (ohne Markierung) nie überschreiben – Nutzer prüft selbst
            self._toast(integration.FOREIGN_AUTOSTART_MSG, timeout=8)
            revert()
            return

        def apply() -> None:
            try:
                integration.set_autostart(want, path)
            except Exception as exc:
                self._toast(f"Autostart nicht geändert: {exc}")
                revert()
                return
            self._toast("Autostart eingeschaltet" if want else "Autostart ausgeschaltet")

        content = integration.autostart_content(enabled=want)
        dlg = Adw.AlertDialog(
            heading="Autostart einschalten?" if want else "Autostart ausschalten?",
            body=f"Datei: {path}\n\n" + ("Diese Datei wird angelegt:" if want else
                                         "Die Datei wird deaktiviert (Hidden=true), nicht gelöscht:"))
        lbl = Gtk.Label(label=content, xalign=0, wrap=True, selectable=True)
        lbl.add_css_class("command-preview")
        dlg.set_extra_child(lbl)
        dlg.add_response("cancel", "Abbrechen")
        dlg.add_response("ok", "Übernehmen")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, r: apply() if r == "ok" else revert())
        dlg.present(self)
