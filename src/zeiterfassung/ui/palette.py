"""Feste Tracksy-Palette (Akzent, Zustands-, Fehler-/Warnfarben) auf allen Arbeitsumgebungen.

Warum eigene Palette: Linux Mint patcht libadwaita so, dass es das System-Theme
(Mint-Y) lädt, Ubuntu 24.04 färbt den Akzent per Patch nach Yaru (Orange), ab
libadwaita 1.6 kommt der Akzent aus dem Portal, und KDE schreibt Farben nach
``~/.config/gtk-4.0/colors.css``. Ohne eigene Palette sähe Tracksy auf jedem
Desktop anders aus. Flächen und Hell/Dunkel folgen weiter dem System.

Die Farben stehen nur in ``resources/palette-*.css`` (keine Farben im Code).
Je Schema eine Datei; beim Wechsel Hell ⇄ Dunkel (``Adw.StyleManager:dark``)
wird der Provider getauscht. Priorität ``USER + 1``: die Dateien enthalten nur
Farbdefinitionen und überstimmen damit Theme- und Desktop-Farben, aber keine
Regeln des Nutzers.
"""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

RESOURCES = Path(__file__).resolve().parents[1] / "resources"
PALETTE_PRIORITY = Gtk.STYLE_PROVIDER_PRIORITY_USER + 1


def uses_css_variables(version: tuple[int, int] | None = None) -> bool:
    """libadwaita >= 1.6 stylt über CSS-Variablen (``--accent-bg-color``)."""
    if version is None:
        version = (Adw.get_major_version(), Adw.get_minor_version())
    return tuple(version) >= (1, 6)


def palette_files(dark: bool, version: tuple[int, int] | None = None) -> list[Path]:
    """CSS-Dateien der Palette für das Schema (benannte Farben, ab 1.6 + Variablen)."""
    scheme = "dark" if dark else "light"
    files = [RESOURCES / f"palette-{scheme}.css"]
    if uses_css_variables(version):
        files.append(RESOURCES / f"palette-vars-{scheme}.css")
    return files


class PaletteLoader:
    """Hält die Palette-Provider eines Displays und tauscht sie bei Hell/Dunkel."""

    def __init__(self, display, style_manager=None) -> None:
        self.display = display
        self.style_manager = style_manager or Adw.StyleManager.get_default()
        self.providers: list[Gtk.CssProvider] = []
        self.dark: bool | None = None
        self._handler = self.style_manager.connect("notify::dark", lambda *_: self.apply())
        self.apply()

    def apply(self) -> None:
        dark = bool(self.style_manager.get_dark())
        if dark == self.dark and self.providers:
            return
        new = []
        for path in palette_files(dark):
            p = Gtk.CssProvider()
            p.load_from_path(str(path))
            Gtk.StyleContext.add_provider_for_display(self.display, p, PALETTE_PRIORITY)
            new.append(p)
        # erst die neuen hinzufügen, dann die alten entfernen: kein Aufblitzen der Systemfarben
        for p in self.providers:
            Gtk.StyleContext.remove_provider_for_display(self.display, p)
        self.providers = new
        self.dark = dark
