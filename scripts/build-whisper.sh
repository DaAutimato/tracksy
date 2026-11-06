#!/usr/bin/env bash
# Baut die Spracherkennung: whisper.cpp (MIT, mitgeliefert in third_party/whisper.cpp)
# + native/tracksy_whisper.c zu EINER Bibliothek libtracksy_whisper.so
# (whisper/ggml statisch eingebunden). Kein Netzwerkzugriff.
#
#   scripts/build-whisper.sh [--out DIR] [--src DIR] [--portable] [--dry-run]
#
#   --out DIR    Zielordner (Default: <repo>/native/build)
#   --src DIR    anderer whisper.cpp-Quellbaum statt third_party/whisper.cpp
#                (nur für Updates/Tests; muss zur C-Brücke passen)
#   --portable   ohne -march=native (für Weitergabe an andere Rechner)
#   --print-source-hash  nur den Fingerabdruck der Quellen ausgeben (für
#                install.sh/tracksy-update.sh: Neubau nur bei geänderten Quellen)
#
# Nur Open Source, kein Netzwerkcode: RPC/curl/BLAS/GPU-Backends, Beispiele,
# Server und Tests sind abgeschaltet, CMake-Downloads gesperrt. Am Ende wird
# geprüft, dass die Bibliothek keine Netzwerkfunktionen (socket, connect …) importiert.
# Braucht: cmake, g++ (apt: cmake g++).
set -euo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
SRC="$REPO/third_party/whisper.cpp"
OUT="$REPO/native/build"
NATIVE=ON
DRY_RUN=0
PRINT_HASH=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --src) SRC="${2:?--src braucht ein Verzeichnis}"; shift ;;
        --out) OUT="${2:?--out braucht ein Verzeichnis}"; shift ;;
        --portable) NATIVE=OFF ;;
        --dry-run) DRY_RUN=1 ;;
        --print-source-hash) PRINT_HASH=1 ;;
        -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unbekannte Option: $1" >&2; exit 2 ;;
    esac
    shift
done

# Fingerabdruck der Quellen, aus denen die Bibliothek entsteht: native/ (ohne
# build/), dieses Skript, Manifest + Herkunft von third_party/whisper.cpp
# (der Build prüft die Dateien gegen das Manifest) und die Zieloption. Nur für
# den mitgelieferten Quellbaum; mit --src gibt es keinen (→ immer neu bauen).
source_hash() {
    [[ "$(readlink -f "$SRC")" == "$(readlink -f "$REPO/third_party/whisper.cpp")" ]] || return 1
    (
        cd "$REPO"
        {
            find native -path native/build -prune -o -type f -print
            echo scripts/build-whisper.sh
            for f in third_party/whisper.cpp/TRACKSY_SHA256SUMS third_party/whisper.cpp/TRACKSY_VENDOR; do
                [[ -f "$f" ]] && echo "$f"
            done
        } | LC_ALL=C sort | xargs -r -d '\n' sha256sum
        echo "native=$NATIVE"
    ) | sha256sum | cut -d' ' -f1
}
if (( PRINT_HASH )); then
    source_hash || { echo "Kein Fingerabdruck für --src" >&2; exit 1; }
    exit 0
fi

for tool in cmake g++ nm readelf; do
    command -v "$tool" >/dev/null || { echo "Fehlt: $tool (sudo apt install cmake g++)" >&2; exit 1; }
done
[[ -f "$SRC/CMakeLists.txt" && -f "$SRC/include/whisper.h" ]] || { echo "Kein whisper.cpp-Quellbaum: $SRC" >&2; exit 1; }
VERSION="$(head -1 "$SRC/TRACKSY_VENDOR" 2>/dev/null || echo "unbekannt ($SRC)")"

# Mitgelieferten Quellbaum vor dem Bauen gegen das Manifest prüfen (nur lesend).
# Erkennt versehentliche oder unbemerkte Änderungen an third_party/whisper.cpp.
if [[ "$(readlink -f "$SRC")" == "$(readlink -f "$REPO/third_party/whisper.cpp")" ]]; then
    command -v sha256sum >/dev/null || { echo "Fehlt: sha256sum (coreutils)" >&2; exit 1; }
    [[ -f "$SRC/TRACKSY_SHA256SUMS" ]] || { echo "ABBRUCH: Manifest TRACKSY_SHA256SUMS fehlt" >&2; exit 1; }
    if ! (cd "$SRC" && sha256sum --quiet --strict -c TRACKSY_SHA256SUMS >&2); then
        echo "ABBRUCH: third_party/whisper.cpp weicht vom Manifest TRACKSY_SHA256SUMS ab" >&2
        exit 1
    fi
fi

if [[ $DRY_RUN -eq 1 ]]; then
    echo "[dry-run] whisper.cpp $VERSION bauen, Ziel: $OUT/libtracksy_whisper.so"
    exit 0
fi

WORK="$(mktemp -d -t tracksy-whisper-build.XXXXXX)"
echo "Baue whisper.cpp $VERSION (statisch, CPU, ohne Netzwerk-Backends) …"
cmake -S "$REPO/native" -B "$WORK" -DCMAKE_BUILD_TYPE=Release \
    -DWHISPER_SRC="$SRC" -DGGML_NATIVE="$NATIVE" >"$WORK/cmake-config.log"
cmake --build "$WORK" --config Release --target tracksy_whisper -j "$(nproc)" >"$WORK/cmake-build.log"

lib="$WORK/libtracksy_whisper.so"
[[ -f "$lib" ]] || { echo "Build fehlgeschlagen, siehe $WORK/cmake-build.log" >&2; exit 1; }
net="$(nm -D --undefined-only "$lib" \
    | grep -wE 'socket|connect|bind|listen|accept|send|sendto|sendmsg|recv|recvfrom|recvmsg|getaddrinfo|gethostbyname' || true)"
if [[ -n "$net" ]]; then
    echo "ABBRUCH: Bibliothek importiert Netzwerkfunktionen:" >&2
    echo "$net" >&2
    exit 1
fi

# Nur Compiler-/C-Laufzeit als Abhängigkeit; libcurl, libssl o. ä. würden Netzwerkcode nachladen
allowed_needed=" libgomp.so.1 libstdc++.so.6 libm.so.6 libgcc_s.so.1 libc.so.6 ld-linux-x86-64.so.2 ld-linux-aarch64.so.1 "
while read -r needed; do
    if [[ "$allowed_needed" != *" $needed "* ]]; then
        echo "ABBRUCH: unerwartete Abhängigkeit der Bibliothek: $needed" >&2
        exit 1
    fi
done < <(readelf -d "$lib" | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p')

mkdir -p "$OUT"
install -m 755 "$lib" "$OUT/.libtracksy_whisper.so.tmp"
mv -f "$OUT/.libtracksy_whisper.so.tmp" "$OUT/libtracksy_whisper.so"
printf '%s\n' "$VERSION" >"$OUT/WHISPER_VERSION"
# Fingerabdruck merken: install.sh/tracksy-update.sh bauen nur neu, wenn er sich ändert
hash="$(source_hash)" || hash="unbekannt (--src)"
printf '%s\n' "$hash" >"$OUT/SOURCE_HASH"
echo "Fertig: $OUT/libtracksy_whisper.so ($(du -h "$OUT/libtracksy_whisper.so" | cut -f1))"
echo "Temporäres Build-Verzeichnis kann entfernt werden: $WORK"
