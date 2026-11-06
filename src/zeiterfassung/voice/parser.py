"""Regelbasierter Parser für deutsche Sprachbefehle.

Rein Python, keine Audio-/GUI-Abhängigkeit.

Grundsätze:
- Tolerant gegenüber Füllwörtern, Groß-/Kleinschreibung, Satzzeichen und
  typischen Whisper-Fehlerkennungen ("Ticker" -> Ticket).
- Kunde/Thema behalten die ursprüngliche Schreibweise; Endsatzzeichen werden
  entfernt.
- Nie raten: Was nicht eindeutig erkannt wird, ergibt ``Intent.UNKNOWN`` bzw.
  ein leeres Feld (``None``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

__all__ = ["Intent", "Command", "parse_command", "words_to_number", "parse_number_tokens"]


class Intent(str, Enum):
    START = "start"
    STOP = "stop"
    PAUSE = "pause"
    RESUME = "resume"
    STATUS = "status"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Command:
    intent: Intent
    customer: str | None = None
    topic: str | None = None
    ticket: str | None = None
    location: str | None = None  # "remote" | "onsite" | None
    raw: str = ""


# ---------------------------------------------------------------------------
# Zahlwörter
# ---------------------------------------------------------------------------

_UNITS = {
    "null": 0, "ein": 1, "eins": 1, "eine": 1, "einen": 1, "zwei": 2, "zwo": 2,
    "drei": 3, "vier": 4, "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9,
}
_TEENS = {
    "zehn": 10, "elf": 11, "zwölf": 12, "dreizehn": 13, "vierzehn": 14,
    "fünfzehn": 15, "sechzehn": 16, "siebzehn": 17, "achtzehn": 18, "neunzehn": 19,
}
_TENS = {
    "zwanzig": 20, "dreißig": 30, "vierzig": 40, "fünfzig": 50,
    "sechzig": 60, "siebzig": 70, "achtzig": 80, "neunzig": 90,
}


def _lt100(s: str) -> int | None:
    if s in _UNITS:
        return _UNITS[s]
    if s in _TEENS:
        return _TEENS[s]
    if s in _TENS:
        return _TENS[s]
    if "und" in s:
        unit, _, tens = s.partition("und")
        if unit in _UNITS and tens in _TENS and _UNITS[unit] > 0:
            return _UNITS[unit] + _TENS[tens]
    return None


def _lt1000(s: str) -> int | None:
    if not s:
        return 0
    if "hundert" in s:
        head, _, tail = s.partition("hundert")
        h = 1 if head in ("", "ein") else _UNITS.get(head)
        if h is None or h == 0:
            return None
        rest = 0 if not tail else _lt100(tail)
        return None if rest is None else h * 100 + rest
    return _lt100(s)


def words_to_number(word: str) -> int | None:
    """Wandelt ein deutsches Zahlwort (0..999999) in eine Zahl um, sonst None.

    Beispiel: "eintausendzweihundertvierunddreißig" -> 1234.
    """
    s = word.lower().replace("ss", "ß").replace("-", "").strip()
    if not s:
        return None
    if "tausend" in s:
        head, _, tail = s.partition("tausend")
        t = 1 if head in ("", "ein") else _lt1000(head)
        if t is None or t == 0:
            return None
        rest = _lt1000(tail)
        return None if rest is None else t * 1000 + rest
    if s == "hundert":
        return 100
    return _lt1000(s)


_DIGITS_RE = re.compile(r"^\d+(?:[.\s]\d{3})*$")


def _token_to_digits(token: str) -> str | None:
    """Ziffernfolge eines Tokens ("4711", "1.234", "zwölf") oder None."""
    t = token.strip().lstrip("#")
    if _DIGITS_RE.match(t):
        return t.replace(".", "").replace(" ", "")
    if t.isdigit():
        return t
    n = words_to_number(t)
    return None if n is None else str(n)


def parse_number_tokens(tokens: list[str]) -> tuple[str | None, int]:
    """Liest aufeinanderfolgende Zahl-Token und verkettet sie als Ziffern.

    "zwölf drei vier" -> "1234", "12 34" -> "1234". Gibt (ziffern, anzahl_token)
    zurück, bzw. (None, 0) wenn das erste Token keine Zahl ist.
    """
    parts: list[str] = []
    for tok in tokens:
        d = _token_to_digits(tok)
        if d is None:
            break
        parts.append(d)
    if not parts:
        return None, 0
    return "".join(parts), len(parts)


# ---------------------------------------------------------------------------
# Tokenisierung
# ---------------------------------------------------------------------------

_EDGE_PUNCT = ".,;:!?\"'„“”‚‘’»«()[]{}…–—"
_BOUNDARY_CHARS = ",;:.!?…"


@dataclass
class _Tok:
    orig: str          # Originalschreibweise ohne umgebende Satzzeichen (außer Punkt am Ende)
    norm: str          # klein, ohne Satzzeichen, korrigiert
    boundary: bool     # danach folgt ein Satzzeichen (Komma, Punkt, ...)


_CORRECTIONS = {
    # Whisper-Fehlerkennungen / Varianten
    "stopp": "stop", "stops": "stop", "stoppt": "stop",
    "tema": "thema", "themen": "thema", "topic": "thema",
    "kunden": "kunde", "kundin": "kunde", "kund": "kunde",
    "on-site": "onsite", "vorort": "vor_ort",
    "stoppetask": "stop", "stopptask": "stop", "stoptask": "stop",
    "pausieren": "pause", "pausiere": "pause", "pausier": "pause", "pausen": "pause",
}

_TICKET_RE = re.compile(
    r"^(?:ticket|tickets|ticker|tiket|tikett|tickett|ticked|tickit|ticket's)"
    r"(?:-?(?:nummer|nr|no|number|id))?$"
)
_TICKET_SUFFIX = {"nummer", "nr", "no", "number", "id", "nr."}


def _normalize_word(w: str) -> str:
    n = w.lower().strip(_EDGE_PUNCT)
    if _TICKET_RE.match(n):
        return "ticket"
    return _CORRECTIONS.get(n, n)


def _tokenize(text: str) -> list[_Tok]:
    # Komma/Semikolon ohne folgendes Leerzeichen trotzdem als Grenze behandeln
    text = re.sub(r"([,;])(?=\S)", r"\1 ", text)
    toks: list[_Tok] = []
    pieces: list[str] = []
    for piece in text.split():
        # "Kunde-Müller" (Whisper koppelt Schlüsselwort und Wert) -> "Kunde Müller"
        m = re.match(r"^(kunde|kunden|thema|tema)-(\S+)$", piece, re.IGNORECASE)
        pieces.extend(m.groups() if m else [piece])
    for piece in pieces:
        stripped = piece.strip(_EDGE_PUNCT)
        if not stripped:
            if toks:
                toks[-1].boundary = True
            continue
        boundary = piece[-1] in _BOUNDARY_CHARS
        orig = piece.lstrip(_EDGE_PUNCT).rstrip(_EDGE_PUNCT.replace(".", ""))
        toks.append(_Tok(orig=orig, norm=_normalize_word(stripped), boundary=boundary))
    # Mehrwort-Ausdrücke zusammenführen: "vor ort", "on site", "ticket nummer"
    merged: list[_Tok] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if nxt is not None and not t.boundary:
            pair = (t.norm, nxt.norm)
            if pair == ("vor", "ort"):
                merged.append(_Tok(f"{t.orig} {nxt.orig}", "vor_ort", nxt.boundary))
                i += 2
                continue
            if pair == ("on", "site"):
                merged.append(_Tok(f"{t.orig} {nxt.orig}", "onsite", nxt.boundary))
                i += 2
                continue
            if t.norm == "ticket" and nxt.norm.rstrip(".") in _TICKET_SUFFIX:
                merged.append(_Tok(f"{t.orig} {nxt.orig}", "ticket", nxt.boundary))
                i += 2
                continue
            if pair in (("setze", "fort"), ("setz", "fort"), ("mach", "weiter"), ("mache", "weiter")):
                merged.append(_Tok(f"{t.orig} {nxt.orig}", "weiter", nxt.boundary))
                i += 2
                continue
        merged.append(t)
        i += 1
    return merged


# ---------------------------------------------------------------------------
# Vokabular
# ---------------------------------------------------------------------------

_FILLERS = {
    "ich", "nun", "jetzt", "mal", "bitte", "so", "also", "äh", "ähm", "öhm", "hm", "hmm",
    "ok", "okay", "gut", "dann", "ja", "gerade", "grad", "eben", "kurz", "sofort",
}
_HESITATIONS = {"äh", "ähm", "öhm", "hm", "hmm", "also"}

# Wörter, die in Kurzbefehlen ohne Bedeutungsänderung vorkommen dürfen
_COMMAND_NOISE = _FILLERS | {
    "task", "aufgabe", "timer", "zeiterfassung", "erfassung", "zeit", "uhr",
    "der", "die", "das", "den", "dem", "ein", "eine", "einen", "eins",
    "mach", "mache", "machen", "bin", "ist", "es", "wir", "mir", "zeig", "zeige",
    "wie", "was", "aktuell", "aktuelle", "aktuellen", "aktueller", "kurze", "kleine",
    "jetzt", "damit", "hiermit", "hier", "mit", "laufenden", "halt", "doch",
}

_START_VERBS = {
    "starte", "starten", "startet", "start", "beginne", "beginnen", "begin",
    "fange", "fang", "anfangen", "startest",
}
_START_PREFIX_SKIP = _FILLERS | {"möchte", "will", "würde", "werde", "gerne", "gern", "wir", "lass", "lasst", "uns"}
_NEW_TASK_ADJ = {"neuer", "neue", "neuen", "neues"}

_STOP_CORE = {"stop", "stoppe", "stoppen", "beende", "beenden", "fertig", "ende", "schluss", "feierabend"}
_PAUSE_CORE = {"pause"}
_RESUME_CORE = {"weiter", "fortsetzen", "fortfahren", "weitermachen", "fortführen", "resume"}
_STATUS_CORE = {"status"}
_RESUME_EXTRA = {"geht", "gehts", "geht's", "es", "fort"}
_PAUSE_END = {"beenden", "beende", "ende", "vorbei", "zu", "aus"}

_FIELD_KEYWORDS = {"kunde": "customer", "thema": "topic", "ticket": "ticket"}
_CUSTOMER_PREPS = {"für"}
_TOPIC_PREPS = {"am", "an", "mit", "zum", "zur", "bei", "beim", "im", "auf"}
_TRAILING_STRIP = _FILLERS | _TOPIC_PREPS | _CUSTOMER_PREPS | {
    "und", "zu", "dem", "den", "der", "die", "das", "von", "vom", "in", "task", "aufgabe",
}
_LOCATION_WORDS = {"onsite": "onsite", "vor_ort": "onsite", "remote": "remote"}


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _clean_value(toks: list[_Tok]) -> str | None:
    while toks and toks[0].norm in _HESITATIONS:
        toks = toks[1:]
    # "Kunde, mit dem Meier" -> "Meier" (führende Präposition + Artikel)
    if toks and toks[0].norm in _TOPIC_PREPS | _CUSTOMER_PREPS:
        toks = toks[1:]
        if toks and toks[0].norm in {"dem", "der", "den", "die", "das"}:
            toks = toks[1:]
    while toks and toks[-1].norm in _TRAILING_STRIP:
        toks = toks[:-1]
    if not toks:
        return None
    value = " ".join(t.orig for t in toks).strip()
    value = value.rstrip(".!?,;:… ").strip()
    return value or None


def _is_location(toks: list[_Tok], i: int) -> bool:
    """Ortsangabe nur an Wertgrenzen, damit "Thema Remote Desktop" Thema bleibt."""
    t = toks[i]
    if t.norm not in _LOCATION_WORDS:
        return False
    if t.boundary or i == len(toks) - 1:
        return True
    nxt = toks[i + 1].norm
    return nxt in _FIELD_KEYWORDS or nxt in _LOCATION_WORDS or nxt in _CUSTOMER_PREPS


def _is_beim_kunden(toks: list[_Tok], i: int) -> bool:
    """"beim Kunden" am Wertende = onsite."""
    if toks[i].norm not in ("beim", "bei") or i + 1 >= len(toks):
        return False
    k = toks[i + 1]
    if k.norm != "kunde" or k.orig.lower().strip(_EDGE_PUNCT) != "kunden":
        return False
    return k.boundary or i + 2 >= len(toks) or toks[i + 2].norm in _FIELD_KEYWORDS


def _parse_ticket(toks: list[_Tok], i: int) -> tuple[str | None, int]:
    """Liest eine Ticketnummer ab Index i. Gibt (ticket, verbrauchte Token)."""
    if i >= len(toks):
        return None, 0
    first = toks[i]
    raw = first.orig.strip(_EDGE_PUNCT).lstrip("#")
    # "ABC-1234", "abc-12"
    m = re.match(r"^([A-Za-z]{1,10})-(\d+)$", raw)
    if m:
        return f"{m.group(1).upper()}-{m.group(2)}", 1
    # "ABC 1234" (Bindestrich von Whisper verschluckt) - nur bei Großbuchstaben-Kürzel
    if re.match(r"^[A-Z]{2,10}$", raw) and words_to_number(raw) is None and i + 1 < len(toks) and not first.boundary:
        digits, n = parse_number_tokens([t.orig.strip(_EDGE_PUNCT) for t in toks[i + 1:]])
        if digits:
            return f"{raw}-{digits}", 1 + n
    words: list[str] = []
    for t in toks[i:]:
        words.append(t.orig.strip(_EDGE_PUNCT))
        if t.boundary:
            break
    digits, n = parse_number_tokens(words)
    if digits:
        return digits, n
    return None, 0


def _detect_start(toks: list[_Tok]) -> int | None:
    """Index nach dem Startverb, wenn der Satz ein Startbefehl ist."""
    for i, t in enumerate(toks):
        if t.norm in _START_VERBS:
            return i + 1
        if t.norm in _NEW_TASK_ADJ and i + 1 < len(toks) and toks[i + 1].norm in ("task", "aufgabe", "auftrag"):
            return i + 2
        if t.norm in _START_PREFIX_SKIP:
            continue
        return None
    return None


def _parse_start(toks: list[_Tok], pos: int, raw: str) -> Command:
    fields: dict[str, str | None] = {"customer": None, "topic": None, "ticket": None}
    location: str | None = None
    explicit_later = lambda slot, idx: any(  # noqa: E731
        _FIELD_KEYWORDS.get(t.norm) == slot for t in toks[idx:]
    )

    slot: str | None = None
    buf: list[_Tok] = []

    def flush() -> None:
        nonlocal buf
        if slot in ("customer", "topic"):
            v = _clean_value(buf)
            if v is not None:
                fields[slot] = v
        buf = []

    i = pos
    while i < len(toks):
        t = toks[i]
        n = t.norm

        if _is_beim_kunden(toks, i):
            flush()
            slot = None
            location = "onsite"
            i += 2
            continue

        if n in _FIELD_KEYWORDS:
            flush()
            slot = _FIELD_KEYWORDS[n]
            if slot == "ticket":
                ticket, used = _parse_ticket(toks, i + 1)
                if ticket:
                    fields["ticket"] = ticket
                slot = None
                i += 1 + used
                continue
            i += 1
            continue

        if _is_location(toks, i):
            flush()
            slot = None
            location = _LOCATION_WORDS[n]
            i += 1
            continue

        nxt_is_kw = i + 1 < len(toks) and toks[i + 1].norm in _FIELD_KEYWORDS
        # "für Müller" -> Kunde (nur wenn kein explizites "Kunde" folgt)
        if (
            n in _CUSTOMER_PREPS
            and fields["customer"] is None
            and slot in (None,)
            and not nxt_is_kw
            and not explicit_later("customer", i)
        ):
            flush()
            slot = "customer"
            i += 1
            continue
        # "am Serverumzug" -> Thema (nur nach Kunde, wenn kein "Thema" folgt)
        if (
            n in _TOPIC_PREPS
            and slot == "customer"
            and fields["topic"] is None
            and buf
            and not nxt_is_kw
            and not explicit_later("topic", i)
            and i + 1 < len(toks)
        ):
            flush()
            slot = "topic"
            i += 1
            continue

        if slot in ("customer", "topic"):
            buf.append(t)
        i += 1
    flush()

    return Command(
        intent=Intent.START,
        customer=fields["customer"],
        topic=fields["topic"],
        ticket=fields["ticket"],
        location=location,
        raw=raw,
    )


def _parse_short(toks: list[_Tok], raw: str) -> Command:
    norms = [t.norm for t in toks]
    # "Pause beenden" / "Pause vorbei" -> fortsetzen
    if "pause" in norms:
        idx = norms.index("pause")
        rest = set(norms[idx + 1:])
        if rest & _PAUSE_END and not (set(norms) - _COMMAND_NOISE - _PAUSE_END - {"pause"}):
            return Command(Intent.RESUME, raw=raw)

    hits: set[Intent] = set()
    leftover: list[str] = []
    for n in norms:
        if n in _STOP_CORE:
            hits.add(Intent.STOP)
        elif n in _PAUSE_CORE:
            hits.add(Intent.PAUSE)
        elif n in _RESUME_CORE:
            hits.add(Intent.RESUME)
        elif n in _STATUS_CORE:
            hits.add(Intent.STATUS)
        elif n in _COMMAND_NOISE or n in _RESUME_EXTRA:
            continue
        else:
            leftover.append(n)
    if len(hits) != 1 or leftover:
        return Command(Intent.UNKNOWN, raw=raw)
    intent = hits.pop()
    if intent is not Intent.RESUME and any(n in ("fort", "geht", "gehts", "geht's") for n in norms):
        return Command(Intent.UNKNOWN, raw=raw)
    return Command(intent, raw=raw)


def parse_command(transcript: str) -> Command:
    """Wertet ein Transkript aus. Liefert immer ein ``Command`` (ggf. UNKNOWN)."""
    raw = transcript or ""
    toks = _tokenize(raw.strip())
    if not toks:
        return Command(Intent.UNKNOWN, raw=raw)
    start_pos = _detect_start(toks)
    if start_pos is not None:
        return _parse_start(toks, start_pos, raw)
    return _parse_short(toks, raw)
