"""GUI-freie Tray-Logik: aus dem Statusobjekt Icon, Label und Menüzustand ableiten."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from ..ui.integration import ActiveHotkeys
from ..ui.viewmodel import TaskView, format_tray_label, shorten, task_view_from_status

# Menüzeile/Tooltip: lange Kundennamen kürzen, sonst wird das Menü breiter als der
# Bildschirm (zulässig sind bis zu 500 Zeichen ohne Leerzeichen).
MENU_TITLE_CHARS = 40
TOOLTIP_TITLE_CHARS = 60

# Mikrofon im Tray: Klick startet, nächster Klick beendet (Halten ist im Menü nicht möglich)
VOICE_START = "Sprachbefehl aufnehmen"
VOICE_STOP = "Aufnahme beenden"
SHORTCUTS_ITEM = "Tastenkürzel …"
SHORTCUTS_SETUP_ITEM = "Systemweite Tastenkürzel einrichten …"


@dataclass(frozen=True)
class TrayState:
    icon: str
    icon_desc: str
    label: str
    status_line: str
    show_start: bool
    pause_label: str
    pause_enabled: bool
    stop_label: str
    stop_enabled: bool
    voice_label: str
    tooltip: str = "Tracksy"
    toggle_accel: str | None = None   # GTK-Accelerator neben „Task starten/Stoppen“
    voice_accel: str | None = None    # neben dem Mikrofon-Eintrag
    shortcuts_label: str = SHORTCUTS_ITEM


ENV_TRAY_BACKEND = "ZEIT_TRAY_BACKEND"


def choose_backend(env: Mapping[str, str], xapp_available: bool,
                   xapp_monitored: bool) -> str:
    """``"xapp"`` oder ``"appindicator"``.

    Cinnamon zeigt das AppIndicator-Label nicht an (xapp-sn-watcher übersetzt nur
    Symbol, Menü und Tooltip/Titel). Ein natives ``XApp.StatusIcon`` hat dagegen ein
    sichtbares Panel-Label. ``auto``: XApp, wenn Cinnamon läuft, die Bibliothek
    vorhanden ist und ein Status-Applet lauscht; sonst AppIndicator.
    ``ZEIT_TRAY_BACKEND=xapp|appindicator`` erzwingt eine Variante.
    """
    choice = (env.get(ENV_TRAY_BACKEND) or "auto").strip().lower()
    if choice == "appindicator" or not xapp_available:
        return "appindicator"
    if choice == "xapp":
        return "xapp"
    desk = (env.get("XDG_CURRENT_DESKTOP") or "") + ":" + (env.get("DESKTOP_SESSION") or "")
    return "xapp" if "cinnamon" in desk.lower() and xapp_monitored else "appindicator"


def hotkeys_from(status: dict[str, Any] | None,
                 fallback: ActiveHotkeys | None = None) -> ActiveHotkeys | None:
    """Kürzel aus dem StatusChanged-Signal der App, sonst der selbst gelesene Stand."""
    hk = ActiveHotkeys.from_status((status or {}).get("hotkeys"))
    return hk if hk is not None else fallback


def tray_state(status: dict[str, Any] | None, now: datetime | None = None,
               connected: bool = True, hotkeys: ActiveHotkeys | None = None) -> TrayState:
    shortcuts = (SHORTCUTS_SETUP_ITEM if hotkeys is not None and hotkeys.known
                 and not hotkeys.any else SHORTCUTS_ITEM)
    toggle_accel = hotkeys.toggle if hotkeys is not None else None
    voice_accel = hotkeys.voice if hotkeys is not None else None
    if not connected:
        return TrayState("zeit-idle", "Tracksy nicht verbunden", "",
                         "Tracksy nicht erreichbar", False, "Pause", False,
                         "Stoppen …", False, VOICE_START,
                         "Tracksy – nicht verbunden", toggle_accel, voice_accel, shortcuts)
    v: TaskView = task_view_from_status(status)
    elapsed = v.elapsed(now)
    recording = v.voice == "listening"
    if v.active:
        label = format_tray_label(elapsed)
        if v.state == "paused":
            label += " ⏸"
        # Zeit vorn: bleibt auch bei langen Kundennamen sichtbar
        line = f"{format_tray_label(elapsed)} h – {shorten(v.title, MENU_TITLE_CHARS)} ({v.state_label})"
        tooltip = (f"Tracksy – {format_tray_label(elapsed)} h – "
                   f"{shorten(v.title, TOOLTIP_TITLE_CHARS)} ({v.state_label})")
    else:
        label = ""
        line = "Kein Task aktiv"
        tooltip = "Tracksy – kein Task aktiv"
    if recording:
        line = "Aufnahme läuft … " + ("" if not v.active else
                                       f"({shorten(v.title, MENU_TITLE_CHARS)})")
        tooltip = "Tracksy – Aufnahme läuft" + (
            f" ({format_tray_label(elapsed)} h)" if v.active else "")
    return TrayState(
        icon=v.tray_icon,
        icon_desc=f"Tracksy: {'Aufnahme' if recording else v.state_label}",
        label=label,
        status_line=line.strip(),
        show_start=not v.active,
        pause_label="Weiter" if v.state == "paused" else "Pause",
        pause_enabled=v.state in ("running", "paused"),
        stop_label="Abschließen …" if v.state == "stopping" else "Stoppen …",
        stop_enabled=v.active,
        voice_label=VOICE_STOP if recording else VOICE_START,
        tooltip=tooltip,
        toggle_accel=toggle_accel,
        voice_accel=voice_accel,
        shortcuts_label=shortcuts,
    )
