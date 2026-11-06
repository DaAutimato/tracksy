"""Übersicht aller Tastenkürzel (Menü „Tastenkürzel“, Strg+?).

Oben die systemweiten Kürzel – sie sind das, was man im Hintergrundbetrieb nicht
sieht –, mit tatsächlicher Belegung aus den Einstellungen der Arbeitsumgebung
(``integration.read_active_hotkeys``) und einem Weg zur Einrichtung. Darunter die
Kürzel im Fenster. ``Adw.Dialog`` statt ``GtkShortcutsWindow`` (ab GTK 4.18
veraltet, kann keine Knöpfe enthalten).
"""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .integration import ActiveHotkeys  # noqa: E402
from .widgets import Keycaps, a11y  # noqa: E402

HOLD = "Gedrückt halten, solange du sprichst"
TOGGLE_WAYLAND = "Drücken startet, erneut drücken beendet (Wayland)"
NOT_SET = "nicht eingerichtet"

# (Titel, Accelerator, Untertitel) – Kürzel im Fenster (app.py ACCELS, Stopp-Dialog)
WINDOW_SHORTCUTS: tuple[tuple[str, tuple[tuple[str, str, str | None], ...]], ...] = (
    ("Im Fenster", (
        ("Sprachbefehl", "<Control>m", HOLD),
        ("Task starten / stoppen", "<Control>s", None),
        ("Pause / Weiter", "<Control>p", None),
        ("Zum Startformular", "<Control>l", None),
    )),
    ("Task abschließen", (
        ("Speichern", "<Control>Return", None),
        ("Weiterlaufen lassen", "Escape", None),
        ("Ergänzung diktieren", "<Control>m", HOLD),
    )),
    ("Allgemein", (
        ("Erfassen", "<Control>1", None),
        ("Übersicht", "<Control>2", None),
        ("Einstellungen", "<Control>comma", None),
        ("Tastenkürzel", "<Control>question", None),
        ("Fenster schließen (Tracksy läuft weiter)", "<Control>w", None),
        ("Beenden", "<Control>q", None),
    )),
)


def global_hint(hk: ActiveHotkeys | None, hold_supported: bool) -> str:
    """Beschreibung der Gruppe „Überall“ je nach Stand."""
    if hk is None:
        return "Wird gelesen …"
    if not hk.known:
        return ("Tracksy kann die Kürzel dieser Arbeitsumgebung nicht auslesen. "
                "Anleitung und Befehle: Einstellungen → System.")
    if not hk.any:
        return ("Nicht eingerichtet – ohne sie wirken Tastenkürzel nur, solange das "
                "Tracksy-Fenster aktiv ist.")
    if hold_supported:
        return "Wirken auch, wenn Tracksy im Hintergrund läuft."
    return "Wirken auch im Hintergrund. Unter Wayland: Sprachkürzel drücken = Start/Stopp."


def _shortcut_row(title: str, accel: str | None, subtitle: str | None = None) -> Adw.ActionRow:
    row = Adw.ActionRow(title=GLib.markup_escape_text(title))
    if subtitle:
        row.set_subtitle(GLib.markup_escape_text(subtitle))
    if accel:
        row.add_suffix(Keycaps(accel))
    else:
        lbl = Gtk.Label(label=NOT_SET, valign=Gtk.Align.CENTER)
        lbl.add_css_class("dim-label")
        row.add_suffix(lbl)
    row.accel = accel  # type: ignore[attr-defined]  # für Tests
    return row


class ShortcutsDialog(Adw.Dialog):
    def __init__(self, hotkeys: ActiveHotkeys | None, hold_supported: bool,
                 open_settings: Callable[[], None]) -> None:
        super().__init__(title="Tastenkürzel")
        self.set_content_width(480)
        self.set_content_height(680)
        self._open_settings = open_settings
        self.hold_supported = hold_supported
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        self.page = Adw.PreferencesPage()
        self.global_group = Adw.PreferencesGroup(title="Überall")
        self.global_group.set_name("shortcuts-global")
        self.setup_btn = Gtk.Button(label="Einrichten …", valign=Gtk.Align.CENTER)
        self.setup_btn.add_css_class("flat")
        self.setup_btn.connect("clicked", lambda *_: self._settings())
        self.global_group.set_header_suffix(self.setup_btn)
        self.page.add(self.global_group)
        self._global_rows: list[Gtk.Widget] = []
        for title, items in WINDOW_SHORTCUTS:
            g = Adw.PreferencesGroup(title=title)
            for t, accel, sub in items:
                g.add(_shortcut_row(t, accel, sub))
            self.page.add(g)
        tv.set_content(self.page)
        self.set_child(tv)
        self.set_hotkeys(hotkeys)

    def _settings(self) -> None:
        self.close()
        self._open_settings()

    def set_hotkeys(self, hk: ActiveHotkeys | None) -> None:
        for r in self._global_rows:
            self.global_group.remove(r)
        self._global_rows = []
        self.hotkeys = hk
        self.global_group.set_description(GLib.markup_escape_text(
            global_hint(hk, self.hold_supported)))
        if hk is not None and hk.known:
            voice_sub = HOLD if self.hold_supported else TOGGLE_WAYLAND
            for title, accel, sub in (("Sprachbefehl", hk.voice, voice_sub if hk.voice else None),
                                      ("Task starten / stoppen", hk.toggle, None)):
                row = _shortcut_row(title, accel, sub)
                self.global_group.add(row)
                self._global_rows.append(row)
        installed = hk is not None and hk.known and hk.any
        label = "Ändern …" if installed else ("Anleitung …" if hk is not None and not hk.known
                                              else "Einrichten …")
        self.setup_btn.set_label(label)
        a11y(self.setup_btn, "Systemweite Tastenkürzel " + (
            "ändern" if installed else "einrichten"),
            "Einstellungen → System öffnen")
        self.setup_btn.set_visible(hk is not None)
