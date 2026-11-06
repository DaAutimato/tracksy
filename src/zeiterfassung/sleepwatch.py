"""Ruhezustand (Standby/Hibernate) erkennen – desktop-unabhängig über systemd-logind.

- ``org.freedesktop.login1.Manager.PrepareForSleep(true)`` auf dem System-Bus: das
  System schläft gleich ein → ``on_sleep(zeitpunkt)``. Damit die Pause vor dem
  Einschlafen gespeichert ist, hält der Wächter einen **Delay-Inhibitor**
  (``Inhibit("sleep", …, "delay")``). Er verzögert das Einschlafen nur, bis die
  Rückrufe gelaufen sind (logind begrenzt das auf ``InhibitDelayMaxSec``, Standard
  5 s), und wird danach sofort freigegeben; nach dem Aufwachen wird er neu geholt.
  Er kann das Einschlafen nicht verhindern.
- ``PrepareForSleep(false)``: aufgewacht. Ist die Sitzung gesperrt (logind
  ``Session.LockedHint``), wartet der Wächter auf das Entsperren (``LockedHint``
  → false, bzw. ``ActiveChanged(false)`` eines Bildschirmschoners auf dem
  Session-Bus – Cinnamon/GNOME/KDE/MATE). Dann ``on_ready()``.
- Ohne logind: Zeitsprung im Timer erkennen (``CLOCK_BOOTTIME`` zählt den
  Ruhezustand mit, ``CLOCK_MONOTONIC`` nicht) – ``check_clock_jump()``.

Nur Signale abonnieren und Eigenschaften lesen; keine Systemänderungen.
``ZEIT_NO_SLEEP_WATCH=1`` schaltet die Bus-Anbindung ab (Tests im Isolationsmuster).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Callable

from gi.repository import Gio, GLib

from .logutil import log_failure

log = logging.getLogger(__name__)

ENV_NO_SLEEP_WATCH = "ZEIT_NO_SLEEP_WATCH"
LOGIND = "org.freedesktop.login1"
MANAGER_PATH = "/org/freedesktop/login1"
MANAGER_IFACE = "org.freedesktop.login1.Manager"
SESSION_IFACE = "org.freedesktop.login1.Session"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
SCREENSAVER_IFACES = ("org.freedesktop.ScreenSaver", "org.gnome.ScreenSaver",
                      "org.cinnamon.ScreenSaver", "org.mate.ScreenSaver")
#: Ab diesem Zeitsprung (Ruhezustand) reagiert der Fallback ohne logind
JUMP_THRESHOLD_SECONDS = 30
CALL_TIMEOUT_MS = 3000


def _boottime() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def _monotonic() -> float:
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def _now() -> datetime:
    return datetime.now().astimezone()


class SleepWatcher:
    """Meldet Einschlafen (``on_sleep(at)``) und „wieder bedienbar“ (``on_ready()``).

    Alle Rückrufe laufen im GLib-Main-Loop (Signale werden im Kontext des
    abonnierenden Threads zugestellt)."""

    def __init__(self, on_sleep: Callable[[datetime], None], on_ready: Callable[[], None],
                 clock: Callable[[], datetime] = _now,
                 boottime: Callable[[], float] = _boottime,
                 monotonic: Callable[[], float] = _monotonic) -> None:
        self._on_sleep = on_sleep
        self._on_ready = on_ready
        self._clock = clock
        self._boottime = boottime
        self._monotonic = monotonic
        self.logind = False            # Signal-Abo aktiv
        self.waiting_for_unlock = False
        self._sys: Gio.DBusConnection | None = None
        self._sess: Gio.DBusConnection | None = None
        self._subs: list[tuple[Gio.DBusConnection, int]] = []
        self._session_path: str | None = None
        self._inhibit_fd: int | None = None
        self._ref = (self._boottime(), self._monotonic())

    # ------------------------------------------------------------ Anbindung
    def start(self, system_bus: Gio.DBusConnection | None = None,
              session_bus: Gio.DBusConnection | None = None) -> bool:
        """Mit logind verbinden. ``False``: kein logind (Fallback Zeitsprung)."""
        if system_bus is None and os.environ.get(ENV_NO_SLEEP_WATCH):
            return False
        try:
            self._sys = system_bus or Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as exc:
            log_failure(log, "System-Bus nicht erreichbar – Ruhezustand per Zeitsprung", exc)
            return False
        self._subs.append((self._sys, self._sys.signal_subscribe(
            LOGIND, MANAGER_IFACE, "PrepareForSleep", MANAGER_PATH, None,
            Gio.DBusSignalFlags.NONE, self._sig_prepare)))
        self._session_path = self._resolve_session()
        if self._session_path:
            self._subs.append((self._sys, self._sys.signal_subscribe(
                LOGIND, PROPS_IFACE, "PropertiesChanged", self._session_path, SESSION_IFACE,
                Gio.DBusSignalFlags.NONE, self._sig_session_props)))
        self.logind = True
        self._take_inhibitor()
        try:
            self._sess = session_bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._subs.append((self._sess, self._sess.signal_subscribe(
                None, None, "ActiveChanged", None, None,
                Gio.DBusSignalFlags.NONE, self._sig_screensaver)))
        except GLib.Error as exc:  # pragma: no cover - Session-Bus fehlt nur in Sonderfällen
            log_failure(log, "Bildschirmschoner-Signale nicht verfügbar", exc)
        return True

    def stop(self) -> None:
        for conn, sid in self._subs:
            try:
                conn.signal_unsubscribe(sid)
            except Exception:  # pragma: no cover
                pass
        self._subs.clear()
        self._release_inhibitor()
        self.logind = False

    def _call(self, path: str, iface: str, method: str, args: GLib.Variant | None,
              reply: str) -> GLib.Variant | None:
        assert self._sys is not None
        try:
            return self._sys.call_sync(LOGIND, path, iface, method, args,
                                       GLib.VariantType.new(reply), Gio.DBusCallFlags.NONE,
                                       CALL_TIMEOUT_MS, None)
        except GLib.Error as exc:
            log.debug("logind %s.%s fehlgeschlagen: %s", iface, method, exc.message)
            return None

    def _resolve_session(self) -> str | None:
        """Objektpfad der eigenen Sitzung (für ``LockedHint``-Änderungen)."""
        for method, args in (("GetSession", GLib.Variant("(s)", ("auto",))),
                             ("GetSessionByPID", GLib.Variant("(u)", (os.getpid(),)))):
            res = self._call(MANAGER_PATH, MANAGER_IFACE, method, args, "(o)")
            if res is not None:
                return res.unpack()[0]
        sid = os.environ.get("XDG_SESSION_ID")
        if sid:
            res = self._call(MANAGER_PATH, MANAGER_IFACE, "GetSession",
                             GLib.Variant("(s)", (sid,)), "(o)")
            if res is not None:
                return res.unpack()[0]
        return None

    def _query_locked(self) -> bool:
        if self._sys is None or not self._session_path:
            return False
        res = self._call(self._session_path, PROPS_IFACE, "Get",
                         GLib.Variant("(ss)", (SESSION_IFACE, "LockedHint")), "(v)")
        if res is None:
            return False
        return bool(res.unpack()[0])

    # ------------------------------------------------------------ Inhibitor
    def _take_inhibitor(self) -> None:
        if self._sys is None or self._inhibit_fd is not None:
            return
        try:
            res, fds = self._sys.call_with_unix_fd_list_sync(
                LOGIND, MANAGER_PATH, MANAGER_IFACE, "Inhibit",
                GLib.Variant("(ssss)", ("sleep", "Tracksy",
                                        "Laufenden Task vor dem Ruhezustand pausieren",
                                        "delay")),
                GLib.VariantType.new("(h)"), Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS,
                None, None)
        except GLib.Error as exc:
            log_failure(log, "Ruhezustand-Inhibitor nicht erhalten (Pause wird beim "
                        "Aufwachen nachgetragen)", exc)
            return
        idx = res.unpack()[0]
        stolen = list(fds.steal_fds()) if fds is not None else []
        for i, fd in enumerate(stolen):
            if i == idx:
                self._inhibit_fd = fd
            else:
                os.close(fd)

    def _release_inhibitor(self) -> None:
        fd, self._inhibit_fd = self._inhibit_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:  # pragma: no cover
                pass

    # ------------------------------------------------------------ Signale
    def _sig_prepare(self, _conn, _sender, _path, _iface, _signal, params) -> None:
        self.prepare_for_sleep(bool(params.unpack()[0]))

    def _sig_session_props(self, _conn, _sender, _path, _iface, _signal, params) -> None:
        _iface_name, changed, _inv = params.unpack()
        if "LockedHint" in changed:
            self.lock_changed(bool(changed["LockedHint"]))

    def _sig_screensaver(self, _conn, _sender, _path, iface, _signal, params) -> None:
        if iface not in SCREENSAVER_IFACES:
            return
        try:
            active = bool(params.unpack()[0])
        except (IndexError, TypeError):
            return
        self.lock_changed(active)

    # ------------------------------------------------------------ Ablauf
    def _slept_since_ref(self) -> float:
        b, m = self._boottime(), self._monotonic()
        slept = (b - self._ref[0]) - (m - self._ref[1])
        self._ref = (b, m)
        return slept

    def prepare_for_sleep(self, going_to_sleep: bool) -> None:
        if going_to_sleep:
            at = self._clock()
            slept = self._slept_since_ref()
            if slept >= JUMP_THRESHOLD_SECONDS:
                # Signal kam erst nach dem Aufwachen an (kein Inhibitor): Zeitpunkt
                # des Einschlafens aus dem Zeitsprung zurückrechnen
                at = at - timedelta(seconds=slept)
            try:
                self._on_sleep(at)
            except Exception:
                log_failure(log, "Fehler beim Pausieren vor dem Ruhezustand", level=logging.ERROR)
            finally:
                self._release_inhibitor()
            return
        self._ref = (self._boottime(), self._monotonic())
        self._take_inhibitor()
        if self._query_locked():
            self.waiting_for_unlock = True
        else:
            self._ready()

    def lock_changed(self, locked: bool) -> None:
        if locked or not self.waiting_for_unlock:
            return
        self._ready()

    def _ready(self) -> None:
        self.waiting_for_unlock = False
        try:
            self._on_ready()
        except Exception:
            log_failure(log, "Fehler nach dem Aufwachen", level=logging.ERROR)

    def check_clock_jump(self) -> bool:
        """Fallback ohne logind (vom App-Timer aufgerufen): Ruhezustand am Zeitsprung
        erkennen. Der Zeitpunkt des Einschlafens ist dann auf ein Timer-Intervall genau."""
        if self.logind:
            # logind meldet selbst; den Bezugspunkt nicht verbrauchen
            return False
        slept = self._slept_since_ref()
        if slept < JUMP_THRESHOLD_SECONDS:
            return False
        self._on_sleep(self._clock() - timedelta(seconds=slept))
        self._ready()
        return True
