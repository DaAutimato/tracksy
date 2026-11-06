"""Dialog zum Bearbeiten bzw. manuellen Anlegen eines Eintrags."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..core.sanitize import MAX_FIELD_LEN, MAX_TEXT_LEN, InputLimitError  # noqa: E402
from .viewmodel import (  # noqa: E402
    combine_local,
    drop_unchanged_rounded,
    format_date_de,
    format_date_short,
    format_hhmm,
    parse_date_de,
    parse_minutes,
    parse_time_hm,
    resolve_end,
    spans_beyond_default,
)
from .widgets import (  # noqa: E402
    ItemsEditor,
    LocationToggle,
    MultilineField,
    SuggestEntryRow,
    a11y,
    limit_length,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..app import ZeitApp
    from ..core.models import Entry


class EntryEditor(Adw.Dialog):
    """Modi: gespeicherter Eintrag, neuer Eintrag, laufender Task (eingeschränkt)."""

    def __init__(self, app: "ZeitApp", entry: "Entry | None" = None, day: date | None = None,
                 active: bool = False) -> None:
        super().__init__()
        self.app = app
        self.entry = entry
        self.active = active
        self.set_content_width(520)
        self.set_content_height(760)
        if active:
            title = "Laufenden Task bearbeiten"
        elif entry is None:
            title = "Eintrag anlegen"
        else:
            title = "Eintrag bearbeiten"
        self.set_title(title)

        tv = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Abbrechen")
        cancel.connect("clicked", lambda *_: self.close())
        a11y(cancel, "Abbrechen")
        save = Gtk.Button(label="Speichern")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_: self._save())
        a11y(save, "Eintrag speichern", "Speichern (Strg+Enter)")
        header.pack_start(cancel)
        header.pack_end(save)
        tv.add_top_bar(header)
        self.banner = Adw.Banner()
        tv.add_top_bar(self.banner)

        page = Adw.PreferencesPage()
        g1 = Adw.PreferencesGroup(title="Task")
        self.customer = SuggestEntryRow("Kunde", app.customers)
        self.topic = Adw.EntryRow(title="Thema")
        self.ticket = Adw.EntryRow(title="Ticket")
        a11y(self.topic, "Thema", False)
        a11y(self.ticket, "Ticket", False)
        limit_length(self.topic, MAX_FIELD_LEN)
        limit_length(self.ticket, MAX_FIELD_LEN)
        self.location = LocationToggle(app.config.general.default_location)
        loc_row = Adw.ActionRow(title="Ort")
        loc_row.add_suffix(self.location)
        for w in (self.customer, self.topic, self.ticket, loc_row):
            g1.add(w)
        page.add(g1)

        g2 = Adw.PreferencesGroup(title="Zeit")
        self.time_group = g2
        self.date = Adw.EntryRow(title="Datum (TT.MM.JJJJ)")
        self.start = Adw.EntryRow(title="Beginn (HH:MM)")
        self.end = Adw.EntryRow(title="Ende (HH:MM)")
        self.pause = Adw.EntryRow(title="Pause in Minuten")
        for w, n in ((self.date, "Datum"), (self.start, "Beginn"), (self.end, "Ende"),
                     (self.pause, "Pause in Minuten")):
            a11y(w, n, False)
            g2.add(w)
        if active:
            self.end.set_visible(False)
            self.pause.set_visible(False)
            g2.set_description("Ende und Pausen ergeben sich beim Abschließen.")
        page.add(g2)

        g3 = Adw.PreferencesGroup(title="Beschreibung")
        self.description = Adw.EntryRow(title="Beschreibung")
        a11y(self.description, "Beschreibung", False)
        limit_length(self.description, MAX_TEXT_LEN)
        g3.add(self.description)
        page.add(g3)

        self.addendum = MultilineField("Ergänzung", on_limit=self.toast)
        self.items = ItemsEditor("Weitere erledigte Tätigkeiten")
        if not active:
            g4 = Adw.PreferencesGroup()
            g4.add(self.addendum)
            page.add(g4)
            page.add(self.items)

        tv.set_content(page)
        self.toasts = Adw.ToastOverlay(child=tv)
        self.set_child(self.toasts)

        sc = Gtk.ShortcutController()
        sc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        sc.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>Return"),
            Gtk.CallbackAction.new(lambda *_: (self._save(), True)[1])))
        self.add_controller(sc)

        self._load(day)
        self.set_focus(self.customer)

    def _load(self, day: date | None) -> None:
        e = self.entry
        if e is None:
            d = day or date.today()
            now = datetime.now()
            end = now.replace(second=0, microsecond=0)
            start = end - timedelta(hours=1)
            self.date.set_text(format_date_de(d))
            self.start.set_text(format_hhmm(start))
            self.end.set_text(format_hhmm(end))
            self.pause.set_text("0")
            return
        st = e.start_at.astimezone()
        self.customer.set_text_silent(e.customer)
        self.topic.set_text(e.topic)
        self.ticket.set_text(e.ticket)
        self.location.set_value(str(e.location))
        self.date.set_text(format_date_de(st.date()))
        self.start.set_text(format_hhmm(st))
        self.end.set_text(format_hhmm(e.end_at.astimezone()) if e.end_at else "")
        if e.end_at is not None and not self.active and spans_beyond_default(e.start_at, e.end_at):
            # Eintrag über 24 h / mehrere Tage: Enddatum ist im Feld nicht sichtbar
            self.time_group.set_description(
                f"Ende am {format_date_short(e.end_at.astimezone().date())}. Eine geänderte "
                "Ende-Uhrzeit gilt für den Tag des Beginns bzw. den Folgetag.")
        self.pause.set_text(str(int(e.pause_seconds) // 60))
        self.description.set_text(e.description)
        self.addendum.set_text(e.addendum)
        self.items.set_items(e.items)

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=5))

    def _field_widget(self, field: str) -> Gtk.Widget | None:
        return {"customer": self.customer, "topic": self.topic, "ticket": self.ticket,
                "description": self.description, "addendum": self.addendum.view,
                "items": self.items.add_row}.get(field)

    def _fail(self, msg: str, row: Gtk.Widget | None = None) -> None:
        self.banner.set_title(msg)
        self.banner.set_revealed(True)
        if row is not None:
            row.add_css_class("error")
            row.grab_focus()

    def _collect(self) -> dict[str, Any] | None:
        for r in (self.date, self.start, self.end, self.pause, self.customer, self.topic,
                  self.ticket, self.description, self.addendum.view, self.items.add_row):
            r.remove_css_class("error")
        try:
            d = parse_date_de(self.date.get_text())
        except ValueError as exc:
            self._fail(str(exc), self.date)
            return None
        try:
            start = combine_local(d, parse_time_hm(self.start.get_text()))
        except ValueError as exc:
            self._fail(str(exc), self.start)
            return None
        data: dict[str, Any] = {
            "customer": self.customer.get_text().strip(),
            "topic": self.topic.get_text().strip(),
            "ticket": self.ticket.get_text().strip(),
            "location": self.location.get_value(),
            "description": self.description.get_text().strip(),
            "start_at": start,
        }
        if self.active:
            return data
        try:
            end_t = parse_time_hm(self.end.get_text())
        except ValueError as exc:
            self._fail(str(exc), self.end)
            return None
        e = self.entry
        end = resolve_end(d, end_t, start, e.start_at if e else None,
                          e.end_at if e else None)
        try:
            pause_min = parse_minutes(self.pause.get_text())
        except ValueError:
            self._fail("Pause bitte als Minuten (z. B. 15) eingeben", self.pause)
            return None
        data.update({
            "end_at": end,
            "pause_seconds": pause_min * 60,
            "addendum": self.addendum.get_text(),
            "items": self.items.get_items(),
        })
        return data

    def _save(self) -> None:
        data = self._collect()
        if data is None:
            return
        svc = self.app.service
        try:
            if self.entry is None:
                svc.create_entry(**data, source="gui")
                msg = "Eintrag angelegt"
            else:
                # Beginn/Ende/Pause nur ändern, wenn sich die angezeigte Minute
                # geändert hat – sonst gingen Sekunden der Rohzeit verloren
                drop_unchanged_rounded(data, self.entry, self.active)
                svc.update_entry(self.entry.uuid, **data)
                msg = "Änderungen gespeichert"
        except InputLimitError as exc:
            self.toast(str(exc))
            w = self._field_widget(exc.field)
            if w is not None:
                w.add_css_class("error")
                w.grab_focus()
            return
        except (ValueError, LookupError) as exc:
            self._fail(str(exc))
            return
        self.app.toast(msg)
        self.close()
