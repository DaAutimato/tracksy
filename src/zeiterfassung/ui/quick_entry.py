"""Schnellerfassung: kleines Fenster, Fokus auf „Kunde“, Enter startet."""

from __future__ import annotations

from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .keyhold import attach_voice_hold  # noqa: E402
from .main_window import StartForm  # noqa: E402
from .widgets import MicButton, a11y  # noqa: E402

VOICE_HINT = "Strg+M halten und sagen: „Starte für Kunde … Thema …“"

if TYPE_CHECKING:  # pragma: no cover
    from ..app import ZeitApp


class QuickEntryWindow(Adw.ApplicationWindow):
    def __init__(self, app: "ZeitApp") -> None:
        super().__init__(application=app, title="Schnellerfassung")
        self.app = app
        self.set_default_size(440, -1)
        self.set_resizable(False)
        self.toasts = Adw.ToastOverlay()
        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.add_css_class("flat")
        tv.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_start(18)
        box.set_margin_end(18)
        box.set_margin_bottom(18)
        self.form = StartForm(app.customers, self._submit,
                              app.config.general.default_location, title="")
        self.hint = Gtk.Label(xalign=0, wrap=True)
        self.hint.add_css_class("dim-label")
        self.hint.add_css_class("caption")
        box.append(self.form)
        box.append(self.hint)

        row = Gtk.Box(spacing=12)
        self.mic = MicButton(lambda: app.voice.begin("command"), app.voice.end,
                             lambda: app.voice.state, small=True)
        row.append(self.mic)
        self.mic_label = Gtk.Label(label=VOICE_HINT, xalign=0, hexpand=True, wrap=True)
        self.mic_label.add_css_class("mic-hint")
        row.append(self.mic_label)
        start = Gtk.Button(label="Starten")
        start.add_css_class("suggested-action")
        start.add_css_class("pill")
        a11y(start, "Task starten", "Task starten (Enter)")
        start.connect("clicked", lambda *_: self.form.submit())
        row.append(start)
        box.append(row)
        tv.set_content(box)
        self.toasts.set_child(tv)
        self.set_content(self.toasts)

        sc = Gtk.ShortcutController()
        sc.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("Escape"),
                                         Gtk.CallbackAction.new(lambda *_: (self.close(), True)[1])))
        self.add_controller(sc)
        attach_voice_hold(self, app.voice_key_begin, app.voice_key_end)
        self.connect("close-request", self._on_close)

    def _on_close(self, *_a) -> bool:
        self.app.quick_entry_closed(self)
        return False

    def prepare(self) -> None:
        st = self.app.service.status()
        if st["state"] != "idle":
            self.hint.set_label("Ein Task läuft – Starten schließt ihn vorher ab.")
            self.hint.set_visible(True)
        else:
            self.hint.set_visible(False)
        self.form.customer.grab_focus()

    def _submit(self, values: dict) -> None:
        if self.app.start_task(values, source="hotkey", parent=self):
            self.close()

    def set_voice_state(self, state: str, available: bool) -> None:
        self.mic.set_voice_state(state, available)
        if state == "listening":
            self.mic_label.set_label("Aufnahme läuft …")
            self.mic_label.add_css_class("recording")
        else:
            self.mic_label.remove_css_class("recording")
            self.mic_label.set_label("Wird erkannt …" if state == "transcribing"
                                     else VOICE_HINT)
