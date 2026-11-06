"""Ersteinrichtungs-Assistent.

Jederzeit abbrechbar: Schließen-Kreuz/Escape schließt nur den Assistenten (Tracksy
läuft weiter), der Knopf „Beenden“ beendet Tracksy. In beiden Fällen werden die bis
dahin gewählten Werte verworfen und nichts gespeichert (``first_run_done`` bleibt
``False``); der Assistent beginnt beim nächsten Start bzw. über das Hauptmenü
(„Einrichtung starten“) von vorn. Gespeichert wird erst mit „Fertig“ bzw.
„Überspringen“ im letzten Schritt.
"""

from __future__ import annotations

import copy
from dataclasses import fields
from typing import TYPE_CHECKING, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from .preferences import (  # noqa: E402
    LOCATION_CHOICES,
    MODEL_SIZES,
    PRIVACY_DOWNLOAD,
    ROUNDING_CHOICES,
    HotkeyGroup,
    ModelDownloader,
    WorktimeGroup,
    confirm_download,
    model_installed,
    model_subtitle,
)
from ..core.sanitize import MAX_FIELD_LEN  # noqa: E402
from .widgets import a11y, limit_length  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ..app import ZeitApp

STEPS = 6
WELCOME_LEAD = "Tracksy hilft dir dabei, deine Arbeit zu dokumentieren."
WELCOME_FEEDBACK = ("Hast du einen Fehler gefunden oder fehlt dir eine Funktion?\n"
                    "Melde dich gerne.")


class FirstRunDialog(Adw.Dialog):
    def __init__(self, app: "ZeitApp") -> None:
        super().__init__()
        self.app = app
        self.cfg = app.config
        self.set_title("Einrichtung")
        self.set_content_width(520)
        self.set_content_height(580)
        # Stand vor dem Assistenten: wird bei Abbruch wiederhergestellt
        self._snapshot = copy.deepcopy(app.config)
        self._closed = False
        self.set_can_close(False)
        self.connect("close-attempt", lambda *_: self._abort())
        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.set_child(self.toasts)

        self.nav.add(self._page_welcome())
        self._pages: dict[str, Callable[[], Adw.NavigationPage]] = {
            "name": self._page_name,
            "work": self._page_work,
            "worktime": self._page_worktime,
            "voice": self._page_voice,
            "hotkeys": self._page_hotkeys,
        }

    # -------------------------------------------------------------- Gerüst
    def _page(self, tag: str, title: str, step: int, body: Gtk.Widget, next_label: str,
              on_next: Callable[[], None], skip: Callable[[], None] | None = None
              ) -> Adw.NavigationPage:
        tv = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=True, show_title=False)
        tv.add_top_bar(header)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_start(24)
        box.set_margin_end(24)
        box.set_margin_top(6)
        box.set_margin_bottom(12)
        stp = Gtk.Label(label=f"Schritt {step} von {STEPS}", xalign=0)
        stp.add_css_class("wizard-step")
        h = Gtk.Label(label=title, xalign=0.5 if tag == "welcome" else 0, wrap=True,
                      justify=Gtk.Justification.CENTER if tag == "welcome" else Gtk.Justification.LEFT)
        h.add_css_class("title-1")
        box.append(stp)
        box.append(h)
        box.append(body)
        scroller.set_child(box)
        tv.set_content(scroller)
        bar = Gtk.Box(spacing=10)
        bar.set_margin_start(24)
        bar.set_margin_end(24)
        bar.set_margin_top(8)
        bar.set_margin_bottom(16)
        qb = Gtk.Button(label="Beenden")
        qb.set_tooltip_text("Einrichtung abbrechen und Tracksy beenden")
        a11y(qb, "Einrichtung abbrechen und Tracksy beenden")
        qb.connect("clicked", lambda *_: self._abort_and_quit())
        bar.append(qb)
        bar.append(Gtk.Box(hexpand=True))
        if skip is not None:
            sk = Gtk.Button(label="Überspringen")
            a11y(sk, f"{title} überspringen")
            sk.connect("clicked", lambda *_: skip())
            bar.append(sk)
        nb = Gtk.Button(label=next_label)
        nb.add_css_class("suggested-action")
        nb.add_css_class("pill")
        a11y(nb, next_label)
        nb.connect("clicked", lambda *_: on_next())
        bar.append(nb)
        tv.add_bottom_bar(bar)
        page = Adw.NavigationPage(child=tv, title=title, tag=tag)
        page.next_button = nb  # type: ignore[attr-defined]
        return page

    def _go(self, tag: str) -> None:
        if tag == "done":
            self._finish()
            return
        page = self._pages[tag]()
        self.nav.push(page)
        target = getattr(page, "focus_target", None) or page.next_button
        GLib.idle_add(lambda: (target.grab_focus(), False)[1])

    # -------------------------------------------------------------- Seiten
    def _page_welcome(self) -> Adw.NavigationPage:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        logo = Gtk.Image(icon_name="de.local.Zeiterfassung", pixel_size=96, halign=Gtk.Align.CENTER)
        logo.set_accessible_role(Gtk.AccessibleRole.PRESENTATION)
        body.append(logo)
        lead = Gtk.Label(label=WELCOME_LEAD, wrap=True, xalign=0)
        lead.add_css_class("title-4")
        body.append(lead)
        intro = Gtk.Label(
            label="Erfasse Arbeitszeit pro Kunde, Thema und Ticket – per Klick, Tastenkürzel "
                  "oder Sprache. Beim Stoppen fragt die App nach Ergänzungen; erst dann wird "
                  "gespeichert.", wrap=True, xalign=0)
        body.append(intro)
        priv = Gtk.Label(
            label="Datenschutz: Alles bleibt auf diesem Rechner. Keine Cloud, keine Telemetrie. "
                  "Das Mikrofon ist nur aktiv, solange du die Sprachtaste benutzt; Audio wird "
                  "nie gespeichert. Die Daten liegen geschützt (nur für dein Benutzerkonto lesbar) in:\n"
                  f"{self.app.paths.data_dir}", wrap=True, xalign=0)
        priv.add_css_class("privacy-note")
        priv.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        body.append(priv)
        feedback = Gtk.Label(label=WELCOME_FEEDBACK, wrap=True, xalign=0)
        feedback.add_css_class("dim-label")
        body.append(feedback)
        return self._page("welcome", "Willkommen bei Tracksy", 1, body,
                          "Los geht’s", lambda: self._go("name"))

    def _page_name(self) -> Adw.NavigationPage:
        grp = Adw.PreferencesGroup(
            description="Erscheint im Leistungsnachweis (Export). Später änderbar.")
        self.name = Adw.EntryRow(title="Dein Name")
        name = self.cfg.general.employee_name
        self.name.set_text("" if name == "Max Mustermann" else name)
        limit_length(self.name, MAX_FIELD_LEN)
        a11y(self.name, "Mitarbeitername", False)
        self.name.connect("entry-activated", lambda *_: self._save_name())
        grp.add(self.name)
        page = self._page("name", "Wie heißt du?", 2, grp, "Weiter", self._save_name)
        page.focus_target = self.name  # type: ignore[attr-defined]
        return page

    def _save_name(self) -> None:
        text = self.name.get_text().strip()
        if text:
            self.cfg.general.employee_name = text
        self._go("work")

    def _page_work(self) -> Adw.NavigationPage:
        grp = Adw.PreferencesGroup()
        self.loc = Adw.ComboRow(title="Wo arbeitest du meistens?",
                                model=Gtk.StringList.new([l for _k, l in LOCATION_CHOICES]))
        self.loc.set_selected(0 if self.cfg.general.default_location == "remote" else 1)
        a11y(self.loc, "Standardort", False)
        self.rounding = Adw.ComboRow(
            title="Rundung beim Export",
            subtitle="Je Eintrag aufrunden; gespeichert wird immer die exakte Zeit",
            model=Gtk.StringList.new(
                ["keine" if m == 0 else f"auf {m} Minuten" for m in ROUNDING_CHOICES]))
        cur = self.cfg.export.rounding_minutes
        self.rounding.set_selected(ROUNDING_CHOICES.index(cur) if cur in ROUNDING_CHOICES else 4)
        a11y(self.rounding, "Rundung beim Export", False)
        grp.add(self.loc)
        grp.add(self.rounding)

        def nxt() -> None:
            self.cfg.general.default_location = LOCATION_CHOICES[self.loc.get_selected()][0]
            self.cfg.export.rounding_minutes = ROUNDING_CHOICES[self.rounding.get_selected()]
            self._go("worktime")

        return self._page("work", "Arbeitsweise", 3, grp, "Weiter", nxt)

    def _page_worktime(self) -> Adw.NavigationPage:
        # Werte landen sofort in self.cfg (Kopie wird bei Abbruch wiederhergestellt),
        # gespeichert wird erst am Ende des Assistenten.
        self.worktime = WorktimeGroup(self.cfg)

        def nxt() -> None:
            if not self.worktime.valid:
                self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(
                    "Bitte die rot markierten Zeiten korrigieren (HH:MM, Ende nach Beginn).")))
                return
            self._go("voice")

        page = self._page("worktime", "Regelarbeitszeit", 4, self.worktime, "Weiter", nxt)
        page.focus_target = self.worktime.rows[0]["switch"]  # type: ignore[attr-defined]
        return page

    def _page_voice(self) -> Adw.NavigationPage:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        txt = Gtk.Label(
            label="Für Sprachbefehle wird ein Whisper-Modell benötigt. Größere Modelle erkennen "
                  "besser, brauchen aber mehr Rechenleistung. Ohne Modell funktioniert die App "
                  "vollständig – nur ohne Sprache.", wrap=True, xalign=0)
        body.append(txt)
        grp = Adw.PreferencesGroup()
        self.model = Adw.ComboRow(title="Modellgröße", model=Gtk.StringList.new(list(MODEL_SIZES)))
        self.model.set_selected(MODEL_SIZES.index(self.cfg.whisper.model_size))
        a11y(self.model, "Whisper-Modellgröße", False)
        self.model_state = Adw.ActionRow(title="Status")
        grp.add(self.model)
        grp.add(self.model_state)
        body.append(grp)
        self.consent = Gtk.CheckButton(label="Ich stimme dem einmaligen Download zu.")
        a11y(self.consent, "Zustimmung zum Modell-Download", False)
        note = Gtk.Label(label=PRIVACY_DOWNLOAD, wrap=True, xalign=0)
        note.add_css_class("privacy-note")
        note.add_css_class("caption")
        body.append(note)
        body.append(self.consent)
        dl_row = Gtk.Box(spacing=12)
        self.progress = Gtk.ProgressBar(show_text=True, visible=False, hexpand=True,
                                        valign=Gtk.Align.CENTER)
        dl_row.append(self.progress)
        self.cancel_btn = Gtk.Button(label="Abbrechen", visible=False, valign=Gtk.Align.CENTER)
        self.cancel_btn.connect("clicked", lambda *_: ModelDownloader.cancel_running())
        a11y(self.cancel_btn, "Modell-Download abbrechen")
        dl_row.append(self.cancel_btn)
        body.append(dl_row)

        def refresh(*_a) -> None:
            size = MODEL_SIZES[self.model.get_selected()]
            inst = model_installed(self.app, size)
            self.model.set_subtitle(model_subtitle(size))
            self.model_state.set_subtitle("Bereits installiert" if inst else "Nicht installiert")
            self.consent.set_visible(not inst)
            page.next_button.set_label("Weiter" if inst else "Herunterladen")
            page.next_button.set_sensitive(inst or self.consent.get_active())

        def nxt() -> None:
            size = MODEL_SIZES[self.model.get_selected()]
            self.cfg.whisper.model_size = size
            if model_installed(self.app, size):
                self._go("hotkeys")
                return
            if not self.consent.get_active():
                return

            def start() -> None:
                self.progress.set_visible(True)
                self.cancel_btn.set_visible(True)
                page.next_button.set_sensitive(False)
                pulse = GLib.timeout_add(150, lambda: (self.progress.pulse(), True)[1])

                def done(ok: bool, msg: str) -> None:
                    GLib.source_remove(pulse)
                    self.cancel_btn.set_visible(False)
                    if self._closed:
                        return  # Assistent schon geschlossen: keine Anzeige mehr
                    self.progress.set_text(msg)
                    self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(
                        msg if ok or msg == "Download abgebrochen." else f"Download fehlgeschlagen: {msg}")))
                    refresh()
                    if ok:
                        self._go("hotkeys")

                ModelDownloader(self.app, size, self.progress.set_text, done).start()

            confirm_download(self, size, start)

        page = self._page("voice", "Spracherkennung", 5, body, "Weiter", nxt,
                          skip=lambda: self._skip_voice())
        self.model.connect("notify::selected", refresh)
        self.consent.connect("toggled", refresh)
        refresh()
        return page

    def _skip_voice(self) -> None:
        self.cfg.whisper.model_size = MODEL_SIZES[self.model.get_selected()]
        self._go("hotkeys")

    def _page_hotkeys(self) -> Adw.NavigationPage:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        txt = Gtk.Label(
            label="Mit globalen Tastenkürzeln startest und stoppst du Tasks aus jeder Anwendung. "
                  "Vor dem Einrichten siehst du die genauen Befehle.", wrap=True, xalign=0)
        body.append(txt)
        hold = Gtk.Label(
            label="Sprachbefehl: Super+Umschalt+V gedrückt halten, solange du sprichst "
                  "(Wayland: drücken = Start/Stopp).", wrap=True, xalign=0)
        hold.add_css_class("dim-label")
        body.append(hold)
        body.append(HotkeyGroup(self.app, lambda: self,
                                lambda m: self.toasts.add_toast(
                                    Adw.Toast(title=GLib.markup_escape_text(m)))))
        return self._page("hotkeys", "Tastenkürzel", 6, body, "Fertig",
                          lambda: self._go("done"), skip=lambda: self._go("done"))

    # -------------------------------------------------------------- Abbruch
    def _discard(self) -> None:
        """Gewählte Werte verwerfen (nichts speichern), laufenden Download abbrechen
        und den Assistenten schließen."""
        self._closed = True
        ModelDownloader.cancel_running()
        for f in fields(self.cfg):
            setattr(self.cfg, f.name, copy.deepcopy(getattr(self._snapshot, f.name)))
        self.force_close()

    def _abort(self) -> None:
        """Schließen-Kreuz/Escape: nur den Assistenten schließen, Tracksy läuft weiter."""
        self._discard()
        self.app.update_first_run_action()

    def _abort_and_quit(self) -> None:
        """Knopf „Beenden“: Assistent verwerfen und Tracksy beenden."""
        self._discard()
        self.app.really_quit(notify_running=False)

    # -------------------------------------------------------------- Abschluss
    def _finish(self) -> None:
        self.cfg.general.first_run_done = True
        try:
            self.app.save_config()
        except Exception as exc:
            self.app.toast(f"Einstellungen nicht gespeichert: {exc}",
                           public="Einstellungen nicht gespeichert – Details im Tracksy-Fenster")
        self.app.voice.reconfigure(self.cfg)
        self.app.refresh_all()
        self.app.refresh_voice_state()
        self.app.update_first_run_action()
        self.force_close()
