"""Tray-Prozess: ``python -m zeiterfassung.tray`` bzw. ``zeit-tray``.

GTK3 + AyatanaAppIndicator3 bzw. unter Cinnamon ``XApp.StatusIcon`` (dort ist
das AppIndicator-Label unsichtbar, siehe :func:`model.choose_backend`).
Kommuniziert ausschließlich über ``ipc`` (StatusChanged-Signal; Polling nur als
Fallback). Beendet sich, wenn die Haupt-App vom Bus verschwindet.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from ..logutil import log_failure

log = logging.getLogger("zeiterfassung.tray")

ICON_DIR = Path(__file__).resolve().parents[1] / "resources" / "icons"
POLL_FALLBACK_S = 60
LABEL_REFRESH_S = 15
STARTUP_GRACE_S = 20
HOTKEY_REFRESH_S = 300  # eingerichtete Kürzel selbst nachlesen (App meldet Änderungen sofort)


def icon_file(name: str) -> Path:
    """Statussymbol als Datei (Logo als PNG, Zustände als SVG)."""
    png = ICON_DIR / f"{name}.png"
    return png if png.is_file() else ICON_DIR / f"{name}.svg"


class IndicatorBackend:
    """AyatanaAppIndicator3. Label nur in Panels mit Label-Unterstützung (nicht
    Cinnamon); der Titel dient dort als Tooltip (xapp-sn-watcher)."""

    name = "appindicator"

    def __init__(self) -> None:
        import gi  # noqa: PLC0415

        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as AI  # noqa: PLC0415

        self.ind = AI.Indicator.new("de.local.Zeiterfassung", "zeit-idle",
                                    AI.IndicatorCategory.APPLICATION_STATUS)
        self.ind.set_icon_theme_path(str(ICON_DIR))
        self.ind.set_title("Tracksy")
        self.ind.set_status(AI.IndicatorStatus.ACTIVE)

    def set_menu(self, menu, show_item, _on_show) -> None:
        self.ind.set_menu(menu)
        self.ind.set_secondary_activate_target(show_item)  # Mittelklick

    def update(self, s) -> None:
        self.ind.set_icon_full(s.icon, s.icon_desc)
        self.ind.set_label(s.label, "00:00 ⏸")
        self.ind.set_title(s.tooltip)


class XAppBackend:
    """Natives Cinnamon-Statussymbol: Label (laufende Zeit) im Panel sichtbar."""

    name = "xapp"

    def __init__(self, XApp) -> None:
        self.icon = XApp.StatusIcon()
        self.icon.set_name("de.local.Zeiterfassung")
        self._last: tuple = ()

    def set_menu(self, menu, _show_item, on_show) -> None:
        self.icon.set_primary_menu(menu)  # Links- und Rechtsklick: Menü
        self.icon.set_secondary_menu(menu)
        # Mittelklick („activate“ nur für Tasten ohne Menü): Fenster öffnen
        self.icon.connect("activate", lambda _i, button, _t: on_show() if button == 2 else None)

    def update(self, s) -> None:
        key = (s.icon, s.label, s.tooltip)
        if key == self._last:
            return
        self._last = key
        self.icon.set_icon_name(str(icon_file(s.icon)))  # absoluter Pfad erlaubt
        self.icon.set_label(s.label)
        self.icon.set_tooltip_text(s.tooltip)


def make_backend():
    from .model import choose_backend  # noqa: PLC0415

    xapp = None
    try:
        import gi  # noqa: PLC0415

        gi.require_version("XApp", "1.0")
        from gi.repository import XApp  # noqa: PLC0415
        xapp = XApp
    except (ImportError, ValueError) as exc:
        log_failure(log, "XApp nicht verfügbar", exc, level=logging.INFO)
    monitored = False
    if xapp is not None:
        try:
            monitored = bool(xapp.StatusIcon.any_monitors())
        except Exception as exc:  # noqa: BLE001 – D-Bus-Probleme → AppIndicator
            log_failure(log, "XApp-Statusabfrage fehlgeschlagen", exc, level=logging.INFO)
            monitored = False
    kind = choose_backend(os.environ, xapp is not None, monitored)
    log.info("Tray-Backend: %s", kind)
    if kind == "xapp":
        return XAppBackend(xapp)
    return IndicatorBackend()


class Tray:
    def __init__(self) -> None:
        import gi  # noqa: PLC0415

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gio, GLib, Gtk  # noqa: PLC0415

        from .. import ipc  # noqa: PLC0415
        from . import TRAY_BUS_NAME  # noqa: PLC0415

        self.Gtk, self.GLib, self.Gio, self.ipc = Gtk, GLib, Gio, ipc
        self.status: dict[str, Any] | None = None
        self.connected = False
        self.seen_app = False
        self._unsub = None
        self._last_signal = 0.0
        self.hotkeys = None  # integration.ActiveHotkeys (selbst gelesen)
        self._accels: dict[int, str | None] = {}

        self.backend = make_backend()
        self._build_menu()

        Gio.bus_own_name(Gio.BusType.SESSION, TRAY_BUS_NAME, Gio.BusNameOwnerFlags.DO_NOT_QUEUE,
                         None, None, lambda *_: self._quit("Tray läuft bereits"))
        Gio.bus_watch_name(Gio.BusType.SESSION, ipc.BUS_NAME, Gio.BusNameWatcherFlags.NONE,
                           self._on_app_appeared, self._on_app_vanished)
        GLib.timeout_add_seconds(LABEL_REFRESH_S, self._tick)
        GLib.timeout_add_seconds(POLL_FALLBACK_S, self._poll)
        GLib.timeout_add_seconds(STARTUP_GRACE_S, self._grace_over)
        GLib.timeout_add_seconds(HOTKEY_REFRESH_S, lambda: (self._read_hotkeys(), True)[1])
        self._read_hotkeys()
        self._apply()

    # ------------------------------------------------------------------ Menü
    def _item(self, label: str, cb=None) -> Any:
        it = self.Gtk.MenuItem(label=label)
        if cb is not None:
            it.connect("activate", lambda *_: cb())
        self.menu.append(it)
        return it

    def _build_menu(self) -> None:
        Gtk = self.Gtk
        self.menu = Gtk.Menu()
        self.m_status = self._item("Kein Task aktiv")
        self.m_status.set_sensitive(False)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.m_start = self._item("Task starten …", lambda: self._act("quick-entry"))
        self.m_pause = self._item("Pause", lambda: self._act("pause"))
        self.m_stop = self._item("Stoppen …", lambda: self._act("stop"))
        from .model import VOICE_START, SHORTCUTS_ITEM  # noqa: PLC0415

        # Mikrofon: Klick startet, nächster Klick beendet (im Menü kann man nicht halten)
        self.m_voice = self._item(VOICE_START, lambda: self._act("voice"))
        self.menu.append(Gtk.SeparatorMenuItem())
        self.m_shortcuts = self._item(SHORTCUTS_ITEM, lambda: self._act("shortcuts"))
        self.m_show = self._item("Fenster öffnen", lambda: self._act("show"))
        self.m_quit = self._item("Beenden", self._confirm_quit)
        self.menu.show_all()
        self.backend.set_menu(self.menu, self.m_show, lambda: self._act("show"))

    def _act(self, name: str) -> None:
        try:
            # ohne Aktivierungs-Token: der Tray hat nie ein frisches (geerbte wären
            # veraltet und könnten unter X11 den Fokus sogar verhindern)
            self.ipc.activate_action(name, platform_data={})
        except self.ipc.IpcError as exc:
            log_failure(log, "Aktion an die App fehlgeschlagen", exc)

    def _confirm_quit(self) -> None:
        """„Beenden“: Rückfrage als libadwaita-Dialog im Hauptfenster (App-Aktion
        ``quit-confirm``) – auf allen Desktops gleich und unter Wayland korrekt
        zugeordnet. Ohne laufenden Task beendet die App sofort, als bewusstes
        Beenden (keine Ausfallzeit-Frage beim nächsten Start)."""
        if not self.connected:
            self._quit("Beenden ohne erreichbare App")
            return
        self._act("quit-confirm")

    def _set_accel(self, item, accel: str | None) -> None:
        """Tastenkürzel rechts neben dem Menüeintrag anzeigen (nur Anzeige: Der Tray
        fängt keine Tasten ab, die Arbeitsumgebung ruft ``zeit`` auf)."""
        if self._accels.get(id(item)) == accel:
            return
        self._accels[id(item)] = accel
        label = item.get_child()
        if not isinstance(label, self.Gtk.AccelLabel):
            return
        key, mods = self.Gtk.accelerator_parse(accel) if accel else (0, 0)
        label.set_accel(key, mods)

    def _read_hotkeys(self) -> None:
        """Eingerichtete Kürzel im Hintergrund lesen (gsettings, nur lesend)."""
        def work() -> None:
            from ..ui.integration import read_active_hotkeys  # noqa: PLC0415

            try:
                hk = read_active_hotkeys()
            except Exception as exc:  # noqa: BLE001 – nur Anzeige
                log_failure(log, "Tastenkürzel nicht lesbar", exc, level=logging.INFO)
                return
            self.GLib.idle_add(lambda: (self._got_hotkeys(hk), False)[1])

        threading.Thread(target=work, name="tray-hotkeys", daemon=True).start()

    def _got_hotkeys(self, hk) -> None:
        self.hotkeys = hk
        self._apply()

    # ------------------------------------------------------------------ Status
    def _on_app_appeared(self, conn, _name, _owner) -> None:
        self.seen_app = True
        self.connected = True
        if self._unsub is None:
            self._unsub = self.ipc.subscribe_status(self._on_status, connection=conn)
        self._poll()

    def _on_app_vanished(self, _conn, _name) -> None:
        self.connected = False
        if self.seen_app:
            self._quit("Haupt-App beendet")
        else:
            self._apply()

    def _grace_over(self) -> bool:
        if not self.seen_app:
            self._quit("Haupt-App nicht gefunden")
        return False

    def _on_status(self, status: dict[str, Any]) -> None:
        self._last_signal = self.GLib.get_monotonic_time() / 1e6
        self.status = status
        self.connected = True
        self._apply()

    def _poll(self) -> bool:
        now = self.GLib.get_monotonic_time() / 1e6
        if not self.connected:
            return True
        if self.status is not None and now - self._last_signal < POLL_FALLBACK_S:
            return True
        try:
            self.status = self.ipc.get_status()
            self._last_signal = now
        except self.ipc.IpcError as exc:
            log_failure(log, "GetStatus fehlgeschlagen", exc, level=logging.INFO)
            return True
        self._apply()
        return True

    def _tick(self) -> bool:
        if self.status and self.status.get("state") == "running":
            self._apply()
        return True

    def _apply(self) -> None:
        from .model import hotkeys_from, tray_state  # noqa: PLC0415

        hk = hotkeys_from(self.status, self.hotkeys)
        if hk is not None:
            self.hotkeys = hk  # Meldung der App gilt auch nach einem Poll ohne Kürzel
        s = tray_state(self.status, connected=self.connected, hotkeys=hk)
        self.backend.update(s)
        self.m_status.set_label(s.status_line)
        self.m_start.set_visible(s.show_start)
        self.m_pause.set_label(s.pause_label)
        self.m_pause.set_sensitive(s.pause_enabled)
        self.m_pause.set_visible(not s.show_start)
        self.m_stop.set_label(s.stop_label)
        self.m_stop.set_sensitive(s.stop_enabled)
        self.m_stop.set_visible(not s.show_start)
        self.m_voice.set_label(s.voice_label)
        self.m_shortcuts.set_label(s.shortcuts_label)
        self._set_accel(self.m_start, s.toggle_accel)
        self._set_accel(self.m_stop, s.toggle_accel)
        self._set_accel(self.m_voice, s.voice_accel)
        for it in (self.m_start, self.m_pause, self.m_stop, self.m_voice, self.m_shortcuts,
                   self.m_show):
            it.set_sensitive(self.connected and (it is not self.m_pause or s.pause_enabled))

    def _quit(self, reason: str) -> None:
        log.info("Tray beendet: %s", reason)
        self.Gtk.main_quit()

    def run(self) -> None:
        self.GLib.unix_signal_add(self.GLib.PRIORITY_DEFAULT, signal.SIGTERM,
                                  lambda: (self.Gtk.main_quit(), False)[1])
        self.GLib.unix_signal_add(self.GLib.PRIORITY_DEFAULT, signal.SIGINT,
                                  lambda: (self.Gtk.main_quit(), False)[1])
        self.Gtk.main()


def main() -> int:
    logging.basicConfig(level=os.environ.get("ZEIT_LOG", "WARNING").upper(),
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        tray = Tray()
    except (ImportError, ValueError) as exc:
        # Importfehler (fehlendes GI-Paket) enthalten keine Nutzerdaten
        print(f"Tray nicht verfügbar: {exc}", file=sys.stderr)
        return 1
    tray.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
