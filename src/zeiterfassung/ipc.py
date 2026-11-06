"""D-Bus-Schnittstelle (Session-Bus) für Haupt-App, CLI und Tray.

- Bus-Name / App-ID: ``de.local.Zeiterfassung``
- Objektpfad:        ``/de/local/Zeiterfassung``
- GActions der Gtk.Application über ``org.gtk.Actions`` (Gio exportiert sie
  automatisch für eine Gtk.Application mit dieser ID).
- Eigenes Interface ``de.local.Zeiterfassung.Tracker``:
  ``GetStatus() -> s`` (JSON) und Signal ``StatusChanged(s)`` (JSON).

Server-Seite (in ``Gtk.Application.do_dbus_register``)::

    self._tracker_export = ipc.export_tracker(connection, self.service, object_path)

Client-Seite (CLI/Tray)::

    ipc.is_running(); ipc.activate_action("toggle"); ipc.get_status()
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any, Callable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from .service import TrackerService
from .logutil import log_failure

log = logging.getLogger(__name__)

BUS_NAME = "de.local.Zeiterfassung"
APP_ID = BUS_NAME
OBJECT_PATH = "/de/local/Zeiterfassung"
TRACKER_IFACE = "de.local.Zeiterfassung.Tracker"
ACTIONS_IFACE = "org.gtk.Actions"

# Name → Parametertyp (None = ohne Parameter). Die UI legt die GActions so an.
ACTIONS: dict[str, str | None] = {
    "start": "s",  # JSON-String mit Feldern oder "" (→ UI zeigt Startformular)
    "stop": None,
    "toggle": None,
    "pause": None,  # Pause ⇄ Weiter
    "voice": None,
    "show": None,
    "quit": None,
    "quick-entry": None,
    "quit-confirm": None,  # Tray „Beenden“: Rückfrage im Hauptfenster (libadwaita)
}

TRACKER_XML = f"""
<node>
  <interface name="{TRACKER_IFACE}">
    <method name="GetStatus">
      <arg type="s" name="status_json" direction="out"/>
    </method>
    <signal name="StatusChanged">
      <arg type="s" name="status_json"/>
    </signal>
  </interface>
</node>
"""

DEFAULT_TIMEOUT_MS = 5000


class IpcError(RuntimeError):
    """App nicht erreichbar oder D-Bus-Aufruf fehlgeschlagen."""


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
class TrackerExport:
    """Exportiertes Tracker-Interface; meldet Statusänderungen automatisch."""

    def __init__(
        self,
        connection: Gio.DBusConnection,
        service: "TrackerService | None",
        object_path: str = OBJECT_PATH,
        auto_emit: bool = True,
    ):
        self.connection = connection
        self.service: "TrackerService | None" = None
        self.object_path = object_path
        self._auto_emit = auto_emit
        node = Gio.DBusNodeInfo.new_for_xml(TRACKER_XML)
        self._reg_id = connection.register_object(
            object_path, node.interfaces[0], self._on_method_call, None, None
        )
        self._unsubscribe: Callable[[], None] | None = None
        if service is not None:
            self.attach(service)

    def attach(self, service: "TrackerService") -> None:
        """Service nachträglich verbinden (z. B. erst in ``do_startup``)."""
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        self.service = service
        if self._auto_emit:
            self._unsubscribe = service.subscribe(self._on_status)

    def _on_method_call(self, conn, sender, path, iface, method, params, invocation):
        if method == "GetStatus":
            if self.service is None:
                invocation.return_dbus_error(f"{TRACKER_IFACE}.NotReady", "Dienst startet noch")
                return
            try:
                invocation.return_value(GLib.Variant("(s)", (self.service.status_json(),)))
            except Exception as exc:  # pragma: no cover - defensiv
                log_failure(log, "GetStatus fehlgeschlagen", exc)
                invocation.return_dbus_error(f"{TRACKER_IFACE}.Error", str(exc))
        else:  # pragma: no cover - Gio prüft Methodennamen bereits
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod", f"Unbekannte Methode {method}"
            )

    def _on_status(self, event: str, status: dict[str, Any]) -> None:
        emit_status_changed(self.connection, status, self.object_path)

    def emit(self) -> None:
        if self.service is None:
            return
        emit_status_changed(self.connection, self.service.status(), self.object_path)

    def unexport(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        if self._reg_id:
            self.connection.unregister_object(self._reg_id)
            self._reg_id = 0


def export_tracker(
    connection: Gio.DBusConnection,
    service: "TrackerService | None",
    object_path: str = OBJECT_PATH,
    auto_emit: bool = True,
) -> TrackerExport:
    """Tracker-Interface auf ``connection`` exportieren.

    ``service`` darf ``None`` sein und später per ``TrackerExport.attach()``
    gesetzt werden (bis dahin liefert GetStatus den Fehler ``…Tracker.NotReady``).

    Mit ``auto_emit`` wird bei jeder Service-Änderung ``StatusChanged`` gesendet
    (``emit_signal`` ist thread-sicher).
    """
    return TrackerExport(connection, service, object_path, auto_emit)


def emit_status_changed(
    connection: Gio.DBusConnection,
    status: dict[str, Any] | str,
    object_path: str = OBJECT_PATH,
) -> None:
    payload = status if isinstance(status, str) else json.dumps(status, ensure_ascii=False)
    connection.emit_signal(
        None, object_path, TRACKER_IFACE, "StatusChanged", GLib.Variant("(s)", (payload,))
    )


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------
def _bus() -> Gio.DBusConnection:
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as exc:
        raise IpcError(f"Kein D-Bus-Session-Bus: {exc.message}") from exc


def is_running(bus_name: str = BUS_NAME) -> bool:
    """Ist die Haupt-App (Bus-Name) auf dem Session-Bus vorhanden?"""
    try:
        res = _bus().call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", GLib.Variant("(s)", (bus_name,)), GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE, DEFAULT_TIMEOUT_MS, None,
        )
    except (GLib.Error, IpcError):
        return False
    return bool(res.unpack()[0])


# Status-Bereiche im Panel (für den Hinweis „Tray-Symbol“): Cinnamon/XApp-Applets
# besitzen ``org.x.StatusIconMonitor.*``; KDE Plasma, die GNOME-Erweiterung
# „AppIndicator“ und xapp-sn-watcher stellen den StatusNotifierWatcher.
XAPP_MONITOR_PREFIX = "org.x.StatusIconMonitor."
SNI_WATCHER = "org.kde.StatusNotifierWatcher"
SNI_WATCHER_PATH = "/StatusNotifierWatcher"


def tray_host_available(timeout_ms: int = 1000) -> bool:
    """Zeigt das Panel Tray-Symbole an (XApp-Monitor oder SNI-Host registriert)?

    Nur lesend. Bei D-Bus-Fehlern ``True`` (im Zweifel keinen falschen Hinweis).
    """
    try:
        bus = _bus()
        names = bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "ListNames", None, GLib.VariantType("(as)"), Gio.DBusCallFlags.NONE,
            timeout_ms, None).unpack()[0]
    except (GLib.Error, IpcError):
        return True
    if any(n.startswith(XAPP_MONITOR_PREFIX) for n in names):
        return True
    if SNI_WATCHER not in names:
        return False
    try:
        res = bus.call_sync(
            SNI_WATCHER, SNI_WATCHER_PATH, "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", (SNI_WATCHER, "IsStatusNotifierHostRegistered")),
            GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, timeout_ms, None)
    except GLib.Error:
        return True  # Watcher ohne lesbare Eigenschaft: vermutlich mit Host
    return bool(res.unpack()[0])


def wait_for_name(timeout_s: float = 10.0, bus_name: str = BUS_NAME, interval_s: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if is_running(bus_name):
            return True
        time.sleep(interval_s)
    return is_running(bus_name)


# Aktivierungs-Token des Starters (Wayland: xdg-activation, X11: Startup-Notification).
# GTK übernimmt sie aus den Plattformdaten von org.gtk.Actions.Activate; ohne Token
# verweigern Mutter/KWin dem Fenster der App ggf. den Fokus (Hotkey, Starter-Aktion).
ACTIVATION_ENV = (("XDG_ACTIVATION_TOKEN", "activation-token"),
                  ("DESKTOP_STARTUP_ID", "desktop-startup-id"))
MAX_TOKEN_LEN = 512


def activation_platform_data(env: dict[str, str] | None = None) -> dict[str, GLib.Variant]:
    """Plattformdaten mit den Aktivierungs-Token aus der Umgebung (leer, wenn keine)."""
    env = os.environ if env is None else env
    out: dict[str, GLib.Variant] = {}
    for var, key in ACTIVATION_ENV:
        value = (env.get(var) or "").strip()
        if value and len(value) <= MAX_TOKEN_LEN and value.isprintable():
            out[key] = GLib.Variant("s", value)
    return out


def activate_action(
    name: str,
    param: str | None = None,
    *,
    bus_name: str = BUS_NAME,
    object_path: str = OBJECT_PATH,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    platform_data: dict[str, GLib.Variant] | None = None,
) -> None:
    """GAction der laufenden App per ``org.gtk.Actions.Activate`` auslösen.

    ``param``: String-Parameter (nur ``start``); für ``start`` ohne Wert wird
    ``""`` gesendet, weil die Action den Typ ``s`` hat. ``platform_data``:
    Standard sind die Aktivierungs-Token aus der Umgebung
    (:func:`activation_platform_data`), damit das Fenster unter Wayland den Fokus bekommt.
    """
    ptype = ACTIONS.get(name, "s" if param is not None else None)
    if ptype == "s":
        args = [GLib.Variant("s", param or "")]
    else:
        args = []
    if platform_data is None:
        platform_data = activation_platform_data()
    try:
        _bus().call_sync(
            bus_name, object_path, ACTIONS_IFACE, "Activate",
            GLib.Variant("(sava{sv})", (name, args, platform_data)), None,
            Gio.DBusCallFlags.NONE, timeout_ms, None,
        )
    except GLib.Error as exc:
        raise IpcError(f"Action '{name}' fehlgeschlagen: {exc.message}") from exc


def get_status(
    *, bus_name: str = BUS_NAME, object_path: str = OBJECT_PATH, timeout_ms: int = DEFAULT_TIMEOUT_MS
) -> dict[str, Any]:
    try:
        res = _bus().call_sync(
            bus_name, object_path, TRACKER_IFACE, "GetStatus", None, GLib.VariantType("(s)"),
            Gio.DBusCallFlags.NONE, timeout_ms, None,
        )
    except GLib.Error as exc:
        raise IpcError(f"GetStatus fehlgeschlagen: {exc.message}") from exc
    return json.loads(res.unpack()[0])


def subscribe_status(
    callback: Callable[[dict[str, Any]], None],
    *,
    bus_name: str = BUS_NAME,
    object_path: str = OBJECT_PATH,
    connection: Gio.DBusConnection | None = None,
) -> Callable[[], None]:
    """Signal ``StatusChanged`` abonnieren (für den Tray). Benötigt einen laufenden
    GLib-Main-Loop. Liefert eine Abmeldefunktion."""
    conn = connection or _bus()

    def _handler(_conn, _sender, _path, _iface, _signal, params):
        try:
            callback(json.loads(params.unpack()[0]))
        except Exception:  # pragma: no cover
            log_failure(log, "StatusChanged-Callback fehlgeschlagen")

    sid = conn.signal_subscribe(
        bus_name, TRACKER_IFACE, "StatusChanged", object_path, None,
        Gio.DBusSignalFlags.NONE, _handler,
    )
    return lambda: conn.signal_unsubscribe(sid)
