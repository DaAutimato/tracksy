# Sicherheit und Datenschutz bei Meldungen

## Sicherheitslücke melden

Bitte melde vermutete Sicherheitslücken **nicht** als öffentliches Issue, sondern vertraulich
auf einem dieser Wege:

- **auf GitHub:** im Repository unter „Security“ → „Report a vulnerability“ (nur du und ich
  sehen die Meldung; ein GitHub-Konto ist nötig),
- **per E-Mail** an tracksy@pm.me (dieselbe Adresse steht in Tracksy unter Menü → „Über Tracksy“).

Hilfreich sind:

- betroffene Version (`zeit --version` bzw. Menü → „Über Tracksy“) und System (z. B. Linux Mint 22.3),
- eine kurze Beschreibung, wie sich das Problem auslösen lässt,
- was ein Angreifer damit erreichen könnte.

Ich antworte so bald wie möglich, in der Regel innerhalb einer Woche. Tracksy ist ein privat
gepflegtes Projekt ohne Support-Team – feste Reaktionszeiten kann ich nicht zusagen. Nach der
Behebung wird die Lücke im `CHANGELOG.md` genannt; wenn du möchtest, mit deinem Namen.

## Unterstützte Versionen

Sicherheitskorrekturen gibt es nur für die jeweils neueste Version. Aktualisieren:
`./scripts/tracksy-update.sh` (siehe README).

## Bitte keine echten Daten senden

Tracksy speichert Arbeitszeiten, Kundennamen und Notizen – oft personenbezogene Daten, auch von
Dritten. Bitte füge Issues, E-Mails und Screenshots **keine echten Einträge, Exporte,
Datenbanken (`zeiterfassung.sqlite3`), Sicherungen oder ungeprüften Logs** bei. Nutze erfundene Beispieldaten oder
schwärze Namen, Kunden und Zeiten vorher.

Was mir trotzdem an personenbezogenen Daten zugeht, nutze ich nur zur Fehlersuche und lösche es,
sobald die Meldung erledigt ist. Enthält ein öffentliches Issue versehentlich solche Daten,
entferne ich sie bzw. bitte dich, sie zu entfernen.

## Offizielle Quelle

Offiziell ist nur das Repository `DaAutimato/tracksy` auf GitHub. Kopien (Forks) anderer Personen
sind nicht von mir geprüft – installiere Tracksy nur von dort oder aus einer Quelle, der du
vertraust.
