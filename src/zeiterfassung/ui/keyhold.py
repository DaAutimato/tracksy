"""Push-to-talk per Tastatur: Taste gedrückt halten = Aufnahme, Loslassen = Ende.

Drei Bausteine:

* :class:`HoldKey` – GUI-freie Logik für Strg+M im Fenster: Druck → ``begin``,
  Tastenwiederholung ignorieren, Loslassen → ``end(short)``; kürzer als
  :data:`TAP_THRESHOLD_S` gilt als Antippen (Aufnahme verwerfen, Hinweis).
  Loslassen wird um :data:`RELEASE_DEBOUNCE_MS` verzögert ausgewertet: Ohne
  „detectable autorepeat“ liefert X11 bei gehaltener Taste Paare aus Loslassen
  und Drücken – ein Druck innerhalb dieser Frist gilt als Wiederholung.
* :func:`attach_voice_hold` – GTK4-Anbindung (``Gtk.EventControllerKey`` in der
  Capture-Phase, damit kein Eingabefeld Strg+M schluckt).
* :class:`GlobalHold` – systemweites Kürzel (``zeit voice`` über die Kürzel der
  Arbeitsumgebung, das nur beim Drücken feuert): unter X11 wird nach dem Start
  alle :data:`POLL_MS` ms per ``XQueryKeymap`` (libX11 über ctypes, nur lesend)
  geprüft, ob die Tasten des eingerichteten Kürzels noch gedrückt sind.
  Unter Wayland gibt es keinen Zugriff auf den Tastenzustand → Umschalten.

Datenschutz: ``XQueryKeymap`` liefert den Zustand aller Tasten; ausgewertet
werden nur die Tasten des Kürzels, nur während einer laufenden Aufnahme, nichts
wird gespeichert oder protokolliert.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import time
from collections.abc import Callable, Mapping
from typing import Any

from ..logutil import log_failure
from .integration import parse_accel

log = logging.getLogger(__name__)

TAP_THRESHOLD_S = 0.3      # kürzer gedrückt = Antippen → Aufnahme verwerfen
RELEASE_DEBOUNCE_MS = 40   # Loslassen+Drücken innerhalb dieser Frist = Tastenwiederholung
POLL_MS = 30               # Abfrageintervall des Tastenzustands (systemweit, X11)
# Obergrenze der Tastenabfrage: spätestens nach der längsten zulässigen
# Aufnahme (config.MAX_RECORD_SECONDS = 120 s) + Reserve endet das Abfragen, auch
# wenn ein Pfad das Stoppen verpasst – kein dauerhaftes Mitlesen des Tastenzustands.
MAX_HOLD_S = 125.0

TAP_HINT = "Taste gedrückt halten, solange du sprichst"

Schedule = Callable[[int, Callable[[], None]], Any]
Unschedule = Callable[[Any], None]


# ---------------------------------------------------------------------------
# Strg+M im Fenster
# ---------------------------------------------------------------------------
class HoldKey:
    """Halten-Logik einer Taste (rein, Zeitstempel in Sekunden von außen).

    ``begin()`` → ``True``, wenn eine Aufnahme gestartet wurde; nur dann folgt
    beim Loslassen ``end(short)``. ``schedule``/``unschedule`` verzögern das
    Loslassen (``None`` = sofort auswerten, z. B. in Tests).
    """

    def __init__(self, begin: Callable[[], bool], end: Callable[[bool], None], *,
                 tap_s: float = TAP_THRESHOLD_S, debounce_ms: int = RELEASE_DEBOUNCE_MS,
                 schedule: Schedule | None = None, unschedule: Unschedule | None = None) -> None:
        self._begin, self._end = begin, end
        self.tap_s = tap_s
        self.debounce_ms = debounce_ms
        self._schedule, self._unschedule = schedule, unschedule
        self.held = False
        self.active = False      # Aufnahme läuft wegen dieses Tastendrucks
        self._t0 = 0.0
        self._pending: Any = None

    def press(self, t: float) -> None:
        if self._pending is not None:  # Loslassen+Drücken = Wiederholung (X11 ohne XKB-Erkennung)
            if self._unschedule is not None:
                self._unschedule(self._pending)
            self._pending = None
            return
        if self.held:  # Tastenwiederholung: nichts tun
            return
        self.held = True
        self._t0 = t
        self.active = bool(self._begin())

    def release(self, t: float) -> None:
        if not self.held or self._pending is not None:
            return
        if self._schedule is None or self.debounce_ms <= 0:
            self._finish(t)
            return
        self._pending = self._schedule(self.debounce_ms, lambda: self._finish(t))

    def cancel_hold(self) -> None:
        """Fenster verliert den Fokus: das Loslassen käme nie an → jetzt beenden
        (nicht als Antippen werten)."""
        if self._pending is not None and self._unschedule is not None:
            self._unschedule(self._pending)
        self._pending = None
        if self.held:
            self.held = False
            if self.active:
                self.active = False
                self._end(False)

    def _finish(self, t: float) -> None:
        self._pending = None
        if not self.held:
            return
        self.held = False
        if self.active:
            self.active = False
            self._end(t - self._t0 < self.tap_s)


def attach_voice_hold(window, begin: Callable[[], bool],
                      end: Callable[[bool], None]) -> HoldKey:
    """Strg+M (gedrückt halten) an ``window`` binden; liefert die :class:`HoldKey`."""
    import gi  # noqa: PLC0415

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, GLib, Gtk  # noqa: PLC0415

    hold = HoldKey(begin, end,
                   schedule=lambda ms, fn: GLib.timeout_add(ms, lambda: (fn(), False)[1]),
                   unschedule=GLib.source_remove)
    other_mods = (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.ALT_MASK
                  | Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.META_MASK
                  | Gdk.ModifierType.HYPER_MASK)
    m_codes: set[int] | None = None

    def is_m(keyval: int, keycode: int) -> bool:
        nonlocal m_codes
        if keyval in (Gdk.KEY_m, Gdk.KEY_M):
            return True
        if m_codes is None:  # andere Tastaturbelegungen (z. B. Kyrillisch): Taste mit „m“
            ok, keys = window.get_display().map_keyval(Gdk.KEY_m)
            m_codes = {k.keycode for k in keys} if ok and keys else set()
        return keycode in m_codes

    def stamp(ctrl) -> float:
        t = ctrl.get_current_event_time()
        if t:
            return t / 1000.0
        return GLib.get_monotonic_time() / 1e6

    def on_pressed(ctrl, keyval, keycode, state) -> bool:
        if hold.held and is_m(keyval, keycode):
            hold.press(stamp(ctrl))  # Wiederholung schlucken
            return True
        if (is_m(keyval, keycode) and state & Gdk.ModifierType.CONTROL_MASK
                and not state & other_mods):
            hold.press(stamp(ctrl))
            return True
        return False

    def on_released(ctrl, keyval, keycode, _state) -> None:
        if hold.held and (is_m(keyval, keycode)
                          or keyval in (Gdk.KEY_Control_L, Gdk.KEY_Control_R)):
            hold.release(stamp(ctrl))

    keys = Gtk.EventControllerKey()
    keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    keys.connect("key-pressed", on_pressed)
    keys.connect("key-released", on_released)
    window.add_controller(keys)
    window.connect("notify::is-active",
                   lambda w, _p: hold.cancel_hold() if not w.is_active() else None)
    window.voice_hold = hold  # für Tests
    return hold


# ---------------------------------------------------------------------------
# Systemweites Kürzel (X11)
# ---------------------------------------------------------------------------
def can_watch_keys(env: Mapping[str, str] | None = None) -> bool:
    """Tastenzustand abfragbar? Nur in einer X11-Sitzung (unter Wayland liefert
    XWayland den Zustand nur, solange ein X-Fenster den Fokus hat)."""
    env = os.environ if env is None else env
    session = (env.get("XDG_SESSION_TYPE") or "").lower()
    if session == "wayland" or (env.get("WAYLAND_DISPLAY") and session != "x11"):
        return False
    return bool(env.get("DISPLAY"))


_MOD_KEYSYMS = {
    "control": ("Control_L", "Control_R"),
    "shift": ("Shift_L", "Shift_R"),
    "alt": ("Alt_L", "Alt_R"),
    "super": ("Super_L", "Super_R"),
    "meta": ("Meta_L", "Meta_R"),
    "hyper": ("Hyper_L", "Hyper_R"),
}


class X11Keyboard:
    """Nur lesender Zugriff auf den Tastenzustand über libX11 (ctypes).

    Eigene Display-Verbindung (unabhängig von GTK). Kein Grab, keine Eingaben."""

    def __init__(self, lib: Any, display: Any) -> None:
        self._lib = lib
        self._dpy = display

    @classmethod
    def open(cls, display_name: str | None = None) -> "X11Keyboard | None":
        path = ctypes.util.find_library("X11")
        if not path:
            return None
        try:
            lib = ctypes.CDLL(path)
            lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
            lib.XOpenDisplay.restype = ctypes.c_void_p
            lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
            lib.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.c_char * 32]
            lib.XStringToKeysym.argtypes = [ctypes.c_char_p]
            lib.XStringToKeysym.restype = ctypes.c_ulong
            lib.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            lib.XKeysymToKeycode.restype = ctypes.c_ubyte
            dpy = lib.XOpenDisplay(display_name.encode() if display_name else None)
        except (OSError, AttributeError) as exc:
            log_failure(log, "libX11 nicht nutzbar", exc, level=logging.INFO)
            return None
        if not dpy:
            return None
        return cls(lib, dpy)

    def _keycode(self, name: str) -> int:
        for cand in dict.fromkeys((name, name.lower(), name.upper(), name.capitalize())):
            sym = self._lib.XStringToKeysym(cand.encode("ascii", "ignore"))
            if sym:
                code = int(self._lib.XKeysymToKeycode(self._dpy, sym))
                if code:
                    return code
        return 0

    def keycode_groups(self, accel: str) -> list[frozenset[int]] | None:
        """Je Taste des Kürzels die möglichen Keycodes (links/rechts bei Modifikatoren)."""
        parsed = parse_accel(accel)
        if parsed is None:
            return None
        mods, key = parsed
        groups = []
        for m in mods:
            codes = frozenset(c for c in (self._keycode(n) for n in _MOD_KEYSYMS.get(m, ())) if c)
            if not codes:
                return None
            groups.append(codes)
        code = self._keycode(key)
        if not code:
            return None
        groups.append(frozenset({code}))
        return groups

    def pressed(self, groups: list[frozenset[int]]) -> bool:
        """Sind alle Tasten des Kürzels gedrückt (je Gruppe mindestens eine)?"""
        buf = (ctypes.c_char * 32)()
        self._lib.XQueryKeymap(self._dpy, buf)
        raw = bytes(buf)
        return all(any(raw[c // 8] & (1 << (c % 8)) for c in g) for g in groups)

    def close(self) -> None:
        if self._dpy:
            self._lib.XCloseDisplay(self._dpy)
            self._dpy = None


class GlobalHold:
    """Halten-Semantik für das systemweite Sprach-Kürzel.

    :meth:`start` prüft beim Eintreffen des Kürzels, ob seine Tasten gedrückt sind:

    * ``"hold"`` – ja: Abfrage alle :data:`POLL_MS` ms, beim Loslassen
      ``on_release(short)``; ``short`` = kürzer als :data:`TAP_THRESHOLD_S` seit dem
      Start (dann wie Antippen behandeln: Umschalten).
    * ``"tap"`` – nein (schon losgelassen, Aufruf aus Terminal oder Tray):
      Umschalten wie bisher, der nächste Aufruf beendet die Aufnahme.
    * ``"unsupported"`` – Wayland, kein libX11, Kürzel unbekannt → Umschalten.

    ``schedule(ms, fn)`` muss ``fn`` wiederholt aufrufen, solange es ``True`` liefert
    (``GLib.timeout_add``).
    """

    def __init__(self, on_release: Callable[[bool], None], *,
                 keyboard_factory: Callable[[], Any] | None = None,
                 schedule: Schedule | None = None, unschedule: Unschedule | None = None,
                 env: Mapping[str, str] | None = None, poll_ms: int = POLL_MS,
                 tap_s: float = TAP_THRESHOLD_S, max_s: float = MAX_HOLD_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.on_release = on_release
        self.tap_s = tap_s
        self.max_s = max_s
        self._clock = clock
        self._t0 = 0.0
        self._factory = keyboard_factory or X11Keyboard.open
        self._schedule, self._unschedule = schedule, unschedule
        self._env = env
        self.poll_ms = poll_ms
        self._kbd: Any = None
        self._groups: list[frozenset[int]] | None = None
        self._source: Any = None

    @property
    def active(self) -> bool:
        return self._source is not None

    def supported(self) -> bool:
        return can_watch_keys(self._env)

    def _keyboard(self) -> Any:
        if self._kbd is None:
            self._kbd = self._factory()
        return self._kbd

    def start(self, accel: str | None) -> str:
        self.stop()
        if not accel or not self.supported() or self._schedule is None:
            return "unsupported"
        try:
            kbd = self._keyboard()
            groups = kbd.keycode_groups(accel) if kbd is not None else None
            if not groups:
                return "unsupported"
            if not kbd.pressed(groups):
                return "tap"
        except Exception as exc:  # noqa: BLE001 – nie die Aufnahme verhindern
            log_failure(log, "Tastenzustand nicht lesbar", exc, level=logging.INFO)
            return "unsupported"
        self._groups = groups
        self._t0 = self._clock()
        self._source = self._schedule(self.poll_ms, self._poll)
        return "hold"

    def _poll(self) -> bool:
        try:
            held = self._kbd.pressed(self._groups)
        except Exception as exc:  # noqa: BLE001
            log_failure(log, "Tastenzustand nicht lesbar", exc, level=logging.INFO)
            held = False
        if held and self._clock() - self._t0 <= self.max_s:
            return True  # weiter abfragen
        self._source = None
        self.on_release(self._clock() - self._t0 < self.tap_s)
        return False

    def stop(self) -> None:
        if self._source is not None and self._unschedule is not None:
            self._unschedule(self._source)
        self._source = None

    def close(self) -> None:
        self.stop()
        if self._kbd is not None:
            try:
                self._kbd.close()
            finally:
                self._kbd = None
