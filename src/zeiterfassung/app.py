"""Haupt-App: ``Adw.Application`` (Single-Instance, D-Bus, Hintergrundbetrieb).

- App-ID ``de.local.Zeiterfassung``; GActions start/stop/toggle/pause/voice/show/
  quit/quick-entry.
- Interface ``de.local.Zeiterfassung.Tracker`` via ``ipc.export_tracker``;
  ``StatusChanged`` enthält zusätzlich ``"voice"`` (idle/listening/transcribing).
- Fenster schließen = verstecken; Beenden nur über Menü, Tray oder ``app.quit``.
- Startet den Tray-Prozess (``python -m zeiterfassung.tray``) als Kindprozess.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import ipc  # noqa: E402
from .config import (  # noqa: E402
    Config,
    DataDirUnavailable,
    Paths,
    load_config,
    preserve_invalid_config,
    save_config,
)
from .tray import TRAY_BUS_NAME  # noqa: E402
from .ui.viewmodel import (  # noqa: E402
    command_to_dict,
    count_entries,
    describe_start_fields,
    format_clock,
    task_view_from_status,
)
from .ui.voice_bridge import VoiceBridge  # noqa: E402
from .logutil import log_failure  # noqa: E402
from .core.sanitize import MAX_FIELD_LEN  # noqa: E402

log = logging.getLogger(__name__)

RESOURCES = Path(__file__).resolve().parent / "resources"
ICON_NAME = "de.local.Zeiterfassung"
# Adresse des GitHub-Repositorys (nur als Link im Über-Dialog; die App selbst kontaktiert GitHub nie).
# Leer = Platzhalter „folgt“ im Über-Dialog (Mitwirkende), kein Link.
REPO_URL = "https://github.com/DaAutimato/tracksy"
ENV_NO_TRAY = "ZEIT_NO_TRAY"
# Fokus-Fallback (Wayland): Ist das Fenster nach present() nicht aktiv (Mutter/KWin
# verweigern den Fokus ohne Aktivierungs-Token), weist eine Benachrichtigung auf die
# offene Rückfrage hin; ein Klick darauf öffnet sie mit gültigem Token.
ATTENTION_DELAY_MS = 800
ATTENTION_NOTIFICATION_ID = "attention"
#: Neutrale Fassung für Systembenachrichtigungen, wenn eine Fehlermeldung Inhalte enthalten kann.
PUBLIC_ERROR = "Aktion nicht möglich – Details im Tracksy-Fenster"


def public_config_error(msg: str) -> str:
    """Konfigurationsfehler für eine Systembenachrichtigung: ohne eingetragene Werte.

    Meldungen zu ``worktime`` (Urlaub, eigene Feiertage, Regelzeiten) können die
    eingetragenen Tage enthalten – dann nur den Schlüssel nennen. Die übrigen
    Meldungen nennen nur Schlüssel und erlaubte Werte bzw. Zeile/Spalte.
    """
    if msg.startswith("worktime"):
        key = msg.split(":", 1)[0].strip()
        return f"{key} enthält einen ungültigen Eintrag"
    if msg.startswith("[Errno"):  # OSError: nennt sonst den Dateipfad samt Benutzernamen
        return "Einstellungsdatei nicht lesbar"
    return msg
ACTIVATION_ENV_VARS = frozenset(var for var, _key in ipc.ACTIVATION_ENV)

# Eingabegrenzen für die D-Bus-Action ``start`` (jeder Prozess des Benutzers
# kann sie auslösen): Gesamtgröße des JSON und Länge je Textfeld.
MAX_START_PAYLOAD = 8192
MAX_START_FIELD_LEN = MAX_FIELD_LEN  # zentral in core.sanitize
_START_KEYS = ("customer", "topic", "ticket", "location", "source")

ACCELS = {
    "app.start-stop": ["<Control>s"],
    "app.pause": ["<Control>p"],
    # Strg+M ist kein Accelerator: Push-to-talk braucht Drücken UND Loslassen
    # (ui/keyhold.attach_voice_hold im Haupt- und Schnellerfassungsfenster)
    "app.shortcuts": ["<Control>question"],
    "win.view-track": ["<Control>1"],
    "win.view-overview": ["<Control>2"],
    "app.preferences": ["<Control>comma"],
    "app.quit-confirm": ["<Control>q"],
    "win.hide": ["<Control>w"],
    "win.focus-form": ["<Control>l"],
}

# Sprache: Rückmeldungen beim Halten/Antippen
VOICE_BUSY_HINT = "Die letzte Aufnahme wird noch erkannt – gleich noch einmal"
VOICE_WARMING_HINT = "Spracheingabe wird vorbereitet – gleich noch einmal"
# Wiederholte Aufrufe von app.voice (z. B. Tastenwiederholung des systemweiten
# Kürzels) in kürzerem Abstand gelten als ein Druck.
VOICE_REPEAT_S = 0.35
VOICE_WARMUP_DELAY_S = 2
HOTKEY_REFRESH_S = 60  # eingerichtete Kürzel höchstens so oft neu lesen (Fenster aktiv)


class ZeitApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=ipc.APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.add_main_option("hidden", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Ohne Fenster im Hintergrund starten", None)
        self.add_main_option("no-tray", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Kein Tray-Symbol starten", None)
        self.service = None
        self.paths: Paths | None = None
        self.config: Config = Config()
        self.voice: VoiceBridge | None = None
        self.window = None
        self.quick = None
        self.stop_dialog = None
        self.gap_dialog = None
        self.palette = None  # ui.palette.PaletteLoader
        self.sleep_dialog = None
        self.sleep_watch = None
        # Ruhezustand: {"uuid", "at"} der rückwirkenden Pause, bis beantwortet
        self._sleep_question: dict[str, Any] | None = None
        self._sleep_hint = False
        self._awake = True
        self._clean_quit = False
        self._heartbeat_id: int | None = None
        self.tracker_export = None
        self.really_quitting = False
        self.hidden_hint_shown = False
        self._dbus_conn: Gio.DBusConnection | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._tray_proc: subprocess.Popen | None = None
        self._no_tray = bool(os.environ.get(ENV_NO_TRAY))
        self.tray_watch = None  # ui.tray_watch.TrayWatch
        # Offene Fokus-Benachrichtigung: (Besitzer, Fenster, Handler-ID)
        self._attention: tuple[Any, Any, int] | None = None
        self._first_run_shown = False
        self.restart_requested = False
        self._config_error = None
        self._config_backup = None
        self._retention_purged = 0
        self._dict_button = None
        self._dict_sink: Callable[[str], None] | None = None
        self._main_thread = threading.main_thread()
        # Systemweite Kürzel (integration.ActiveHotkeys; None = noch nicht gelesen)
        self.hotkeys = None
        self._hotkeys_read_at: float | None = None
        self._hotkeys_reading = False
        self.global_hold = None  # ui.keyhold.GlobalHold (Halten des systemweiten Kürzels)
        self._voice_action_t: float | None = None
        self.shortcuts_dialog = None

    # ================================================================ Lebenszyklus
    def do_dbus_register(self, connection, object_path) -> bool:
        Adw.Application.do_dbus_register(self, connection, object_path)
        self._dbus_conn = connection
        self.tracker_export = ipc.export_tracker(connection, None, object_path, auto_emit=False)
        return True

    def do_dbus_unregister(self, connection, object_path) -> None:
        if self.tracker_export is not None:
            self.tracker_export.unexport()
            self.tracker_export = None
        Adw.Application.do_dbus_unregister(self, connection, object_path)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        from .service import TrackerService  # noqa: PLC0415

        self._load_css()
        try:
            self.paths = Paths.resolve().ensure()
        except DataDirUnavailable as exc:
            # Nie still eine neue, leere Datenbank an anderer Stelle beginnen
            log_failure(log, "Speicherort nicht verfügbar", exc, level=logging.ERROR)
            print(f"Tracksy: {exc}", file=sys.stderr)
            # Benachrichtigung ohne Pfade (Verlauf/Sperrbildschirm); Details im Terminal oben
            self.notify("Tracksy: Speicherort nicht verfügbar",
                        "Der eingestellte Datenordner ist nicht erreichbar (Laufwerk nicht "
                        "eingehängt oder Ordner entfernt?). Tracksy wird beendet. Einstellung: "
                        "general.data_dir in config.toml.")
            GLib.idle_add(lambda: (self.quit(), False)[1])
            return
        try:
            self.config = load_config(self.paths.config_file)
            self._config_error = None
        except (ValueError, OSError) as exc:
            log_failure(log, "Konfiguration fehlerhaft", exc, level=logging.ERROR)
            self.config = Config()
            self._config_error = str(exc)
            # vor dem ersten Speichern sichern, sonst gehen alle Einstellungen verloren
            self._config_backup = preserve_invalid_config(self.paths.config_file)
        self.service = TrackerService.open(self.paths, self.config)
        self.config = self.service.config
        try:  # Aufbewahrungsfrist (general.retention_months, 0 = aus)
            purged = self.service.apply_retention()
            if purged is not None:
                log.info("Aufbewahrungsfrist: %d Einträge gelöscht, %d interne Sicherungen "
                         "entfernt", purged.entries, purged.backups_removed)
                self._retention_purged = purged.entries
        except Exception as exc:
            log_failure(log, "Aufbewahrungsfrist nicht angewendet", exc, level=logging.ERROR)
        try:  # Reste abgebrochener Modell-Downloads (.part) nach Absturz entfernen
            from .voice.models import cleanup_partial  # noqa: PLC0415

            cleanup_partial(self.paths.models_dir)
        except Exception as exc:
            log_failure(log, "Aufräumen unvollständiger Modell-Downloads fehlgeschlagen", exc)
        if self.tracker_export is not None:
            self.tracker_export.attach(self.service)
        self._unsubscribe = self.service.subscribe(self._on_service_event)
        self.voice = VoiceBridge(self.config, self.paths)
        self.voice.connect("state-changed", self._on_voice_state)
        self.voice.connect("level", self._on_voice_level)
        self.voice.connect("result", self._on_voice_result)
        self.voice.connect("error", self._on_voice_error)
        from .ui.keyhold import GlobalHold  # noqa: PLC0415

        self.global_hold = GlobalHold(self._global_hold_released, schedule=GLib.timeout_add,
                                      unschedule=GLib.source_remove)
        self._install_actions()
        self.apply_color_scheme()
        self.hold()
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._on_sigterm)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._on_sigterm)
        self._start_heartbeat()
        self._start_sleep_watch()
        self._start_tray_watch()
        self.refresh_hotkeys()
        # Sprach-Module und PortAudio im Hintergrund laden, damit die erste Aufnahme
        # nicht den Main-Loop blockiert (Mikrofon bleibt zu, Modell wird nicht geladen)
        if self.config.audio.warmup:  # Einstellungen → Sprache; ZEIT_VOICE_WARMUP=0 hat Vorrang
            GLib.timeout_add_seconds(VOICE_WARMUP_DELAY_S,
                                     lambda: (self.voice.warm_up(), False)[1])
        GLib.idle_add(self._after_startup)

    def do_command_line(self, cmdline: Gio.ApplicationCommandLine) -> int:
        if self.service is None:  # Start abgebrochen (z. B. Speicherort fehlt)
            return 1
        opts = cmdline.get_options_dict().end().unpack()
        if opts.get("no-tray"):
            self._no_tray = True
        if not opts.get("hidden"):
            self.activate()
        GLib.idle_add(self._ensure_tray)
        return 0

    def do_activate(self) -> None:
        self.present_main()

    def do_shutdown(self) -> None:
        self._stop_heartbeat()
        if self.sleep_watch is not None:
            self.sleep_watch.stop()
        if self.tray_watch is not None:
            self.tray_watch.close()
            self.tray_watch = None
        self._stop_tray()
        if self.global_hold is not None:
            self.global_hold.close()
        if self.voice is not None:
            self.voice.shutdown()
        if self._unsubscribe:
            self._unsubscribe()
        if self.service is not None:
            if not self.service.closed:
                if self._clean_quit:  # bewusst beendet: keine Ausfallzeit-Frage beim Start
                    self.service.mark_clean_shutdown()
                else:  # SIGTERM (Abmelden/Herunterfahren): genaues letztes Lebenszeichen
                    self.service.heartbeat()
            self.service.close()
        Adw.Application.do_shutdown(self)

    def _after_startup(self) -> bool:
        st = self.service.status()
        self._emit_status(st)
        if self._config_error:
            saved = (f" Die alte Datei wurde gesichert: {self._config_backup}"
                     if self._config_backup else
                     " Die alte Datei konnte nicht gesichert werden – sie wird beim nächsten "
                     "Speichern der Einstellungen überschrieben.")
            reason = public_config_error(self._config_error)
            self.notify("Einstellungen fehlerhaft",
                        f"Standardwerte werden verwendet: {reason}.{saved}")
        if self._retention_purged:
            self.notify("Aufbewahrungsfrist angewendet",
                        f"{count_entries(self._retention_purged, 'alte')} endgültig gelöscht "
                        "(Einstellungen → Daten).")
        if st["state"] == "stopping":
            self.show_stop_dialog()
        elif st.get("recovered"):
            from .ui.gap_dialog import ASK, KEEP, gap_action  # noqa: PLC0415

            action = gap_action(st)
            if action == ASK:  # Ausfall > 30 min: Rückfrage statt stillem Mitzählen
                self.show_gap_dialog(st)
                return False
            if action == KEEP:  # kurze Lücke zählt wie bisher; Status bereinigen
                self.service.keep_gap(automatic=True)
            v = task_view_from_status(st)
            self.toast(f"Task fortgesetzt: {v.title} ({format_clock(v.elapsed())})", high=False,
                       public="Task fortgesetzt")
        return False

    # ================================================================ Lebenszeichen
    def _start_heartbeat(self) -> None:
        from .service import HEARTBEAT_INTERVAL_SECONDS  # noqa: PLC0415

        if self._heartbeat_id is None and self.service is not None:
            self._heartbeat_id = GLib.timeout_add_seconds(HEARTBEAT_INTERVAL_SECONDS,
                                                          self._heartbeat_tick)

    def _heartbeat_tick(self) -> bool:
        """Timer-Rückruf: Lebenszeichen schreiben (der Service schreibt nur bei
        laufendem Task). Endet, wenn der Service geschlossen ist (Umzug, Beenden)."""
        svc = self.service
        if svc is None or svc.closed:
            self._heartbeat_id = None
            return False
        if self.sleep_watch is not None:
            self.sleep_watch.check_clock_jump()  # Ruhezustand ohne logind
        svc.heartbeat()
        return True

    def _stop_heartbeat(self) -> None:
        if self._heartbeat_id is not None:
            GLib.source_remove(self._heartbeat_id)
            self._heartbeat_id = None

    # ================================================================ Ausfallzeit
    def show_gap_dialog(self, status: dict[str, Any]) -> None:
        """Rückfrage „Ausfallzeit abziehen?“ – öffnet das Fenster auch bei ``--hidden``
        (Autostart), weil sonst Stunden still als Arbeitszeit zählen."""
        from .ui.gap_dialog import RESP_SUBTRACT, build_gap_dialog, result_toast  # noqa: PLC0415

        if self.gap_dialog is not None:
            return
        seconds = int(status.get("recovered_gap_seconds") or 0)
        holder: list = []

        def on_response(resp: str) -> None:
            if not holder or self.gap_dialog is not holder[0]:
                return  # von _sync_gap_dialog geschlossen: nichts entscheiden
            self.gap_dialog = None  # vor dem Service-Aufruf: dessen Ereignis schließt sonst
            try:
                if resp == RESP_SUBTRACT:
                    self.service.subtract_gap()
                else:
                    self.service.keep_gap()
            except Exception as exc:  # Task inzwischen beendet o. Ä.
                log_failure(log, "Ausfallzeit nicht übernommen", exc)
                self.toast(f"Ausfallzeit nicht geändert: {exc}", public=PUBLIC_ERROR)
            else:
                self.toast(result_toast(resp, seconds))
            GLib.idle_add(lambda: (self._next_prompt(), False)[1])

        self.present_main()
        self.gap_dialog = build_gap_dialog(status, on_response)
        holder.append(self.gap_dialog)
        self.gap_dialog.present(self.window)
        dlg = self.gap_dialog
        self.ensure_attention(self.window, "Tracksy: Ausfallzeit prüfen",
                              "Soll die Zeit seit dem letzten Lebenszeichen abgezogen werden?",
                              still_needed=lambda: self.gap_dialog is dlg, owner=dlg)

    def _sync_gap_dialog(self, status: dict[str, Any]) -> None:
        """Gibt es keine offene Ausfallzeit mehr (Task anderweitig beendet), die
        Rückfrage schließen – sie hätte nichts mehr zu entscheiden."""
        dlg = self.gap_dialog
        if dlg is not None and not status.get("recovered_gap_seconds"):
            self.gap_dialog = None
            withdraw_attention(self, dlg)  # Frage ohne Fensterfokus erledigt
            dlg.force_close()

    # ================================================================ Ruhezustand
    def _start_sleep_watch(self) -> None:
        from .sleepwatch import SleepWatcher  # noqa: PLC0415

        if self.service is None:
            return
        self.sleep_watch = SleepWatcher(self._on_system_sleep, self._on_system_ready)
        if not self.sleep_watch.start():
            log.info("Ruhezustand ohne logind: Erkennung per Zeitsprung")

    def _on_system_sleep(self, at) -> None:
        """System schläft ein (bzw. hat geschlafen): Aufnahme verwerfen, laufenden
        Task rückwirkend pausieren. Läuft vor dem Freigeben des Delay-Inhibitors."""
        self._awake = False
        if self.voice is not None and self.voice.state != "idle":
            self.voice.cancel()  # Aufnahme abbrechen, Audio wird verworfen
        svc = self.service
        if svc is None or svc.closed:
            return
        state = str(svc.state)
        if state == "running":
            if svc.pause_at(at, reason="sleep"):
                e = svc.active_entry()
                op = e.open_pause if e is not None else None
                if e is not None and op is not None:
                    self._sleep_question = {"uuid": e.uuid, "at": op.start_at}
                    self._sleep_hint = False
        elif state == "paused" and self._sleep_question is None:
            self._sleep_hint = True

    def _on_system_ready(self) -> None:
        """Nach dem Aufwachen und Entsperren: offene Frage stellen."""
        self._awake = True
        self._next_prompt()

    def _sleep_question_valid(self) -> bool:
        q = self._sleep_question
        if q is None or self.service is None:
            return False
        e = self.service.active_entry()
        op = e.open_pause if e is not None else None
        return (e is not None and e.uuid == q["uuid"] and str(e.status) == "paused"
                and op is not None and op.start_at == q["at"])

    def _next_prompt(self) -> None:
        """Höchstens eine Rückfrage zugleich: Ausfallzeit → Stopp-Dialog → Ruhezustand."""
        if not self._awake or self.service is None:
            return
        if self.gap_dialog is not None or self.stop_dialog is not None \
                or self.sleep_dialog is not None:
            return  # wird beim Schließen erneut aufgerufen
        if self._sleep_question is not None:
            if self._sleep_question_valid():
                self.show_sleep_dialog()
                return
            self._sleep_question = None  # inzwischen fortgesetzt/gestoppt
        if self._sleep_hint:
            from .ui.sleep_dialog import SLEEP_HINT  # noqa: PLC0415

            self._sleep_hint = False
            if str(self.service.state) == "paused":
                self.toast(SLEEP_HINT, high=False)

    def show_sleep_dialog(self) -> None:
        from datetime import datetime  # noqa: PLC0415

        from .ui.sleep_dialog import (  # noqa: PLC0415
            RESP_END, RESP_RESUME, build_sleep_dialog, result_toast as sleep_toast)

        q = self._sleep_question
        if q is None or self.sleep_dialog is not None:
            return
        holder: list = []

        def on_response(resp: str) -> None:
            if not holder or self.sleep_dialog is not holder[0]:
                return  # von _sync_sleep_dialog geschlossen
            self.sleep_dialog = None
            self._sleep_question = None
            try:
                if resp == RESP_RESUME:
                    self.service.resume()
                elif resp == RESP_END:
                    self.service.request_stop_at_pause_start()  # Ereignis öffnet Ergänzung
            except Exception as exc:
                log_failure(log, "Antwort nach Ruhezustand nicht ausgeführt", exc)
                self.toast(f"Nicht möglich: {exc}", public=PUBLIC_ERROR)
                return
            text = sleep_toast(resp)
            if text:
                self.toast(text, high=False)

        self.present_main()
        self.sleep_dialog = build_sleep_dialog(self.service.status(), q["at"],
                                               datetime.now().astimezone(), on_response)
        holder.append(self.sleep_dialog)
        self.sleep_dialog.present(self.window)
        dlg = self.sleep_dialog
        self.ensure_attention(self.window, "Tracksy: Nach dem Ruhezustand",
                              "Der Task wurde pausiert. Fortsetzen oder beenden?",
                              still_needed=lambda: self.sleep_dialog is dlg, owner=dlg)

    def _sync_sleep_dialog(self) -> None:
        """Task inzwischen fortgesetzt/gestoppt (Tray, Hotkey, CLI): Frage schließen."""
        dlg = self.sleep_dialog
        if dlg is not None and not self._sleep_question_valid():
            self.sleep_dialog = None
            self._sleep_question = None
            withdraw_attention(self, dlg)
            dlg.force_close()

    def _on_sigterm(self) -> bool:
        # Abmelden/Herunterfahren/kill: kein bewusstes Beenden – lief ein Task,
        # fragt der nächste Start ab 30 min nach der Ausfallzeit
        self.really_quit(notify_running=False, clean=False)
        return False

    def _load_css(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(RESOURCES / "style.css"))
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # Feste Tracksy-Palette (Akzent Mint-Aqua + Zustandsfarben) auf allen
        # Arbeitsumgebungen, je Hell/Dunkel getauscht (ui/palette.py)
        from .ui.palette import PaletteLoader  # noqa: PLC0415

        self.palette = PaletteLoader(display)
        Gtk.IconTheme.get_for_display(display).add_search_path(str(RESOURCES / "icons"))
        Gtk.Window.set_default_icon_name(ICON_NAME)

    # ================================================================ Tray
    def _ensure_tray(self) -> bool:
        if self._tray_proc is not None and self._tray_proc.poll() is not None:
            self._tray_proc = None  # Tray ist beendet/abgestürzt: einsammeln, neu startbar
        if self._no_tray or self._tray_proc is not None or self.really_quitting:
            return False
        watch = getattr(self, "tray_watch", None)  # Attrappen in Tests ohne Watch
        known = watch.tray_running if watch is not None else None
        # Stand aus dem Bus-Watch; nur solange er noch unbekannt ist, fragen
        if known or (known is None and ipc.is_running(TRAY_BUS_NAME)):
            return False
        env = child_env()
        try:
            self._tray_proc = subprocess.Popen(
                [sys.executable, "-m", "zeiterfassung.tray"],
                stdin=subprocess.DEVNULL, env=env, close_fds=True)
        except OSError as exc:
            log_failure(log, "Tray konnte nicht gestartet werden", exc)
        return False

    def _start_tray_watch(self) -> None:
        """Tray-Prozess und Tray-Bereich des Panels per Bus-Ereignis beobachten,
        damit ``tray_status`` nie synchron fragen muss."""
        from .ui.tray_watch import TrayWatch  # noqa: PLC0415

        conn = self.get_dbus_connection()
        if conn is None:
            return
        try:
            self.tray_watch = TrayWatch(conn, TRAY_BUS_NAME)
        except GLib.Error as exc:
            log_failure(log, "Tray-Beobachtung nicht gestartet", exc)

    def tray_status(self) -> str:
        """``"ok"`` (Tray-Symbol sichtbar), ``"disabled"`` (``--no-tray``) oder
        ``"missing"`` (Tray läuft nicht bzw. Panel ohne Tray-Bereich, z. B.
        Vanilla-GNOME ohne AppIndicator-Erweiterung).

        Blockiert nie: liest den zwischengespeicherten Stand von ``TrayWatch``.
        Ohne Beobachtung (kein Bus) gilt „ok“ – kein falscher Hinweis."""
        from .ui.tray_watch import tray_state  # noqa: PLC0415

        proc = self._tray_proc
        alive = proc is not None and proc.poll() is None
        watch = getattr(self, "tray_watch", None)  # Attrappen in Tests ohne Watch
        return tray_state(self._no_tray, alive,
                          watch.tray_running if watch is not None else None,
                          watch.host_available if watch is not None else None)

    def _stop_tray(self) -> None:
        proc, self._tray_proc = self._tray_proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    # ================================================================ Actions
    def _install_actions(self) -> None:
        def add(name: str, cb: Callable[..., Any], ptype: str | None = None) -> None:
            act = Gio.SimpleAction.new(name, GLib.VariantType.new(ptype) if ptype else None)
            act.connect("activate", lambda _a, p: self._guard(cb, p))
            self.add_action(act)

        add("start", self._act_start, "s")
        add("stop", lambda _p: self.stop_task())
        add("toggle", lambda _p: self._act_toggle())
        add("pause", lambda _p: self._act_pause())
        add("voice", lambda _p: self.voice_action())
        add("show", lambda _p: self.present_main())
        add("quit", lambda _p: self.really_quit(notify_running=True))
        add("quick-entry", lambda _p: self.show_quick_entry())
        add("quit-confirm", lambda _p: self._quit_confirm())
        add("start-stop", lambda _p: self._act_start_stop())
        add("preferences", lambda _p: self.show_preferences())
        add("shortcuts", lambda _p: self._show_shortcuts())
        add("about", lambda _p: self._show_about())
        add("first-run", lambda _p: self.show_first_run())
        add("releases", lambda _p: self._show_releases())
        self.update_first_run_action()
        for action, accels in ACCELS.items():
            self.set_accels_for_action(action, accels)

    def _guard(self, cb: Callable[..., Any], param) -> None:
        try:
            cb(param.unpack() if param is not None else None)
        except Exception as exc:  # Aktionen dürfen die App nie beenden
            log_failure(log, "Aktion fehlgeschlagen", exc, level=logging.ERROR)
            self.toast(str(exc) or type(exc).__name__, public=PUBLIC_ERROR)

    def _act_start(self, payload: str | None) -> None:
        fields: dict[str, Any] = {}
        if payload:
            if len(payload) > MAX_START_PAYLOAD:
                log.warning("start-Action abgelehnt: Parameter zu groß (%d Zeichen)", len(payload))
                self.toast("Start abgelehnt: Eingabe zu lang")
                return
            try:
                data = json.loads(payload)
                if isinstance(data, dict):
                    # nur Text-Felder übernehmen (keine Listen/Objekte/Zahlen)
                    fields = {k: data[k] for k in _START_KEYS
                              if isinstance(data.get(k), str) and data[k] != ""}
            except (ValueError, TypeError, RecursionError):
                fields = {}
            if any(len(v) > MAX_START_FIELD_LEN for v in fields.values()):
                log.warning("start-Action abgelehnt: Feld länger als %d Zeichen", MAX_START_FIELD_LEN)
                self.toast("Start abgelehnt: Eingabe zu lang")
                return
        if not any(fields.get(k) for k in ("customer", "topic", "ticket")):
            if self.window is not None and self.window.get_visible():
                self.present_main()
                self.window.focus_form()
            else:
                self.show_quick_entry()
            return
        source = str(fields.pop("source", "cli") or "cli")
        self.start_task(fields, source=source)

    def _act_toggle(self) -> None:
        r = self.service.toggle()
        if r == "idle":
            self.show_quick_entry()
        elif r == "stopping":
            self.show_stop_dialog()

    def _act_pause(self) -> None:
        st = self.service.state
        if st in ("running", "paused"):
            r = self.service.toggle_pause()
            self.toast("Pausiert" if r == "paused" else "Weiter geht’s")
        elif st == "stopping":
            self.show_stop_dialog()
        else:
            self.toast("Kein Task aktiv")

    def _act_start_stop(self) -> None:
        st = self.service.state
        if st in ("running", "paused"):
            self.stop_task()
        elif st == "stopping":
            self.show_stop_dialog()
        elif self.window is not None and self.window.is_active():
            self.window.form.submit()
        else:
            self.show_quick_entry()

    # ================================================================ Task-Logik
    def start_task(self, values: dict[str, Any], source: str = "gui",
                   parent: Gtk.Widget | None = None) -> bool:
        customer = (values.get("customer") or "").strip()
        if not customer:
            self.ask_customer(values, source, parent)
            return False
        try:
            res = self.service.start(customer, values.get("topic"), values.get("ticket"),
                                     values.get("location"), source)
        except ValueError as exc:
            self.toast(f"Start nicht möglich: {exc}", public=PUBLIC_ERROR)
            return False
        if res == "pending":
            self.toast("Der neue Task startet nach dem Abschließen des laufenden.")
        return True

    def ask_customer(self, values: dict[str, Any], source: str,
                     parent: Gtk.Widget | None = None, on_started: Callable[[], None] | None = None,
                     heard: str | None = None) -> None:
        """Kunde nachfragen. ``heard`` = Transkript bei Sprachstart (nur Anzeige, kein Log),
        damit sichtbar ist, warum der Kunde fehlt (z. B. „Kunde“ nicht verstanden)."""
        from .ui.widgets import a11y  # noqa: PLC0415

        from .core.sanitize import strip_control  # noqa: PLC0415

        rest = describe_start_fields(None, values.get("topic"), values.get("ticket"), None)
        rest = rest.replace("Ohne Kunde", "").strip(", ")
        # Steuer- und Unicode-Formatzeichen (z. B. Richtungswechsel U+202E) nicht anzeigen
        heard = "".join(ch for ch in strip_control(heard or "")
                        if unicodedata.category(ch) != "Cf")
        heard = " ".join(heard.split())
        if len(heard) > 200:
            heard = heard[:199] + "…"
        suggested = bool((values.get("customer") or "").strip())
        dlg = Adw.AlertDialog(heading="Für welchen Kunden?",
                              body=(f"Gehört: „{heard}“\n" if heard else "")
                              + (f"Erkannt: {rest}. " if rest else "")
                              + ("Der Kunde ist noch unbekannt – bitte Schreibweise prüfen und "
                                 "mit „Starten“ bestätigen." if suggested else
                                 "Bitte gib den Kunden an, damit der Task zugeordnet werden kann."))
        entry = Gtk.Entry(placeholder_text="Kunde", activates_default=True,
                          max_length=MAX_FIELD_LEN)
        entry.set_text((values.get("customer") or "").strip())  # Vorschlag aus der Sprache
        a11y(entry, "Kunde", False)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Abbrechen")
        dlg.add_response("start", "Starten")
        dlg.set_response_appearance("start", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("start")
        dlg.set_close_response("cancel")
        dlg.set_response_enabled("start", bool(entry.get_text().strip()))
        entry.connect("changed", lambda e: dlg.set_response_enabled(
            "start", bool(e.get_text().strip())))

        def on_resp(_d, resp: str) -> None:
            if resp != "start":
                return
            v = dict(values)
            v["customer"] = entry.get_text().strip()
            if self.start_task(v, source) and on_started:
                on_started()

        dlg.connect("response", on_resp)
        if parent is None:
            self.present_main()
            parent = self.window
        dlg.present(parent)
        GLib.idle_add(lambda: (entry.grab_focus(), False)[1])

    def stop_task(self) -> None:
        st = self.service.state
        if st == "idle":
            self.toast("Kein Task aktiv")
            return
        if st == "stopping":
            self.show_stop_dialog()
            return
        self.service.request_stop()  # Ereignis öffnet den Dialog

    def show_stop_dialog(self) -> None:
        from .ui.stop_dialog import StopDialog  # noqa: PLC0415

        entry = self.service.active_entry()
        if entry is None or str(entry.status) != "stopping":
            return
        self.present_main()
        if self.stop_dialog is not None and self.stop_dialog.entry_uuid == entry.uuid:
            return
        self.stop_dialog = StopDialog(self, entry)
        self.stop_dialog.present(self.window)
        dlg = self.stop_dialog
        self.ensure_attention(self.window, "Tracksy: Task gestoppt",
                              "Ergänzung eintragen und speichern – die Zeit ist angehalten.",
                              still_needed=lambda: self.stop_dialog is dlg, owner=dlg)

    def stop_dialog_closed(self, dlg) -> None:
        if self.stop_dialog is dlg:
            self.stop_dialog = None
            withdraw_attention(self, dlg)
            if self.voice.mode == "dictation" and self.voice.state == "listening":
                self.voice.cancel()
            self._dict_button = None
            self._dict_sink = None
            # Nach dem Schließen gibt GTK den Fokus an das zuletzt fokussierte Widget
            # des Hauptfensters zurück – oft der Mikrofon-Button. Enter/Leertaste
            # (z. B. Tastenwiederholung von Strg+Enter = Speichern) starteten dann
            # unbemerkt eine Aufnahme → „Nichts erkannt“. Fokus deshalb lösen.
            win = self.window
            if win is not None:
                GLib.idle_add(lambda: (self._release_mic_focus(win), False)[1])
            GLib.idle_add(lambda: (self._next_prompt(), False)[1])

    @staticmethod
    def _release_mic_focus(win) -> None:
        mic = getattr(win, "mic", None)
        focus = win.get_focus()
        if mic is not None and focus is not None and (focus is mic or focus.is_ancestor(mic)):
            win.set_focus(None)

    def edit_active(self) -> None:
        from .ui.entry_editor import EntryEditor  # noqa: PLC0415

        entry = self.service.active_entry()
        if entry is None or str(entry.status) == "stopping":
            return
        self.present_main()
        EntryEditor(self, entry=entry, active=True).present(self.window)

    def customers(self) -> list[str]:
        try:
            return self.service.customers()
        except Exception:  # pragma: no cover
            return []

    def save_config(self) -> None:
        save_config(self.config, self.paths.config_file)
        self.service.config = self.config

    # ================================================================ Speicherort
    def relocate_data(self, target: Path):
        """Daten nach ``target`` umziehen (Einstellungen → Daten → Speicherort).

        Die Konfiguration (``general.data_dir``) bleibt am Standardort und wird erst
        nach erfolgreicher Prüfung der Kopie geschrieben. Ziel = Standardort →
        Eintrag wird geleert. Danach ist die Datenbank geschlossen → :meth:`restart`.
        """
        from .storage.relocate import RelocateError  # noqa: PLC0415

        if self.paths.data_source == "env":
            raise RelocateError("Der Speicherort ist über ZEIT_DATA_DIR festgelegt.")
        default = self.paths.default_data_dir

        def commit(dest: Path) -> None:
            old = self.config.general.data_dir
            same = default is not None and dest == Path(os.path.realpath(default))
            self.config.general.data_dir = "" if same else str(dest)
            try:
                save_config(self.config, self.paths.config_file)
            except Exception:
                self.config.general.data_dir = old
                raise

        res = self.service.relocate_data(target, commit, default_dir=default)
        log.info("Speicherort geändert (%d Dateien übernommen)", len(res.copied))
        return res

    def restart(self) -> None:
        """App beenden und neu starten (nach dem Umzug der Daten)."""
        self.restart_requested = True
        self.really_quit(notify_running=False)

    # ================================================================ Service-Ereignisse
    def _on_service_event(self, event: str, status: dict[str, Any]) -> None:
        if threading.current_thread() is self._main_thread:
            self._handle_event(event, status)
        else:
            GLib.idle_add(lambda: (self._handle_event(event, status), False)[1])

    def _handle_event(self, event: str, status: dict[str, Any]) -> None:
        status = dict(status)
        status["event"] = event
        self._emit_status(status)
        if self.window is not None:
            self.window.update_status(status)
        self._sync_gap_dialog(status)
        self._sync_sleep_dialog()
        if event == "stop_requested":
            self.show_stop_dialog()
        elif event in ("stop_cancelled", "stopped", "discarded"):
            if self.stop_dialog is not None:
                self.stop_dialog.refresh_from_service()
            if event == "stopped":
                last = status.get("last_stopped") or {}
                self.toast(f"Gespeichert: {last.get('customer') or 'Task'}",
                           public="Task gespeichert")
            elif event == "stop_cancelled":
                self.toast("Task läuft weiter")
        elif event == "started" and self.quick is not None:
            self.quick.close()

    def _emit_status(self, status: dict[str, Any] | None = None) -> None:
        st = dict(status or self.service.status())
        st["voice"] = self.voice.state if self.voice else "idle"
        if self.hotkeys is not None:  # Tray: eingerichtete Kürzel im Menü anzeigen
            st["hotkeys"] = self.hotkeys.as_status()
        if self._dbus_conn is not None:
            try:
                ipc.emit_status_changed(self._dbus_conn, st)
            except Exception:  # pragma: no cover
                log_failure(log, "StatusChanged fehlgeschlagen")

    def first_run_available(self) -> bool:
        """Einrichtung erneut anbieten: nur solange sie nicht abgeschlossen ist und
        noch kein Whisper-Modell heruntergeladen wurde."""
        if self.config.general.first_run_done:
            return False
        try:
            from .voice.models import installed_sizes  # noqa: PLC0415

            return not installed_sizes(self.paths.models_dir)
        except Exception as exc:  # pragma: no cover
            log_failure(log, "Modellprüfung fehlgeschlagen", exc)
            return True

    def update_first_run_action(self) -> None:
        act = self.lookup_action("first-run")
        if act is not None:
            act.set_enabled(self.first_run_available())

    def show_first_run(self) -> None:
        """Einrichtungsassistent (erneut) öffnen – Menü „Einrichtung starten“."""
        if not self.first_run_available():
            return
        from .ui.first_run import FirstRunDialog  # noqa: PLC0415

        self.present_main()
        if self.window.get_visible_dialog() is None:
            self._first_run_shown = True
            FirstRunDialog(self).present(self.window)

    def refresh_all(self) -> None:
        self.update_first_run_action()
        st = self.service.status()
        if self.window is not None:
            self.window.update_status(st)
            if self.window.stack.get_visible_child_name() == "overview":
                self.window.overview.refresh()

    # ================================================================ Sprache
    # Drei Wege: Maus (MicButton: halten oder klicken), Strg+M im Fenster (nur halten,
    # ui/keyhold.HoldKey) und app.voice (systemweites Kürzel/CLI/Tray: unter X11
    # halten, sonst umschalten – ui/keyhold.GlobalHold).
    def _voice_toast(self, text: str, public: str | None = None) -> None:
        if self.stop_dialog is not None:
            self.stop_dialog.toast(text)
        else:
            self.toast(text, public=public)

    def _voice_blocked(self) -> bool:
        """Aufnahme gerade nicht möglich → sichtbarer Hinweis statt stillem Ignorieren."""
        if self.voice.state == "transcribing":
            self._voice_toast(VOICE_BUSY_HINT)
            return True
        if self.voice.warming:
            self._voice_toast(VOICE_WARMING_HINT)
            return True
        return False

    def _begin_voice(self) -> bool:
        """Aufnahme starten: im Ergänzungsdialog Diktat ins Feld „Ergänzung“, sonst Befehl."""
        btn = self.stop_dialog.addendum.dictation_button if self.stop_dialog is not None else None
        if btn is not None:
            self._dict_button, self._dict_sink = btn, self.stop_dialog.addendum.append_text
            if self.voice.begin("dictation"):
                return True
            self._dict_button = self._dict_sink = None
            return False
        return self.voice.begin("command")

    def voice_key_begin(self) -> bool:
        """Strg+M gedrückt (Fenster). ``True`` = Aufnahme läuft bis zum Loslassen."""
        if self.global_hold is not None:
            self.global_hold.stop()
        if self.voice.state == "listening":  # per Klick/Tray gestartet → Strg+M beendet
            self.voice.end()
            return False
        if self._voice_blocked():
            return False
        return self._begin_voice()

    def voice_key_end(self, short: bool) -> None:
        """Strg+M losgelassen. Kurz angetippt → Aufnahme verwerfen und erklären."""
        if self.voice.state != "listening":
            return
        if not short:
            self.voice.end()
            return
        from .ui.keyhold import TAP_HINT  # noqa: PLC0415

        self.voice.cancel()  # Audio wird verworfen, keine Erkennung
        if self._dict_button is not None:
            self._dict_button.set_recording(False)
        self._dict_button = self._dict_sink = None
        self._voice_toast(TAP_HINT)

    def voice_action(self) -> None:
        """``app.voice``: systemweites Kürzel (``zeit voice``), CLI, Tray-Menü.

        Unter X11 mit eingerichtetem Kürzel: solange die Tasten gehalten werden,
        läuft die Aufnahme (``GlobalHold``); sonst Umschalten (nächster Aufruf beendet)."""
        now = time.monotonic()
        last, self._voice_action_t = self._voice_action_t, now
        if self.global_hold is not None and self.global_hold.active:
            return  # Kürzel wird noch gehalten: Tastenwiederholung
        if last is not None and now - last < VOICE_REPEAT_S:
            return
        if self.voice.state == "listening":
            self.voice.end()
            return
        if self._voice_blocked():
            return
        accel = self.hotkeys.voice if self.hotkeys is not None else None
        mode = self.global_hold.start(accel) if self.global_hold is not None else "unsupported"
        log.debug("Sprach-Kürzel: %s", mode)
        if not self._begin_voice() and self.global_hold is not None:
            self.global_hold.stop()

    def _global_hold_released(self, short: bool) -> None:
        """Tasten des systemweiten Kürzels losgelassen. Kurz gedrückt → Umschalten
        (Aufnahme läuft weiter bis zum nächsten Druck, wie ein Klick auf das Mikrofon)."""
        if not short and self.voice.state == "listening":
            self.voice.end()

    def hold_supported(self) -> bool:
        """Halten des systemweiten Kürzels möglich (X11)? Unter Wayland: Umschalten."""
        from .ui.keyhold import can_watch_keys  # noqa: PLC0415

        return can_watch_keys()

    # ---------------------------------------------------------- Systemweite Kürzel
    def refresh_hotkeys(self, force: bool = True) -> None:
        """Eingerichtete systemweite Kürzel im Hintergrund lesen (gsettings, nur lesend)."""
        if self._hotkeys_reading:
            return
        now = time.monotonic()
        if (not force and self._hotkeys_read_at is not None
                and now - self._hotkeys_read_at < HOTKEY_REFRESH_S):
            return
        self._hotkeys_reading = True
        self._hotkeys_read_at = now

        def work() -> None:
            from .ui.integration import ActiveHotkeys, read_active_hotkeys  # noqa: PLC0415

            try:
                hk = read_active_hotkeys()
            except Exception as exc:  # noqa: BLE001 – nur Anzeige
                log_failure(log, "Tastenkürzel nicht lesbar", exc, level=logging.INFO)
                hk = ActiveHotkeys(False)
            GLib.idle_add(lambda: (self.set_hotkeys(hk), False)[1])

        threading.Thread(target=work, name="hotkey-read", daemon=True).start()

    def set_hotkeys(self, hk) -> None:
        self._hotkeys_reading = False
        changed = hk != self.hotkeys
        self.hotkeys = hk
        hold = self.hold_supported()
        if self.window is not None:
            self.window.set_hotkeys(hk, hold)
        if self.shortcuts_dialog is not None:
            self.shortcuts_dialog.set_hotkeys(hk)
        if changed and self.service is not None and not self.service.closed:
            self._emit_status()

    def toggle_dictation(self, button, sink: Callable[[str], None]) -> None:
        if self.voice.state == "listening":
            self.voice.end()
            return
        if self.voice.state != "idle":
            return
        self._dict_button, self._dict_sink = button, sink
        if not self.voice.begin("dictation"):
            self._dict_button = self._dict_sink = None

    def refresh_voice_state(self) -> None:
        available = self.voice.available()[0]
        st = self.voice.state
        if self.window is not None:
            self.window.set_voice_state(st, available)
        if self.quick is not None:
            self.quick.set_voice_state(st, available)

    def _on_voice_state(self, _v, state: str) -> None:
        if state != "listening" and self.global_hold is not None:
            self.global_hold.stop()
        self.refresh_voice_state()
        if self._dict_button is not None:
            self._dict_button.set_recording(state == "listening" and self.voice.mode == "dictation",
                                            busy=state == "transcribing")
        self._emit_status()

    def _on_voice_error(self, _v, msg: str) -> None:
        # Diktat-Zustand zurücksetzen, sonst bleibt der Knopf als „aufnehmend“ stehen
        if self._dict_button is not None:
            self._dict_button.set_recording(False)
        self._dict_button = self._dict_sink = None
        self._voice_toast(msg, public="Spracheingabe nicht möglich – Details im Tracksy-Fenster")

    def _on_voice_level(self, _v, level: float) -> None:
        if self.window is not None:
            self.window.set_level(level)

    def _on_voice_result(self, _v, text: str, cmd: Any, mode: str) -> None:
        if mode == "dictation":
            sink, self._dict_sink = self._dict_sink, None
            if self._dict_button is not None:
                self._dict_button.set_recording(False)
            self._dict_button = None
            if not text.strip():
                self.toast("Nichts erkannt")
            elif sink is not None:
                sink(text)
            return
        self.handle_command(text, cmd)

    def _known_customer(self, text: str, customer: str | None) -> str | None:
        """Kunde mit vorhandenen Kunden abgleichen (Schreibweise, verklebtes „Kunde…“)."""
        from .voice.parser import match_known_customer  # noqa: PLC0415

        try:
            known = self.service.customers()
        except Exception as exc:  # Abgleich ist nur eine Hilfe – Start nie daran scheitern lassen
            log_failure(log, "Kundenliste für Sprachbefehl nicht lesbar", exc)
            return customer
        return match_known_customer(text, customer, known)

    def _is_known_customer(self, name: str) -> bool:
        try:
            return name in self.service.customers()
        except Exception as exc:
            log_failure(log, "Kundenliste für Sprachbefehl nicht lesbar", exc)
            return False

    def handle_command(self, text: str, cmd: Any) -> None:
        """Sprachbefehl auswerten. Nicht erkannt → Transkript zeigen, nicht raten."""
        data = command_to_dict(cmd) if cmd is not None else {"intent": "unknown"}
        intent = data.get("intent")
        if not text.strip():
            self.toast("Nichts erkannt – bitte noch einmal sprechen")
            return
        if intent in (None, "unknown"):
            self.toast(f"Nicht verstanden: „{text.strip()}“", "Nochmal",
                       lambda: self.voice.begin("command"), timeout=6,
                       public="Sprachbefehl nicht verstanden – Details im Tracksy-Fenster")
            return
        if intent == "start":
            fields = {k: data.get(k) for k in ("customer", "topic", "ticket", "location")}
            raw_customer = fields.get("customer")
            fields["customer"] = self._known_customer(text, raw_customer)
            # Kunde aus dem zweiten Whisper-Lauf, der keinem bekannten Kunden entspricht:
            # nicht still starten, sondern bestätigen lassen (vorausgefüllt)
            unconfirmed = (bool(getattr(cmd, "second_pass", False)) and bool(raw_customer)
                           and fields["customer"] == raw_customer
                           and not self._is_known_customer(raw_customer))
            if unconfirmed or not (fields.get("customer") or "").strip():
                self.ask_customer(fields, "voice", self.quick if self.quick else None,
                                  heard=text)
                return
            if self.start_task(fields, source="voice"):
                self.toast("Gestartet: " + describe_start_fields(**fields), "Bearbeiten",
                           self.edit_active, timeout=6, public="Task gestartet")
            return
        try:
            if intent == "stop":
                self.stop_task()
            elif intent in ("pause", "resume"):
                if intent == "resume" and self.service.state == "stopping":
                    # „weiter“ im Stopp-Zustand = „Weiterlaufen lassen“ (wie service.apply_command)
                    if self.stop_dialog is not None:
                        self.stop_dialog._keep_running()  # bricht Stopp ab, schließt Dialog
                    else:
                        self.service.cancel_stop()
                    if self.service.state != "stopping":
                        self.toast("Stopp abgebrochen – Task läuft weiter")
                elif intent == "pause" and self.service.state == "running":
                    self.service.pause()
                    self.toast("Pausiert")
                elif intent == "resume" and self.service.state == "paused":
                    self.service.resume()
                    self.toast("Weiter geht’s")
                else:
                    self.toast("Nicht möglich: " + ("kein laufender Task" if intent == "pause"
                                                    else "kein pausierter Task"))
            elif intent == "status":
                v = task_view_from_status(self.service.status())
                if v.active:
                    self.toast(f"{v.title} – {v.subtitle}, {v.state_label.lower()}, "
                               f"{format_clock(v.elapsed())}", timeout=6,
                               public=f"Task {v.state_label.lower()}")
                else:
                    self.toast("Kein Task aktiv")
        except Exception as exc:
            self.toast(str(exc), public=PUBLIC_ERROR)

    # ================================================================ Fenster
    def present_main(self) -> None:
        from .ui.main_window import MainWindow  # noqa: PLC0415

        if self.window is None:
            self.window = MainWindow(self)
            self.window.update_status(self.service.status())
            self.refresh_voice_state()
            # geänderte Kürzel (z. B. in den Systemeinstellungen) beim Aktivieren nachlesen
            self.window.connect("notify::is-active", lambda w, _p: self.refresh_hotkeys(
                force=False) if w.is_active() else None)
        self.window.present()
        if not self.config.general.first_run_done and not self._first_run_shown:
            from .ui.first_run import FirstRunDialog  # noqa: PLC0415

            self._first_run_shown = True
            FirstRunDialog(self).present(self.window)

    def show_quick_entry(self) -> None:
        from .ui.quick_entry import QuickEntryWindow  # noqa: PLC0415

        if self.quick is None:
            self.quick = QuickEntryWindow(self)
            self.refresh_voice_state()
        self.quick.present()
        self.quick.prepare()
        quick = self.quick
        self.ensure_attention(quick, "Tracksy: Schnellerfassung offen",
                              "Zum Eingeben hier klicken.", action="app.quick-entry",
                              still_needed=lambda: self.quick is quick, owner=quick)

    def quick_entry_closed(self, win) -> None:
        if self.quick is win:
            self.quick = None
            withdraw_attention(self, win)

    def show_preferences(self, page: str | None = None) -> None:
        """Einstellungen öffnen; ``page="system"`` springt zu den Tastenkürzeln."""
        from .ui.preferences import PreferencesDialog  # noqa: PLC0415

        self.present_main()
        dlg = PreferencesDialog(self)
        # Kürzel eingerichtet/entfernt → Anzeige im Fenster und im Tray aktualisieren
        dlg.connect("closed", lambda *_: self.refresh_hotkeys())
        dlg.present(self.window)
        if page and not select_preferences_page(dlg, page):
            log.debug("Einstellungsseite %s nicht gefunden", page)

    def _show_shortcuts(self) -> None:
        from .ui.shortcuts_dialog import ShortcutsDialog  # noqa: PLC0415

        self.present_main()
        if self.shortcuts_dialog is not None:
            return
        dlg = ShortcutsDialog(self.hotkeys, self.hold_supported(),
                              lambda: self.show_preferences(page="system"))
        self.shortcuts_dialog = dlg

        def closed(*_a) -> None:
            if self.shortcuts_dialog is dlg:
                self.shortcuts_dialog = None

        dlg.connect("closed", closed)
        dlg.present(self.window)
        self.refresh_hotkeys(force=False)

    def apply_color_scheme(self) -> None:
        """Erscheinungsbild aus der Einstellung (System/Hell/Dunkel) anwenden."""
        scheme = {
            "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK,
        }.get(self.config.general.color_scheme, Adw.ColorScheme.DEFAULT)
        Adw.StyleManager.get_default().set_color_scheme(scheme)

    def _show_releases(self) -> None:
        from . import __version__  # noqa: PLC0415

        self.present_main()
        dlg = Adw.AlertDialog(
            heading=f"Tracksy {__version__} (experimentell)",
            body="Dies ist eine frühe Testversion. Funktionen und Bedienung können sich noch "
                 "ändern – bitte sichere deine Daten regelmäßig (Einstellungen → Daten).\n\n"
                 "Neu in 0.12: Eigene Feiertage auch als Zeiträume (z. B. Betriebsruhe), "
                 "neues Feld „Urlaub“ (zählt wie ein normaler Werktag, nur zur Übersicht); "
                 "Feiertage gelten bundesweit plus Bundesland. Die vollständige Liste der "
                 "Änderungen steht in CHANGELOG.md im Tracksy-Ordner. 0.12.1: Fehlerhafte "
                 "Einstellungsdatei wird vor dem Überschreiben gesichert; Korrekturen bei "
                 "Zeiträumen. 0.12.5: Systembenachrichtigungen ohne Kundendaten und "
                 "Sprachtexte; Sprachaufnahme nur mit Schutz vor Absturzabbildern; "
                 "unterbrochenes Löschen wird im CSV-Protokoll nachgeholt.\n\n"
                 "Neu in 0.11: Regelarbeitszeit mit Faktoren und Feiertagen im Export, "
                 "Sprachaufnahme per Strg+M gedrückt halten, Übersicht der Tastenkürzel "
                 "(Strg+?). Achtung: Die Rundung im Export erfolgt jetzt je Anteil "
                 "(regulär/außerhalb) und kann abgerechnete Summen auch bestehender Einträge "
                 "erhöhen. 0.11.1: Sprachbefehl „weiter“ bei gestopptem Task lässt ihn "
                 "weiterlaufen.\n\n"
                 "Sprache: Tracksy ist derzeit nur auf Deutsch verfügbar. Englisch wird zu einem "
                 "späteren Zeitpunkt umgesetzt.\n\n"
                 "Feedback ist ausdrücklich erwünscht – Fehler, Wünsche und Ideen gern über "
                 "die Kontaktangabe unter Menü → „Über Tracksy“.")
        dlg.add_response("close", "Schließen")
        dlg.add_response("about", "Über Tracksy")
        dlg.set_default_response("close")
        dlg.set_close_response("close")

        def on_response(_d, r: str) -> None:
            if r == "about":
                GLib.idle_add(lambda: (self._show_about(), False)[1])

        dlg.connect("response", on_response)
        dlg.present(self.window)

    def _show_about(self) -> None:
        self.present_main()
        from . import __version__  # noqa: PLC0415

        about = Adw.AboutDialog(
            application_name="Tracksy", application_icon=ICON_NAME,
            version=f"{__version__} (experimentell)",
            developer_name="Christian Dworschak",
            developers=["Christian Dworschak <tracksy@pm.me>"],
            support_url="mailto:tracksy@pm.me",
            copyright="© 2026 Christian Dworschak",
            license_type=Gtk.License.MIT_X11,
            comments="Lokale Zeiterfassung per Sprache, Tastenkürzel und Klick. "
                     "Alle Daten bleiben auf diesem Rechner.\n\n"
                     "Experimentelle Version – bitte Daten regelmäßig sichern.\n"
                     "Kontakt: tracksy@pm.me")
        # Mitwirkende → Quellcode: Link zum GitHub-Repository (bis dahin Platzhalter)
        about.add_credit_section(
            "Quellcode", [f"Tracksy auf GitHub {REPO_URL}" if REPO_URL
                          else "GitHub-Repository (Adresse folgt)"])
        if REPO_URL:
            about.set_website(REPO_URL)
            about.set_issue_url(f"{REPO_URL.rstrip('/')}/issues")
        about.add_legal_section("whisper.cpp / ggml", "© The ggml authors",
                                Gtk.License.MIT_X11, None)
        about.add_legal_section("Whisper-Sprachmodelle", "© OpenAI", Gtk.License.MIT_X11, None)
        about.present(self.window)

    def toast(self, text: str, button: str | None = None,
              on_button: Callable[[], None] | None = None, timeout: int = 4,
              high: bool = True, public: str | None = None) -> None:
        """Rückmeldung im Fenster; ist keins sichtbar, als Systembenachrichtigung.

        Benachrichtigungen landen im Verlauf des Desktops (teils auf dem
        Sperrbildschirm). Texte mit Kundendaten, Themen, Transkripten oder
        Fehlermeldungen übergeben deshalb mit ``public`` eine neutrale Fassung,
        die statt ``text`` in der Benachrichtigung erscheint.
        """
        if self.quick is not None and self.quick.is_active():
            t = Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout)
            self.quick.toasts.add_toast(t)
        elif self.window is not None and self.window.get_visible():
            self.window.toast(text, button, on_button, timeout, high)
        else:
            self.notify("Tracksy", text if public is None else public)

    def notify(self, title: str, body: str = "") -> None:
        n = Gio.Notification.new(title)
        if body:
            n.set_body(body)
        self.send_notification("zeit", n)

    def ensure_attention(self, win, title: str, body: str, action: str = "app.show",
                         still_needed: Callable[[], bool] | None = None,
                         delay_ms: int = ATTENTION_DELAY_MS, owner: Any = None) -> None:
        """Fokus-Fallback: Ist ``win`` kurz nach ``present()`` nicht aktiv (Wayland ohne
        Aktivierungs-Token, Fokusschutz), eine Benachrichtigung mit Standardaktion
        ``action`` senden. Sie verschwindet, sobald das Fenster aktiv wird oder die
        Frage ``owner`` (Standard: ``win``) anderweitig erledigt ist
        (``withdraw_attention``)."""
        if win is None:
            return
        owner = win if owner is None else owner

        def check() -> bool:
            if self.really_quitting or not win.get_visible() or win.is_active():
                return False
            if still_needed is not None and not still_needed():
                return False
            n = Gio.Notification.new(title)
            n.set_body(body)
            n.set_priority(Gio.NotificationPriority.HIGH)
            n.set_default_action(action)
            if getattr(self, "_attention", None) is not None:
                withdraw_attention(self)  # ältere Meldung samt Handler ablösen
            self.send_notification(ATTENTION_NOTIFICATION_ID, n)

            def on_active(w, _pspec) -> None:
                if w.is_active():
                    withdraw_attention(self, owner)

            self._attention = (owner, win, win.connect("notify::is-active", on_active))
            return False

        GLib.timeout_add(delay_ms, check)

    # ================================================================ Beenden
    def _quit_confirm(self) -> None:
        v = task_view_from_status(self.service.status())
        if not v.active:
            self.really_quit(notify_running=False)
            return
        self.present_main()
        dlg = Adw.AlertDialog(
            heading="Tracksy beenden?",
            body=f"Der Task „{v.title}“ bleibt gespeichert und die Zeit läuft weiter. "
                 "Beim nächsten Start wird er fortgesetzt.\n\nZum Weiterarbeiten im "
                 "Hintergrund genügt es, das Fenster zu schließen.")
        dlg.add_response("cancel", "Abbrechen")
        dlg.add_response("hide", "Nur Fenster schließen")
        dlg.add_response("quit", "Beenden")
        dlg.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("hide")
        dlg.set_close_response("cancel")

        def on_resp(_d, r: str) -> None:
            if r == "quit":
                self.really_quit(notify_running=False)
            elif r == "hide":
                self.window.hide_window()

        dlg.connect("response", on_resp)
        dlg.present(self.window)
        open_ = [True]

        def on_closed(*_a) -> None:
            open_[0] = False
            withdraw_attention(self, dlg)

        dlg.connect("closed", on_closed)
        self.ensure_attention(self.window, "Tracksy beenden?",
                              "Die Rückfrage wartet im Tracksy-Fenster.",
                              still_needed=lambda: open_[0], owner=dlg)

    def really_quit(self, notify_running: bool = True, clean: bool = True) -> None:
        """Beenden. ``clean=True`` (Beenden-Aktion/-Dialog, ``zeit quit``): ein
        laufender Task zählt wie angekündigt weiter, beim nächsten Start ohne
        Ausfallzeit-Frage. ``False`` nur bei SIGTERM/SIGINT."""
        if self.really_quitting:
            return
        self.really_quitting = True
        self._clean_quit = clean
        if notify_running and self.service is not None:
            v = task_view_from_status(self.service.status())
            if v.active:
                self.notify("Tracksy beendet",
                            "Der laufende Task bleibt gespeichert und wird beim nächsten Start "
                            "fortgesetzt.")
        if self.voice is not None:
            self.voice.shutdown()
        self._stop_tray()
        for w in list(self.get_windows()):
            w.destroy()
        self.quit()


APP_DISPLAY_NAME = "Tracksy"


def select_preferences_page(dlg, name: str) -> bool:
    """Seite ``name`` (``general``, ``worktime``, ``voice``, ``system``, ``data``) im
    Einstellungsdialog zeigen; ``preferences.py`` vergibt die Namen per ``set_name``."""
    stack = [dlg]
    while stack:
        w = stack.pop()
        if isinstance(w, Adw.PreferencesPage) and w.get_name() == name:
            dlg.set_visible_page(w)
            return True
        child = w.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return False


def withdraw_attention(app, owner: Any = None) -> None:
    """Fokus-Benachrichtigung zurückziehen.

    Mit ``owner`` nur, wenn sie zu dieser Frage gehört – sonst bliebe z. B. beim
    Schließen der Schnellerfassung der Hinweis auf einen offenen Stopp-Dialog nicht
    stehen. Ohne ``owner``: immer. Löst auch den ``notify::is-active``-Handler."""
    cur = getattr(app, "_attention", None)
    if owner is not None and cur is not None and cur[0] is not owner:
        return
    app._attention = None
    if cur is not None:
        try:
            cur[1].disconnect(cur[2])
        except (TypeError, ValueError):  # Fenster schon zerstört/Handler weg
            pass
    app.withdraw_notification(ATTENTION_NOTIFICATION_ID)


def child_env() -> dict[str, str]:
    """Umgebung für Kindprozesse (Tray, Neustart): eigener ``PYTHONPATH``, ohne
    Aktivierungs-Token. ``os.environ`` ist eine Kopie vom Prozessstart – GTK hat die
    Token längst verbraucht; geerbt wären sie veraltet (unter X11 kann ein alter
    Zeitstempel der Startup-ID den Fokus sogar verhindern)."""
    env = {k: v for k, v in os.environ.items() if k not in ACTIVATION_ENV_VARS}
    src = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def set_process_identity() -> None:
    """Programmname = App-ID, bevor GTK das Display öffnet.

    Unter X11 bildet GTK daraus ``WM_CLASS`` (sonst ``__main__.py`` bei
    ``python -m``); passend zu ``StartupWMClass`` der ``.desktop``-Datei, damit
    Taskleisten (KDE X11, Cinnamon) Fenster und Starter zuordnen. Der
    Anwendungsname erscheint in Benachrichtigungen (freedesktop-Backend).
    """
    GLib.set_prgname(ICON_NAME)
    GLib.set_application_name(APP_DISPLAY_NAME)


def run(argv: list[str]) -> int:
    logging.basicConfig(level=os.environ.get("ZEIT_LOG", "WARNING").upper(),
                        format="%(levelname)s %(name)s: %(message)s")
    if argv and argv[0].startswith("-"):
        argv = ["zeiterfassung", *argv]
    set_process_identity()
    app = ZeitApp()
    rc = app.run(argv)
    if app.restart_requested:
        exec_restart(app, argv)
    return rc


def exec_restart(app: "ZeitApp", argv: list[str], execve=os.execve) -> None:
    """Prozess durch eine neue Instanz ersetzen (gleiche Umgebung, ohne ``--hidden``).

    Vorher den D-Bus-Namen freigeben, damit die neue Instanz die primäre wird
    und sich nicht als Zweitinstanz an den alten Prozess wendet.
    """
    conn = app.get_dbus_connection()
    if conn is not None:
        try:
            conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                           "org.freedesktop.DBus", "ReleaseName",
                           GLib.Variant("(s)", (ipc.APP_ID,)), GLib.VariantType("(u)"),
                           Gio.DBusCallFlags.NONE, 3000, None)
            conn.flush_sync(None)
        except GLib.Error as exc:
            log_failure(log, "D-Bus-Name nicht freigegeben", exc)
    keep = [a for a in argv[1:] if a == "--no-tray"]
    env = child_env()
    logging.shutdown()
    execve(sys.executable, [sys.executable, "-m", "zeiterfassung", *keep], env)
