"""Wiederverwendbare GTK4-Widgets der Zeiterfassung."""

from __future__ import annotations

import math
import time
from typing import Callable, Iterable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..core.sanitize import MAX_FIELD_LEN, MAX_ITEM_LEN, MAX_ITEMS, MAX_TEXT_LEN  # noqa: E402
from .viewmodel import Quanta, filter_suggestions  # noqa: E402

HOLD_THRESHOLD_S = 0.45  # länger gedrückt = Push-to-talk, kürzer = Klick-Umschalter


def a11y(widget: Gtk.Widget, label: str, tooltip: str | None = None) -> Gtk.Widget:
    """Zugänglichen Namen (AT-SPI) und Tooltip setzen.

    Der Name wird zusätzlich als ``zeit_a11y_label`` am Python-Objekt abgelegt,
    damit Tests ihn ohne AT-SPI prüfen können (GTK 4.14 hat keinen Getter).
    """
    widget.update_property([Gtk.AccessibleProperty.LABEL], [label])
    widget.zeit_a11y_label = label  # type: ignore[attr-defined]
    if tooltip is not False:
        widget.set_tooltip_text(tooltip or label)
    return widget


def limit_length(widget: Gtk.Widget, max_chars: int) -> Gtk.Widget:
    """Eingabelänge begrenzen (Gtk.Entry/Gtk.Text direkt, Adw.EntryRow über den Delegate).

    Grenzen kommen aus ``core.sanitize``; der Service prüft zusätzlich.
    """
    target = widget if hasattr(widget, "set_max_length") else widget.get_delegate()
    target.set_max_length(max_chars)
    return widget


def limit_message(max_chars: int) -> str:
    return f"Text gekürzt – höchstens {max_chars:,} Zeichen".replace(",", ".")


def limit_text_buffer(buf: Gtk.TextBuffer, max_chars: int,
                      on_limit: Callable[[], None] | None = None) -> int:
    """TextBuffer auf ``max_chars`` Zeichen begrenzen (Tippen, Einfügen, Diktat).

    Zu langer eingefügter Text wird gekürzt; ``on_limit`` meldet das (z. B. Toast).
    Rückgabe: Handler-ID.
    """
    handler_id = 0

    def on_insert(b: Gtk.TextBuffer, it: Gtk.TextIter, text: str, _length: int) -> None:
        room = max_chars - b.get_char_count()
        if len(text) <= room:
            return
        b.stop_emission_by_name("insert-text")
        if room > 0:
            b.handler_block(handler_id)
            try:
                b.insert(it, text[:room])
            finally:
                b.handler_unblock(handler_id)
        if on_limit is not None:
            GLib.idle_add(lambda: (on_limit(), False)[1])

    handler_id = buf.connect("insert-text", on_insert)
    return handler_id


def clear_box(box: Gtk.Widget) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def set_state_class(widget: Gtk.Widget, state_cls: str) -> None:
    for c in list(widget.get_css_classes()):
        if c.startswith("state-"):
            widget.remove_css_class(c)
    widget.add_css_class(state_cls)


# ---------------------------------------------------------------------------
# Viertelstunden-Leiste
# ---------------------------------------------------------------------------
def focus_response(dlg: Adw.AlertDialog, label: str) -> None:
    """Beim Einblenden den Fokus ausdrücklich auf den Knopf ``label`` legen.

    Enter löst den *fokussierten* Knopf aus, nicht unbedingt die Standardantwort.
    ``Adw.AlertDialog`` fokussiert die Standardantwort meist selbst, aber nicht
    zugesichert (Layoutwechsel schmal/breit, langsamer Erststart). Für Rückfragen,
    bei denen Enter nichts ändern darf, setzen wir ihn deshalb selbst – nach jedem
    ``map`` im nächsten Idle, wenn der Dialog seine eigene Fokuswahl getroffen hat."""

    def button() -> Gtk.Button | None:
        stack = [dlg]
        while stack:
            w = stack.pop()
            if isinstance(w, Gtk.Button) and w.get_label() == label:
                return w
            child = w.get_first_child()
            while child is not None:
                stack.append(child)
                child = child.get_next_sibling()
        return None

    def grab() -> bool:
        btn = button()
        if btn is not None and dlg.get_mapped() and dlg.get_focus() is not btn:
            dlg.set_focus(btn)
        return False

    dlg.connect("map", lambda *_: GLib.idle_add(grab))


class QuantaBar(Gtk.DrawingArea):
    """Zeigt angebrochene Abrechnungseinheiten als Blöcke.

    Je Einheit ein Block; die angebrochene Einheit füllt sich. Nach jeder
    vollen Stunde ein etwas größerer Abstand. Farbe kommt aus CSS (``color``).
    """

    BLOCK_H = 10
    GAP = 4
    HOUR_GAP = 10
    MAX_SLOTS = 40

    def __init__(self) -> None:
        super().__init__()
        self.add_css_class("quanta")
        self.set_content_height(self.BLOCK_H)
        self.set_hexpand(True)
        self._q = Quanta(15, 0, 0.0, 0)
        self.set_draw_func(self._draw)
        a11y(self, "Abrechnungseinheiten", None)

    def set_quanta(self, q: Quanta) -> None:
        if q != self._q:
            self._q = q
            self.queue_draw()

    @staticmethod
    def _rounded(cr, x: float, y: float, w: float, h: float, r: float) -> None:
        r = min(r, h / 2, w / 2)
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def _draw(self, _area, cr, width: int, height: int) -> None:
        q = self._q
        if q.step_minutes <= 0:
            return
        per_hour = max(1, 60 // q.step_minutes) if 60 % q.step_minutes == 0 else 0
        started = q.started
        slots = max(per_hour or 4, started)
        if per_hour:
            slots = int(math.ceil(slots / per_hour) * per_hour)
        slots = min(slots, self.MAX_SLOTS)
        hour_gaps = (slots - 1) // per_hour if per_hour else 0
        gaps_total = (slots - 1) * self.GAP + hour_gaps * (self.HOUR_GAP - self.GAP)
        bw = max(3.0, (width - gaps_total) / slots)
        y = (height - self.BLOCK_H) / 2
        color = self.get_color()
        x = 0.0
        # bei sehr langen Tasks die letzten Einheiten zeigen
        offset = max(0, started - slots)
        for i in range(slots):
            unit = i + offset
            if unit < q.full:
                fill = 1.0
            elif unit == q.full:
                fill = q.partial
            else:
                fill = 0.0
            cr.set_source_rgba(color.red, color.green, color.blue, color.alpha * 0.18)
            self._rounded(cr, x, y, bw, self.BLOCK_H, 3)
            cr.fill()
            if fill > 0:
                cr.set_source_rgba(color.red, color.green, color.blue, color.alpha)
                self._rounded(cr, x, y, max(3.0, bw * fill), self.BLOCK_H, 3)
                cr.fill()
            x += bw + self.GAP
            if per_hour and (i + 1) % per_hour == 0:
                x += self.HOUR_GAP - self.GAP


# ---------------------------------------------------------------------------
# Mikrofon-Button (Push-to-talk: halten ODER klicken)
# ---------------------------------------------------------------------------
MIC_TOOLTIP = "{label}: gedrückt halten und sprechen (Strg+M) – oder klicken zum Starten/Beenden"
MIC_REC_TOOLTIP = "Aufnahme läuft – loslassen oder klicken zum Beenden"


class MicButton(Gtk.Button):
    """Mikrofon-Button.

    Maus: Drücken startet die Aufnahme; wird länger als ``HOLD_THRESHOLD_S``
    gehalten, beendet Loslassen die Aufnahme (Push-to-talk). Ein kurzer Klick
    lässt die Aufnahme laufen, bis erneut geklickt wird.
    Tastatur: Leertaste/Enter bei Fokus schalten um; Strg+M (gedrückt halten) bindet
    das Fenster selbst (``keyhold.attach_voice_hold``).
    """

    def __init__(self, begin: Callable[[], bool], end: Callable[[], bool],
                 get_state: Callable[[], str], small: bool = False,
                 label: str = "Spracheingabe") -> None:
        super().__init__()
        self._begin, self._end, self._get_state = begin, end, get_state
        self._label = label
        self._press_t: float | None = None
        self.set_icon_name("audio-input-microphone-symbolic")
        self.add_css_class("mic-button")
        self.add_css_class("circular")
        if small:
            self.add_css_class("small")
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        a11y(self, label, MIC_TOOLTIP.format(label=label))
        g = Gtk.GestureClick()
        g.set_button(1)
        g.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        g.connect("pressed", self._on_pressed)
        g.connect("released", self._on_released)
        self.add_controller(g)
        self.connect("clicked", self._on_clicked)

    def _on_pressed(self, gesture, _n, _x, _y) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        st = self._get_state()
        if st == "listening":
            self._press_t = None
            self._end()
        elif st == "idle":
            self._press_t = time.monotonic() if self._begin() else None

    def _on_released(self, _gesture, _n, _x, _y) -> None:
        if self._press_t is not None and time.monotonic() - self._press_t >= HOLD_THRESHOLD_S:
            if self._get_state() == "listening":
                self._end()
        self._press_t = None

    def _on_clicked(self, _btn) -> None:  # Tastatur
        st = self._get_state()
        if st == "listening":
            self._end()
        elif st == "idle":
            self._begin()

    def set_voice_state(self, state: str, available: bool = True) -> None:
        for c in ("recording", "transcribing"):
            self.remove_css_class(c)
        if state == "listening":
            self.add_css_class("recording")
            self.set_icon_name("media-playback-stop-symbolic")
            a11y(self, f"{self._label} – Aufnahme läuft", MIC_REC_TOOLTIP)
        elif state == "transcribing":
            self.add_css_class("transcribing")
            self.set_icon_name("audio-input-microphone-symbolic")
            a11y(self, f"{self._label} – wird erkannt", "Sprache wird erkannt …")
        else:
            self.set_icon_name("audio-input-microphone-symbolic")
            a11y(self, self._label, MIC_TOOLTIP.format(label=self._label)
                 if available else "Spracheingabe nicht verfügbar – siehe Einstellungen")
        self.set_sensitive(available and state != "transcribing")


DICTATION_TOOLTIP = "{label}: klicken zum Starten/Beenden"


class DictationButton(Gtk.Button):
    """Kleiner Diktat-Button (Umschalter) für Textfelder.

    Strg+M (gedrückt halten) diktiert im Ergänzungsdialog in das Feld „Ergänzung“."""

    def __init__(self, label: str, on_toggle: Callable[["DictationButton"], None]) -> None:
        super().__init__(icon_name="audio-input-microphone-symbolic")
        self.add_css_class("flat")
        self.add_css_class("circular")
        self.add_css_class("dictation-button")  # Aufnahmefarbe aus der Palette (style.css)
        self.set_valign(Gtk.Align.CENTER)
        self._label = label
        a11y(self, label, DICTATION_TOOLTIP.format(label=label))
        self.connect("clicked", lambda *_: on_toggle(self))

    def set_recording(self, on: bool, busy: bool = False) -> None:
        if on:
            self.add_css_class("destructive-action")
            self.remove_css_class("flat")
            self.set_icon_name("media-playback-stop-symbolic")
            a11y(self, f"{self._label} – Aufnahme läuft", "Aufnahme läuft – klicken zum Beenden")
        else:
            self.remove_css_class("destructive-action")
            self.add_css_class("flat")
            self.set_icon_name("audio-input-microphone-symbolic")
            a11y(self, self._label, DICTATION_TOOLTIP.format(label=self._label))
        self.set_sensitive(not busy)


# ---------------------------------------------------------------------------
# Tastenkürzel-Hinweis (Tastenkappen + kurzer Text)
# ---------------------------------------------------------------------------
class Keycaps(Gtk.Box):
    """Tastenkürzel als Tastenkappen (``[Super] [Umschalt] [V]``), deutsch beschriftet
    und in derselben Reihenfolge wie ``integration.accel_display`` in Texten."""

    def __init__(self, accel: str | None = None) -> None:
        super().__init__(spacing=3, valign=Gtk.Align.CENTER)
        self.add_css_class("keycaps")
        self.accel: str | None = None
        self.set_accel(accel)

    def set_accel(self, accel: str | None) -> None:
        from .integration import accel_display, accel_parts  # noqa: PLC0415

        self.accel = accel
        clear_box(self)
        for part in accel_parts(accel):
            cap = Gtk.Label(label=part)
            cap.add_css_class("keycap")
            self.append(cap)
        text = accel_display(accel)
        if text:
            a11y(self, text, False)


class KeyHint(Gtk.Box):
    """Eine Zeile „[Strg] [M] im Fenster halten“: Tastenkappen und dezenter Text.
    Ohne Kürzel unsichtbar."""

    def __init__(self, accel: str | None = None, text: str = "") -> None:
        super().__init__(spacing=8, halign=Gtk.Align.CENTER)
        self.add_css_class("key-hint")
        self.keys = Keycaps()
        self.keys.set_accessible_role(Gtk.AccessibleRole.PRESENTATION)
        self.label = Gtk.Label(xalign=0, wrap=True, valign=Gtk.Align.CENTER)
        self.label.add_css_class("key-hint-text")
        self.append(self.keys)
        self.append(self.label)
        self.accel: str | None = None
        self.set_hint(accel, text)

    def set_hint(self, accel: str | None, text: str, spoken: str | None = None) -> None:
        """``spoken``: Name für Screenreader (Standard: Anzeigetext + ``text``)."""
        from .integration import accel_display  # noqa: PLC0415

        self.accel = accel
        self.set_visible(bool(accel))
        if not accel:
            return
        self.keys.set_accel(accel)
        self.label.set_label(text)
        a11y(self, spoken or f"{accel_display(accel)} {text}".strip(), False)


# ---------------------------------------------------------------------------
# Remote/Vor-Ort-Umschalter
# ---------------------------------------------------------------------------
class LocationToggle(Gtk.Box):
    def __init__(self, value: str = "remote") -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.add_css_class("linked")
        self.add_css_class("segmented")
        self.add_css_class("location-toggle")
        self.set_valign(Gtk.Align.CENTER)
        self.remote = Gtk.ToggleButton(label="Remote")
        self.onsite = Gtk.ToggleButton(label="Vor Ort")
        self.onsite.set_group(self.remote)
        a11y(self.remote, "Ort: Remote")
        a11y(self.onsite, "Ort: Vor Ort")
        self.append(self.remote)
        self.append(self.onsite)
        self.set_value(value)

    def get_value(self) -> str:
        return "onsite" if self.onsite.get_active() else "remote"

    def set_value(self, value: str) -> None:
        (self.onsite if value == "onsite" else self.remote).set_active(True)


# ---------------------------------------------------------------------------
# Kunde mit Autovervollständigung
# ---------------------------------------------------------------------------
class SuggestEntryRow(Adw.EntryRow):
    """EntryRow mit Vorschlagsliste (Pfeiltasten, Enter, Klick)."""

    def __init__(self, title: str, candidates: Callable[[], Iterable[str]],
                 max_len: int = MAX_FIELD_LEN) -> None:
        super().__init__(title=title)
        limit_length(self, max_len)
        self._candidates = candidates
        self._suppress = False
        a11y(self, title, False)
        self._list = Gtk.ListBox()
        self._list.add_css_class("suggestions")
        self._list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list.connect("row-activated", self._on_row_activated)
        self._pop = Gtk.Popover()
        self._pop.set_autohide(False)
        self._pop.set_has_arrow(False)
        self._pop.set_can_focus(False)
        self._pop.set_position(Gtk.PositionType.BOTTOM)
        self._pop.set_halign(Gtk.Align.START)
        self._pop.set_child(self._list)
        self._pop.set_parent(self)
        self.connect("changed", self._on_changed)
        self.connect("destroy", lambda *_: self._pop.unparent())
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: GLib.timeout_add(150, self._hide))
        self.add_controller(focus)

    def do_unrealize(self) -> None:  # Popover sauber lösen
        self._pop.popdown()
        Adw.EntryRow.do_unrealize(self)

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        Adw.EntryRow.do_size_allocate(self, width, height, baseline)
        self._pop.present()

    def set_text_silent(self, text: str) -> None:
        self._suppress = True
        self.set_text(text)
        self._suppress = False

    def _hide(self) -> bool:
        self._pop.popdown()
        return False

    def _on_changed(self, *_a) -> None:
        if self._suppress:
            return
        items = filter_suggestions(self._candidates(), self.get_text())
        clear_box(self._list)
        if not items or not self.has_focus_inside():
            self._pop.popdown()
            return
        for it in items:
            lbl = Gtk.Label(label=it, xalign=0)
            row = Gtk.ListBoxRow()
            row.set_child(lbl)
            row.value = it  # type: ignore[attr-defined]
            a11y(row, f"Vorschlag {it}", False)
            self._list.append(row)
        self._list.set_size_request(max(200, self.get_width() - 24), -1)
        self._pop.popup()

    def has_focus_inside(self) -> bool:
        w = self.get_root().get_focus() if self.get_root() else None
        while w is not None:
            if w is self:
                return True
            w = w.get_parent()
        return False

    def _accept(self, row: Gtk.ListBoxRow) -> None:
        self.set_text_silent(row.value)  # type: ignore[attr-defined]
        self.set_position(-1)
        self._pop.popdown()

    def _on_row_activated(self, _lb, row) -> None:
        self._accept(row)
        self.grab_focus()

    def _on_key(self, _ctrl, keyval, _code, _state) -> bool:
        from gi.repository import Gdk  # noqa: PLC0415

        if not self._pop.get_visible():
            return False
        sel = self._list.get_selected_row()
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            idx = sel.get_index() if sel else -1
            idx += 1 if keyval == Gdk.KEY_Down else -1
            row = self._list.get_row_at_index(max(0, idx))
            if row is not None:
                self._list.select_row(row)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter) and sel is not None:
            self._accept(sel)
            return True
        if keyval == Gdk.KEY_Escape:
            self._pop.popdown()
            return True
        if keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            self._pop.popdown()
        return False


# ---------------------------------------------------------------------------
# Liste „Weitere erledigte Tätigkeiten“
# ---------------------------------------------------------------------------
class ItemsEditor(Adw.PreferencesGroup):
    """Liste weiterer Tätigkeiten; höchstens ``max_items`` Einträge à ``max_len`` Zeichen."""

    ADD_TITLE = "Tätigkeit hinzufügen (Enter)"

    def __init__(self, title: str, description: str | None = None,
                 dictation: Callable[[DictationButton, Callable[[str], None]], None] | None = None,
                 max_items: int = MAX_ITEMS, max_len: int = MAX_ITEM_LEN) -> None:
        super().__init__(title=title)
        if description:
            self.set_description(description)
        self.max_items, self.max_len = max_items, max_len
        self._items: list[str] = []
        self._rows: list[Gtk.Widget] = []
        self.add_row = Adw.EntryRow(title=self.ADD_TITLE)
        self.add_row.set_show_apply_button(False)
        limit_length(self.add_row, max_len)
        a11y(self.add_row, "Tätigkeit hinzufügen", False)
        self.add_row.connect("entry-activated", lambda *_: self._add_from_entry())
        add_btn = Gtk.Button(icon_name="list-add-symbolic")
        add_btn.add_css_class("flat")
        add_btn.set_valign(Gtk.Align.CENTER)
        a11y(add_btn, "Tätigkeit zur Liste hinzufügen")
        add_btn.connect("clicked", lambda *_: self._add_from_entry())
        self.add_row.add_suffix(add_btn)
        self.dictation_button: DictationButton | None = None
        if dictation is not None:
            self.dictation_button = DictationButton(
                "Tätigkeit diktieren", lambda b: dictation(b, self._add_dictated))
            self.add_row.add_suffix(self.dictation_button)
        self.add(self.add_row)

    def _add_dictated(self, text: str) -> None:
        text = text.strip()
        if text:
            self.add_item(text)

    def _add_from_entry(self) -> None:
        text = self.add_row.get_text().strip()
        if text and self.add_item(text):
            self.add_row.set_text("")
        self.add_row.grab_focus()

    @property
    def full(self) -> bool:
        return len(self._items) >= self.max_items

    def _update_limit(self) -> None:
        full = self.full
        self.add_row.set_sensitive(not full)
        self.add_row.set_title(f"Höchstens {self.max_items} Tätigkeiten" if full else self.ADD_TITLE)

    def add_item(self, text: str) -> bool:
        """Tätigkeit anhängen (auf ``max_len`` gekürzt); ``False``, wenn die Liste voll ist."""
        if self.full:
            self.add_row.error_bell()
            return False
        text = text[:self.max_len]
        self._items.append(text)
        row = Adw.ActionRow(title=GLib.markup_escape_text(text))
        row.set_title_lines(3)
        rm = Gtk.Button(icon_name="edit-delete-symbolic")
        rm.add_css_class("flat")
        rm.set_valign(Gtk.Align.CENTER)
        a11y(rm, f"Tätigkeit entfernen: {text}", "Entfernen")
        row.add_suffix(rm)
        row.item_text = text  # type: ignore[attr-defined]
        rm.connect("clicked", lambda *_: self._remove(row))
        self.remove(self.add_row)
        self.add(row)
        self.add(self.add_row)
        self._rows.append(row)
        self._update_limit()
        return True

    def _remove(self, row) -> None:
        idx = self._rows.index(row)
        self._rows.pop(idx)
        self._items.pop(idx)
        self.remove(row)
        self._update_limit()
        self.add_row.grab_focus()

    def set_items(self, items: Iterable[str]) -> None:
        for r in list(self._rows):
            self.remove(r)
        self._rows.clear()
        self._items.clear()
        for t in items:
            self.add_item(t)
        self._update_limit()

    def get_items(self) -> list[str]:
        items = list(self._items)
        pending = self.add_row.get_text().strip()
        if pending and len(items) < self.max_items:  # nicht übernommenen Text nicht verlieren
            items.append(pending)
        return items


class MultilineField(Gtk.Box):
    """Mehrzeiliges Textfeld mit optionalem Diktat-Button."""

    def __init__(self, label: str, dictation: Callable[[DictationButton, Callable[[str], None]], None]
                 | None = None, placeholder: str = "", max_chars: int = MAX_TEXT_LEN,
                 on_limit: Callable[[str], None] | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        head = Gtk.Box(spacing=8)
        self.title = Gtk.Label(label=label, xalign=0, wrap=True, hexpand=True)
        self.title.add_css_class("dialog-question")
        head.append(self.title)
        self.dictation_button: DictationButton | None = None
        if dictation is not None:
            self.dictation_button = DictationButton(
                f"{label} – diktieren", lambda b: dictation(b, self.append_text))
            head.append(self.dictation_button)
        self.append(head)
        self.view = Gtk.TextView()
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_accepts_tab(False)
        self.view.add_css_class("addendum-view")
        self.view.set_top_margin(2)
        self.view.set_bottom_margin(2)
        a11y(self.view, label, False)
        self.title.set_mnemonic_widget(self.view)
        self.append(self.view)
        self.placeholder = placeholder
        self.max_chars = max_chars

        def limit_hit() -> None:
            self.view.error_bell()
            if on_limit is not None:
                on_limit(limit_message(max_chars))

        limit_text_buffer(self.view.get_buffer(), max_chars, limit_hit)

    def get_text(self) -> str:
        buf = self.view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()

    def set_text(self, text: str) -> None:
        self.view.get_buffer().set_text(text or "")

    def append_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        cur = self.get_text()
        self.set_text(f"{cur} {text}".strip() if cur else text)
