#!/usr/bin/env bash
# Zeiterfassung – Deinstallation pro Benutzer.
#
# Entfernt die von install.sh angelegten Programmdateien:
#   venv, Wrapper (zeit, zeit-tray, zeiterfassung), Starter, Icon, Autostart,
#   Spracherkennung (lib/libtracksy_whisper.so).
# Nutzerdaten bleiben IMMER erhalten: Datenbank, CSV-Protokoll, Exporte,
# Modelle (${XDG_DATA_HOME}/zeiterfassung) und Konfiguration
# (${XDG_CONFIG_HOME}/zeiterfassung).
#
# Standard: Programmdateien werden nicht gelöscht, sondern nach
#   ${XDG_DATA_HOME}/zeiterfassung/_archiv/<zeitstempel>/
# verschoben (rückgängig machbar). Mit --endgueltig werden genau diese Pfade
# gelöscht (spart Platz; die venv kann > 1 GB groß sein).
# Dateien ohne Installer-Markierung werden nie angefasst.
set -euo pipefail

APP_ID="de.local.Zeiterfassung"
MARKER="X-Zeiterfassung-Installer=1"
WRAPPER_MARKER="# zeiterfassung-installer-wrapper"
VENV_MARKER_NAME=".zeiterfassung-installer"

DRY_RUN=0
PURGE=0
PREFIX_HOME=""

usage() {
    cat <<EOF
Aufruf: $(basename "$0") [Optionen]

  --dry-run            nur anzeigen, was getan würde
  --endgueltig         Programmdateien löschen statt ins Archiv verschieben
  --prefix-home DIR    DIR als Home verwenden (XDG-Pfade darunter; für Tests)
  -h, --help           diese Hilfe
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --endgueltig) PURGE=1 ;;
        --prefix-home) PREFIX_HOME="${2:?--prefix-home braucht ein Verzeichnis}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unbekannte Option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

# Nur absolute XDG-Pfade gelten (XDG-Spezifikation); relative → Default.
abs_or_default() {
    local value="$1" default="$2" name="$3"
    case "$value" in
        "") printf '%s' "$default" ;;
        /*) printf '%s' "$value" ;;
        *) echo "Hinweis: $name ist nicht absolut – verwende $default" >&2
           printf '%s' "$default" ;;
    esac
}
if [[ -n "$PREFIX_HOME" ]]; then
    HOME_DIR="$(realpath -m -- "$PREFIX_HOME")"
    DATA_HOME="$HOME_DIR/.local/share"
    CONFIG_HOME="$HOME_DIR/.config"
else
    HOME_DIR="${HOME:?HOME ist nicht gesetzt}"
    case "$HOME_DIR" in /*) ;; *) echo "Fehler: HOME ist kein absoluter Pfad." >&2; exit 1 ;; esac
    DATA_HOME="$(abs_or_default "${XDG_DATA_HOME:-}" "$HOME_DIR/.local/share" XDG_DATA_HOME)"
    CONFIG_HOME="$(abs_or_default "${XDG_CONFIG_HOME:-}" "$HOME_DIR/.config" XDG_CONFIG_HOME)"
fi
BIN_DIR="$HOME_DIR/.local/bin"
APP_DATA="$DATA_HOME/zeiterfassung"
VENV="$APP_DATA/venv"
ARCHIVE="$APP_DATA/_archiv/$(date +%Y%m%d-%H%M%S)"

targets=()
# venv nur, wenn es eine venv ist UND die Installer-Markierung trägt – oder eine
# abgebrochene Anlage (nur die Markierung)
if [[ -f "$VENV/$VENV_MARKER_NAME" ]] && \
   [[ -f "$VENV/pyvenv.cfg" || "$(ls -A -- "$VENV")" == "$VENV_MARKER_NAME" ]]; then
    targets+=("$VENV")
elif [[ -e "$VENV" ]]; then
    echo "Übersprungen (nicht vom Installer, Markierung $VENV_MARKER_NAME fehlt): $VENV"
fi
for f in "$BIN_DIR/zeit" "$BIN_DIR/zeit-tray" "$BIN_DIR/zeiterfassung"; do
    if [[ -f "$f" ]]; then
        if grep -qF -- "$WRAPPER_MARKER" "$f"; then
            targets+=("$f")
        else
            echo "Übersprungen (nicht vom Installer): $f"
        fi
    fi
done
for f in "$DATA_HOME/applications/$APP_ID.desktop" "$CONFIG_HOME/autostart/$APP_ID.desktop"; do
    if [[ -f "$f" ]]; then
        if grep -qF -- "$MARKER" "$f"; then
            targets+=("$f")
        else
            echo "Übersprungen (nicht vom Installer): $f"
        fi
    fi
done
for icon in "$DATA_HOME"/icons/hicolor/*/apps/"$APP_ID".png "$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg"; do
    [[ -f "$icon" ]] && targets+=("$icon")
done
# Spracherkennung (vom Installer gebaut, Markierung WHISPER_VERSION)
[[ -f "$APP_DATA/lib/WHISPER_VERSION" ]] && targets+=("$APP_DATA/lib")

if (( ${#targets[@]} == 0 )); then
    echo "Keine installierten Programmdateien gefunden."
else
    for t in "${targets[@]}"; do
        if (( PURGE )); then
            if (( DRY_RUN )); then echo "[dry-run] lösche $t"; else rm -rf -- "$t"; echo "gelöscht: $t"; fi
        else
            # Struktur relativ zu HOME im Archiv nachbilden
            rel="${t#"$HOME_DIR"/}"
            [[ "$rel" == "$t" ]] && rel="extern${t}"
            dest="$ARCHIVE/$rel"
            if (( DRY_RUN )); then
                echo "[dry-run] verschiebe $t -> $dest"
            else
                mkdir -p -m 700 "$(dirname "$dest")"
                mv -- "$t" "$dest"
                echo "archiviert: $t -> $dest"
            fi
        fi
    done
    if command -v update-desktop-database >/dev/null && [[ -d "$DATA_HOME/applications" ]]; then
        if (( DRY_RUN )); then
            echo "[dry-run] update-desktop-database $DATA_HOME/applications"
        else
            update-desktop-database "$DATA_HOME/applications" || true
        fi
    fi
fi

echo
echo "Ihre Daten bleiben erhalten:"
echo "  Daten/Modelle: $APP_DATA"
echo "  Konfiguration: $CONFIG_HOME/zeiterfassung"
# Eigener Speicherort (Einstellungen → Daten → Speicherort, general.data_dir) – nur anzeigen
CUSTOM_DATA=""
if [[ -f "$CONFIG_HOME/zeiterfassung/config.toml" ]]; then
    CUSTOM_DATA="$(sed -n 's/^data_dir[[:space:]]*=[[:space:]]*"\(.*\)"[[:space:]]*$/\1/p' \
        "$CONFIG_HOME/zeiterfassung/config.toml" 2>/dev/null | head -n 1 || true)"
fi
if [[ -n "$CUSTOM_DATA" ]]; then
    echo "  Eigener Speicherort der Daten: $CUSTOM_DATA"
fi
if (( ! PURGE )) && (( ${#targets[@]} )); then
    echo "  Archivierte Programmdateien: $ARCHIVE (kann gelöscht werden)"
fi
(( DRY_RUN )) && echo "(Dry-Run: es wurde nichts verändert.)"
exit 0
