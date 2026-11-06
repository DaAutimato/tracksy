"""Tray-Verfügbarkeit ohne Blockieren des Main-Loops beobachten.

``ipc.is_running``/``ipc.tray_host_available`` fragen synchron (je bis 1 s) –
für die CLI richtig, in der GUI ruckelt dann bei hängendem Bus die Oberfläche.
``TrayWatch`` hält deshalb den Stand zwischengespeichert und aktualisiert ihn
nur über Bus-Ereignisse und asynchrone Aufrufe:

* Tray-Prozess: ``Gio.bus_watch_name`` auf den Bus-Namen des Trays.
* Panel mit Tray-Bereich: ``NameOwnerChanged`` (XApp-Monitor, StatusNotifier-
  Watcher/-Host) und die Host-Signale des Watchers lösen eine asynchrone
  Neubewertung aus (``ListNames`` → ggf. ``IsStatusNotifierHostRegistered``).

Unbekannt (``None``) gilt wie ``ipc.tray_host_available`` als „vorhanden“: lieber
kein Hinweis als ein falscher. Nur lesend, keine Rechte nötig.
"""

from __future__ import annotations

import logging
from typing import Callable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .. import ipc  # noqa: E402
from ..logutil import log_failure  # noqa: E402

log = logging.getLogger(__name__)

DBUS = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
SNI_HOST_PREFIX = "org.kde.StatusNotifierHost"
REFRESH_DELAY_MS = 200   # mehrere Namenswechsel (Panel-Neustart) zusammenfassen
CALL_TIMEOUT_MS = 1000

DISABLED = "disabled"
MISSING = "missing"
OK = "ok"


def tray_state(no_tray: bool, proc_alive: bool, tray_running: bool | None,
               host_available: bool | None) -> str:
    """``ok`` / ``missing`` / ``disabled`` aus dem zwischengespeicherten Stand.

    ``None`` = noch unbekannt (Antwort des Busses steht aus) → nicht als fehlend werten."""
    if no_tray:
        return DISABLED
    if not proc_alive and tray_running is False:
        return MISSING
    return MISSING if host_available is False else OK


def host_from_names(names: list[str]) -> bool | None:
    """Erste Stufe der Bewertung: ``True``/``False`` oder ``None`` = Watcher fragen."""
    if any(n.startswith(ipc.XAPP_MONITOR_PREFIX) for n in names):
        return True
    if ipc.SNI_WATCHER not in names:
        return False
    return None


def relevant_name(name: str) -> bool:
    return (name.startswith(ipc.XAPP_MONITOR_PREFIX) or name == ipc.SNI_WATCHER
            or name.startswith(SNI_HOST_PREFIX))


class TrayWatch:
    """Beobachtet Tray-Prozess und Tray-Bereich des Panels auf ``conn``.

    ``on_change()`` wird im Main-Loop aufgerufen, wenn sich der Stand ändert."""

    def __init__(self, conn: Gio.DBusConnection, tray_bus_name: str,
                 on_change: Callable[[], None] | None = None) -> None:
        self.conn = conn
        self.on_change = on_change
        self.tray_running: bool | None = None
        self.host_available: bool | None = None
        self._generation = 0
        self._refresh_id = 0
        self._cancel = Gio.Cancellable()
        self._watch_id = Gio.bus_watch_name_on_connection(
            conn, tray_bus_name, Gio.BusNameWatcherFlags.NONE,
            lambda *_a: self._set_tray(True), lambda *_a: self._set_tray(False))
        self._subs = [
            conn.signal_subscribe(DBUS, DBUS, "NameOwnerChanged", DBUS_PATH, None,
                                  Gio.DBusSignalFlags.NONE, self._on_owner_changed),
            # StatusNotifierHostRegistered/-Unregistered des Watchers (KDE, AppIndicator)
            conn.signal_subscribe(ipc.SNI_WATCHER, ipc.SNI_WATCHER, None,
                                  ipc.SNI_WATCHER_PATH, None, Gio.DBusSignalFlags.NONE,
                                  lambda *_a: self.schedule_refresh()),
        ]
        self.refresh()

    # ------------------------------------------------------------- Stand
    def _set_tray(self, running: bool) -> None:
        if running != self.tray_running:
            self.tray_running = running
            self._changed()

    def _set_host(self, generation: int, available: bool) -> None:
        if generation != self._generation:
            return  # überholt von einer neueren Abfrage
        if available != self.host_available:
            self.host_available = available
            self._changed()

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception as exc:  # Beobachter darf den Watch nicht beenden
                log_failure(log, "Tray-Stand nicht weitergegeben", exc)

    # ------------------------------------------------------------- Bus
    def _on_owner_changed(self, _conn, _sender, _path, _iface, _signal, params) -> None:
        name = params.unpack()[0]
        if relevant_name(name):
            self.schedule_refresh()

    def schedule_refresh(self) -> None:
        if self._refresh_id:
            return

        def run() -> bool:
            self._refresh_id = 0
            self.refresh()
            return False

        self._refresh_id = GLib.timeout_add(REFRESH_DELAY_MS, run)

    def refresh(self) -> None:
        """Tray-Bereich asynchron neu bewerten (blockiert nie)."""
        self._generation += 1
        gen = self._generation
        self.conn.call(DBUS, DBUS_PATH, DBUS, "ListNames", None, GLib.VariantType("(as)"),
                       Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, self._cancel,
                       self._on_names, gen)

    def _on_names(self, conn, res, gen) -> None:
        try:
            names = conn.call_finish(res).unpack()[0]
        except GLib.Error as exc:
            if not exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self._set_host(gen, True)  # im Zweifel kein falscher Hinweis
            return
        verdict = host_from_names(names)
        if verdict is not None:
            self._set_host(gen, verdict)
            return
        conn.call(ipc.SNI_WATCHER, ipc.SNI_WATCHER_PATH, "org.freedesktop.DBus.Properties",
                  "Get", GLib.Variant("(ss)", (ipc.SNI_WATCHER, "IsStatusNotifierHostRegistered")),
                  GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS,
                  self._cancel, self._on_host_registered, gen)

    def _on_host_registered(self, conn, res, gen) -> None:
        try:
            value = conn.call_finish(res).unpack()[0]
        except GLib.Error as exc:
            if not exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self._set_host(gen, True)  # Watcher ohne lesbare Eigenschaft
            return
        self._set_host(gen, bool(value))

    def close(self) -> None:
        self._cancel.cancel()
        self.on_change = None
        if self._refresh_id:
            GLib.source_remove(self._refresh_id)
            self._refresh_id = 0
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
            self._watch_id = 0
        for sid in self._subs:
            self.conn.signal_unsubscribe(sid)
        self._subs = []
