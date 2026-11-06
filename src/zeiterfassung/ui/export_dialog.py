"""Export-Dialog: Monat + Kunde wählen, XLSX/CSV, Speicherort per Gtk.FileDialog."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .viewmodel import (  # noqa: E402
    ALL_CUSTOMERS_NOTE_TEXT,
    ALL_CUSTOMERS_NOTE_TITLE,
    format_month,
    month_range,
    needs_overwrite_confirm,
    overwrite_body,
    shift_month,
    with_export_suffix,
)
from .widgets import a11y  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ..app import ZeitApp


ALL_LABEL = "Alle Kunden"


def export_module():
    try:
        from .. import export  # noqa: PLC0415
    except Exception:  # pragma: no cover
        return None
    if not hasattr(export, "export_xlsx") or not hasattr(export, "export_csv"):
        return None
    return export


TEMPLATE_NOTE = ("Bilder (z. B. eine Unterschrift), Kommentare und ausgeblendete Blätter der "
                 "Vorlage werden in jeden Export übernommen. Vorlage vor der Weitergabe prüfen.")


class NoteBox(Gtk.Box):
    """Hinweis-Kasten (Symbol, Titel, Text) – optisch und für die Tastatur keine Einstellung.

    Nicht fokussierbar, Rolle ``NOTE`` für AT-SPI; Farbe kommt aus ``style.css``
    (Klassen ``note-box`` + ``note-<kind>``; nicht die
    Adwaita-Klasse ``warning``, die den Text einfärben würde).
    """

    def __init__(self, kind: str, icon_name: str, title: str, text: str) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                         accessible_role=Gtk.AccessibleRole.NOTE)
        self.add_css_class("note-box")
        self.add_css_class(f"note-{kind}")
        self.set_focusable(False)
        icon = Gtk.Image(icon_name=icon_name, valign=Gtk.Align.START,
                         accessible_role=Gtk.AccessibleRole.PRESENTATION)
        icon.add_css_class("note-icon")
        self.append(icon)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self._title = Gtk.Label(label=title, xalign=0, wrap=True, max_width_chars=40,
                                use_markup=False)
        self._title.add_css_class("note-title")
        self._text = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=40,
                               use_markup=False)
        self._text.add_css_class("note-text")
        texts.append(self._title)
        texts.append(self._text)
        self.append(texts)

    def get_title(self) -> str:
        return self._title.get_label()

    def set_title(self, title: str) -> None:
        self._title.set_label(title)

    def get_subtitle(self) -> str:
        return self._text.get_label()


class ExportDialog(Adw.Dialog):
    CONTENT_WIDTH = 460

    def __init__(self, app: "ZeitApp", window, year: int, month: int) -> None:
        super().__init__()
        self.app = app
        self.window = window
        self.year, self.month = year, month
        self.set_title("Exportieren")
        # F-02: Höhe folgt dem Inhalt (Hinweise ein-/ausgeblendet, siehe _fit_height);
        # reicht der Platz nicht, scrollt der Inhalt, „Speichern unter …“ bleibt in der
        # Fußleiste sichtbar
        self.set_content_width(self.CONTENT_WIDTH)

        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        tv.add_top_bar(header)
        grp = Adw.PreferencesGroup(
            description="Leistungsnachweis pro Kunde (oder für alle Kunden) und Monat. Zeiten werden je Eintrag auf "
                        f"{app.config.export.rounding_minutes} Minuten aufgerundet; "
                        "die gespeicherten Rohzeiten bleiben unverändert.")
        self.month_row = Adw.ActionRow(title="Monat")
        prev_b = Gtk.Button(icon_name="go-previous-symbolic", valign=Gtk.Align.CENTER)
        prev_b.add_css_class("flat")
        a11y(prev_b, "Vorheriger Monat")
        prev_b.connect("clicked", lambda *_: self._shift(-1))
        self.month_label = Gtk.Label()
        next_b = Gtk.Button(icon_name="go-next-symbolic", valign=Gtk.Align.CENTER)
        next_b.add_css_class("flat")
        a11y(next_b, "Nächster Monat")
        next_b.connect("clicked", lambda *_: self._shift(1))
        self.month_row.add_suffix(prev_b)
        self.month_row.add_suffix(self.month_label)
        self.month_row.add_suffix(next_b)
        grp.add(self.month_row)

        self.customer_model = Gtk.StringList()
        self.customer_row = Adw.ComboRow(title="Kunde", model=self.customer_model)
        a11y(self.customer_row, "Kunde für Export", False)
        grp.add(self.customer_row)
        self.format_row = Adw.ComboRow(title="Format",
                                       model=Gtk.StringList.new(["XLSX (Excel)", "CSV"]))
        a11y(self.format_row, "Exportformat", False)
        grp.add(self.format_row)

        # Gesamtexport enthält fremde Kundendaten – als Warnhinweis direkt unter
        # der Einstellungskarte, nicht als Zeile darin (F-03)
        self.all_note = NoteBox("warning", "dialog-warning-symbolic",
                                ALL_CUSTOMERS_NOTE_TITLE, ALL_CUSTOMERS_NOTE_TEXT)
        self.all_note.set_name("export-all-customers-note")
        a11y(self.all_note, f"Hinweis: {ALL_CUSTOMERS_NOTE_TITLE}. {ALL_CUSTOMERS_NOTE_TEXT}",
             False)
        self.all_note.set_visible(False)

        # Inhalte einer eigenen Vorlage gehen in jeden Export über
        tpl = self._template()
        self.template_note = NoteBox("info", "dialog-information-symbolic",
                                     f"Eigene Vorlage: {tpl.name}" if tpl else "", TEMPLATE_NOTE)
        self.template_note.set_name("export-template-note")
        a11y(self.template_note, "Hinweis zur eigenen Vorlage", False)
        self.format_row.connect("notify::selected", lambda *_: self._update_template_note())

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.add_css_class("export-content")
        content.append(grp)
        content.append(self.all_note)
        content.append(self.template_note)
        clamp = Adw.Clamp(maximum_size=self.CONTENT_WIDTH,
                          tightening_threshold=self.CONTENT_WIDTH, child=content)
        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                           propagate_natural_height=True, vexpand=True,
                                           child=clamp)
        tv.set_content(self.scroller)

        self.btn = Gtk.Button(label="Speichern unter …", halign=Gtk.Align.CENTER)
        self.btn.add_css_class("suggested-action")
        self.btn.add_css_class("pill")
        a11y(self.btn, "Export speichern unter")
        self.btn.connect("clicked", lambda *_: self._ask_report_kind())
        self.info = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=48)
        self.info.add_css_class("dim-label")
        self.info.set_visible(False)
        # leere Statuszeile nimmt keinen Platz in der Fußleiste ein
        self.info.connect("notify::label", self._on_info_changed)
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        footer.add_css_class("export-footer")
        footer.append(self.info)
        footer.append(self.btn)
        tv.add_bottom_bar(footer)
        self.set_child(tv)
        self.customer_row.connect("notify::selected", lambda *_: self._update_all_note())
        self._update_template_note()
        self._load()

    def _fit_height(self) -> None:
        """Dialoghöhe = natürliche Höhe des Inhalts (F-02).

        ``follows-content-size`` taugt hier nicht: umbrechende Texte haben eine kleine
        natürliche Breite, der Dialog würde schmal. Adw.Dialog begrenzt die Höhe selbst
        auf das Fenster; der Rest scrollt.
        """
        child = self.get_child()
        if child is None:
            return
        _min, nat, _b1, _b2 = child.measure(Gtk.Orientation.VERTICAL, self.CONTENT_WIDTH)
        self.set_content_height(nat)

    def _on_info_changed(self, label: Gtk.Label, _pspec) -> None:
        # leere Statuszeile nimmt keinen Platz in der Fußleiste ein
        label.set_visible(bool(label.get_label()))
        self._fit_height()

    def _update_all_note(self) -> None:
        """Hinweis + Beschreibung (AT-SPI) nur bei „Alle Kunden“."""
        customer, _fmt = self._selection()
        is_all = customer is None
        self.all_note.set_visible(is_all)
        desc = f"{ALL_CUSTOMERS_NOTE_TITLE}. {ALL_CUSTOMERS_NOTE_TEXT}" if is_all else ""
        self.customer_row.update_property([Gtk.AccessibleProperty.DESCRIPTION], [desc])
        self.customer_row.zeit_a11y_description = desc  # für Tests (kein Getter in GTK 4.14)
        self._fit_height()

    def _update_template_note(self) -> None:
        xlsx = self.format_row.get_selected() != 1
        self.template_note.set_visible(xlsx and self._template() is not None)
        self._fit_height()

    def _shift(self, d: int) -> None:
        self.year, self.month = shift_month(self.year, self.month, d)
        self._load()

    def _load(self) -> None:
        self.month_label.set_label(format_month(self.year, self.month))
        von, bis = month_range(self.year, self.month)
        entries = self.app.service.list_entries(von, bis)
        customers = sorted({e.customer for e in entries if e.customer}, key=str.lower)
        # erster Eintrag: Gesamtexport aller Kunden; vorausgewählt bleibt wie bisher
        # der erste Kunde
        choices = ([ALL_LABEL] if customers else []) + customers
        self.customer_model.splice(0, self.customer_model.get_n_items(), choices)
        if customers:
            self.customer_row.set_selected(1)
        has = bool(customers)
        self.customer_row.set_sensitive(has)
        self._update_all_note()
        mod = export_module()
        if mod is None:
            self.info.set_label("Das Export-Modul ist in dieser Version noch nicht verfügbar.")
            self.btn.set_sensitive(False)
        elif not has:
            self.info.set_label("In diesem Monat gibt es keine gespeicherten Einträge.")
            self.btn.set_sensitive(False)
        else:
            self.info.set_label("")
            self.btn.set_sensitive(True)

    def _selection(self) -> tuple[str | None, str]:
        """(Kunde, Format); Kunde ``None`` = alle Kunden, ``""`` = keine Auswahl."""
        item = self.customer_row.get_selected_item()
        fmt = "csv" if self.format_row.get_selected() == 1 else "xlsx"
        if item is None:
            return "", fmt
        if self.customer_row.get_selected() == 0:
            return None, fmt
        return item.get_string(), fmt

    def _ask_report_kind(self) -> Adw.AlertDialog | None:
        """Vor dem Speichern fragen: Monatsbericht (Tage zusammengefasst) oder einzeln."""
        customer, _fmt = self._selection()
        if customer == "":
            return None
        dlg = Adw.AlertDialog(
            heading="Welcher Bericht?",
            body="Monatsbericht: alle Tätigkeiten eines Tages in einer Zeile – übersichtlich "
                 "für den Kunden.\n\nTätigkeitsbericht: jede Tätigkeit einzeln mit eigener "
                 "Beginn- und Endzeit (wie bisher).\n\nDie Monatssumme ist in beiden gleich.")
        dlg.add_response("cancel", "Abbrechen")
        dlg.add_response("single", "Tätigkeitsbericht")
        dlg.add_response("summary", "Monatsbericht")
        dlg.set_response_appearance("summary", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("summary")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, r: self._choose_file(summary=r == "summary")
                    if r in ("single", "summary") else None)
        dlg.present(self)
        return dlg

    def _choose_file(self, summary: bool = False) -> None:
        mod = export_module()
        if mod is None:
            return
        customer, fmt = self._selection()
        if customer == "":
            return
        month = f"{self.year:04d}-{self.month:02d}"
        fd = Gtk.FileDialog(title="Export speichern")
        fd.set_initial_name(mod.suggest_filename(customer or mod.ALL_CUSTOMERS, month, fmt,
                                                 summary=summary))
        docs = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
        if docs:
            fd.set_initial_folder(Gio.File.new_for_path(docs))
        flt = Gtk.FileFilter()
        flt.set_name("Excel-Arbeitsmappe" if fmt == "xlsx" else "CSV-Datei")
        flt.add_suffix(fmt)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(flt)
        fd.set_filters(filters)

        def done(dialog, res) -> None:
            try:
                f = dialog.save_finish(res)
            except GLib.Error:
                return  # abgebrochen
            if f is None or f.get_path() is None:
                return
            self._export(Path(f.get_path()), customer, month, fmt, summary=summary)

        fd.save(self.window, None, done)

    def _template(self):
        tp = getattr(self.app.config.export, "template_path", "") or ""
        return Path(tp).expanduser() if tp else None

    def _export(self, path: Path, customer: str | None, month: str, fmt: str,
                summary: bool = False, confirmed: bool = False) -> Adw.AlertDialog | None:
        """Exportieren; gibt die Überschreib-Rückfrage zurück, falls eine nötig ist."""
        target = with_export_suffix(path, fmt)
        if not confirmed and needs_overwrite_confirm(path, target):
            return self._confirm_overwrite(
                target, fmt,
                lambda: self._export(target, customer, month, fmt, summary=summary,
                                     confirmed=True))
        self._write(target, customer, month, fmt, summary)
        return None

    def _confirm_overwrite(self, target: Path, fmt: str, on_overwrite) -> Adw.AlertDialog:
        """Ergänzte Endung trifft auf vorhandene Datei – nur nach Bestätigung ersetzen."""
        dlg = Adw.AlertDialog(heading="Datei überschreiben?", body=overwrite_body(target, fmt))
        dlg.add_response("cancel", "Abbrechen")
        dlg.add_response("overwrite", "Überschreiben")
        dlg.set_response_appearance("overwrite", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def on_response(_d, response: str) -> None:
            if response == "overwrite":
                on_overwrite()
            else:
                self.info.set_label("Nicht gespeichert. Die vorhandene Datei bleibt unverändert.")

        dlg.connect("response", on_response)
        dlg.present(self)
        return dlg

    def _write(self, path: Path, customer: str | None, month: str, fmt: str,
               summary: bool) -> None:
        mod = export_module()
        von, bis = month_range(self.year, self.month)
        try:
            entries = mod.filter_entries(self.app.service.list_entries(von, bis),
                                         customer=customer, month=month)
            rounding = self.app.config.export.rounding_minutes
            schedule = self.app.config.worktime.schedule()
            if summary:
                entries = mod.summarize_by_day(entries)
            if fmt == "csv":
                out = mod.export_csv(entries, path, round_minutes=rounding, schedule=schedule)
            else:
                out = mod.export_xlsx(entries, path, customer=customer or mod.ALL_CUSTOMERS,
                                      month=month,
                                      employee_name=self.app.config.general.employee_name,
                                      round_minutes=rounding,
                                      template_path=self._template(),
                                      show_customer=customer is None,
                                      schedule=schedule)
        except Exception as exc:
            self.info.set_label(f"Export fehlgeschlagen: {exc}")
            return
        uri = Gio.File.new_for_path(str(out)).get_uri()

        def open_folder() -> None:
            Gtk.FileLauncher.new(Gio.File.new_for_uri(uri)).open_containing_folder(
                self.window, None, None)

        self.app.toast(f"Exportiert: {Path(out).name}", "Ordner öffnen", open_folder,
                       public="Export gespeichert")
        self.close()
