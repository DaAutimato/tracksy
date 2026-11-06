#!/usr/bin/env bash
# Tracksy – Update einer per git clone + scripts/install.sh installierten App.
#
#   cd <Klon> && scripts/tracksy-update.sh [Optionen] [-- install.sh-Optionen]
#
# Ablauf (nur auf ausdrücklichen Aufruf; die App selbst prüft nie auf Updates):
#   Netzwerk: Schritt 2 kontaktiert das Git-Remote des Klons (veröffentlichte
#   Fassung: github.com; übertragen werden IP-Adresse und Git-Protokolldaten),
#   Schritt 5 PyPI (pip). Ohne Git-Zugriff: neuen Stand selbst kopieren, dann
#   --no-pull – PyPI wird auch dann angefragt; ganz ohne Internet geht es nicht.
#   1. Klon prüfen: keine lokalen Änderungen an versionierten Dateien (werden
#      nie verworfen – Abbruch mit Hinweis), Branch mit Upstream.
#   2. Neue Version holen: git fetch + nur Vorspulen (wie git pull --ff-only).
#   3. Laufende App sauber beenden (zeit quit); ein laufender Task bleibt
#      gespeichert und läuft nach dem Neustart weiter.
#   4. Sicherung der Daten (zeit backup), weil die neue Version die Datenbank
#      beim ersten Start migrieren kann.
#   5. scripts/install.sh erneut ausführen: venv-Abhängigkeiten aus
#      constraints.txt, Befehle, Starter und Icons. Autostart bleibt, wie er ist;
#      eine frühere Installation mit --no-voice bleibt ohne Spracherkennung;
#      whisper.cpp wird nur neu gebaut, wenn native/, third_party/whisper.cpp
#      oder die ABI sich geändert haben.
#   6. App wieder starten (im Hintergrund), wenn sie vorher lief.
#
# Optionen:
#   --dry-run            nur anzeigen, was getan würde (holt nur den Stand per git fetch)
#   --no-restart         App nach dem Update nicht wieder starten
#   --no-backup          keine Sicherung vor dem Update
#   --no-pull            kein git; nur die Installation an den Stand des Klons angleichen
#                        (install.sh fragt weiterhin PyPI an)
#   --ja                 neue Version ohne Rückfrage übernehmen (für Skripte)
#   --neu-installieren   auch installieren, wenn die Installation schon aktuell ist
#   --prefix-home DIR    DIR als Home verwenden (für Tests; App wird dann nicht
#                        beendet/gestartet)
#   -h, --help           diese Hilfe
# Alles nach "--" geht unverändert an scripts/install.sh (z. B. -- --rebuild-voice).
set -euo pipefail

APP_ID="de.local.Zeiterfassung"
VENV_MARKER_NAME=".zeiterfassung-installer"
SELF="$(readlink -f "$0")"
REPO="$(cd "$(dirname "$SELF")/.." && pwd)"

say() { echo "==> $*"; }
# Steuerzeichen (außer Tab/Zeilenumbruch) aus Fremdtext entfernen: Commit-Texte
# und Versionsangaben stammen aus dem geholten Stand (keine Terminal-Escapes)
# C0-Steuerzeichen (außer Tab/Zeilenumbruch), DEL und C1-Steuerzeichen in UTF-8
# (U+0080–U+009F, z. B. U+009B = CSI) entfernen
clean() { LC_ALL=C tr -d '\000-\010\013-\037\177' | LC_ALL=C sed 's/\xc2[\x80-\x9f]//g'; }
err() { echo "Fehler: $*" >&2; }

main() {
    local orig_args=("$@")
    DRY_RUN=0; NO_RESTART=0; NO_BACKUP=0; NO_PULL=0; REINSTALL=0; ASSUME_YES=0; PREFIX_HOME=""
    local continue_from="" install_extra=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run) DRY_RUN=1 ;;
            --no-restart) NO_RESTART=1 ;;
            --no-backup) NO_BACKUP=1 ;;
            --no-pull) NO_PULL=1 ;;
            --ja|--yes) ASSUME_YES=1 ;;
            --neu-installieren) REINSTALL=1 ;;
            --prefix-home) PREFIX_HOME="${2:?--prefix-home braucht ein Verzeichnis}"; shift ;;
            --_continue) continue_from="${2:?}"; shift ;;  # intern: Teil 2 nach git
            --) shift; install_extra=("$@"); break ;;
            -h|--help) sed -n '2,/^[^#]/{/^#/s/^# \{0,1\}//p}' "$SELF"; return 0 ;;
            *) err "unbekannte Option: $1"; echo "Hilfe: $(basename "$0") --help" >&2; return 2 ;;
        esac
        shift
    done

    resolve_paths
    if [[ ! -f "$VENV_MARKER" ]]; then
        err "keine Tracksy-Installation gefunden ($VENV_MARKER fehlt)."
        echo "Bitte zuerst installieren: $REPO/scripts/install.sh" >&2
        return 1
    fi
    read_marker

    if [[ -z "$continue_from" ]] && (( ! NO_PULL )); then
        local old_head
        old_head="$(git_update)" || return $?
        if (( ! DRY_RUN )) && [[ "$old_head" != "$(git -C "$REPO" rev-parse HEAD)" ]]; then
            # Neue Fassung dieses Skripts für den Rest verwenden (Teil 2)
            exec bash "$REPO/scripts/tracksy-update.sh" --_continue "$old_head" "${orig_args[@]}"
        fi
    fi
    install_update "${install_extra[@]}"
}

resolve_paths() {
    if [[ -n "$PREFIX_HOME" ]]; then
        HOME_DIR="$(realpath -m -- "$PREFIX_HOME")"
        DATA_HOME="$HOME_DIR/.local/share"
        CONFIG_HOME="$HOME_DIR/.config"
    else
        HOME_DIR="${HOME:?HOME ist nicht gesetzt}"
        DATA_HOME="${XDG_DATA_HOME:-$HOME_DIR/.local/share}"
        CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME_DIR/.config}"
        [[ "$DATA_HOME" == /* ]] || DATA_HOME="$HOME_DIR/.local/share"
        [[ "$CONFIG_HOME" == /* ]] || CONFIG_HOME="$HOME_DIR/.config"
    fi
    BIN_DIR="$HOME_DIR/.local/bin"
    APP_DATA="$DATA_HOME/zeiterfassung"
    VENV="$APP_DATA/venv"
    VENV_MARKER="$VENV/$VENV_MARKER_NAME"
}

marker_value() { sed -n "s/^$1=//p" "$VENV_MARKER" | head -n 1; }

read_marker() {
    INST_VERSION="$(marker_value version)"
    INST_COMMIT="$(marker_value commit)"
    INST_VOICE="$(marker_value voice)"
    INST_REPO="$(marker_value repo)"
    if [[ -z "$INST_VERSION" ]]; then  # Installation vor Einführung von update.sh
        INST_VERSION="$("$VENV/bin/python" -c \
            'import importlib.metadata as m; print(m.version("zeiterfassung"))' 2>/dev/null || echo unbekannt)"
    fi
    if [[ -z "$INST_VOICE" ]]; then
        [[ -f "$APP_DATA/lib/libtracksy_whisper.so" ]] && INST_VOICE=1 || INST_VOICE=0
    fi
}

# Version aus src/zeiterfassung/__init__.py eines git-Stands (oder des Arbeitsbaums)
repo_version() {
    local text
    if [[ -n "${1:-}" ]]; then
        text="$(git -C "$REPO" show "$1:src/zeiterfassung/__init__.py" 2>/dev/null || true)"
    else
        text="$(cat "$REPO/src/zeiterfassung/__init__.py" 2>/dev/null || true)"
    fi
    sed -n 's/^__version__ = "\(.*\)"/\1/p' <<<"$text" | head -n 1 | clean
}

describe() { git -C "$REPO" describe --tags --always "$1" 2>/dev/null || echo "${1:0:12}"; }

# Abschnitte aus CHANGELOG.md des geholten Stands von oben bis zur bisherigen Version
# anzeigen (nur lesen, ohne Netz; Steuerzeichen entfernt; höchstens 60 Zeilen)
CHANGELOG_MAX_LINES=60
show_changelog() {
    local new="$1" old_version="$2" text out total
    # höchstens 256 KiB lesen, Zeilen auf 300 Byte kürzen
    text="$( (git -C "$REPO" show "$new:CHANGELOG.md" 2>/dev/null || true) | head -c 262144 | clean)" || return 0
    [[ -n "$text" ]] || return 0
    out="$(awk -v old="## [$old_version]" -v CHANGELOG_LINE_MAX=300 '
        index($0, "## [") == 1 { if (old != "## []" && index($0, old) == 1) exit; show = 1 }
        show && !/^\[[^]]*\]: / { print substr($0, 1, CHANGELOG_LINE_MAX) }' <<<"$text")"
    [[ -n "${out//[[:space:]]/}" ]] || return 0
    total="$(wc -l <<<"$out")"
    say "Änderungen laut CHANGELOG.md:" >&2
    head -n "$CHANGELOG_MAX_LINES" <<<"$out" | sed 's/^/    /' >&2
    if (( total > CHANGELOG_MAX_LINES )); then
        echo "    … gekürzt (vollständig: git -C \"$REPO\" show ${new:0:12}:CHANGELOG.md)" >&2
    fi
}

# Teil 1: Klon aktualisieren. Gibt den alten HEAD auf stdout aus.
git_update() {
    command -v git >/dev/null || { err "git ist nicht installiert (sudo apt install git)."; return 1; }
    if [[ "$(git -C "$REPO" rev-parse --show-toplevel 2>/dev/null)" != "$REPO" ]]; then
        err "$REPO ist kein git-Klon."
        echo "Ohne git: neue Fassung herunterladen und dort scripts/install.sh ausführen" >&2
        echo "(oder hier nur neu installieren: $(basename "$0") --no-pull)." >&2
        return 1
    fi
    local dirty
    dirty="$(git -C "$REPO" status --porcelain --untracked-files=no)"
    if [[ -n "$dirty" ]]; then
        err "Im Klon gibt es lokale Änderungen – das Update verwirft sie nicht und bricht ab:"
        sed 's/^/    /' <<<"$dirty" >&2
        # Nur Dateirechte geändert (Inhalt gleich, z. B. Ausführungsrecht nach dem
        # Kopieren verloren)? Dann gezielten, verlustfreien Befehl nennen.
        if [[ -z "$(git -C "$REPO" -c core.fileMode=false status --porcelain --untracked-files=no)" ]]; then
            local files
            files="$(git -C "$REPO" diff HEAD --name-only -z | LC_ALL=C xargs -0 printf ' %q')"
            cat >&2 <<EOF
Geändert haben sich nur Dateirechte, nicht der Inhalt (z. B. nach Kopieren oder
durch einen Dateimanager). Rechte wiederherstellen – dabei geht nichts verloren:
    git -C "$REPO" restore --source=HEAD --staged --worktree --$files
Danach erneut: $REPO/scripts/tracksy-update.sh
EOF
            return 1
        fi
        cat >&2 <<EOF
Bitte selbst entscheiden, z. B.:
    git -C "$REPO" diff          # Änderungen ansehen
    git -C "$REPO" stash         # beiseitelegen (später: git stash pop)
Danach erneut: $REPO/scripts/tracksy-update.sh
EOF
        return 1
    fi
    local branch upstream remote url
    if ! branch="$(git -C "$REPO" symbolic-ref -q --short HEAD)"; then
        err "Der Klon steht auf keinem Branch (detached HEAD)."
        echo "Zurück zum Hauptzweig: git -C \"$REPO\" switch main" >&2
        return 1
    fi
    if ! upstream="$(git -C "$REPO" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"; then
        err "Branch '$branch' hat keinen Upstream – woher das Update kommen soll, ist unklar."
        echo "Einrichten z. B.: git -C \"$REPO\" branch --set-upstream-to=origin/main" >&2
        return 1
    fi
    remote="$(git -C "$REPO" config --get "branch.$branch.remote" || echo origin)"
    # Zugangsdaten in der URL (https://user:token@…) nie ausgeben
    # (gierig bis zum letzten @ vor dem ersten /, auch bei unkodiertem @ im Passwort)
    url="$(git -C "$REPO" remote get-url "$remote" 2>/dev/null | sed -E 's#://[^/]*@#://#' | clean || true)"
    [[ -n "$url" ]] || url="$remote"
    say "Hole neuen Stand von $url ($upstream)" >&2
    if ! git -C "$REPO" fetch --quiet --tags "$remote" >&2; then
        err "Abruf von $url fehlgeschlagen (keine Internetverbindung, Adresse geändert oder kein Zugriff)."
        echo "Es wurde nichts verändert. Später erneut versuchen." >&2
        return 1
    fi
    local old new ahead
    old="$(git -C "$REPO" rev-parse HEAD)"
    new="$(git -C "$REPO" rev-parse '@{u}')"
    if [[ "$old" == "$new" ]]; then
        say "Klon ist auf dem neuesten Stand ($(describe "$old"))." >&2
    elif git -C "$REPO" merge-base --is-ancestor "$old" "$new"; then
        local n
        n="$(git -C "$REPO" rev-list --count "$old..$new")"
        say "Neu verfügbar: Version $(repo_version "$new") ($(describe "$new"), $n neue Commits)" >&2
        git -C "$REPO" log --oneline --no-decorate "$old..$new" | head -n 15 | clean | sed 's/^/    /' >&2
        # Vor der Rückfrage sichtbar machen, dass die Liste gekürzt ist
        if (( n > 15 )); then
            echo "    … und $(( n - 15 )) weitere (vollständig: git -C \"$REPO\" log --oneline $old..$new)" >&2
        fi
        show_changelog "$new" "$(repo_version "$old")"
        if (( DRY_RUN )); then
            echo "[dry-run] git merge --ff-only $upstream (Klon vorspulen)" >&2
        elif ! confirm_update; then
            echo "Abgebrochen – es wurde nichts verändert." >&2
            return 1
        elif ! git -C "$REPO" merge --ff-only --quiet "$new" >&2; then
            err "Vorspulen auf den neuen Stand nicht möglich (z. B. würde eine nicht versionierte Datei überschrieben)."
            echo "Es wurde nichts verändert. Siehe Meldung von git oben." >&2
            return 1
        fi
    elif git -C "$REPO" merge-base --is-ancestor "$new" "$old"; then
        ahead="$(git -C "$REPO" rev-list --count "$new..$old")"
        say "Klon enthält $ahead eigene Commits und ist sonst aktuell – nichts zu holen." >&2
    else
        err "Ihr Klon und das Repository sind auseinandergelaufen (eigene Commits oder das Repository wurde neu geschrieben)."
        cat >&2 <<EOF
Es wurde nichts verändert. Unterschiede ansehen:
    git -C "$REPO" log --oneline --left-right HEAD...$upstream
Eigene Commits behalten Sie z. B. mit:  git -C "$REPO" branch meine-aenderungen
Erst danach den Klon auf den offiziellen Stand setzen (verwirft eigene Commits auf '$branch'!):
    git -C "$REPO" reset --hard $upstream
EOF
        return 1
    fi
    echo "$old"
}

# Rückfrage vor dem Übernehmen des geholten Codes: Erst danach laufen die
# neuen Fassungen von update.sh/install.sh. Ohne Terminal nur mit --ja.
confirm_update() {
    (( ASSUME_YES )) && return 0
    if [[ ! -t 0 ]]; then
        err "Rückfrage nicht möglich (keine Eingabe vom Terminal). Zum Übernehmen ohne Rückfrage: --ja"
        return 1
    fi
    local answer=""
    read -r -p "Diese Version jetzt übernehmen und installieren? [j/N] " answer >&2 || true
    [[ "$answer" =~ ^[jJyY] ]]
}

# App-Erkennung über den Session-Bus (Bus-Name der App), mit dem venv-Python
app_running() {
    [[ -x "$VENV/bin/python" ]] || return 1
    "$VENV/bin/python" -c '
import sys
try:
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    r = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                      "NameHasOwner", GLib.Variant("(s)", (sys.argv[1],)),
                      GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 2000, None)
    sys.exit(0 if r.unpack()[0] else 1)
except Exception:
    sys.exit(1)
' "$APP_ID" 2>/dev/null
}

manage_app() {
    # Mit --prefix-home (Tests) nie die echte App der Sitzung beenden/starten
    [[ -z "$PREFIX_HOME" || "${ZEIT_UPDATE_MANAGE_APP:-0}" == 1 ]]
}

# Umgebung für Aufrufe der installierten CLI (bei --prefix-home dorthin umbiegen)
zeit_cli() {
    if [[ -n "$PREFIX_HOME" ]]; then
        env -u ZEIT_DATA_DIR -u ZEIT_CONFIG -u ZEIT_MODELS_DIR HOME="$HOME_DIR" \
            XDG_DATA_HOME="$DATA_HOME" XDG_CONFIG_HOME="$CONFIG_HOME" "$VENV/bin/zeit" "$@"
    else
        "$VENV/bin/zeit" "$@"
    fi
}

stop_app() {
    say "Beende Tracksy für das Update (ein laufender Task bleibt gespeichert und läuft danach weiter)"
    if (( DRY_RUN )); then
        echo "[dry-run] zeit quit"
        return 0
    fi
    zeit_cli quit >/dev/null || true
    local i
    for (( i = 0; i < 100; i++ )); do  # bis 20 s
        app_running || return 0
        sleep 0.2
    done
    err "Tracksy hat sich nicht beendet. Bitte selbst beenden (Tray → Beenden) und das Update erneut starten."
    echo "Der Klon ist bereits aktualisiert; die Installation noch nicht." >&2
    return 1
}

backup_data() {
    say "Sichere die Daten vor dem Update (zeit backup)"
    if (( DRY_RUN )); then
        echo "[dry-run] zeit backup"
        return 0
    fi
    local out rc=0
    out="$(zeit_cli backup 2>&1)" || rc=$?
    if (( rc == 0 )); then
        sed 's/^/    /' <<<"$out"
    elif grep -q "nichts zu sichern" <<<"$out"; then
        echo "    Noch keine Daten – keine Sicherung nötig."
    else
        sed 's/^/    /' <<<"$out" >&2
        err "Sicherung fehlgeschlagen – Update abgebrochen (ohne Sicherung: --no-backup)."
        echo "Der Klon ist bereits aktualisiert; die Installation noch nicht." >&2
        return 1
    fi
}

start_app() {
    say "Starte Tracksy wieder (im Hintergrund, Tray-Symbol)"
    if (( DRY_RUN )); then
        echo "[dry-run] $BIN_DIR/zeiterfassung --hidden"
        return 0
    fi
    if [[ -n "$PREFIX_HOME" ]]; then
        setsid -f env HOME="$HOME_DIR" XDG_DATA_HOME="$DATA_HOME" XDG_CONFIG_HOME="$CONFIG_HOME" \
            "$BIN_DIR/zeiterfassung" --hidden </dev/null >/dev/null 2>&1
    else
        setsid -f "$BIN_DIR/zeiterfassung" --hidden </dev/null >/dev/null 2>&1
    fi
}

# Teil 2: Installation an den Stand des Klons angleichen
install_update() {
    local head="" new_version
    command -v git >/dev/null && head="$(git -C "$REPO" rev-parse --verify -q HEAD 2>/dev/null || true)"
    if (( DRY_RUN )) && (( ! NO_PULL )) && [[ -n "$head" ]]; then
        # Im Dry-Run wurde nicht vorgespult: Ziel ist der Upstream-Stand
        head="$(git -C "$REPO" rev-parse -q --verify '@{u}' 2>/dev/null || echo "$head")"
        new_version="$(repo_version "$head")"
    else
        new_version="$(repo_version)"
    fi
    if [[ -n "$INST_REPO" && "$INST_REPO" != "$REPO" ]]; then
        echo "Hinweis: Die Installation stammt aus $INST_REPO – sie wird jetzt aus $REPO aktualisiert."
    fi
    if (( ! REINSTALL )) && [[ -n "$head" && "$INST_COMMIT" == "$head" ]]; then
        say "Tracksy ist aktuell (Version ${new_version:-?}, Stand $(describe "$head"))."
        (( DRY_RUN )) && echo "(Dry-Run: es wurde nichts verändert.)"
        return 0
    fi
    say "Update: Version ${INST_VERSION:-?} → ${new_version:-?}"

    local was_running=0
    if manage_app && app_running; then
        was_running=1
        stop_app || return 1
    fi
    if (( ! NO_BACKUP )); then
        backup_data || return 1
    fi

    local args=()
    [[ -n "$PREFIX_HOME" ]] && args+=(--prefix-home "$HOME_DIR")
    (( DRY_RUN )) && args+=(--dry-run)
    [[ "$INST_VOICE" == 0 ]] && args+=(--no-voice)
    # Kein --autostart: eine vorhandene Autostart-Datei (auch "aus" in den
    # Einstellungen) bleibt unverändert.
    say "Aktualisiere die Installation (scripts/install.sh ${args[*]} $*)"
    if ! bash "$REPO/scripts/install.sh" "${args[@]}" "$@"; then
        err "Installation fehlgeschlagen. Ihre Daten sind unverändert."
        echo "Ursache oben beheben (z. B. fehlende Systempakete) und erneut ausführen: $REPO/scripts/tracksy-update.sh" >&2
        (( was_running )) && echo "Tracksy wurde beendet und nicht neu gestartet." >&2
        return 1
    fi

    if (( was_running )) && (( ! NO_RESTART )); then
        start_app
    elif (( was_running )); then
        echo "Tracksy wurde beendet und nicht neu gestartet (--no-restart)."
    fi
    if (( DRY_RUN )); then
        say "Dry-Run abgeschlossen."
    else
        say "Update abgeschlossen: Tracksy ${new_version:-?}"
    fi
}

main "$@"
exit $?
