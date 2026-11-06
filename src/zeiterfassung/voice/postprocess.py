"""Nachbearbeitung von Transkripten: Whisper-Wiederholungsschleifen entfernen.

Whisper wiederholt bei kurzen Aufnahmen gelegentlich den letzten Satz bis zum
Tokenlimit ("Server 1 neu gestartet. Server 1 neu gestartet. … Server 1 neu
gest"). Die Ursache wird in der Decoder-Konfiguration behoben (Zeitstempel,
Tokenbudget); dieses Modul ist die zusätzliche, bewusst konservative
Absicherung:

- Aufeinanderfolgende identische Sätze (Vergleich ohne Groß-/Kleinschreibung
  und Satzzeichen) werden auf einen reduziert. Ein-Wort-Sätze erst ab drei
  Wiederholungen ("Ja. Ja." bleibt).
- Ein abgeschnittener Rest am Ende (ohne Satzzeichen), der der Anfang des
  wiederholten Satzes ist, wird entfernt – nur wenn vorher wirklich eine
  Wiederholung gefunden wurde.
- Wortfolgen (1–8 Wörter), die mindestens dreimal direkt hintereinander
  stehen, werden auf ein Vorkommen reduziert ("Pause Pause Pause" → "Pause");
  zweifache Wiederholungen ("sehr sehr") bleiben unangetastet.

Reine Textfunktion, kein Logging von Inhalten.
"""

from __future__ import annotations

import re

__all__ = ["collapse_repeats"]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_TERMINAL = ".!?…"
_MAX_PHRASE_WORDS = 8
_MIN_PHRASE_REPEATS = 3


def _norm(text: str) -> str:
    """Vergleichsform: Kleinbuchstaben, nur Wortzeichen, einfache Leerzeichen."""
    return " ".join(re.findall(r"\w+", text.casefold()))


def _collapse_sentences(sentences: list[str]) -> tuple[list[str], bool]:
    out: list[str] = []
    changed = False
    i = 0
    while i < len(sentences):
        cur = sentences[i]
        key = _norm(cur)
        j = i + 1
        while j < len(sentences) and key and _norm(sentences[j]) == key:
            j += 1
        count = j - i
        min_count = 2 if len(key.split()) >= 2 else _MIN_PHRASE_REPEATS
        if count >= min_count:
            out.append(cur)
            changed = True
        else:
            out.extend(sentences[i:j])
        i = j
    return out, changed


def _drop_truncated_tail(sentences: list[str]) -> list[str]:
    """Abgeschnittenen Satzanfang am Ende entfernen, wenn er den Vorgänger wiederholt."""
    if len(sentences) < 2:
        return sentences
    tail = sentences[-1].rstrip()
    if not tail or tail[-1] in _TERMINAL:
        return sentences
    prev = _norm(sentences[-2])
    tail_key = _norm(tail)
    if tail_key and prev.startswith(tail_key) and tail_key != prev:
        return sentences[:-1]
    return sentences


def _collapse_phrases(text: str) -> tuple[str, bool]:
    """Wortfolgen, die >= 3x direkt hintereinander stehen, auf eine reduzieren."""
    words = text.split()
    keys = [_norm(w) for w in words]
    out: list[str] = []
    changed = False
    i = 0
    n = len(words)
    while i < n:
        best_k, best_reps = 0, 1
        for k in range(1, min(_MAX_PHRASE_WORDS, (n - i) // _MIN_PHRASE_REPEATS) + 1):
            unit = keys[i:i + k]
            if not any(unit):
                continue
            reps = 1
            while keys[i + reps * k:i + (reps + 1) * k] == unit:
                reps += 1
            if reps >= _MIN_PHRASE_REPEATS and reps * k > best_reps * best_k:
                best_k, best_reps = k, reps
        if best_k:
            # letztes Vorkommen behalten (trägt ggf. das Satzzeichen)
            start = i + (best_reps - 1) * best_k
            out.extend(words[start:start + best_k])
            i += best_reps * best_k
            changed = True
        else:
            out.append(words[i])
            i += 1
    return " ".join(out), changed


def collapse_repeats(text: str) -> str:
    """Whisper-Wiederholungsschleifen aus ``text`` entfernen (konservativ).

    Gibt den Text unverändert zurück, wenn keine Wiederholung erkannt wurde.
    """
    stripped = text.strip()
    if not stripped:
        return stripped
    sentences = [s for s in _SENTENCE_SPLIT.split(stripped) if s]
    sentences, changed = _collapse_sentences(sentences)
    if changed:
        sentences = _drop_truncated_tail(sentences)
    joined = " ".join(sentences)
    joined, phrase_changed = _collapse_phrases(joined)
    if not (changed or phrase_changed):
        return stripped
    return joined
