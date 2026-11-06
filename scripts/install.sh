#!/usr/bin/env bash
# Zeiterfassung – Installation pro Benutzer (ohne root).
#
# Legt an:
#   venv        ${XDG_DATA_HOME:-~/.local/share}/zeiterfassung/venv  (--system-site-packages,
#               Markierung venv/.zeiterfassung-installer mit Version/Stand/Optionen
#               für scripts/tracksy-update.sh; Versionen aus constraints.txt)
#   Wrapper     ~/.local/bin/zeit, ~/.local/bin/zeit-tray, ~/.local/bin/zeiterfassung
#   Starter     ${XDG_DATA_HOME}/applications/de.local.Zeiterfassung.desktop
#   Icon        ${XDG_DATA_HOME}/icons/hicolor/<N>x<N>/apps/de.local.Zeiterfassung.png (Tracksy-Logo)
#   Autostart   ${XDG_CONFIG_HOME:-~/.config}/autostart/de.local.Zeiterfassung.desktop (nur mit --autostart)
#   Sprache     ${XDG_DATA_HOME}/zeiterfassung/lib/libtracksy_whisper.so (whisper.cpp, lokal
#               kompiliert über scripts/build-whisper.sh; entfällt mit --no-voice;
#               bei erneutem Lauf nur neu gebaut, wenn Quellen oder ABI sich ändern)
#
# Wiederholbar: ein zweiter Lauf aktualisiert die bestehende Installation
# (so ruft scripts/tracksy-update.sh es auf). Die App wird außerhalb des Repos gebaut
# (temporäre Kopie), damit im Klon kein build/ oder *.egg-info entsteht.
#
# Kopiert KEINE Daten (data/), Modelle (models/) oder Excel-Vorlagen. Das
# Whisper-Modell lädt der Nutzer bei Bedarf im Einrichtungsassistenten.
# apt wird NIE ausgeführt; fehlende Pakete werden nur gemeldet.
set -euo pipefail

APP_ID="de.local.Zeiterfassung"
MARKER="X-Zeiterfassung-Installer=1"
WRAPPER_MARKER="# zeiterfassung-installer-wrapper"
VENV_MARKER_NAME=".zeiterfassung-installer"
REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
CONSTRAINTS="$REPO/constraints.txt"
# python3.X-venv wird passend zur gefundenen Python-Version ergänzt (Ubuntu 24.04: 3.12, neuere: 3.13+)
DEFAULT_PKGS="gir1.2-gtk-4.0 gir1.2-adw-1 python3-gi gir1.2-ayatanaappindicator3-0.1 libportaudio2"
# Nur für die Spracherkennung (Build von whisper.cpp):
VOICE_PKGS="cmake g++"
# Nur für Tests überschreibbar:
APT_PKGS="${ZEIT_APT_PACKAGES:-$DEFAULT_PKGS}"
APT_VOICE_PKGS="${ZEIT_APT_VOICE_PACKAGES:-$VOICE_PKGS}"

DRY_RUN=0
AUTOSTART=0
NO_DEPS=0
NO_VOICE=0
REBUILD_VOICE=0
WHEEL=""
PYTHON_BIN=""
PREFIX_HOME=""

usage() {
    cat <<EOF
Aufruf: $(basename "$0") [Optionen]

  --autostart          App bei Anmeldung im Hintergrund starten (--hidden)
  --dry-run            nur anzeigen, was getan würde
  --prefix-home DIR    DIR als Home verwenden (XDG-Pfade darunter; für Tests)
  --python PFAD        Python-Interpreter (Default: python3.12 bzw. python3 ≥ 3.12)
  --wheel DATEI        vorgebautes Wheel statt Installation aus dem Repo
  --no-deps            Python-Abhängigkeiten nicht installieren (offline/Tests)
  --no-voice           Spracherkennung (whisper.cpp) nicht bauen
  --rebuild-voice      Spracherkennung auch bauen, wenn sie aktuell ist
  -h, --help           diese Hilfe
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --autostart) AUTOSTART=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --prefix-home) PREFIX_HOME="${2:?--prefix-home braucht ein Verzeichnis}"; shift ;;
        --python) PYTHON_BIN="${2:?--python braucht einen Pfad}"; shift ;;
        --wheel) WHEEL="${2:?--wheel braucht eine Datei}"; shift ;;
        --no-deps) NO_DEPS=1 ;;
        --no-voice) NO_VOICE=1 ;;
        --rebuild-voice) REBUILD_VOICE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unbekannte Option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

# --- Pfade -------------------------------------------------------------------
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
check_path_chars() {
    local p bad='["$`\|&%'"'"';]'
    for p in "$@"; do
        if [[ "$p" =~ $bad || "$p" == *$'\n'* ]]; then
            echo "Fehler: Pfad enthält nicht unterstützte Sonderzeichen: $p" >&2
            exit 1
        fi
    done
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
# Pfade landen in Wrapper-Skripten, sed-Ersetzungen und Desktop-Dateien:
# Zeichen mit Sonderbedeutung dort ablehnen statt fehleranfällig zu maskieren.
check_path_chars "$HOME_DIR" "$DATA_HOME" "$CONFIG_HOME"
BIN_DIR="$HOME_DIR/.local/bin"
APP_DATA="$DATA_HOME/zeiterfassung"
VENV="$APP_DATA/venv"
VENV_MARKER="$VENV/$VENV_MARKER_NAME"
APPS_DIR="$DATA_HOME/applications"
ICON_BASE="$DATA_HOME/icons/hicolor"
AUTOSTART_DIR="$CONFIG_HOME/autostart"
DESKTOP_FILE="$APPS_DIR/$APP_ID.desktop"
AUTOSTART_FILE="$AUTOSTART_DIR/$APP_ID.desktop"

say() { echo "==> $*"; }
run() {
    if (( DRY_RUN )); then
        printf '[dry-run] %q' "$1"; shift; printf ' %q' "$@"; echo
    else
        "$@"
    fi
}
# write_file PFAD MODUS  (Inhalt von stdin)
write_file() {
    local path="$1" mode="$2" content
    content="$(cat)"
    if (( DRY_RUN )); then
        echo "[dry-run] schreibe $path (Modus $mode):"
        sed 's/^/    | /' <<<"$content"
    else
        printf '%s\n' "$content" >"$path.tmp.$$"
        chmod "$mode" "$path.tmp.$$"
        mv -f "$path.tmp.$$" "$path"
    fi
}
# Fremde Dateien nie überschreiben: nur eigene (mit Marker) oder fehlende.
check_ours() {
    local path="$1" marker="$2"
    if [[ -e "$path" ]] && ! grep -qF -- "$marker" "$path" 2>/dev/null; then
        echo "Fehler: $path existiert und stammt nicht von diesem Installer – bitte selbst prüfen." >&2
        exit 1
    fi
}

# --- 1. Voraussetzungen ------------------------------------------------------
say "Prüfe Systempakete"
# System-Python bevorzugen: nur dafür liefert apt PyGObject (python3-gi)
if [[ -z "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3 || command -v python3.12 || true)"
fi
if [[ -z "$PYTHON_BIN" ]] || ! "$PYTHON_BIN" -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
    echo "Fehler: Python ≥ 3.12 nicht gefunden (Option --python)." >&2
    exit 1
fi
PY_VER="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
VENV_PKG="python${PY_VER}-venv"

missing=()
pkgs="$APT_PKGS $VENV_PKG"
(( NO_VOICE )) || pkgs="$pkgs $APT_VOICE_PKGS"
for pkg in $pkgs; do
    status="$(dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null || true)"
    if [[ "$status" != "install ok installed" ]]; then
        missing+=("$pkg")
    fi
done
if (( ${#missing[@]} )); then
    echo "Fehlende Systempakete: ${missing[*]}" >&2
    echo "Bitte installieren (benötigt Administratorrechte) und das Skript erneut starten:" >&2
    echo "    sudo apt install ${missing[*]}" >&2
    (( NO_VOICE )) || echo "(cmake und g++ werden nur für die Spracherkennung gebraucht; ohne: --no-voice)" >&2
    exit 1
fi
if ! "$PYTHON_BIN" -c 'import gi' 2>/dev/null; then
    echo "Fehler: PyGObject (gi) fehlt für $PYTHON_BIN – bitte python3-gi installieren bzw. mit --python das System-Python angeben." >&2
    exit 1
fi

if [[ -n "$WHEEL" && ! -f "$WHEEL" ]]; then
    echo "Fehler: Wheel nicht gefunden: $WHEEL" >&2
    exit 1
fi
for f in "$BIN_DIR/zeit" "$BIN_DIR/zeit-tray" "$BIN_DIR/zeiterfassung"; do
    check_ours "$f" "$WRAPPER_MARKER"
done
check_ours "$DESKTOP_FILE" "$MARKER"
(( AUTOSTART )) && check_ours "$AUTOSTART_FILE" "$MARKER"
# Vorhandene venv nur aktualisieren, wenn sie von diesem Installer stammt
if [[ -e "$VENV" && ! -f "$VENV_MARKER" ]]; then
    echo "Fehler: $VENV existiert und stammt nicht von diesem Installer (Markierung $VENV_MARKER_NAME fehlt) – bitte selbst prüfen bzw. umbenennen." >&2
    exit 1
fi

# Eigene Temp-Verzeichnisse (Constraints-Kopie, Quellkopie) am Ende entfernen
# (rm statt Archiv: nur selbst angelegte Kopien, keine Nutzerdaten).
TMP_DIRS=()
cleanup_tmp() {
    local d
    for d in "${TMP_DIRS[@]}"; do
        [[ -n "$d" && -d "$d" && "$(basename "$d")" == tracksy-install.* ]] && rm -rf -- "$d"
    done
    return 0
}
trap cleanup_tmp EXIT

# --- 2. venv + App -----------------------------------------------------------
say "Lege venv an: $VENV"
run install -d -m 700 "$APP_DATA"
if [[ -x "$VENV/bin/python" ]]; then
    say "venv existiert bereits – wird aktualisiert"
else
    # Markierung VOR der venv anlegen: bricht "python -m venv" ab, erkennt
    # der nächste Lauf die halbe venv als eigene und legt sie erneut an.
    [[ -d "$VENV" ]] || run install -d -m 700 "$VENV"
    [[ -f "$VENV_MARKER" ]] || echo "zeiterfassung-installer venv" | write_file "$VENV_MARKER" 600
    run "$PYTHON_BIN" -m venv --system-site-packages "$VENV"
fi
pip_args=(install --upgrade --disable-pip-version-check)
if [[ -f "$CONSTRAINTS" ]]; then
    # -c gilt nicht für die isolierte Build-Umgebung von pip (setuptools): per
    # Umgebungsvariable auch dort die getesteten Versionen erzwingen – immer,
    # auch mit --no-deps.
    # pip trennt PIP_CONSTRAINT an Leerraum: liegt der Checkout in einem Pfad mit
    # Leerzeichen, eine Kopie (nur Versionsnummern) in einem eigenen
    # Temp-Verzeichnis ohne Leerraum verwenden.
    PIP_CONSTRAINT_FILE="$CONSTRAINTS"
    if [[ "$CONSTRAINTS" =~ [[:space:]] ]]; then
        if (( DRY_RUN )); then
            PIP_CONSTRAINT_FILE="<temp>/constraints.txt"
            echo "[dry-run] kopiere $CONSTRAINTS nach $PIP_CONSTRAINT_FILE (Pfad enthält Leerzeichen)"
        else
            CONSTRAINTS_TMP=""
            for tmp_base in "${TMPDIR:-/tmp}" /tmp; do
                [[ "$tmp_base" =~ [[:space:]] ]] && continue
                CONSTRAINTS_TMP="$(mktemp -d "$tmp_base/tracksy-install.XXXXXXXX")" && break
            done
            TMP_DIRS+=("$CONSTRAINTS_TMP")
            if [[ -z "$CONSTRAINTS_TMP" || "$CONSTRAINTS_TMP" =~ [[:space:]] ]]; then
                echo "Fehler: kein temporäres Verzeichnis ohne Leerzeichen für $CONSTRAINTS verfügbar." >&2
                exit 1
            fi
            install -m 600 -- "$CONSTRAINTS" "$CONSTRAINTS_TMP/constraints.txt"
            PIP_CONSTRAINT_FILE="$CONSTRAINTS_TMP/constraints.txt"
        fi
    fi
    export PIP_CONSTRAINT="$PIP_CONSTRAINT_FILE"
    say "Build-Umgebung nutzt getestete Versionen (PIP_CONSTRAINT=$PIP_CONSTRAINT_FILE)"
fi
if (( NO_DEPS )); then
    pip_args+=(--no-deps)
elif [[ -f "$CONSTRAINTS" ]]; then
    say "Verwende getestete Versionen aus $CONSTRAINTS"
    pip_args+=(-c "$CONSTRAINTS")
else
    echo "Warnung: $CONSTRAINTS fehlt – Abhängigkeiten werden ungepinnt installiert." >&2
fi
if [[ -n "$WHEEL" ]]; then
    say "Installiere App aus Wheel $WHEEL"
    run "$VENV/bin/pip" "${pip_args[@]}" "$WHEEL"
else
    say "Installiere App aus $REPO (lädt Python-Abhängigkeiten von PyPI)"
    # Aus einer temporären Kopie bauen: setuptools legt build/ und *.egg-info
    # sonst im Repo an – stört git pull/sauberen Klon, und alte Dateien in
    # build/lib können in neue Versionen rutschen.
    src_parts=(pyproject.toml src)
    for f in README.md LICENSE; do [[ -f "$REPO/$f" ]] && src_parts+=("$f"); done
    if (( DRY_RUN )); then
        echo "[dry-run] kopiere ${src_parts[*]} aus $REPO nach <temp>/src (Build außerhalb des Repos)"
        run "$VENV/bin/pip" "${pip_args[@]}" "<temp>/src"
    else
        SRC_TMP="$(mktemp -d "${TMPDIR:-/tmp}/tracksy-install.XXXXXXXX")"
        TMP_DIRS+=("$SRC_TMP")
        mkdir -m 700 "$SRC_TMP/src"
        tar -C "$REPO" --exclude=__pycache__ --exclude='*.py[co]' --exclude='*.egg-info' \
            -cf - "${src_parts[@]}" | tar -C "$SRC_TMP/src" -xf -
        run "$VENV/bin/pip" "${pip_args[@]}" "$SRC_TMP/src"
    fi
fi

# --- 2b. Spracherkennung (whisper.cpp) ---------------------------------------
# voice_current: vorhandene Bibliothek passt zu den Quellen (Fingerabdruck von
# build-whisper.sh) und zur ABI der installierten App → kein Neubau nötig.
voice_current() {
    local lib="$APP_DATA/lib/libtracksy_whisper.so" want have expected actual
    [[ -f "$lib" && -f "$APP_DATA/lib/SOURCE_HASH" ]] || return 1
    want="$("$REPO/scripts/build-whisper.sh" --print-source-hash 2>/dev/null)" || return 1
    have="$(head -n 1 "$APP_DATA/lib/SOURCE_HASH")"
    [[ -n "$want" && "$want" == "$have" ]] || return 1
    expected="$(sed -n 's/^ABI_VERSION = \([0-9][0-9]*\).*/\1/p' \
        "$REPO/src/zeiterfassung/voice/whispercpp.py" 2>/dev/null | head -n 1)"
    [[ -n "$expected" && -x "$VENV/bin/python" ]] || return 1
    actual="$("$VENV/bin/python" -c 'import ctypes, sys
lib = ctypes.CDLL(sys.argv[1])
lib.tw_abi_version.restype = ctypes.c_int
print(lib.tw_abi_version())' "$lib" 2>/dev/null)" || return 1
    [[ "$actual" == "$expected" ]]
}
if (( NO_VOICE )); then
    say "Spracherkennung übersprungen (--no-voice)"
elif (( ! REBUILD_VOICE )) && voice_current; then
    say "Spracherkennung ist aktuell – kein Neubau nötig (erzwingen: --rebuild-voice)"
else
    say "Baue Spracherkennung whisper.cpp aus third_party/ (offline, dauert einige Minuten)"
    run "$REPO/scripts/build-whisper.sh" --out "$APP_DATA/lib"
fi

# --- 3. Wrapper --------------------------------------------------------------
say "Lege Befehle in $BIN_DIR an"
run install -d -m 755 "$BIN_DIR"
for name in zeit zeit-tray; do
    write_file "$BIN_DIR/$name" 755 <<EOF
#!/usr/bin/env bash
$WRAPPER_MARKER
exec $(printf '%q' "$VENV/bin/$name") "\$@"
EOF
done
write_file "$BIN_DIR/zeiterfassung" 755 <<EOF
#!/usr/bin/env bash
$WRAPPER_MARKER
exec $(printf '%q' "$VENV/bin/python") -m zeiterfassung "\$@"
EOF

# --- 4. Starter + Icon -------------------------------------------------------
ICON_SRC_BASE="$REPO/src/zeiterfassung/resources/icons/hicolor"
shopt -s nullglob
icons=("$ICON_SRC_BASE"/*/apps/"$APP_ID".png)
shopt -u nullglob
if (( ${#icons[@]} )); then
    say "Installiere Icon (${#icons[@]} Größen)"
    for src in "${icons[@]}"; do
        size_dir="$(basename "$(dirname "$(dirname "$src")")")"
        run install -d -m 755 "$ICON_BASE/$size_dir/apps"
        run install -m 644 "$src" "$ICON_BASE/$size_dir/apps/$APP_ID.png"
    done
    if command -v gtk-update-icon-cache >/dev/null && [[ -f "$ICON_BASE/index.theme" ]]; then
        run gtk-update-icon-cache -q -t "$ICON_BASE" || true
    fi
    # Absoluter Pfad statt Themen-Name: Existierte ~/.local/share/icons/hicolor beim
    # Anmelden noch nicht (Erstinstallation), beobachtet das Cinnamon-Menü den Ordner
    # nicht und zeigt bis zur nächsten Anmeldung kein Icon (ohne index.theme greift
    # auch gtk-update-icon-cache nicht). Ein absoluter Pfad wirkt sofort.
    if [[ -f "$ICON_BASE/256x256/apps/$APP_ID.png" ]] || (( DRY_RUN )); then
        ICON_NAME="$ICON_BASE/256x256/apps/$APP_ID.png"
    else
        ICON_NAME="$APP_ID"
    fi
else
    say "Kein App-Icon in resources/ gefunden – verwende Systemsymbol"
    ICON_NAME="x-office-calendar"
fi

DESKTOP_TEMPLATE="$REPO/src/zeiterfassung/resources/$APP_ID.desktop.in"
# desktop_entry EXEC_ZUSATZ [ZUSATZZEILE] – Markierung (und Zusatzzeile) direkt
# unter [Desktop Entry] einfügen.
desktop_entry() {
    desktop_body "$1" | while IFS= read -r line; do
        printf '%s\n' "$line"
        if [[ "$line" == "[Desktop Entry]" ]]; then
            echo "$MARKER"
            [[ -n "${2:-}" ]] && echo "$2"
        fi
    done
    return 0
}
desktop_body() {
    if [[ -f "$DESKTOP_TEMPLATE" ]]; then
        # Vorlage der UI: Platzhalter @EXEC@ / @CLI@ auf die Installation setzen
        sed -e "s|@EXEC@|\"$BIN_DIR/zeiterfassung\"$1|g" \
            -e "s|@CLI@|\"$BIN_DIR/zeit\"|g" \
            -e "s|^Icon=.*|Icon=$ICON_NAME|" "$DESKTOP_TEMPLATE"
    else
        cat <<EOF
[Desktop Entry]
Type=Application
Name=Zeiterfassung
Comment=Arbeitszeit per Sprache, Hotkey und Klick erfassen
Exec="$BIN_DIR/zeiterfassung"$1
Icon=$ICON_NAME
Terminal=false
Categories=Office;Utility;
StartupNotify=true
StartupWMClass=$APP_ID
DBusActivatable=false
EOF
    fi
}

say "Lege Starter an: $DESKTOP_FILE"
run install -d -m 755 "$APPS_DIR"
desktop_entry "" "" | write_file "$DESKTOP_FILE" 644
if command -v update-desktop-database >/dev/null; then
    run update-desktop-database "$APPS_DIR" || true
fi

# --- 5. Autostart (optional) -------------------------------------------------
if (( AUTOSTART )); then
    say "Lege Autostart an: $AUTOSTART_FILE"
    run install -d -m 755 "$AUTOSTART_DIR"
    desktop_entry " --hidden" "X-GNOME-Autostart-enabled=true" | write_file "$AUTOSTART_FILE" 644
fi

# --- Abschluss ---------------------------------------------------------------
# Installationsstand für scripts/tracksy-update.sh in der venv-Markierung festhalten
# (Version, git-Stand des Klons, Spracherkennung ja/nein, Ort des Klons).
INSTALLED_VERSION="unbekannt"
if [[ -x "$VENV/bin/python" ]]; then
    INSTALLED_VERSION="$("$VENV/bin/python" -c \
        'import importlib.metadata as m; print(m.version("zeiterfassung"))' 2>/dev/null || echo unbekannt)"
fi
INSTALLED_COMMIT="unbekannt"
if command -v git >/dev/null; then
    INSTALLED_COMMIT="$(git -C "$REPO" rev-parse --verify -q HEAD 2>/dev/null || echo unbekannt)"
fi
write_file "$VENV_MARKER" 600 <<MARKER_EOF
zeiterfassung-installer venv
version=$INSTALLED_VERSION
commit=$INSTALLED_COMMIT
voice=$(( NO_VOICE ? 0 : 1 ))
repo=$REPO
MARKER_EOF
say "Fertig."
echo "Programm:  $VENV"
echo "Daten:     $APP_DATA (Datenbank, CSV, Modelle)"
echo "Config:    $CONFIG_HOME/zeiterfassung/config.toml"
if [[ -d "$REPO/.git" && -f "$REPO/scripts/tracksy-update.sh" ]]; then
    echo "Update:    cd $(printf '%q' "$REPO") && scripts/tracksy-update.sh"
fi
case ":${PATH}:" in
    *":$BIN_DIR:"*) ;;
    *) echo "Hinweis: $BIN_DIR ist nicht im PATH – ggf. neu anmelden oder PATH ergänzen." ;;
esac
(( DRY_RUN )) && echo "(Dry-Run: es wurde nichts verändert.)"
exit 0
