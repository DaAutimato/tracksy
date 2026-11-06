"""Hauptfenster: Erfassen (Hero, Mikrofon, Startformular) und Übersicht."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from ..core.sanitize import MAX_FIELD_LEN  # noqa: E402
from .overview import OverviewPage  # noqa: E402
from .viewmodel import (  # noqa: E402
    TaskView,
    describe_quanta,
    format_clock,
    quanta,
    shorten,
    task_view_from_status,
)
from .keyhold import attach_voice_hold  # noqa: E402
from .widgets import (  # noqa: E402
    KeyHint,
    LocationToggle,
    MicButton,
    QuantaBar,
    SuggestEntryRow,
    a11y,
    limit_length,
    set_state_class,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..app import ZeitApp


class StartForm(Adw.PreferencesGroup):
    """Kunde/Thema/Ticket/Ort + Start-Button. Wird auch in der Schnellerfassung genutzt."""

    def __init__(self, customers: Callable[[], list[str]], on_submit: Callable[[dict], None],
                 default_location: str = "remote", title: str = "Neuer Task") -> None:
        super().__init__(title=title)
        self._on_submit = on_submit
        self.customer = SuggestEntryRow("Kunde", customers)
        self.topic = Adw.EntryRow(title="Thema")
        self.ticket = Adw.EntryRow(title="Ticket")
        a11y(self.topic, "Thema", False)
        a11y(self.ticket, "Ticket", False)
        self.ticket.set_input_purpose(Gtk.InputPurpose.FREE_FORM)
        for row in (self.topic, self.ticket):
            limit_length(row, MAX_FIELD_LEN)
        for row in (self.customer, self.topic, self.ticket):
            row.connect("entry-activated", lambda *_: self.submit())
            self.add(row)
        self.location = LocationToggle(default_location)
        loc_row = Adw.ActionRow(title="Ort")
        loc_row.add_suffix(self.location)
        self.add(loc_row)

    def values(self) -> dict[str, Any]:
        return {
            "customer": self.customer.get_text().strip(),
            "topic": self.topic.get_text().strip(),
            "ticket": self.ticket.get_text().strip(),
            "location": self.location.get_value(),
        }

    def set_values(self, customer: str = "", topic: str = "", ticket: str = "",
                   location: str | None = None) -> None:
        self.customer.set_text_silent(customer or "")
        self.topic.set_text(topic or "")
        self.ticket.set_text(ticket or "")
        if location:
            self.location.set_value(location)

    def clear(self, default_location: str = "remote") -> None:
        self.set_values("", "", "", default_location)

    def submit(self) -> None:
        self._on_submit(self.values())


# Lange Texte: Zeilenbegrenzung im Hero, Tooltip ab dieser Länge, Toast kürzen
TITLE_LINES = 2
SUBTITLE_LINES = 3
LONG_TEXT_TOOLTIP = 60
TOAST_CHARS = 140

MIC_HINT_IDLE = "Gedrückt halten und sprechen"
WINDOW_VOICE_ACCEL = "<Control>m"
SETUP_HOTKEYS_LABEL = "Systemweite Kürzel einrichten …"

# Höhe des Streifens, den Toasts am unteren Rand belegen (Toast + Außenabstand).
TOAST_SPACE = 64
# Unterhalb dieser Breiten: Übersicht-Leiste zweizeilig, Export nur als Symbol;
# sehr schmal zusätzlich kurzes Datum. Bei mehreren passenden gilt der zuletzt
# hinzugefügte Breakpoint.
COMPACT_BREAKPOINT = "max-width: 720sp"
NARROW_BREAKPOINT = "max-width: 440sp"


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: "ZeitApp") -> None:
        super().__init__(application=app, title="Tracksy")
        self.app = app
        self.set_default_size(560, 820)
        self.set_size_request(360, 480)
        self._view = TaskView()
        self._tick_id = 0

        # Toasts überlagern nie Bedienelemente: Das ToastOverlay liegt in einem eigenen
        # Streifen unter dem Inhalt, der nur aufklappt, solange ein Toast sichtbar ist.
        # Der Inhalt (scrollbar) wird dabei kürzer statt verdeckt.
        self.toasts = Adw.ToastOverlay(vexpand=False)
        self._toast_count = 0
        space = Gtk.Box()
        space.set_size_request(-1, TOAST_SPACE)
        self.toast_space = Gtk.Revealer(child=space, reveal_child=False,
                                        transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
                                        transition_duration=150)
        self.toasts.set_child(self.toast_space)
        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        self.switcher = switcher = Adw.ViewSwitcher(stack=self.stack,
                                                    policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        # Logo = flacher Knopf zum Über-Dialog (Fläche nur bei Hover/Fokus sichtbar)
        logo_img = Gtk.Image(icon_name="de.local.Zeiterfassung", pixel_size=24)
        logo_img.set_accessible_role(Gtk.AccessibleRole.PRESENTATION)
        self.logo_btn = Gtk.Button(child=logo_img, action_name="app.about")
        self.logo_btn.add_css_class("flat")
        self.logo_btn.add_css_class("header-logo")
        self.logo_btn.set_valign(Gtk.Align.CENTER)
        a11y(self.logo_btn, "Über Tracksy")
        header.pack_start(self.logo_btn)

        menu = Gio.Menu()
        sec1 = Gio.Menu()
        sec1.append("Schnellerfassung", "app.quick-entry")
        sec1.append("Eintrag manuell anlegen", "win.new-entry")
        sec1.append("Exportieren …", "win.export")
        menu.append_section(None, sec1)
        sec2 = Gio.Menu()
        setup = Gio.MenuItem.new("Einrichtung starten", "app.first-run")
        # nur sichtbar, solange die Einrichtung offen ist und kein Modell geladen wurde
        setup.set_attribute_value("hidden-when", GLib.Variant.new_string("action-disabled"))
        sec2.append_item(setup)
        sec2.append("Einstellungen", "app.preferences")
        sec2.append("Tastenkürzel", "app.shortcuts")
        sec2.append("Releases", "app.releases")
        sec2.append("Über Tracksy", "app.about")
        menu.append_section(None, sec2)
        sec3 = Gio.Menu()
        sec3.append("Fenster schließen", "win.hide")
        sec3.append("Beenden", "app.quit-confirm")
        menu.append_section(None, sec3)
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                  primary=True)
        a11y(menu_btn, "Hauptmenü")
        header.pack_end(menu_btn)
        tv.add_top_bar(header)

        self.track_page = self._build_track_page()
        p1 = self.stack.add_titled_with_icon(self.track_page, "track", "Erfassen",
                                             "alarm-symbolic")
        self.overview = OverviewPage(app, self)
        p2 = self.stack.add_titled_with_icon(self.overview, "overview", "Übersicht",
                                             "x-office-calendar-symbolic")
        p1.set_use_underline(False)
        p2.set_use_underline(False)
        self.stack.connect("notify::visible-child-name", self._on_page_changed)
        self.stack.set_vexpand(True)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.stack)
        body.append(self.toasts)
        tv.set_content(body)
        self.set_content(tv)

        # Schmale Fenster: Übersicht-Leiste umbrechen statt Mindestbreite zu erhöhen
        for level, cond in ((1, COMPACT_BREAKPOINT), (2, NARROW_BREAKPOINT)):
            bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(cond))
            bp.connect("apply", lambda _b, lv=level: self.overview.set_compact(lv))
            bp.connect("unapply", lambda *_: self.overview.set_compact(0))
            if level == 2:  # Tabs: Symbol über Text, damit „Übersicht“ nicht abgeschnitten wird
                val = GObject.Value(Adw.ViewSwitcherPolicy, Adw.ViewSwitcherPolicy.NARROW)
                bp.add_setter(switcher, "policy", val)
            self.add_breakpoint(bp)

        self._install_actions()
        # Strg+M gedrückt halten = Sprachbefehl (bzw. Diktat im Abschlussdialog)
        attach_voice_hold(self, app.voice_key_begin, app.voice_key_end)
        self._hotkeys = None
        self.set_hotkeys(getattr(app, "hotkeys", None), app.hold_supported()
                         if hasattr(app, "hold_supported") else True)
        self.connect("close-request", self._on_close_request)
        # Mikrofon nie als Standardfokus (Datenschutz): Enter/Leertaste nach dem
        # Öffnen würden sonst unbemerkt eine Aufnahme starten.
        self._mapped_once = False
        self.connect("map", self._on_map)
        self.connect("notify::is-active", self._on_active_changed)

    # ------------------------------------------------------------------ Aufbau
    def _build_track_page(self) -> Gtk.Widget:
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=560, tightening_threshold=420)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        outer.set_margin_top(18)
        outer.set_margin_bottom(24)
        outer.set_margin_start(16)
        outer.set_margin_end(16)

        # --- Hero
        self.hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.hero.add_css_class("card")
        self.hero.add_css_class("hero")
        self.hero.add_css_class("state-idle")

        top = Gtk.Box(spacing=10)
        self.pill = Gtk.Box(spacing=7)
        self.pill.add_css_class("status-pill")
        self.pill.add_css_class("state-idle")
        self.pill.set_valign(Gtk.Align.CENTER)
        dot = Gtk.Box()
        dot.add_css_class("dot")
        dot.set_valign(Gtk.Align.CENTER)
        self.pill_label = Gtk.Label(label="Bereit")
        self.pill.append(dot)
        self.pill.append(self.pill_label)
        top.append(self.pill)
        self.since = Gtk.Label(label="", xalign=0, hexpand=True)
        self.since.add_css_class("since-label")
        top.append(self.since)
        self.loc_badge = Gtk.Label(label="Remote")
        self.loc_badge.add_css_class("location-badge")
        self.loc_badge.set_valign(Gtk.Align.CENTER)
        a11y(self.loc_badge, "Ort", "Arbeitsort des laufenden Tasks")
        top.append(self.loc_badge)
        self.hero.append(top)

        self.timer = Gtk.Label(label="0:00:00", xalign=0)
        self.timer.add_css_class("timer")
        a11y(self.timer, "Laufende Zeit", False)
        self.hero.append(self.timer)

        # Lange Namen (bis 500 Zeichen, auch ohne Leerzeichen) brechen an jeder Stelle
        # um und werden nach wenigen Zeilen gekürzt – das Fenster wird nie breiter;
        # der volle Text steht im Tooltip.
        self.title = Gtk.Label(label="Kein Task aktiv", xalign=0, wrap=True,
                               wrap_mode=Pango.WrapMode.WORD_CHAR, lines=TITLE_LINES,
                               ellipsize=Pango.EllipsizeMode.END)
        self.title.add_css_class("task-title")
        self.title.set_selectable(False)
        a11y(self.title, "Kunde", False)
        self.subtitle = Gtk.Label(label="", xalign=0, wrap=True,
                                  wrap_mode=Pango.WrapMode.WORD_CHAR, lines=SUBTITLE_LINES,
                                  ellipsize=Pango.EllipsizeMode.END)
        self.subtitle.add_css_class("task-subtitle")
        a11y(self.subtitle, "Thema und Ticket", False)
        self.hero.append(self.title)
        self.hero.append(self.subtitle)

        qbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        qbox.set_margin_top(14)
        self.quanta = QuantaBar()
        self.quanta.add_css_class("state-idle")
        self.quanta_caption = Gtk.Label(label="", xalign=0)
        self.quanta_caption.add_css_class("quanta-caption")
        qbox.append(self.quanta)
        qbox.append(self.quanta_caption)
        self.quanta_box = qbox
        self.hero.append(qbox)

        actions = Gtk.Box(spacing=10)
        actions.add_css_class("action-row")
        actions.set_margin_top(16)
        self.pause_btn = Gtk.Button(label="Pause")
        self.pause_btn.add_css_class("pill")
        self.pause_btn.set_action_name("app.pause")
        a11y(self.pause_btn, "Pause", "Pause / Weiter (Strg+P)")
        self.stop_btn = Gtk.Button(label="Stoppen")
        self.stop_btn.add_css_class("pill")
        self.stop_btn.add_css_class("suggested-action")
        self.stop_btn.set_action_name("app.stop")
        a11y(self.stop_btn, "Stoppen", self._stop_tooltip())
        self.edit_btn = Gtk.Button(icon_name="document-edit-symbolic")
        self.edit_btn.add_css_class("flat")
        self.edit_btn.add_css_class("circular")
        self.edit_btn.set_halign(Gtk.Align.END)
        self.edit_btn.set_hexpand(True)
        self.edit_btn.set_valign(Gtk.Align.CENTER)
        self.edit_btn.set_action_name("win.edit-active")
        a11y(self.edit_btn, "Laufenden Task bearbeiten")
        actions.append(self.pause_btn)
        actions.append(self.stop_btn)
        actions.append(self.edit_btn)
        self.hero_actions = actions
        self.hero.append(actions)
        outer.append(self.hero)

        # --- Mikrofon
        mic_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        mic_box.set_halign(Gtk.Align.CENTER)
        self.mic = MicButton(lambda: self.app.voice.begin("command"),
                             self.app.voice.end, lambda: self.app.voice.state)
        mic_box.append(self.mic)
        self.mic_hint = Gtk.Label(label=MIC_HINT_IDLE,
                                  justify=Gtk.Justification.CENTER, wrap=True)
        self.mic_hint.add_css_class("mic-hint")
        mic_box.append(self.mic_hint)
        # Tastenkürzel für die Sprache – im Fenster und systemweit (Hintergrundbetrieb)
        keys = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        keys.add_css_class("key-hints")
        self.key_local = KeyHint(WINDOW_VOICE_ACCEL, "im Fenster halten")
        self.key_global = KeyHint(None, "")
        self.key_global.set_name("hotkey-voice-hint")
        self.hotkey_setup = Gtk.Button(label=SETUP_HOTKEYS_LABEL, halign=Gtk.Align.CENTER)
        self.hotkey_setup.add_css_class("flat")
        self.hotkey_setup.add_css_class("hotkey-setup")
        self.hotkey_setup.set_name("hotkey-setup")
        a11y(self.hotkey_setup, "Systemweite Tastenkürzel einrichten",
             "Ohne systemweite Kürzel wirken Tasten nur im Tracksy-Fenster – "
             "Einstellungen → System")
        self.hotkey_setup.connect("clicked",
                                  lambda *_: self.app.show_preferences(page="system"))
        self.hotkey_setup.set_visible(False)
        keys.append(self.key_local)
        keys.append(self.key_global)
        keys.append(self.hotkey_setup)
        self.key_hints = keys
        mic_box.append(keys)
        self.level = Gtk.LevelBar(min_value=0, max_value=1)
        self.level.add_css_class("mic-level")
        self.level.set_size_request(180, -1)
        self.level.set_visible(False)
        a11y(self.level, "Mikrofonpegel", False)
        mic_box.append(self.level)
        outer.append(mic_box)

        # --- Startformular
        self.form = StartForm(self.app.customers, self._on_form_submit,
                              self.app.config.general.default_location)
        a11y(self.form, "Startformular", False)
        outer.append(self.form)
        self.start_btn = Gtk.Button(label="Task starten")
        self.start_btn.add_css_class("pill")
        self.start_btn.add_css_class("suggested-action")
        self.start_btn.set_halign(Gtk.Align.CENTER)
        self.start_btn.connect("clicked", lambda *_: self.form.submit())
        a11y(self.start_btn, "Task starten", "Task mit diesen Angaben starten (Enter)")
        start_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        start_box.append(self.start_btn)
        self.key_toggle = KeyHint(None, "")
        self.key_toggle.set_name("hotkey-toggle-hint")
        start_box.append(self.key_toggle)
        outer.append(start_box)

        clamp.set_child(outer)
        scroller.set_child(clamp)
        return scroller

    def _install_actions(self) -> None:
        def add(name: str, cb: Callable[[], Any]) -> None:
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda *_: cb())
            self.add_action(act)

        add("hide", self.hide_window)
        add("view-track", lambda: self.stack.set_visible_child_name("track"))
        add("view-overview", lambda: self.stack.set_visible_child_name("overview"))
        add("new-entry", self.overview.new_entry)
        add("export", self.overview.open_export)
        add("edit-active", self.app.edit_active)
        add("focus-form", self.focus_form)

    # ----------------------------------------------------------------- Verhalten
    def _on_close_request(self, *_a) -> bool:
        if self.app.really_quitting:
            return False
        self.hide_window()
        return True

    def hide_window(self) -> None:
        self.set_visible(False)
        if not self.app.hidden_hint_shown:
            self.app.hidden_hint_shown = True
            from .integration import hide_hint  # noqa: PLC0415

            tray_status = getattr(self.app, "tray_status", lambda: "ok")
            self.app.notify("Tracksy läuft im Hintergrund weiter",
                            hide_hint(tray_status() == "ok"))

    def _on_page_changed(self, *_a) -> None:
        if self.stack.get_visible_child_name() == "overview":
            self.overview.refresh()
        else:  # GtkStack setzt den Fokus beim Wechsel ggf. auf das erste Element (Mikrofon)
            self._defer_focus_guard()

    # ------------------------------------------------------------- Fokus
    # Regel: Der Mikrofon-Knopf bekommt den Fokus nur durch den Nutzer (Tab/Klick),
    # nie als Anfangs- oder Rückkehrfokus. Standard ist „kein Fokus“: Enter und
    # Leertaste bewirken dann nichts, Tab beginnt am ersten Element, Strg+L springt
    # ins Formular. Fokus in einem offenen Dialog wird nie angetastet.
    def present(self) -> None:  # noqa: D401 – überschreibt Gtk.Window.present
        first = not self._mapped_once
        super().present()
        # sofort (GTK hat beim Anzeigen bereits einen Auto-Fokus gesetzt) und nochmals
        # nach dem Abarbeiten anstehender Ereignisse
        self.guard_focus(initial=first)
        self._defer_focus_guard()

    def _on_map(self, *_a) -> None:
        first = not self._mapped_once
        self._mapped_once = True
        # GTK wählt beim ersten Anzeigen selbst ein Element (Tab-Reihenfolge) → lösen
        self._defer_focus_guard(initial=first)

    def _on_active_changed(self, *_a) -> None:
        if self.is_active():
            self._defer_focus_guard()

    def _defer_focus_guard(self, initial: bool = False) -> None:
        GLib.idle_add(lambda: (self.guard_focus(initial), False)[1])

    def mic_has_focus(self) -> bool:
        focus = self.get_focus()
        return focus is not None and (focus is self.mic or focus.is_ancestor(self.mic))

    def guard_focus(self, initial: bool = False) -> None:
        """Löst den Fokus vom Mikrofon (bzw. beim ersten Anzeigen von GTKs Auto-Fokus)."""
        focus = self.get_focus()
        if focus is None or focus.get_ancestor(Adw.Dialog) is not None:
            return
        if initial or self.mic_has_focus():
            self.set_focus(None)

    def focus_form(self) -> None:
        self.stack.set_visible_child_name("track")
        self.form.customer.grab_focus()

    def _on_form_submit(self, values: dict) -> None:
        if self.app.start_task(values, source="gui"):
            self.form.clear(self.app.config.general.default_location)

    def toast(self, text: str, button: str | None = None,
              on_button: Callable[[], None] | None = None, timeout: int = 4,
              high: bool = False) -> Adw.Toast:
        t = Adw.Toast(title=GLib.markup_escape_text(shorten(text, TOAST_CHARS)),
                      timeout=timeout)
        if high:  # sofort zeigen statt hinter älteren Meldungen einzureihen
            t.set_priority(Adw.ToastPriority.HIGH)
        if button and on_button:
            t.set_button_label(button)
            t.connect("button-clicked", lambda *_: on_button())
        t.connect("dismissed", self._on_toast_dismissed)
        self._toast_count += 1
        self.toast_space.set_reveal_child(True)
        self.toasts.add_toast(t)
        return t

    def _on_toast_dismissed(self, *_a) -> None:
        self._toast_count = max(0, self._toast_count - 1)
        if not self._toast_count:
            self.toast_space.set_reveal_child(False)

    # ------------------------------------------------------------------ Status
    def update_status(self, status: dict[str, Any]) -> None:
        v = task_view_from_status(status)
        v.voice = self.app.voice.state
        self._view = v
        set_state_class(self.hero, f"state-{v.state}")
        set_state_class(self.pill, v.css_state)
        set_state_class(self.quanta, f"state-{v.state}")
        self.pill_label.set_label("Aufnahme" if v.voice == "listening" else v.state_label)
        self.since.set_label(v.since_label)
        self.title.set_label(v.title)
        self.subtitle.set_label(v.subtitle)
        for lbl, text in ((self.title, v.title), (self.subtitle, v.subtitle)):
            lbl.set_tooltip_text(text if len(text) > LONG_TEXT_TOOLTIP else None)
        self.loc_badge.set_label(v.location_label)
        self.loc_badge.set_visible(v.active)
        if v.location == "onsite":
            self.loc_badge.add_css_class("onsite")
        else:
            self.loc_badge.remove_css_class("onsite")
        self.hero_actions.set_visible(v.active)
        self.timer.set_visible(v.active)
        self.quanta_box.set_visible(v.active)
        paused = v.state == "paused"
        self.pause_btn.set_label("Weiter" if paused else "Pause")
        a11y(self.pause_btn, "Weiter" if paused else "Pause", "Pause / Weiter (Strg+P)")
        self.pause_btn.set_sensitive(v.state in ("running", "paused"))
        self.stop_btn.set_label("Abschließen …" if v.state == "stopping" else "Stoppen")
        self.edit_btn.set_sensitive(v.state in ("running", "paused"))
        if v.active:
            self.form.set_title("Nächster Task")
            self.form.set_description("Starten schließt den laufenden Task vorher ab.")
            self.start_btn.set_label("Wechseln")
            a11y(self.start_btn, "Task wechseln", "Laufenden Task abschließen und diesen starten")
        else:
            self.form.set_title("Neuer Task")
            self.form.set_description(None)
            self.start_btn.set_label("Task starten")
            a11y(self.start_btn, "Task starten", "Task mit diesen Angaben starten (Enter)")
        self._refresh_time()
        if v.ticking and not self._tick_id:
            self._tick_id = GLib.timeout_add(1000, self._tick)
        elif not v.ticking and self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0
        if self.stack.get_visible_child_name() == "overview" and status.get("event") in (
                "stopped", "discarded", "entry_updated", "entry_created", "purged"):
            self.overview.refresh()

    def _tick(self) -> bool:
        if not self._view.ticking:
            self._tick_id = 0
            return False
        if self.get_visible():
            self._refresh_time()
        return True

    def _refresh_time(self) -> None:
        v = self._view
        secs = v.elapsed(datetime.now().astimezone())
        self.timer.set_label(format_clock(secs))
        q = quanta(secs, self.app.config.export.rounding_minutes)
        self.quanta.set_quanta(q)
        cap = describe_quanta(q)
        if v.state == "running" and q.minutes_to_next:
            cap += f" – nächste Einheit in {q.minutes_to_next} min"
        self.quanta_caption.set_label(cap)

    def set_voice_state(self, state: str, available: bool) -> None:
        self.mic.set_voice_state(state, available)
        rec = state == "listening"
        self.level.set_visible(rec)
        if rec:
            self.mic_hint.set_label("Aufnahme läuft – loslassen oder klicken zum Beenden")
            self.mic_hint.add_css_class("recording")
        else:
            self.mic_hint.remove_css_class("recording")
            if state == "transcribing":
                self.mic_hint.set_label("Wird erkannt …")
            elif available:
                self.mic_hint.set_label(MIC_HINT_IDLE)
            else:
                self.mic_hint.set_label("Spracheingabe nicht eingerichtet – siehe Einstellungen")
        self._view.voice = state
        set_state_class(self.pill, self._view.css_state)
        self.pill_label.set_label("Aufnahme" if rec else self._view.state_label)

    # ------------------------------------------------------------- Tastenkürzel
    def _stop_tooltip(self) -> str:
        hk = getattr(self, "_hotkeys", None)
        glob = hk.display("toggle") if hk is not None else None
        return "Task stoppen und abschließen (Strg+S" + (f", überall {glob})" if glob else ")")

    def set_hotkeys(self, hk, hold_supported: bool = True) -> None:
        """Systemweite Kürzel anzeigen (``integration.ActiveHotkeys`` oder ``None`` =
        noch nicht gelesen). Nicht eingerichtet → Knopf zu Einstellungen → System."""
        self._hotkeys = hk
        voice = hk.voice if hk is not None else None
        toggle = hk.toggle if hk is not None else None
        how = "überall halten" if hold_supported else "überall: Start/Stopp"
        self.key_global.set_hint(voice, how,
                                 spoken=f"{hk.display('voice')} {how}" if voice else None)
        self.key_toggle.set_hint(toggle, "startet und stoppt überall")
        self.hotkey_setup.set_visible(hk is not None and not hk.any)
        a11y(self.stop_btn, "Stoppen", self._stop_tooltip())

    def set_level(self, level: float) -> None:
        self.level.set_value(max(0.0, min(1.0, level)))
