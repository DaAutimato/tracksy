"""Ergänzungsdialog beim Stoppen eines Tasks."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, GObject, Gtk, Pango  # noqa: E402

from .viewmodel import (  # noqa: E402
    LOCATION_LABELS,
    describe_start_fields,
    format_hhmm,
    format_hm,
    shorten,
)
from ..core.sanitize import InputLimitError  # noqa: E402
from ..service import PendingStartError  # noqa: E402
from .widgets import ItemsEditor, MultilineField, a11y  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ..app import ZeitApp
    from ..core.models import Entry


# Lange Texte: Kunde/Thema brechen an jeder Stelle um und werden nach
# wenigen Zeilen gekürzt; die Aktionsleiste bleibt dadurch immer im Bild.
CUSTOMER_LINES = 3
TOPIC_LINES = 4
BANNER_CHARS = 90
# Unterhalb dieser Dialogbreite zeigt „Verwerfen“ nur das Symbol (Name bleibt für AT-SPI).
NARROW_BAR = "max-width: 470sp"
MIN_WIDTH, MIN_HEIGHT = 360, 300


def _wrapping_label(text: str, lines: int) -> Gtk.Label:
    lbl = Gtk.Label(label=text, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                    lines=lines, ellipsize=Pango.EllipsizeMode.END)
    if len(text) > 60:
        lbl.set_tooltip_text(text)
    return lbl


def pending_start_failed_text(done: str, exc: PendingStartError) -> str:
    """Stopp/Verwerfen erledigt, vorgemerkter Start gescheitert (Teilerfolg)."""
    cause = exc.__cause__ if exc.__cause__ is not None else exc
    return f"{done}. Der neue Task konnte nicht gestartet werden: {cause}"


class StopDialog(Adw.Dialog):
    """Speichern (Strg+Enter), Weiterlaufen lassen (Esc), Verwerfen (mit Rückfrage)."""

    def __init__(self, app: "ZeitApp", entry: "Entry") -> None:
        super().__init__()
        self.app = app
        self.entry_uuid = entry.uuid
        self._done = False
        self.set_title("Task abschließen")
        self.set_content_width(540)
        self.set_content_height(720)
        # Mindestgröße: ohne sie meldet Adwaita bei jedem Layout-Durchlauf
        # „does not have a minimum size“ (Inhalt scrollt, Aktionsleiste bleibt sichtbar)
        self.set_size_request(MIN_WIDTH, MIN_HEIGHT)
        self.set_can_close(False)  # Esc/Schließen → „Weiterlaufen lassen“
        self.connect("close-attempt", lambda *_: self._keep_running())

        tv = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        tv.add_top_bar(header)

        pending = app.service.pending_start
        if pending is not None:
            banner = Adw.Banner(title=GLib.markup_escape_text(shorten(
                "Danach startet: " + describe_start_fields(
                    pending.customer, pending.topic, pending.ticket, None), BANNER_CHARS)))
            banner.set_revealed(True)
            tv.add_top_bar(banner)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        body.set_margin_top(6)
        body.set_margin_bottom(18)
        body.set_margin_start(18)
        body.set_margin_end(18)

        # Zusammenfassung
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        card.add_css_class("card")
        card.add_css_class("summary-card")
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        t = _wrapping_label(entry.customer or "Ohne Kunde", CUSTOMER_LINES)
        t.add_css_class("title-3")
        sub_parts = [p for p in (entry.topic, f"Ticket {entry.ticket}" if entry.ticket else "") if p]
        s = _wrapping_label(" – ".join(sub_parts) or "Ohne Thema", TOPIC_LINES)
        s.add_css_class("dim-label")
        end = entry.end_at or datetime.now().astimezone()
        pause = entry.total_pause_seconds(end)
        span = f"{format_hhmm(entry.start_at.astimezone())}–{format_hhmm(end.astimezone())}"
        if pause:
            span += f", Pause {pause // 60} min"
        span += f", {LOCATION_LABELS.get(str(entry.location), str(entry.location))}"
        sp = Gtk.Label(label=span, xalign=0, wrap=True)
        sp.add_css_class("caption")
        sp.add_css_class("dim-label")
        self.title_label, self.topic_label, self.span_label = t, s, sp
        left.append(t)
        left.append(s)
        left.append(sp)
        card.append(left)
        dur = Gtk.Label(label=format_hm(entry.net_seconds(end)), valign=Gtk.Align.CENTER)
        dur.add_css_class("summary-duration")
        a11y(dur, f"Dauer {format_hm(entry.net_seconds(end))}", False)
        card.append(dur)
        body.append(card)

        self.addendum = MultilineField("Möchtest du zum Task noch etwas ergänzen?",
                                       dictation=app.toggle_dictation, on_limit=self.toast)
        self.addendum.set_text(entry.addendum)
        body.append(self.addendum)

        self.items = ItemsEditor(
            "Weitere erledigte Tätigkeiten",
            "Hast du währenddessen weitere Dinge erledigt, die dokumentiert werden sollen?",
            dictation=app.toggle_dictation)
        self.items.set_items(entry.items)
        body.append(self.items)

        scroller.set_child(body)
        tv.set_content(scroller)

        # Aktionen
        bar = Gtk.Box(spacing=10)
        bar.set_margin_top(10)
        bar.set_margin_bottom(12)
        bar.set_margin_start(18)
        bar.set_margin_end(18)
        # Destruktiv, aber nicht dominant: getönte Fläche + Symbol + Text (style.css)
        discard_content = Adw.ButtonContent(icon_name="user-trash-symbolic",
                                            label="Verwerfen …")
        discard = Gtk.Button(child=discard_content)
        discard.add_css_class("discard-button")
        a11y(discard, "Task verwerfen", "Task verwerfen (mit Rückfrage)")
        discard.connect("clicked", lambda *_: self._ask_discard())
        bar.append(discard)
        spacer = Gtk.Box(hexpand=True)
        bar.append(spacer)
        keep = Gtk.Button(label="Weiterlaufen lassen")
        a11y(keep, "Weiterlaufen lassen", "Stopp abbrechen, Task läuft weiter (Esc)")
        keep.connect("clicked", lambda *_: self._keep_running())
        save = Gtk.Button(label="Speichern")
        save.add_css_class("suggested-action")
        a11y(save, "Speichern", "Task speichern (Strg+Enter)")
        save.connect("clicked", lambda *_: self._save())
        bar.append(keep)
        bar.append(save)
        self.discard_button, self.keep_button, self.save_button = discard, keep, save
        tv.add_bottom_bar(bar)
        # Schmale Fenster (Dialog als Bottom-Sheet): „Verwerfen“ nur als Symbol,
        # damit „Weiterlaufen lassen“ und „Speichern“ vollständig sichtbar bleiben.
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(NARROW_BAR))
        bp.add_setter(discard_content, "label", GObject.Value(str, ""))
        self.add_breakpoint(bp)
        # eigene Toasts: Meldungen des Hauptfensters lägen hinter dem Dialog
        self.toasts = Adw.ToastOverlay(child=tv)
        self.set_child(self.toasts)
        self.set_default_widget(save)

        sc = Gtk.ShortcutController()
        sc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        for trig in ("<Control>Return", "<Control>KP_Enter"):
            sc.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(trig),
                Gtk.CallbackAction.new(lambda *_: (self._save(), True)[1])))
        self.add_controller(sc)
        self.set_focus(self.addendum.view)

    # ------------------------------------------------------------------ Aktionen
    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=5))

    def _finish(self) -> None:
        self._done = True
        self.app.stop_dialog_closed(self)
        self.force_close()

    def _save(self) -> None:
        if self._done:
            return
        try:
            self.app.service.confirm_stop(self.addendum.get_text(), self.items.get_items())
        except InputLimitError as exc:
            # Zustand bleibt „stopping“ – Dialog offen lassen, damit gekürzt werden kann
            self.toast(f"Nicht gespeichert: {exc}")
            return
        except PendingStartError as exc:  # Teilerfolg: gespeichert, Folgestart gescheitert
            self.app.toast(pending_start_failed_text("Task gespeichert", exc), public="Aktion nicht möglich – Details im Tracksy-Fenster")
        except Exception as exc:  # SQLite-Fehler, InvalidTransition o. ä.
            if self.app.service.state == "stopping":
                # nichts gespeichert (Rollback): Eingaben behalten, erneut versuchen
                self.toast(f"Speichern nicht möglich: {exc}")
                return
            self.app.toast(f"Speichern nicht möglich: {exc}", public="Aktion nicht möglich – Details im Tracksy-Fenster")
        self._finish()

    def _keep_running(self) -> None:
        if self._done:
            return
        try:
            self.app.service.cancel_stop()
        except Exception as exc:
            self.app.toast(f"{exc}", public="Aktion nicht möglich – Details im Tracksy-Fenster")
        self._finish()

    def _ask_discard(self) -> None:
        dlg = Adw.AlertDialog(
            heading="Task verwerfen?",
            body="Die erfasste Zeit wird nicht gespeichert. Der Vorgang wird im Protokoll "
                 "als „verworfen“ vermerkt.")
        dlg.add_response("cancel", "Abbrechen")
        dlg.add_response("discard", "Verwerfen")
        dlg.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def on_resp(_d, resp: str) -> None:
            if resp == "discard":
                self._discard()

        dlg.connect("response", on_resp)
        dlg.present(self)

    def _discard(self) -> None:
        """Verwerfen ausführen; Erfolgsmeldung nur, wenn es geklappt hat."""
        if self._done:
            return
        try:
            self.app.service.discard()
        except PendingStartError as exc:  # Teilerfolg wie beim Speichern
            self.app.toast(pending_start_failed_text("Task verworfen", exc), public="Aktion nicht möglich – Details im Tracksy-Fenster")
        except Exception as exc:
            # Dialog offen lassen, solange der Stopp noch aussteht
            if self.app.service.state == "stopping":
                self.toast(f"Verwerfen nicht möglich: {exc}")
                return
            self.app.toast(f"Verwerfen nicht möglich: {exc}", public="Aktion nicht möglich – Details im Tracksy-Fenster")
        else:
            self.app.toast("Task verworfen")
        self._finish()

    def refresh_from_service(self) -> None:
        """Wird aufgerufen, wenn der Stopp extern beendet wurde (z. B. zweite Instanz)."""
        if self._done:
            return
        if self.app.service.state != "stopping":
            self._finish()
