"""Übersicht: Tages-/Monatsansicht mit Summen je Kunde, Bearbeiten, Export."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .viewmodel import (  # noqa: E402
    billable_of,
    day_label,
    entry_row_subtitle,
    entry_row_title,
    format_date_compact,
    format_date_long,
    format_hm,
    format_hours_decimal,
    format_month,
    group_by_day,
    month_range,
    shift_month,
    sums_by_customer,
)
from .widgets import a11y, clear_box  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ..app import ZeitApp


# Lange Kunden-/Themennamen: ActionRow bricht zeichenweise um; Zeilen
# begrenzen, damit ein Extremwert nicht die ganze Übersicht füllt.
ROW_TITLE_LINES = 2
ROW_SUBTITLE_LINES = 2


def _limit_lines(row: Adw.ActionRow, full_text: str | None) -> None:
    row.set_title_lines(ROW_TITLE_LINES)
    row.set_subtitle_lines(ROW_SUBTITLE_LINES)
    if full_text and len(full_text) > 60:
        row.set_tooltip_text(full_text)


class OverviewPage(Gtk.Box):
    def __init__(self, app: "ZeitApp", window) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.window = window
        self.mode = "day"
        self.day = date.today()

        # Leiste: breit einzeilig [Tag|Monat] [‹ Zeitraum ›] [+] [Exportieren];
        # schmal (set_compact) zweizeilig, Export nur als Symbol – erhöht die
        # Mindestbreite des Fensters nicht.
        self.bar = bar = Gtk.Box(spacing=8)
        bar.add_css_class("overview-bar")
        bar.set_margin_top(12)
        bar.set_margin_bottom(6)
        bar.set_margin_start(16)
        bar.set_margin_end(16)

        modes = Gtk.Box()
        modes.add_css_class("linked")
        modes.add_css_class("segmented")
        self.day_btn = Gtk.ToggleButton(label="Tag", active=True)
        self.month_btn = Gtk.ToggleButton(label="Monat")
        self.month_btn.set_group(self.day_btn)
        a11y(self.day_btn, "Tagesansicht")
        a11y(self.month_btn, "Monatsansicht")
        self.day_btn.connect("toggled", self._on_mode)
        modes.append(self.day_btn)
        modes.append(self.month_btn)
        bar.append(modes)

        self.nav = nav = Gtk.Box(spacing=2, hexpand=True, halign=Gtk.Align.CENTER)
        prev_btn = Gtk.Button(icon_name="go-previous-symbolic")
        prev_btn.add_css_class("flat")
        a11y(prev_btn, "Vorheriger Zeitraum")
        prev_btn.connect("clicked", lambda *_: self.shift(-1))
        self.period = Gtk.Button(label="")
        self.period.add_css_class("flat")
        self.period.add_css_class("period-label")
        a11y(self.period, "Zeitraum – klicken für heute", "Zurück zu heute")
        self.period.connect("clicked", lambda *_: self.go_today())
        next_btn = Gtk.Button(icon_name="go-next-symbolic")
        next_btn.add_css_class("flat")
        a11y(next_btn, "Nächster Zeitraum")
        next_btn.connect("clicked", lambda *_: self.shift(1))
        nav.append(prev_btn)
        nav.append(self.period)
        nav.append(next_btn)
        bar.append(nav)
        # Platzhalter nur im schmalen Modus (dann sitzt nav in der zweiten Zeile)
        self.bar_spacer = Gtk.Box(hexpand=True, visible=False)
        bar.append(self.bar_spacer)

        add_btn = Gtk.Button(icon_name="list-add-symbolic")
        add_btn.add_css_class("flat")
        a11y(add_btn, "Eintrag manuell anlegen")
        add_btn.connect("clicked", lambda *_: self.new_entry())
        self.export_btn = Gtk.Button(label="Exportieren")
        a11y(self.export_btn, "Exportieren", "Leistungsnachweis als XLSX oder CSV exportieren")
        self.export_btn.connect("clicked", lambda *_: self.open_export())
        bar.append(add_btn)
        bar.append(self.export_btn)
        self.append(bar)
        self.nav_row = Gtk.Box(halign=Gtk.Align.CENTER, visible=False)
        self.nav_row.set_margin_bottom(4)
        self.append(self.nav_row)
        self.compact = False
        self.compact_level = 0

        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=720)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        self.content.set_margin_top(10)
        self.content.set_margin_bottom(24)
        self.content.set_margin_start(16)
        self.content.set_margin_end(16)
        clamp.set_child(self.content)
        scroller.set_child(clamp)
        self.append(scroller)

    def set_compact(self, level: int | bool) -> None:
        """Breite anpassen: 0 = einzeilig; 1 = Zeitraum in eigener Zeile, Export nur
        als Symbol; 2 = zusätzlich kurzes Datum (sehr schmale Fenster)."""
        level = int(level)
        compact = level > 0
        self.compact_level = level
        if compact != self.compact:
            self.compact = compact
            self.nav.get_parent().remove(self.nav)
            if compact:
                self.nav.set_hexpand(False)
                self.nav_row.append(self.nav)
                self.export_btn.set_icon_name("document-save-symbolic")
            else:
                self.nav.set_hexpand(True)
                self.bar.insert_child_after(self.nav, self.bar.get_first_child())
                self.export_btn.set_label("Exportieren")
            self.nav_row.set_visible(compact)
            self.bar_spacer.set_visible(compact)
        self._update_period_label()

    def _update_period_label(self) -> None:
        if self.mode == "month":
            label = format_month(self.day.year, self.day.month)
        elif self.compact_level >= 2:
            label = format_date_compact(self.day)
        else:
            label = format_date_long(self.day)
        self.period.set_label(label)

    # ------------------------------------------------------------- Navigation
    def _on_mode(self, *_a) -> None:
        self.mode = "day" if self.day_btn.get_active() else "month"
        self.refresh()

    def set_mode(self, mode: str) -> None:
        (self.day_btn if mode == "day" else self.month_btn).set_active(True)

    def shift(self, delta: int) -> None:
        if self.mode == "day":
            self.day += timedelta(days=delta)
        else:
            y, m = shift_month(self.day.year, self.day.month, delta)
            self.day = date(y, m, 1)
        self.refresh()

    def go_today(self) -> None:
        self.day = date.today()
        self.refresh()

    def period_range(self) -> tuple[date, date]:
        if self.mode == "day":
            return self.day, self.day
        return month_range(self.day.year, self.day.month)

    # ----------------------------------------------------------------- Aufbau
    def refresh(self) -> None:
        von, bis = self.period_range()
        self._update_period_label()
        step = self.app.config.export.rounding_minutes
        try:
            entries = self.app.service.list_entries(von, bis)
        except Exception as exc:  # pragma: no cover - DB-Fehler sichtbar machen
            entries = []
            self.window.toast(f"Einträge konnten nicht geladen werden: {exc}")
        clear_box(self.content)

        if not entries:
            sp = Adw.StatusPage(
                icon_name="x-office-calendar-symbolic",
                title="Keine Einträge",
                description=("An diesem Tag wurde noch nichts erfasst."
                             if self.mode == "day" else "In diesem Monat wurde noch nichts erfasst."),
            )
            btn = Gtk.Button(label="Eintrag anlegen", halign=Gtk.Align.CENTER)
            btn.add_css_class("pill")
            btn.connect("clicked", lambda *_: self.new_entry())
            a11y(btn, "Eintrag anlegen")
            sp.set_child(btn)
            sp.set_vexpand(True)
            self.content.append(sp)
            return

        total = sum(int(e.net_seconds()) for e in entries)
        try:
            schedule = self.app.config.worktime.schedule()
        except Exception:  # pragma: no cover - ungültige Config wird beim Laden gemeldet
            schedule = None
        billed = sum(billable_of(e, step, schedule) for e in entries)
        head = Gtk.Box(spacing=12)
        tbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        tl = Gtk.Label(label="Gesamt", xalign=0)
        tl.add_css_class("dim-label")
        tv = Gtk.Label(label=format_hm(total), xalign=0)
        tv.add_css_class("total-value")
        a11y(tv, f"Gesamt {format_hm(total)}", False)
        tbox.append(tl)
        tbox.append(tv)
        head.append(tbox)
        bbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bl = Gtk.Label(label=f"abrechenbar ({step} min)" if step else "abrechenbar", xalign=1)
        bl.add_css_class("dim-label")
        bv = Gtk.Label(label=format_hours_decimal(billed), xalign=1)
        bv.add_css_class("total-value")
        bbox.append(bl)
        bbox.append(bv)
        head.append(bbox)
        self.content.append(head)

        sums = Adw.PreferencesGroup(title="Summen je Kunde")
        for s in sums_by_customer(entries, step, schedule):
            row = Adw.ActionRow(title=GLib.markup_escape_text(s.customer or "Ohne Kunde"))
            _limit_lines(row, s.customer)
            sub = f"{s.count} {'Eintrag' if s.count == 1 else 'Einträge'}"
            if s.onsite_seconds:
                sub += f", davon vor Ort {format_hm(s.onsite_seconds)}"
            row.set_subtitle(sub)
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
            v1 = Gtk.Label(label=format_hm(s.seconds), xalign=1)
            v1.add_css_class("sum-value")
            v2 = Gtk.Label(label=f"abr. {format_hours_decimal(s.billed_seconds)}", xalign=1)
            v2.add_css_class("sum-billed")
            vbox.append(v1)
            vbox.append(v2)
            row.add_suffix(vbox)
            sums.add(row)
        self.content.append(sums)

        for g in group_by_day(entries):
            grp = Adw.PreferencesGroup(title=day_label(g.day))
            dsum = Gtk.Label(label=format_hm(g.seconds))
            dsum.add_css_class("sum-value")
            grp.set_header_suffix(dsum)
            for e in g.entries:
                row = Adw.ActionRow(title=GLib.markup_escape_text(entry_row_title(e)),
                                    subtitle=GLib.markup_escape_text(entry_row_subtitle(e)))
                row.set_activatable(True)
                _limit_lines(row, entry_row_title(e))
                dur = Gtk.Label(label=format_hm(e.net_seconds()))
                dur.add_css_class("sum-value")
                row.add_suffix(dur)
                icon = Gtk.Image(icon_name="go-next-symbolic")
                row.add_suffix(icon)
                a11y(row, f"Eintrag {entry_row_title(e)} bearbeiten", False)
                row.connect("activated", lambda _r, uuid=e.uuid: self.edit_entry(uuid))
                grp.add(row)
            self.content.append(grp)

    # ---------------------------------------------------------------- Aktionen
    def edit_entry(self, uuid: str) -> None:
        from .entry_editor import EntryEditor  # noqa: PLC0415

        try:
            entry = self.app.service.get_entry(uuid)
        except LookupError:
            self.window.toast("Eintrag nicht gefunden")
            return
        EntryEditor(self.app, entry=entry).present(self.window)

    def new_entry(self) -> None:
        from .entry_editor import EntryEditor  # noqa: PLC0415

        day = self.day if self.mode == "day" else date.today()
        EntryEditor(self.app, day=day).present(self.window)

    def open_export(self) -> None:
        from .export_dialog import ExportDialog  # noqa: PLC0415

        ExportDialog(self.app, self.window, self.day.year, self.day.month).present(self.window)
