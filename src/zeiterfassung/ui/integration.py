"""Desktop-Integration: Hotkeys (gsettings, Cinnamon und GNOME/Ubuntu) und Autostart.

Grundsatz: Nichts wird automatisch verändert. Die UI zeigt die geplanten
Befehle bzw. den Dateiinhalt an und führt sie erst nach ausdrücklichem Klick
aus. Ist ``ZEIT_NO_SYSTEM_CHANGES`` gesetzt (Tests), wird jede Änderung
verweigert (:class:`SystemChangesDisabled`).

Die Bau-Funktionen sind rein (ohne Seiteneffekte) und per pytest testbar.

Cinnamon-Besonderheiten (geprüft an ``/usr/share/cinnamon/js/ui/keybindings.js``
und ``cinnamon-settings/bin/KeybindingTable.py``, Mint 22):

* Einträge der ``custom-list`` müssen ``custom<N>`` heißen. cinnamon-settings
  rechnet beim Anlegen eigener Kürzel ``int(name.replace("custom", ""))`` –
  ein Name wie ``zeiterfassung-toggle`` lässt das Anlegen dort abstürzen; und
  Cinnamon entfernt beim Neuladen nur Kürzel, deren Name „custom“ enthält
  (Entfernen/Ändern wirkte sonst erst nach Neuanmeldung). Eigene Einträge
  erkennt Tracksy deshalb am Feld ``name`` (:data:`HOTKEYS` ``label``).
* Cinnamon lädt die Kürzel nur neu, wenn sich ``custom-list`` ändert. Wie
  cinnamon-settings wird dazu der Platzhalter ``__dummy__`` abwechselnd
  ergänzt/entfernt – sonst greift „Erneut einrichten“ (geänderter Befehl) nicht.
* Ältere Tracksy-Versionen (Namen ``zeiterfassung-*``) werden beim Einrichten
  und Entfernen mit aufgeräumt.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

ENV_NO_SYSTEM_CHANGES = "ZEIT_NO_SYSTEM_CHANGES"

KEYBINDINGS_SCHEMA = "org.cinnamon.desktop.keybindings"
CUSTOM_SCHEMA = "org.cinnamon.desktop.keybindings.custom-keybinding"
CUSTOM_BASE = "/org/cinnamon/desktop/keybindings/custom-keybindings"


@dataclass(frozen=True)
class KeybindingDesktop:
    """Unterschiede der gsettings-Ablage für eigene Tastenkürzel je Arbeitsumgebung."""
    key: str             # "cinnamon" | "gnome"
    label: str           # für Texte in der Oberfläche
    list_schema: str     # Schema mit der Liste der eigenen Kürzel
    list_key: str        # Schlüssel der Liste
    custom_schema: str   # relocatable Schema eines Kürzels
    base: str            # dconf-Pfad der Kürzel
    list_paths: bool     # Liste enthält volle Pfade (GNOME) statt Namen (Cinnamon)
    binding_strv: bool   # binding ist Liste (Cinnamon) statt Text (GNOME)


CINNAMON = KeybindingDesktop(
    "cinnamon", "Cinnamon", KEYBINDINGS_SCHEMA, "custom-list", CUSTOM_SCHEMA, CUSTOM_BASE,
    list_paths=False, binding_strv=True)
GNOME = KeybindingDesktop(
    "gnome", "GNOME", "org.gnome.settings-daemon.plugins.media-keys", "custom-keybindings",
    "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding",
    "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings",
    list_paths=True, binding_strv=False)

AUTOSTART_FILENAME = "de.local.Zeiterfassung.desktop"
# Dieselbe Markierung wie scripts/install.sh/uninstall.sh: nur Dateien mit dieser
# Zeile gelten als „eigene“ und werden vom Installer überschrieben bzw. entfernt.
INSTALLER_MARKER = "X-Zeiterfassung-Installer=1"


class SystemChangesDisabled(RuntimeError):
    pass


class ForeignAutostartFile(RuntimeError):
    """Autostart-Datei existiert, stammt aber nicht von der Zeiterfassung (keine Markierung)."""


FOREIGN_AUTOSTART_MSG = (
    "Die vorhandene Autostart-Datei stammt nicht von Tracksy und wird nicht "
    "verändert. Bitte selbst prüfen oder umbenennen."
)


@dataclass(frozen=True)
class Hotkey:
    name: str  # Eintrag in custom-list
    label: str
    action: str  # CLI-Befehl
    binding: str  # GTK-Accelerator-Syntax
    display: str


HOTKEYS: tuple[Hotkey, ...] = (
    Hotkey("zeiterfassung-toggle", "Tracksy: Start/Stopp", "toggle",
           "<Super><Shift>t", "Super+Umschalt+T"),
    Hotkey("zeiterfassung-voice", "Tracksy: Spracheingabe", "voice",
           "<Super><Shift>v", "Super+Umschalt+V"),
)


def src_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def cli_command(action: str, python: str | None = None, which=shutil.which) -> str:
    """Befehl, den ein Hotkey ausführt.

    Installiert (``zeit`` neben dem Interpreter oder im PATH) → ``zeit <action>``,
    sonst Entwicklungsaufruf ``env PYTHONPATH=<src> <python> -m zeiterfassung.cli``.
    """
    python = python or sys.executable
    # absolut: die Arbeitsumgebung startet den Befehl mit eigenem Arbeitsverzeichnis
    sibling = Path(os.path.abspath(python)).with_name("zeit")
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return f"{shlex.quote(str(sibling))} {action}"
    found = which("zeit")
    if found:
        return f"{shlex.quote(found)} {action}"
    return (
        f"env PYTHONPATH={shlex.quote(str(src_dir()))} {shlex.quote(python)} "
        f"-m zeiterfassung.cli {action}"
    )


def app_command(python: str | None = None, hidden: bool = True) -> str:
    python = python or sys.executable
    cmd = (
        f"env PYTHONPATH={shlex.quote(str(src_dir()))} {shlex.quote(python)} -m zeiterfassung"
    )
    return cmd + (" --hidden" if hidden else "")


def parse_strv(text: str) -> list[str]:
    """Ausgabe von ``gsettings get`` (``['a', 'b']`` bzw. ``@as []``) → Liste."""
    t = (text or "").strip()
    if t.startswith("@as"):
        t = t[3:].strip()
    if not t:
        return []
    value = ast.literal_eval(t)
    if not isinstance(value, (list, tuple)):
        raise ValueError("Unerwartetes gsettings-Format")
    return [str(v) for v in value]


def _gvariant_str(text: str) -> str:
    """String als GVariant-Text in ``'…'``. gsettings parst Werte als GVariant: ein roher
    Wert, der mit ``'`` beginnt (z. B. per shlex quotierter Pfad), würde sonst abgelehnt
    oder still verändert; ``\\`` in fremden Einträgen würde als Escape gelesen."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def format_strv(items: list[str]) -> str:
    """Liste als GVariant-Text; Backslash und ``'`` werden escaped (fremde Einträge bleiben exakt)."""
    if not items:
        return "@as []"
    return "[" + ", ".join(_gvariant_str(i) for i in items) + "]"


DUMMY_ENTRY = "__dummy__"  # Cinnamon: erzwingt das Neuladen der eigenen Kürzel
_CUSTOM_RE = re.compile(r"custom(\d+)")


def _custom_path(name: str, desktop: KeybindingDesktop = CINNAMON) -> str:
    return f"{desktop.base}/{name}/"


def _list_entry(name: str, desktop: KeybindingDesktop) -> str:
    return _custom_path(name, desktop) if desktop.list_paths else name


def _entry_path(entry: str, desktop: KeybindingDesktop) -> str:
    """dconf-Pfad eines Listeneintrags (GNOME: Eintrag ist der Pfad, Cinnamon: Name)."""
    return entry if desktop.list_paths else _custom_path(entry, desktop)


def _legacy_entries(current_list: list[str], hotkeys=HOTKEYS) -> list[str]:
    """Cinnamon-Einträge älterer Tracksy-Versionen (``zeiterfassung-*``)."""
    names = {hk.name for hk in hotkeys}
    return [e for e in current_list if e in names]


def _free_custom_names(current_list: list[str], count: int) -> list[str]:
    """Nächste freie ``custom<N>`` – gleiche Vergabe wie cinnamon-settings (kleinste freie Zahl)."""
    used = {int(m.group(1)) for e in current_list if (m := _CUSTOM_RE.fullmatch(e))}
    out, i = [], 0
    while len(out) < count:
        if i not in used:
            out.append(f"custom{i}")
        i += 1
    return out


def _toggle_dummy(items: list[str]) -> list[str]:
    return [i for i in items if i != DUMMY_ENTRY] if DUMMY_ENTRY in items else [*items, DUMMY_ENTRY]


def identify_ours(names: Mapping[str, str], hotkeys=HOTKEYS) -> dict[str, str]:
    """Eigene Einträge am Feld ``name`` erkennen: ``{listeneintrag: name}`` → ``{hk.name: eintrag}``.

    Pro Kürzel zählt der erste Treffer; weitere Duplikate liefert :func:`duplicate_entries`.
    """
    by_label = {hk.label: hk.name for hk in hotkeys}
    ours: dict[str, str] = {}
    for entry, label in names.items():
        key = by_label.get(label)
        if key is not None and key not in ours:
            ours[key] = entry
    return ours


def duplicate_entries(names: Mapping[str, str], ours: Mapping[str, str], hotkeys=HOTKEYS) -> list[str]:
    labels = {hk.label for hk in hotkeys}
    keep = set(ours.values())
    return [e for e, label in names.items() if label in labels and e not in keep]


def hotkey_setup_commands(current_list: list[str], hotkeys=HOTKEYS,
                          command_for=cli_command,
                          desktop: KeybindingDesktop = CINNAMON,
                          ours: Mapping[str, str] | None = None,
                          duplicates: list[str] | None = None) -> list[list[str]]:
    """gsettings-Befehle zum Einrichten (idempotent bzgl. der Kürzel-Liste).

    Cinnamon: vorhandene eigene Einträge (``ours``, s. :func:`identify_ours`) werden
    wiederverwendet, sonst freie ``custom<N>`` vergeben; Altnamen ``zeiterfassung-*``
    und Duplikate werden entfernt; ``__dummy__`` erzwingt das Neuladen.
    """
    cmds: list[list[str]] = []
    ours = dict(ours or {})
    drop: list[str] = []
    if desktop.list_paths:
        targets = {hk.name: _list_entry(hk.name, desktop) for hk in hotkeys}
    else:
        drop = _legacy_entries(current_list, hotkeys) + list(duplicates or [])
        rest = [e for e in current_list if e not in drop]
        missing = [hk.name for hk in hotkeys if ours.get(hk.name) not in rest]
        fresh = iter(_free_custom_names(rest, len(missing)))
        targets = {hk.name: (ours[hk.name] if hk.name not in missing else next(fresh))
                   for hk in hotkeys}
    for hk in hotkeys:
        rel = f"{desktop.custom_schema}:{_entry_path(targets[hk.name], desktop)}"
        # name/command immer als GVariant-String übergeben (nicht roh), s. _gvariant_str
        cmds.append(["gsettings", "set", rel, "name", _gvariant_str(hk.label)])
        cmds.append(["gsettings", "set", rel, "command", _gvariant_str(command_for(hk.action))])
        binding = format_strv([hk.binding]) if desktop.binding_strv else _gvariant_str(hk.binding)
        cmds.append(["gsettings", "set", rel, "binding", binding])
    new_list = [e for e in current_list if e not in drop]
    for hk in hotkeys:
        if targets[hk.name] not in new_list:
            new_list.append(targets[hk.name])
    if not desktop.list_paths:
        new_list = _toggle_dummy(new_list)
    cmds.append(["gsettings", "set", desktop.list_schema, desktop.list_key, format_strv(new_list)])
    cmds += _reset_commands(drop, desktop)
    return cmds


def _reset_commands(entries: list[str], desktop: KeybindingDesktop) -> list[list[str]]:
    cmds = []
    for entry in entries:
        rel = f"{desktop.custom_schema}:{_entry_path(entry, desktop)}"
        for key in ("name", "command", "binding"):
            cmds.append(["gsettings", "reset", rel, key])
    return cmds


def _our_entries(current_list: list[str], hotkeys, desktop: KeybindingDesktop,
                 ours: Mapping[str, str] | None, duplicates: list[str] | None) -> list[str]:
    if desktop.list_paths:
        wanted = {_list_entry(hk.name, desktop) for hk in hotkeys}
        return [e for e in current_list if e in wanted]
    wanted = set((ours or {}).values()) | set(duplicates or [])
    return [e for e in current_list if e in wanted] + _legacy_entries(current_list, hotkeys)


def hotkey_remove_commands(current_list: list[str], hotkeys=HOTKEYS,
                           desktop: KeybindingDesktop = CINNAMON,
                           ours: Mapping[str, str] | None = None,
                           duplicates: list[str] | None = None) -> list[list[str]]:
    """Eigene Einträge aus der Liste nehmen und ihre Werte zurücksetzen."""
    if desktop.list_paths:
        entries = [_list_entry(hk.name, desktop) for hk in hotkeys]
    else:
        entries = _our_entries(current_list, hotkeys, desktop, ours, duplicates)
    new_list = [n for n in current_list if n not in entries]
    if not desktop.list_paths:
        new_list = _toggle_dummy(new_list)
    cmds = [["gsettings", "set", desktop.list_schema, desktop.list_key, format_strv(new_list)]]
    return cmds + _reset_commands(entries, desktop)


def hotkeys_installed(current_list: list[str], hotkeys=HOTKEYS,
                      desktop: KeybindingDesktop = CINNAMON,
                      ours: Mapping[str, str] | None = None) -> bool:
    if desktop.list_paths:
        return all(_list_entry(hk.name, desktop) in current_list for hk in hotkeys)
    ours = ours or {}
    return all(ours.get(hk.name) in current_list for hk in hotkeys)


# ---------------------------------------------------------------------------
# Konflikte mit vorhandenen Kürzeln
# ---------------------------------------------------------------------------
_MOD_ALIASES = {"primary": "control", "ctrl": "control", "control": "control",
                "mod1": "alt", "alt": "alt", "super": "super", "mod4": "super",
                "shift": "shift", "meta": "meta", "hyper": "hyper"}

# Schemas mit vordefinierten Kürzeln (nur lesend geprüft)
CONFLICT_SCHEMAS = {
    "cinnamon": ("org.cinnamon.desktop.keybindings.wm",
                 "org.cinnamon.desktop.keybindings.media-keys",
                 "org.cinnamon.desktop.keybindings"),
    "gnome": ("org.gnome.desktop.wm.keybindings", "org.gnome.shell.keybindings",
              "org.gnome.mutter.keybindings", "org.gnome.mutter.wayland.keybindings",
              "org.gnome.settings-daemon.plugins.media-keys"),
}


def normalize_accel(accel: str) -> tuple[frozenset[str], str] | None:
    """``<Primary><Alt>t`` ≡ ``<Control><Alt>T`` – Modifikatoren als Menge, Taste klein."""
    mods = frozenset(_MOD_ALIASES.get(m.lower(), m.lower()) for m in re.findall(r"<([^>]+)>", accel))
    key = re.sub(r"<[^>]+>", "", accel).strip().lower()
    return (mods, key) if key else None


def find_conflicts(bindings: Mapping[str, list[str]], hotkeys=HOTKEYS) -> list[str]:
    """``{"Beschreibung": [accel, …]}`` → Texte „Super+Umschalt+T ist schon belegt: …“."""
    out = []
    for hk in hotkeys:
        want = normalize_accel(hk.binding)
        for owner, accels in bindings.items():
            if any(normalize_accel(a) == want for a in accels):
                out.append(f"{hk.display} ist schon belegt: {owner}")
    return out


def _parse_value(text: str) -> list[str]:
    t = text.strip()
    if t.startswith("[") or t.startswith("@as"):
        try:
            return parse_strv(t)
        except (ValueError, SyntaxError):
            return []
    try:
        v = ast.literal_eval(t)
    except (ValueError, SyntaxError):
        return []
    return [v] if isinstance(v, str) else []


def parse_list_recursively(output: str) -> dict[str, list[str]]:
    """Ausgabe von ``gsettings list-recursively SCHEMA`` → ``{"schema key": [accels]}``."""
    out: dict[str, list[str]] = {}
    for line in output.splitlines():
        parts = line.split(" ", 2)
        if len(parts) != 3:
            continue
        values = [v for v in _parse_value(parts[2]) if "<" in v or len(v) == 1]
        if values:
            out[f"{parts[0]} {parts[1]}"] = values
    return out


def _quote(arg: str) -> str:
    """Lesbares, korrektes Shell-Quoting (doppelte Anführungszeichen bei ``'``)."""
    if "'" in arg and not any(ch in arg for ch in '"$`\\!'):
        return f'"{arg}"'
    return shlex.quote(arg)


def commands_as_text(cmds: list[list[str]], sep: str = "\n\n") -> str:
    return sep.join(" ".join(_quote(a) for a in c) for c in cmds)


def is_cinnamon(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    desk = (env.get("XDG_CURRENT_DESKTOP") or "") + ":" + (env.get("DESKTOP_SESSION") or "")
    return "cinnamon" in desk.lower()


def _desktops(env: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if env is None else env
    return [d for d in (env.get("XDG_CURRENT_DESKTOP") or "").lower().split(":") if d]


def is_kde(env: Mapping[str, str] | None = None) -> bool:
    return "kde" in _desktops(env)


def is_gnome(env: Mapping[str, str] | None = None) -> bool:
    return "gnome" in _desktops(env) and not is_cinnamon(env)


# Globale Kürzel auf Arbeitsumgebungen ohne automatische Einrichtung (z. B. KDE)
HOTKEYS_MANUAL_STATUS = "Manuell einrichten – Befehle oben kopieren"
_HOTKEYS_AUTO_ONLY = "Automatische Einrichtung nur unter Cinnamon und GNOME (Ubuntu)."
_HOTKEYS_KDE_HOWTO = (
    " KDE Plasma: Systemeinstellungen → Tastatur → Tastenkurzbefehle → „Neu hinzufügen“ → "
    "„Befehl oder Skript“ und die Befehle unten eintragen (Plasma 5: Kurzbefehle → Eigene "
    "Kurzbefehle). Alternativ „Neu hinzufügen“ → „Anwendung“ → Tracksy und die Aktionen "
    "„Start/Stopp“ und „Spracheingabe“ belegen.")
_HOTKEYS_GENERIC_HOWTO = (" Lege die Kürzel in den Tastatur-Einstellungen deiner "
                          "Arbeitsumgebung mit diesen Befehlen an.")


def manual_hotkey_description(env: Mapping[str, str] | None = None) -> str:
    """Anleitung für Desktops ohne automatische Kürzel-Einrichtung."""
    return _HOTKEYS_AUTO_ONLY + (_HOTKEYS_KDE_HOWTO if is_kde(env) else _HOTKEYS_GENERIC_HOWTO)


# Tray-Symbol: Hinweise, wenn das Panel keine Tray-Symbole anzeigt
OPEN_WITHOUT_TRAY = "Fenster öffnen über „Tracksy“ im Anwendungsmenü oder „zeit show“."
OPEN_WITH_TRAY = "Fenster öffnen über das Tray-Symbol oder „zeit show“."


def hide_hint(tray_ok: bool) -> str:
    """Text der Benachrichtigung beim ersten Schließen des Fensters."""
    return OPEN_WITH_TRAY if tray_ok else OPEN_WITHOUT_TRAY


def no_tray_hint(disabled: bool = False, env: Mapping[str, str] | None = None) -> str:
    """Erklärung in den Einstellungen, warum kein Tray-Symbol zu sehen ist."""
    if disabled:
        return "Das Tray-Symbol ist abgeschaltet (--no-tray bzw. ZEIT_NO_TRAY). " + OPEN_WITHOUT_TRAY
    if is_gnome(env):
        return ("GNOME zeigt Tray-Symbole nur mit der Erweiterung „AppIndicator and "
                "KStatusNotifierItem Support“ (unter Ubuntu vorinstalliert). " + OPEN_WITHOUT_TRAY)
    return "Das Panel zeigt keine Tray-Symbole an. " + OPEN_WITHOUT_TRAY


def keybinding_desktop(env: Mapping[str, str] | None = None) -> KeybindingDesktop | None:
    """Arbeitsumgebung mit automatischer Kürzel-Einrichtung (Cinnamon, GNOME/Ubuntu) oder None."""
    env = os.environ if env is None else env
    if is_cinnamon(env):
        return CINNAMON
    desk = (env.get("XDG_CURRENT_DESKTOP") or "").lower().split(":")
    if "gnome" in desk:  # Ubuntu: "ubuntu:GNOME", Fedora/Debian: "GNOME"
        return GNOME
    return None


def _guard() -> None:
    if os.environ.get(ENV_NO_SYSTEM_CHANGES):
        raise SystemChangesDisabled("Systemänderungen sind in dieser Umgebung deaktiviert")


def read_custom_list(run=subprocess.run, desktop: KeybindingDesktop = CINNAMON) -> list[str]:
    """Liest die aktuelle Liste der eigenen Kürzel (nur lesend)."""
    if os.environ.get(ENV_NO_SYSTEM_CHANGES):
        return []
    res = run(["gsettings", "get", desktop.list_schema, desktop.list_key],
              capture_output=True, text=True, timeout=5, check=False)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "gsettings nicht verfügbar")
    return parse_strv(res.stdout)


def system_changes_disabled() -> bool:
    return bool(os.environ.get(ENV_NO_SYSTEM_CHANGES))


def _gget(run, schema: str, key: str) -> str:
    res = run(["gsettings", "get", schema, key], capture_output=True, text=True, timeout=5,
              check=False)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "gsettings nicht verfügbar")
    return res.stdout


@dataclass
class HotkeyState:
    entries: list[str]               # aktuelle Liste der eigenen Kürzel der Arbeitsumgebung
    ours: dict[str, str]             # hk.name → Listeneintrag (nur Cinnamon; GNOME: leer)
    duplicates: list[str]            # weitere Tracksy-Einträge (aufräumen)
    names: dict[str, str]            # Listeneintrag → Feld name
    bindings: dict[str, list[str]]   # Listeneintrag → binding


def read_hotkey_state(run=subprocess.run, desktop: KeybindingDesktop = CINNAMON) -> HotkeyState:
    """Liste + Name/Belegung jedes Eintrags lesen (nur lesend)."""
    entries = read_custom_list(run=run, desktop=desktop)
    names: dict[str, str] = {}
    bindings: dict[str, list[str]] = {}
    for e in entries:
        if e == DUMMY_ENTRY:
            continue
        rel = f"{desktop.custom_schema}:{_entry_path(e, desktop)}"
        try:
            vals = _parse_value(_gget(run, rel, "name"))
            names[e] = vals[0] if vals else ""
            bindings[e] = _parse_value(_gget(run, rel, "binding"))
        except (RuntimeError, OSError, subprocess.SubprocessError):
            names[e], bindings[e] = "", []
    ours = {} if desktop.list_paths else identify_ours(names)
    dups = [] if desktop.list_paths else duplicate_entries(names, ours)
    return HotkeyState(entries, ours, dups, names, bindings)


def read_conflicts(state: HotkeyState, run=subprocess.run,
                   desktop: KeybindingDesktop = CINNAMON, hotkeys=HOTKEYS) -> list[str]:
    """Kürzel von Tracksy, die schon anderweitig belegt sind (nur lesend; Fehler → leer)."""
    if system_changes_disabled():
        return []
    mine = set(state.ours.values()) | set(state.duplicates)
    if desktop.list_paths:
        mine |= {_list_entry(hk.name, desktop) for hk in hotkeys}
    found: dict[str, list[str]] = {}
    for entry, accels in state.bindings.items():
        if entry not in mine:
            found[f"eigenes Kürzel „{state.names.get(entry) or entry}“"] = accels
    for schema in CONFLICT_SCHEMAS.get(desktop.key, ()):
        try:
            res = run(["gsettings", "list-recursively", schema], capture_output=True, text=True,
                      timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if res.returncode == 0:
            found.update(parse_list_recursively(res.stdout))
    return find_conflicts(found, hotkeys)


def run_commands(cmds: list[list[str]], run=subprocess.run) -> None:
    """Führt die angezeigten Befehle aus – nur nach Klick des Nutzers aufrufen."""
    _guard()
    for c in cmds:
        res = run(c, capture_output=True, text=True, timeout=10, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"{shlex.join(c)}: {res.stderr.strip()}")


# ---------------------------------------------------------------------------
# Autostart
# ---------------------------------------------------------------------------
def autostart_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME") or Path.home()) / ".config")
    return Path(base) / "autostart" / AUTOSTART_FILENAME


# Zeichen, die laut Desktop-Entry-Spezifikation ein Argument in Exec quotieren
_EXEC_RESERVED = set(' \t\n"\'\\><~|&;$*?#()`')


def desktop_exec_arg(arg: str) -> str:
    """Ein Argument für ``Exec=`` nach Desktop-Entry-Spezifikation quotieren.

    Doppelte Anführungszeichen (einfache kennt die Spezifikation nicht), darin
    ``"`` `` ` `` ``$`` ``\\`` escapen, ``%`` verdoppeln; anschließend gilt die
    allgemeine String-Escape-Regel der Key-Datei (``\\`` → ``\\\\``).
    """
    if "\n" in arg or "\r" in arg or "\0" in arg:
        raise ValueError("Zeilenumbruch im Pfad wird für Exec= nicht unterstützt")
    if arg and not (_EXEC_RESERVED & set(arg)):
        return arg.replace("%", "%%")
    inner = "".join("\\" + c if c in '"`$\\' else c for c in arg)
    quoted = '"' + inner.replace("%", "%%") + '"'
    return quoted.replace("\\", "\\\\")


def autostart_exec(python: str | None = None, hidden: bool = True) -> str:
    """``Exec=``-Wert für den Autostart (gleicher Aufruf wie :func:`app_command`)."""
    argv = ["env", f"PYTHONPATH={src_dir()}", python or sys.executable, "-m", "zeiterfassung"]
    if hidden:
        argv.append("--hidden")
    return " ".join(desktop_exec_arg(a) for a in argv)


def autostart_content(exec_cmd: str | None = None, enabled: bool = True) -> str:
    exec_cmd = exec_cmd or autostart_exec(hidden=True)
    lines = [
        "[Desktop Entry]",
        INSTALLER_MARKER,  # uninstall.sh erkennt die Datei als eigene
        "Type=Application",
        "Name=Tracksy",
        "Comment=Tracksy im Hintergrund starten",
        f"Exec={exec_cmd}",
        "Icon=de.local.Zeiterfassung",
        "Terminal=false",
        "X-GNOME-Autostart-enabled=" + ("true" if enabled else "false"),
        "Hidden=" + ("false" if enabled else "true"),
    ]
    return "\n".join(lines) + "\n"


def autostart_enabled(path: Path | None = None) -> bool:
    path = path or autostart_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return "Hidden=true" not in text and "X-GNOME-Autostart-enabled=false" not in text


def autostart_is_foreign(path: Path | None = None) -> bool:
    """``True``, wenn die Datei existiert, aber nicht die Installer-Markierung trägt.

    Auch Symlinks und nicht lesbare Dateien gelten als fremd (nie überschreiben).
    """
    path = path or autostart_path()
    if path.is_symlink():
        return True
    if not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return True
    return INSTALLER_MARKER not in (ln.strip() for ln in lines)


def set_autostart(enabled: bool, path: Path | None = None) -> Path:
    """Schreibt die Autostart-Datei. Deaktivieren setzt ``Hidden=true`` (kein Löschen).

    Eine vorhandene fremde Datei (ohne :data:`INSTALLER_MARKER`) wird nie
    überschrieben → :class:`ForeignAutostartFile`.
    """
    _guard()
    path = path or autostart_path()
    if autostart_is_foreign(path):
        raise ForeignAutostartFile(FOREIGN_AUTOSTART_MSG)
    if not enabled and not path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(autostart_content(enabled=enabled), encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Eingerichtete Kürzel anzeigen (nur lesend)
# ---------------------------------------------------------------------------
_MOD_LABELS = {"control": "Strg", "super": "Super", "alt": "Alt", "shift": "Umschalt",
               "meta": "Meta", "hyper": "Hyper"}
_MOD_ORDER = ("control", "super", "alt", "shift", "meta", "hyper")
_KEY_LABELS = {"space": "Leertaste", "return": "Eingabe", "kp_enter": "Eingabe",
               "escape": "Esc", "tab": "Tab", "backspace": "Rücktaste", "delete": "Entf",
               "insert": "Einfg", "home": "Pos1", "end": "Ende", "page_up": "Bild↑",
               "page_down": "Bild↓", "up": "↑", "down": "↓", "left": "←", "right": "→",
               "print": "Druck", "pause": "Pause", "comma": ",", "period": ".",
               "minus": "-", "plus": "+", "question": "?", "slash": "/"}


def parse_accel(accel: str) -> tuple[tuple[str, ...], str] | None:
    """``<Super><Shift>v`` → ``(("super", "shift"), "v")`` – Groß-/Kleinschreibung der
    Taste bleibt erhalten (X11-Tastennamen wie ``F12`` sind case-sensitiv)."""
    mods = [_MOD_ALIASES.get(m.lower(), m.lower()) for m in re.findall(r"<([^>]+)>", accel or "")]
    key = re.sub(r"<[^>]+>", "", accel or "").strip()
    if not key:
        return None
    ordered = tuple(m for m in _MOD_ORDER if m in mods) + tuple(
        m for m in dict.fromkeys(mods) if m not in _MOD_ORDER)
    return ordered, key


def accel_parts(accel: str | None) -> list[str]:
    """GTK-Accelerator → Tastennamen in Anzeigereihenfolge (``["Super", "Umschalt", "V"]``).

    Eigene Beschriftung statt ``Gtk.ShortcutLabel``: dessen Texte hängen von den
    GTK-Übersetzungen ab (ohne Sprachpaket „Ctrl“/„Shift“, andere Reihenfolge)."""
    parsed = parse_accel(accel or "")
    if parsed is None:
        return []
    mods, key = parsed
    if len(key) == 1:
        label = key.upper()
    else:
        label = _KEY_LABELS.get(key.lower()) or key.replace("_", " ")
        if label[:1].islower():
            label = label[:1].upper() + label[1:]
    return [_MOD_LABELS.get(m, m.capitalize()) for m in mods] + [label]


def accel_display(accel: str | None) -> str | None:
    """GTK-Accelerator → deutscher Anzeigetext (``<Super><Shift>v`` → ``Super+Umschalt+V``)."""
    parts = accel_parts(accel)
    return "+".join(parts) if parts else None


@dataclass(frozen=True)
class ActiveHotkeys:
    """Tatsächlich eingerichtete systemweite Kürzel von Tracksy.

    ``known``: Arbeitsumgebung mit automatischer Einrichtung (Cinnamon, GNOME) und
    Stand lesbar. ``False`` → unbekannt (z. B. KDE, gsettings-Fehler): die UI zeigt
    dann einen neutralen Hinweis statt „nicht eingerichtet“.
    ``toggle``/``voice``: GTK-Accelerator oder ``None`` (nicht eingerichtet).
    """
    known: bool = False
    toggle: str | None = None
    voice: str | None = None

    @property
    def any(self) -> bool:
        return bool(self.toggle or self.voice)

    def display(self, action: str) -> str | None:
        return accel_display(getattr(self, action, None))

    def as_status(self) -> dict[str, object]:
        return {"known": self.known, "toggle": self.toggle, "voice": self.voice}

    @classmethod
    def from_status(cls, data: object) -> "ActiveHotkeys | None":
        if not isinstance(data, dict):
            return None

        def accel(v: object) -> str | None:
            return v if isinstance(v, str) and parse_accel(v) is not None else None

        return cls(bool(data.get("known")), accel(data.get("toggle")), accel(data.get("voice")))


def active_hotkeys(state: HotkeyState, desktop: KeybindingDesktop = CINNAMON,
                   hotkeys=HOTKEYS) -> ActiveHotkeys:
    """Aus dem gelesenen Stand die wirksame Belegung je Aktion bestimmen (rein).

    Zählt nur, wenn der Eintrag in der Liste der Arbeitsumgebung steht und eine
    Belegung hat – so erscheint auch eine vom Nutzer geänderte Taste richtig."""
    found: dict[str, str | None] = {}
    for hk in hotkeys:
        entry = _list_entry(hk.name, desktop) if desktop.list_paths else state.ours.get(hk.name)
        accels = state.bindings.get(entry or "", []) if entry in state.entries else []
        found[hk.action] = next((a for a in accels if parse_accel(a) is not None), None)
    return ActiveHotkeys(True, found.get("toggle"), found.get("voice"))


def read_active_hotkeys(run=subprocess.run,
                        env: Mapping[str, str] | None = None) -> ActiveHotkeys:
    """Eingerichtete Kürzel lesen (nur lesend, mehrere ``gsettings get``; nicht im
    GTK-Main-Loop aufrufen). Fehler/unbekannte Arbeitsumgebung → ``known=False``.
    Mit ``ZEIT_NO_SYSTEM_CHANGES`` liest :func:`read_custom_list` nichts (Tests)."""
    desktop = keybinding_desktop(env)
    if desktop is None:
        return ActiveHotkeys(False)
    try:
        state = read_hotkey_state(run=run, desktop=desktop)
    except (RuntimeError, OSError, ValueError, SyntaxError, subprocess.SubprocessError):
        return ActiveHotkeys(False)
    return active_hotkeys(state, desktop)
