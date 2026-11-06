# Änderungen

Alle wichtigen Änderungen an Tracksy stehen hier, die neueste Version oben.
Das Format folgt [Keep a Changelog](https://keepachangelog.com/de/1.1.0/), die Versionsnummern
folgen [Semantic Versioning](https://semver.org/lang/de/). Solange die Version mit `0.` beginnt,
ist Tracksy experimentell – auch kleinere Versionssprünge können Bedienung oder Datenformat ändern.

Aktualisieren: `./scripts/tracksy-update.sh` im Ordner `tracksy` (siehe README). Das Skript zeigt
vor der Rückfrage die Änderungen der neuen Versionen aus dieser Datei an.

## [0.12.5] – 2026-11-06

### Behoben
- Löschen: Schlug nach dem Löschen aus der Datenbank das Neuschreiben des CSV-Protokolls fehl
  (z. B. Festplatte voll), konnten die Zeilen der gelöschten Einträge dauerhaft im Protokoll
  bleiben. Tracksy merkt sich ein begonnenes Löschen jetzt (Datei `.loeschen-offen.json` im
  Datenverzeichnis, nur interne Kennungen, keine Inhalte) und holt es beim nächsten Start bzw.
  beim nächsten Löschen nach. App und Kommandozeile kommen sich dabei nicht in die Quere.
- Systembenachrichtigungen enthalten keine Kundennamen, Themen, Tickets, erkannten Sprachtexte
  oder Fehlertexte und keine Dateipfade mehr (auch nicht die Meldung „Tracksy beendet“). Ist kein Tracksy-Fenster sichtbar (z. B. Sprachbefehl über das
  systemweite Kürzel), erscheint nur ein neutraler Hinweis wie „Task gestartet“ oder
  „Sprachbefehl nicht verstanden – Details im Tracksy-Fenster“. Benachrichtigungen landen im
  Verlauf des Desktops und teils auf dem Sperrbildschirm.
- Sprachaufnahme: Lässt sich der Schutz vor Absturzabbildern nicht einschalten, startet die
  Aufnahme nicht mehr (vorher lief sie mit einer Warnung im Log weiter). Das Mikrofon wird dann
  gar nicht geöffnet.

### Hinweis
- `--no-pull` beim Update-Skript vermeidet nur den Zugriff auf das Git-Repository. Die
  anschließende Installation fragt weiterhin PyPI an; ein Update ganz ohne Internet ist derzeit
  nicht möglich. Die bisherige Beschreibung („ohne Netzzugriff“) war falsch.
- Ältere Benachrichtigungen mit Kundendaten aus früheren Versionen bleiben im
  Benachrichtigungsverlauf des Desktops, bis du sie dort löschst.

## [0.12.4] – 2026-11-06

### Neu
- `SECURITY.md`: wie Sicherheitslücken vertraulich gemeldet werden, welche Versionen Korrekturen
  bekommen, die Bitte, keine echten Zeiterfassungsdaten in Meldungen mitzuschicken, und welches
  Repository das offizielle ist.

### Geändert
- Quelltext-Kommentare aufgeräumt: interne Kurzverweise und Hinweise auf nicht veröffentlichte
  Entwicklungsdateien entfernt. Keine Änderung am Verhalten.
- Beschreibung des Sprachmodells „base“ in der Modellauswahl lautet jetzt „leistungsschwache
  Rechner“.
- Die Testkonfiguration ist nicht mehr Teil von `pyproject.toml` (die Tests werden nicht
  veröffentlicht).

## [0.12.3] – 2026-11-03

### Behoben
- Die beiden Logo-Dateien unter `assets/logo/` waren versehentlich als ausführbar gespeichert.
  Ging das Ausführungsrecht verloren (z. B. beim Kopieren oder durch einen Dateimanager), hielt
  das Update-Skript das für eine lokale Änderung und brach ab. Die Dateien haben jetzt normale
  Rechte.

### Geändert
- Das Update-Skript erkennt, wenn sich im Klon nur Dateirechte und nicht der Inhalt geändert
  haben, und zeigt einen Befehl, der die Rechte ohne Datenverlust wiederherstellt. Bei echten
  Änderungen bricht es wie bisher ab und verwirft nichts.

### Hinweis
- Bricht das Update auf diese Version wegen der zwei Logo-Dateien ab, im Ordner `tracksy`
  einmalig ausführen und das Update danach erneut starten (es geht nichts verloren):
  `git restore --source=HEAD --staged --worktree -- assets/logo/tracksy-256.png assets/logo/tracksy-master-1024.png`

## [0.12.2] – 2026-11-03

### Geändert
- Kommentare im Quelltext aufgeräumt: Verweise auf interne Projektunterlagen (Prüfnummern,
  Anforderungsabschnitte, interne Dokumente) entfernt. Funktionen, Bedienung und Datenformat
  bleiben unverändert.

### Hinweis
- Ältere Versionen im Repository enthalten diese Verweise weiterhin; sie werden nicht nachträglich
  geändert, damit das Update-Skript weiter zuverlässig funktioniert.

## [0.12.1] – 2026-11-02

### Behoben
- Überlappende Zeiträume, die zusammen länger als 366 Tage sind, werden jetzt schon bei der
  Eingabe abgelehnt. Bisher meldete das Feld „gespeichert“, danach ließen sich bis zum Neustart
  keine Einstellungen mehr speichern (Zeiteinträge gingen dabei nicht verloren).
- Datumsangaben nur noch mit den Jahren 1900–2200 und nur mit normalen Ziffern 0–9 (Tippfehler
  wie `24.12.0026` werden erkannt).
- Fehlermeldung bei fehlendem Jahr zeigt das Beispiel nur noch einmal.
- Screenreader lesen den Hinweis bzw. die Fehlermeldung unter „Eigene Feiertage“ und „Urlaub“
  zum jeweiligen Feld vor.

### Geändert
- **Fehlerhafte Einstellungsdatei wird gesichert:** Kann Tracksy `config.toml` nicht lesen, startet
  es wie bisher mit Standardwerten – legt aber vorher eine Kopie `config.toml.ungueltig-…` im
  selben Ordner an und nennt den Pfad in der Meldung. So gehen deine Einstellungen beim nächsten
  Speichern nicht mehr verloren; du kannst die Kopie reparieren und zurückkopieren.

### Sicherheit
- Das Update-Skript filtert weitere Steuerzeichen aus dem angezeigten Änderungsprotokoll und
  begrenzt dessen Größe und Zeilenlänge.

### Hinweis
- Der Schutz der Einstellungsdatei gilt ab dieser Version. Gehst du auf eine ältere Version zurück
  (0.12.0 oder früher) und hast Zeiträume oder Urlaub eingetragen, sichere `config.toml` vorher
  selbst – ältere Versionen überschreiben sie beim nächsten Speichern mit Standardwerten.

## [0.12.0] – 2026-11-02

### Neu
- **Urlaub** (Einstellungen → Arbeitszeit, auch im Einrichtungsassistenten): einzelne Tage und
  Zeiträume, z. B. `27.07.–07.08.2026`. Urlaub zählt wie ein normaler Werktag und ändert nichts an
  Berechnung und Export – er dient nur deiner eigenen Übersicht und wird nur lokal gespeichert.
- **Eigene Feiertage als Zeiträume:** statt jeden Tag einzeln einzutippen, z. B.
  `24.12.2026, 28.12.–02.01.2027`. Als Trenner gehen Bindestrich, Gedankenstrich, `/`, `..` oder
  `bis`; fehlt beim Beginn das Jahr, gilt das Jahr des Endes. Überlappende Zeiträume werden
  zusammengefasst, unter dem Feld steht die Anzahl der Tage.

### Geändert
- Das bisherige Feld „Eigene freie Tage“ heißt jetzt **„Eigene Feiertage“** (Bedeutung unverändert:
  zählen wie Sonn-/Feiertag). Bereits eingetragene Tage bleiben erhalten.
- Die Einstellungen sagen jetzt ausdrücklich, dass bei gewähltem Bundesland die **bundesweiten und
  die Landesfeiertage zusammen** gelten (das Verhalten war schon vorher so).

### Hinweis
- Wer Zeiträume oder Urlaub einträgt und danach auf eine ältere Version (0.11.x oder früher)
  zurückgeht, muss diese Einträge vorher entfernen – ältere Versionen lehnen die Einstellungsdatei
  sonst als ungültig ab, verwenden Standardwerte und überschreiben sie beim nächsten Speichern
  (vorher `config.toml` sichern).

## [0.11.1] – 2026-11-02

### Behoben
- Der Sprachbefehl „weiter“ bzw. „fortsetzen“ während des Stopp-Dialogs lässt den Task
  weiterlaufen (bisher kam „kein pausierter Task“).

## [0.11.0] – 2026-11-02

### Neu
- **Regelarbeitszeit und Zuschläge:** Regelarbeitszeit je Wochentag, Faktoren für Arbeit außerhalb
  (werktags mindestens 1,25, Samstag 1,5, Sonn-/Feiertag 2,0), gesetzliche Feiertage je Bundesland
  (lokal berechnet) und eigene freie Tage. Der Export weist die Zeit außerhalb getrennt aus (Beginn,
  Ende, Stunden, aufgeteilt nach werktags/Samstag/Sonn-/Feiertag) und berechnet gewichtete Stunden.
- **Push-to-talk durch Halten:** `Strg+M` im Fenster bzw. das systemweite Sprachkürzel unter X11
  gedrückt halten, Loslassen beendet die Aufnahme.
- Übersicht aller Tastenkürzel (`Strg+?`); die eingerichteten Kürzel stehen im Hauptfenster und im
  Menü des Leistensymbols.
- Spracheingabe wird beim Start vorbereitet (schnellere erste Aufnahme, abschaltbar).

### Geändert
- **Rundung im Export je Anteil:** Jede angefangene Einheit zählt getrennt für Regelzeit und Zeit
  außerhalb. Dadurch können abgerechnete Summen auch für bereits erfasste Einträge höher ausfallen
  als in älteren Versionen.

### Behoben
- Zeitumstellung (Sommer-/Winterzeit) wird bei der Aufteilung auf Regelzeit/außerhalb exakt
  berücksichtigt.

## [0.10.1] – 2026-11-01

### Behoben
- Abgebrochene Downloads des Sprachmodells hinterlassen keine Reste mehr; mehrere Downloads
  gleichzeitig sind gesperrt.

### Geändert
- Die Kontaktadresse steht nur noch unter Menü → „Über Tracksy“.

## [0.10.0] – 2026-11-01

### Neu
- **Ruhezustand-Erkennung:** Nach Standby oder Ruhezustand fragt Tracksy, ob die Zeit dazwischen
  abgezogen werden soll.
- **Update-Skript** `scripts/tracksy-update.sh` mit Rückfrage vor dem Übernehmen, Sicherung vorab
  und `--dry-run`.
- Beschädigtes Sprachmodell lässt sich unter Einstellungen → Sprache erneut herunterladen (die alte
  Datei wird archiviert).
- Schnellaktionen „Start/Stopp“ und „Sprachbefehl“ im Anwendungsmenü-Starter.

### Geändert
- Eigene Farbpalette, auf allen Desktops gleich.
- „Beenden“ im Leistensymbol fragt nach.

### Behoben
- Bei Speicherfehlern wird nichts halb gespeichert (Rückabwicklung).
- Speicherort-Umzug setzt die Zugriffsrechte des Zielordners korrekt.

## [0.9.0] – 2026-10-30

### Neu
- Erste Veröffentlichung (experimentell): Zeiterfassung je Kunde, Thema und Ticket per Fenster,
  Leistensymbol, Tastenkürzel, Kommandozeile und Sprache (offline mit whisper.cpp);
  Ergänzungsdialog beim Stoppen; Tages- und Monatsübersicht; Export als XLSX und CSV;
  Sicherung, Aufbewahrungsfrist und Löschen; Hell/Dunkel.

[0.12.1]: https://github.com/DaAutimato/tracksy/compare/v0.12.0...v0.12.1
[0.12.0]: https://github.com/DaAutimato/tracksy/compare/v0.11.1...v0.12.0
[0.11.1]: https://github.com/DaAutimato/tracksy/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/DaAutimato/tracksy/compare/v0.10.1...v0.11.0
[0.10.1]: https://github.com/DaAutimato/tracksy/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/DaAutimato/tracksy/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/DaAutimato/tracksy/releases/tag/v0.9.0
