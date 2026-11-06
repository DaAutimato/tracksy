"""Einstellungen → Daten: Aufbewahrungsfrist, endgültiges Löschen, Sicherung.

Alle Löschvorgänge laufen über ``TrackerService.purge_*``; die Rückfrage
(„endgültig, nicht rückgängig“) macht diese Seite. Liegen interne Sicherungen
im Datenverzeichnis, bietet die Rückfrage an, sie mitzulöschen
(Standard: aus – nie ohne Bestätigung). Das Service-Ereignis
``purged`` lädt die Übersicht neu (``MainWindow.update_status``).
``keep_backup`` wird bewusst nicht angeboten (Art. 17 DSGVO).

„Alte Einträge löschen“ arbeitet **bis einschließlich** des gewählten Tags
(``TrackerService.purge_until``) – Einträge von heute sind also wählbar.

Speicherort ändern: Ordner wählen → ``TrackerService.check_relocation_target``
→ Bestätigung (Datenschutz-Warnung bei Cloud-Sync-Ordnern) →
``ZeitApp.relocate_data`` (kopieren, prüfen, Konfiguration, Altbestand nach
``_alt-…``) → Neustart. Mit ``ZEIT_DATA_DIR`` ist der Knopf deaktiviert.
"""

from __future__ import annotations

import logging
import os
import stat
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from ..logutil import log_failure  # noqa: E402
from ..storage.maintenance import retention_cutoff  # noqa: E402
from .viewmodel import (  # noqa: E402
    BACKUP_NOTE,
    PURGE_DISCARDED_BODY,
    RETENTION_LEGAL_NOTE,
    backups_left_hint,
    backups_option_hint,
    backups_option_label,
    count_backups,
    count_entries,
    purge_before_heading,
    purge_until_body,
    purge_until_label,
    purge_until_preview,
    purge_discarded_heading,
    purge_done,
    retention_hint,
)
from .widgets import a11y  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ..app import ZeitApp

log = logging.getLogger(__name__)

STORAGE_DESCRIPTION = (
    "Im Datenverzeichnis liegen deine Zeiterfassungsdaten: die Datenbank "
    "(zeiterfassung.sqlite3) mit allen Einträgen, Tätigkeiten, Pausen und dem "
    "Änderungsprotokoll (Audit-Log), das CSV-Protokoll (protokoll.csv) und die internen "
    "Sicherungen (Ordner „backups“, CSV-Sicherungen *.bak). Einstellungen und "
    "Sprachmodelle liegen getrennt und ziehen beim Ändern nicht mit um.")
ENV_LOCKED_HINT = ("Der Speicherort ist über die Umgebungsvariable ZEIT_DATA_DIR festgelegt "
                   "und kann in der App nicht geändert werden.")
CLOUD_WARNING = (
    "Datenschutz: Der Ordner sieht nach einem Cloud-Sync-Ordner aus (z. B. Dropbox, "
    "Nextcloud, OneDrive, Google Drive, MEGA). Deine Zeiterfassungsdaten würden dann ggf. "
    "das Gerät verlassen und bei einem fremden Anbieter gespeichert.")
NOT_EMPTY_NOTE = ("Der Ordner enthält bereits andere Dateien. Empfohlen ist ein eigener, "
                  "leerer Ordner.")

PRIVATE_DIR_MODE = 0o700  # wie storage.relocate (DIR_MODE)


def mode_change_note(old_mode: int, not_empty: bool) -> str:
    """Bestätigung: Tracksy setzt die Rechte des Zielordners auf 0700."""
    text = (f"Tracksy schränkt die Rechte des Ordners auf {PRIVATE_DIR_MODE:04o} ein "
            f"(nur du hast Zugriff, bisher {old_mode:04o}).")
    if not_empty:
        text += (" Andere Benutzer dieses Rechners können dann auch die übrigen Dateien "
                 "darin nicht mehr öffnen.")
    return text


def mode_changed_text(previous_mode: int) -> str:
    """Ergebnis (``RelocateResult.previous_mode``)."""
    return (f"Die Rechte des Ordners wurden von {previous_mode:04o} auf "
            f"{PRIVATE_DIR_MODE:04o} geändert (nur du hast Zugriff).")


def _dir_mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
    except OSError:
        return None


RETENTION_MAX_MONTHS = 1200
SAVE_DELAY_MS = 600  # Frist erst nach kurzer Ruhe speichern (Pfeiltasten/Scrollen)

Toast = Callable[..., None]


def _note(text: str) -> Gtk.Label:
    lbl = Gtk.Label(label=text, wrap=True, xalign=0)
    lbl.add_css_class("privacy-note")
    lbl.add_css_class("caption")
    lbl.set_margin_top(10)
    return lbl


def _row_button(label: str, a11y_label: str, name: str, danger: bool = False) -> Gtk.Button:
    btn = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    btn.set_name(name)
    if danger:
        btn.add_css_class("discard-button")  # getönt rot, wie „Verwerfen …“
    a11y(btn, a11y_label)
    return btn


class DataPage(Adw.PreferencesPage):
    """Seite „Daten“ des Einstellungsdialogs."""

    def __init__(self, app: "ZeitApp", parent: Callable[[], Gtk.Widget], toast: Toast) -> None:
        super().__init__(title="Daten", icon_name="drive-harddisk-symbolic")
        self.set_name("prefs-data")
        self.app = app
        self._parent = parent
        self._toast = toast
        self._save_id = 0
        self.last_dialog: Adw.AlertDialog | None = None  # für Tests
        self.last_backup: Path | None = None

        # ------------------------------------------------------ Aufbewahrung
        g1 = Adw.PreferencesGroup(title="Aufbewahrung")
        self.retention = Adw.SpinRow.new_with_range(0, RETENTION_MAX_MONTHS, 1)
        self.retention.set_name("retention-months")
        self.retention.set_title("Aufbewahrungsfrist in Monaten")
        self.retention.set_numeric(True)
        self.retention.set_snap_to_ticks(True)
        self.retention.get_adjustment().set_page_increment(12)
        self.retention.set_value(app.config.general.retention_months)
        a11y(self.retention, "Aufbewahrungsfrist in Monaten, 0 = unbegrenzt", False)
        self.retention.connect("notify::value", lambda *_: self._on_retention())
        g1.add(self.retention)
        g1.add(_note(RETENTION_LEGAL_NOTE))
        self.add(g1)

        # ------------------------------------------------------ Löschen
        g2 = Adw.PreferencesGroup(
            title="Löschen",
            description="Endgültig – auch im Protokoll und im Änderungsverlauf. "
                        "Der laufende Task und Exporte bleiben unberührt, Sicherungen "
                        "im Datenverzeichnis nur auf Wunsch.")
        self.old_row = Adw.ActionRow(title="Alte Einträge",
                                     subtitle="Alles bis einschließlich eines gewählten Tages")
        self.purge_btn = _row_button("Löschen …", "Alte Einträge löschen", "purge-before",
                                     danger=True)
        self.purge_btn.connect("clicked", lambda *_: self.ask_purge_before())
        self.old_row.add_suffix(self.purge_btn)
        g2.add(self.old_row)
        self.discarded_row = Adw.ActionRow(title="Verworfene Einträge")
        self.discard_btn = _row_button("Löschen …", "Verworfene Einträge löschen",
                                       "purge-discarded", danger=True)
        self.discard_btn.connect("clicked", lambda *_: self.ask_purge_discarded())
        self.discarded_row.add_suffix(self.discard_btn)
        g2.add(self.discarded_row)
        self.add(g2)

        # ------------------------------------------------------ Sicherung
        g3 = Adw.PreferencesGroup(title="Sicherung")
        self.backup_row = Adw.ActionRow(
            title="Sicherung erstellen",
            subtitle="Datenbank, Protokoll und Einstellungen in einen Ordner kopieren")
        self.backup_row.set_subtitle_selectable(True)
        self.backup_btn = _row_button("Erstellen …", "Sicherung erstellen", "create-backup")
        self.backup_btn.connect("clicked", lambda *_: self.choose_backup_folder())
        self.backup_row.add_suffix(self.backup_btn)
        g3.add(self.backup_row)
        g3.add(_note(BACKUP_NOTE))
        self.add(g3)

        # ------------------------------------------------------ Speicherort
        g4 = Adw.PreferencesGroup(title="Speicherort", description=STORAGE_DESCRIPTION)
        paths = app.paths
        self.dir_row = Adw.ActionRow(title="Datenverzeichnis",
                                     subtitle=GLib.markup_escape_text(str(paths.data_dir)))
        self.dir_row.set_subtitle_selectable(True)
        self.move_btn = _row_button("Ändern …", "Speicherort der Daten ändern", "change-data-dir")
        self.move_btn.connect("clicked", lambda *_: self.choose_data_folder())
        self.dir_row.add_suffix(self.move_btn)
        g4.add(self.dir_row)
        self.env_locked = getattr(paths, "data_source", "default") == "env"
        if self.env_locked:
            self.move_btn.set_sensitive(False)
            self.move_btn.set_tooltip_text(ENV_LOCKED_HINT)
            g4.add(_note(ENV_LOCKED_HINT))
        crow = Adw.ActionRow(title="Einstellungen (bleiben hier)",
                             subtitle=GLib.markup_escape_text(str(paths.config_file)))
        crow.set_subtitle_selectable(True)
        g4.add(crow)
        mrow = Adw.ActionRow(title="Sprachmodelle (bleiben hier)",
                             subtitle=GLib.markup_escape_text(str(paths.models_dir)))
        mrow.set_subtitle_selectable(True)
        g4.add(mrow)
        self.add(g4)

        self.refresh()

    # ------------------------------------------------------------------ Anzeige
    @property
    def service(self):
        return self.app.service

    def _count(self, before: date | None = None, discarded: bool = False) -> int:
        try:
            return self.service.purge_preview(before=before, discarded=discarded)
        except Exception as exc:  # pragma: no cover - DB-Fehler
            log_failure(log, "Löschvorschau fehlgeschlagen", exc)
            return 0

    def refresh(self) -> None:
        n = self._count(discarded=True)
        self.discarded_row.set_subtitle(count_entries(n, "verworfene"))
        self.discard_btn.set_sensitive(n > 0)
        self._update_retention_hint()

    def _update_retention_hint(self) -> None:
        months = int(self.retention.get_value())
        cutoff = retention_cutoff(date.today(), months)
        count = self._count(before=cutoff) if cutoff else 0
        self.retention.set_subtitle(retention_hint(months, cutoff, count))
        if months and count:
            self.retention.add_css_class("retention-active")
        else:
            self.retention.remove_css_class("retention-active")

    # ------------------------------------------------------------------ Frist
    def _on_retention(self) -> None:
        self._update_retention_hint()
        if self._save_id:
            GLib.source_remove(self._save_id)
        self._save_id = GLib.timeout_add(SAVE_DELAY_MS, self._save_retention)

    def _save_retention(self) -> bool:
        self._save_id = 0
        months = int(self.retention.get_value())
        cfg = self.app.config
        if cfg.general.retention_months == months:
            return False
        old = cfg.general.retention_months
        cfg.general.retention_months = months
        try:
            self.app.save_config()
        except Exception as exc:
            cfg.general.retention_months = old
            log_failure(log, "Aufbewahrungsfrist nicht gespeichert", exc)
            self._toast(f"Frist nicht gespeichert: {exc}")
            return False
        self._toast("Unbegrenzte Aufbewahrung gespeichert" if months == 0 else
                    f"Aufbewahrungsfrist {months} Monate gespeichert")
        return False

    def flush(self) -> None:
        """Ausstehende Frist sofort speichern (Dialog wird geschlossen)."""
        if self._save_id:
            GLib.source_remove(self._save_id)
            self._save_retention()

    # ------------------------------------------------------------------ Löschen vor Stichtag
    @staticmethod
    def default_purge_date(today: date | None = None) -> date:
        """Vorschlag: 31. Dezember des vorvorigen Jahres (Vorjahr und laufendes Jahr bleiben)."""
        today = today or date.today()
        return date(today.year - 2, 12, 31)

    def ask_purge_before(self) -> Adw.AlertDialog:
        """Schritt 1: Tag wählen (zählt mit – bis einschließlich), Anzahl live anzeigen."""
        start = self.default_purge_date()
        dlg = Adw.AlertDialog(
            heading="Alte Einträge löschen",
            body="Gelöscht werden alle Einträge, die bis einschließlich zum gewählten Tag "
                 "begonnen haben. Ein laufender Task bleibt erhalten.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        cal = Gtk.Calendar()
        cal.set_name("purge-date")
        cal.select_day(GLib.DateTime.new_local(start.year, start.month, start.day, 0, 0, 0))
        a11y(cal, "Stichtag", False)
        preview = Gtk.Label(wrap=True, xalign=0.5)
        preview.add_css_class("purge-preview")
        box.append(cal)
        box.append(preview)
        dlg.set_extra_child(box)
        dlg.add_response("cancel", "Abbrechen")
        dlg.add_response("next", "Weiter …")
        dlg.set_default_response("next")
        dlg.set_close_response("cancel")
        dlg.calendar = cal  # type: ignore[attr-defined]
        dlg.preview = preview  # type: ignore[attr-defined]

        def selected() -> date:
            d = cal.get_date()
            return date(d.get_year(), d.get_month(), d.get_day_of_month())

        def update(*_a) -> None:
            until = selected()
            n = self._count(before=until + timedelta(days=1))
            preview.set_label(purge_until_preview(until, n))
            dlg.set_response_label("next", purge_until_label(until))
            dlg.set_response_enabled("next", n > 0)

        cal.connect("day-selected", update)
        # auch Monats-/Jahreswechsel und programmatische Auswahl aktualisieren die Vorschau
        for prop in ("day", "month", "year"):
            cal.connect(f"notify::{prop}", update)
        update()
        dlg.connect("response", lambda _d, r: self.confirm_purge_before(selected())
                    if r == "next" else None)
        self._present(dlg)
        return dlg

    def confirm_purge_before(self, until: date) -> Adw.AlertDialog:
        """Schritt 2: destruktive Rückfrage mit Anzahl (``until`` zählt mit)."""
        n = self._count(before=until + timedelta(days=1))
        dlg = Adw.AlertDialog(heading=purge_before_heading(n), body=purge_until_body(until))
        dlg.add_response("cancel", "Abbrechen")
        dlg.add_response("delete", "Endgültig löschen")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.set_response_enabled("delete", n > 0)
        check = self._backup_option(dlg)
        dlg.connect("response", lambda _d, r: self.purge_before(
            until, with_backups=bool(check and check.get_active())) if r == "delete" else None)
        self._present(dlg)
        return dlg

    def purge_before(self, until: date, with_backups: bool = False):
        """Einträge bis einschließlich ``until`` löschen (``TrackerService.purge_until``)."""
        try:
            res = self.service.purge_until(until)
        except Exception as exc:
            log_failure(log, "Löschen vor Stichtag fehlgeschlagen", exc)
            self._toast(f"Löschen fehlgeschlagen: {exc}")
            return None
        self._toast(self._after_purge(purge_done(res.entries), with_backups))
        self.refresh()
        return res

    # ------------------------------------------------------------------ interne Sicherungen
    def _internal_backups(self) -> int:
        try:
            return len(self.service.internal_backups())
        except Exception as exc:  # pragma: no cover - Dateisystemfehler
            log_failure(log, "Sicherungen nicht ermittelbar", exc)
            return 0

    def _backup_option(self, dlg: Adw.AlertDialog) -> Gtk.CheckButton | None:
        """Kontrollkästchen „Auch N Sicherungen … löschen“ (Standard aus), falls vorhanden."""
        n = self._internal_backups()
        dlg.backup_check = None  # type: ignore[attr-defined]
        if not n:
            return None
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        check = Gtk.CheckButton(label=backups_option_label(n))
        check.set_name("purge-with-backups")
        check.set_active(False)
        # F-06: Beschriftung umbrechen statt Mindestbreite erzwingen – sonst wird die
        # Rückfrage in einem 360 px breiten Fenster breiter als das Fenster
        child = check.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Label):
                child.set_wrap(True)
                child.set_xalign(0)
            child = child.get_next_sibling()
        a11y(check, backups_option_label(n), False)
        hint = Gtk.Label(label=backups_option_hint(n), wrap=True, xalign=0)
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        box.append(check)
        box.append(hint)
        dlg.set_extra_child(box)
        dlg.backup_check = check  # type: ignore[attr-defined]  # für Tests
        return check

    def _after_purge(self, text: str, with_backups: bool) -> str:
        """Nach dem Löschen: Sicherungen nur auf ausdrücklichen Wunsch entfernen."""
        if with_backups:
            try:
                cleanup = self.service.remove_internal_backups(reason="manual")
            except Exception as exc:
                log_failure(log, "Sicherungen nicht entfernt", exc)
                return f"{text} – Sicherungen nicht entfernt: {exc}"
            if cleanup.failed:
                return (f"{text}, {count_backups(len(cleanup.removed))} entfernt – "
                        f"{len(cleanup.failed)} nicht entfernbar")
            return f"{text}, {count_backups(len(cleanup.removed))} entfernt"
        left = self._internal_backups()
        return f"{text} – {backups_left_hint(left)}" if left else text

    # ------------------------------------------------------------------ Verworfene
    def ask_purge_discarded(self) -> Adw.AlertDialog | None:
        n = self._count(discarded=True)
        if n == 0:
            self._toast("Keine verworfenen Einträge vorhanden")
            self.refresh()
            return None
        dlg = Adw.AlertDialog(heading=purge_discarded_heading(n), body=PURGE_DISCARDED_BODY)
        dlg.add_response("cancel", "Abbrechen")
        dlg.add_response("delete", "Endgültig löschen")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        check = self._backup_option(dlg)
        dlg.connect("response", lambda _d, r: self.purge_discarded(
            with_backups=bool(check and check.get_active())) if r == "delete" else None)
        self._present(dlg)
        return dlg

    def purge_discarded(self, with_backups: bool = False):
        try:
            res = self.service.purge_discarded()
        except Exception as exc:
            log_failure(log, "Löschen verworfener Einträge fehlgeschlagen", exc)
            self._toast(f"Löschen fehlgeschlagen: {exc}")
            return None
        self._toast(self._after_purge(purge_done(res.entries, discarded=True), with_backups))
        self.refresh()
        return res

    # ------------------------------------------------------------------ Sicherung
    def choose_backup_folder(self) -> None:
        fd = Gtk.FileDialog(title="Ordner für die Sicherung wählen", modal=True)
        fd.set_accept_label("Hier sichern")
        fd.set_initial_folder(Gio.File.new_for_path(GLib.get_home_dir()))

        def done(dialog, res) -> None:
            try:
                folder = dialog.select_folder_finish(res)
            except GLib.Error:
                return  # abgebrochen
            if folder is not None and folder.get_path():
                self.create_backup(Path(folder.get_path()))

        fd.select_folder(self._window(), None, done)

    def create_backup(self, target_dir: Path) -> Path | None:
        try:
            out = Path(self.service.backup(target_dir))
        except Exception as exc:
            log_failure(log, "Sicherung fehlgeschlagen", exc)
            self._toast(f"Sicherung fehlgeschlagen: {exc}")
            return None
        self.last_backup = out
        self.backup_row.set_subtitle(GLib.markup_escape_text(f"Zuletzt: {out}"))
        uri = Gio.File.new_for_path(str(out)).get_uri()

        def open_folder() -> None:
            Gtk.FileLauncher.new(Gio.File.new_for_uri(uri)).launch(self._window(), None, None)

        self._toast(f"Sicherung „{out.name}“ erstellt – verschlüsselt aufbewahren",
                    "Ordner öffnen", open_folder, timeout=8)
        return out

    # ------------------------------------------------------------------ Speicherort
    def choose_data_folder(self) -> None:
        if self.env_locked:
            self._toast(ENV_LOCKED_HINT, timeout=8)
            return
        fd = Gtk.FileDialog(title="Neuen Speicherort für die Daten wählen", modal=True)
        fd.set_accept_label("Auswählen")
        fd.set_initial_folder(Gio.File.new_for_path(GLib.get_home_dir()))

        def done(dialog, res) -> None:
            try:
                folder = dialog.select_folder_finish(res)
            except GLib.Error:
                return  # abgebrochen
            if folder is not None and folder.get_path():
                self.ask_relocate(Path(folder.get_path()))

        fd.select_folder(self._window(), None, done)

    def _alert(self, heading: str, body: str) -> Adw.AlertDialog:
        dlg = Adw.AlertDialog(heading=heading, body=body)
        dlg.add_response("ok", "Schließen")
        dlg.set_default_response("ok")
        dlg.set_close_response("ok")
        self._present(dlg)
        return dlg

    def ask_relocate(self, target: Path) -> Adw.AlertDialog:
        """Ziel prüfen; ungeeignet → Meldung, sonst Bestätigung mit Ablauf und Warnungen."""
        from ..storage.relocate import RelocateError  # noqa: PLC0415

        try:
            check = self.service.check_relocation_target(
                target, default_dir=getattr(self.app.paths, "default_data_dir", None))
        except RelocateError as exc:
            return self._alert("Ordner nicht geeignet", str(exc))
        except Exception as exc:
            log_failure(log, "Zielordner nicht prüfbar", exc)
            return self._alert("Ordner nicht geeignet", f"Prüfung fehlgeschlagen: {exc}")
        dest = check.path
        body = (f"Tracksy kopiert die Datenbank, das CSV-Protokoll und die internen "
                f"Sicherungen nach\n{dest}\nund prüft die Kopie. Erst danach werden die "
                f"bisherigen Dateien im alten Datenverzeichnis in einen Ordner „_alt-…“ "
                f"verschoben – gelöscht wird nichts. Anschließend startet Tracksy neu; ein "
                f"laufender Task läuft weiter.")
        dlg = Adw.AlertDialog(heading="Speicherort ändern?", body=body)
        notes = []
        if check.cloud:
            notes.append(CLOUD_WARNING)
        if check.not_empty:
            notes.append(NOT_EMPTY_NOTE)
        old_mode = _dir_mode(dest)
        if old_mode is not None and old_mode != PRIVATE_DIR_MODE:
            notes.append(mode_change_note(old_mode, check.not_empty))
        dlg.cloud_warning = check.cloud  # type: ignore[attr-defined]  # für Tests
        dlg.mode_note = old_mode is not None and old_mode != PRIVATE_DIR_MODE  # type: ignore[attr-defined]
        if notes:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            for text in notes:
                lbl = Gtk.Label(label=text, wrap=True, xalign=0)
                lbl.add_css_class("caption")
                if text is CLOUD_WARNING:
                    lbl.set_name("relocate-cloud-warning")
                    lbl.add_css_class("warning")
                box.append(lbl)
            dlg.set_extra_child(box)
        dlg.add_response("cancel", "Abbrechen")
        dlg.add_response("move", "Verschieben und neu starten")
        dlg.set_response_appearance(
            "move", Adw.ResponseAppearance.DESTRUCTIVE if check.cloud
            else Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, r: self.relocate_to(dest) if r == "move" else None)
        self._present(dlg)
        return dlg

    def relocate_to(self, target: Path):
        """Umzug ausführen; danach Hinweis auf den Altbestand und Neustart."""
        try:
            res = self.app.relocate_data(target)
        except Exception as exc:
            log_failure(log, "Speicherort nicht geändert", exc)
            self._alert("Speicherort nicht geändert",
                        f"{exc}\n\nDie Daten liegen unverändert am bisherigen Ort.")
            return None
        if res.archive_dir is not None and not res.archive_failed:
            old = (f"Die bisherigen Dateien liegen jetzt in\n{res.archive_dir}\n"
                   "Sie enthalten weiterhin personenbezogene Daten und werden von der "
                   "Aufbewahrungsfrist nicht erfasst – nach Prüfung bitte selbst löschen.")
        else:
            old = ("Einige bisherige Dateien konnten nicht verschoben werden "
                   f"({', '.join(res.archive_failed) or 'Archivordner'}). Sie liegen noch im "
                   "alten Datenverzeichnis – bitte prüfen und selbst entfernen.")
        mode = (f"{mode_changed_text(res.previous_mode)}\n\n"
                if res.previous_mode is not None else "")
        dlg = Adw.AlertDialog(heading="Speicherort geändert",
                              body=f"Neuer Speicherort:\n{res.new_dir}\n\n{mode}{old}\n\n"
                                   "Tracksy startet jetzt neu.")
        dlg.add_response("restart", "Jetzt neu starten")
        dlg.set_response_appearance("restart", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("restart")
        dlg.set_close_response("restart")  # Datenbank ist geschlossen → immer Neustart
        dlg.connect("response", lambda *_: self.app.restart())
        self.dir_row.set_subtitle(GLib.markup_escape_text(str(res.new_dir)))
        self.move_btn.set_sensitive(False)
        self._present(dlg)
        return res

    # ------------------------------------------------------------------ Helfer
    def _window(self) -> Gtk.Window | None:
        root = self._parent().get_root()
        return root if isinstance(root, Gtk.Window) else None

    def _present(self, dlg: Adw.AlertDialog) -> None:
        self.last_dialog = dlg
        dlg.present(self._parent())
