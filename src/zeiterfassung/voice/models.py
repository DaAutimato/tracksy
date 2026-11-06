"""Verwaltung der Whisper-Modelle (ggml-Format für whisper.cpp).

Ablage: ``<models_dir>/whisper-cpp/ggml-<size>.bin`` (Ordner 0700, Datei 0600).

DATENSCHUTZ: ``download()`` ist der EINZIGE Netzwerkzugriff der App. Er darf
nur nach ausdrücklicher Zustimmung des Nutzers (Einrichtungsassistent,
Einstellungen) aufgerufen werden. Übertragen wird nur die HTTPS-Anfrage an
huggingface.co (IP-Adresse, User-Agent); es gibt keine Telemetrie und keine
Fremdbibliothek – nur ``urllib`` aus der Python-Standardbibliothek.

INTEGRITÄT: Je Modell sind zwei unabhängige Prüfsummen und die
exakte Größe hinterlegt: SHA-1 aus der whisper.cpp-Dokumentation
(``models/README.md``, v1.9.5) und SHA-256 + Größe aus den LFS-Metadaten von
Hugging Face (API ``/api/models/ggerganov/whisper.cpp/tree/main``, abgefragt
2026-10-30). Der Download wird erst nach erfolgreicher Prüfung beider Summen an
seinen Platz verschoben; bei Abweichung einer vollständig geladenen Datei landet
er unter ``<models_dir>/_archiv/`` (Beleg für eine Manipulation, bewusst nicht
gelöscht).

TEMPORÄRE DATEIEN: Geladen wird in
``<models_dir>/whisper-cpp/ggml-<size>.bin.part``. Bei Abbruch, Netzfehler,
unvollständigem Download oder fehlgeschlagener Aktivierung wird diese EIGENE
``.part``-Datei sofort entfernt (bis ca. 1,5 GB bei ``medium``). Das ist die
einzige Ausnahme von „nicht löschen → ``_archiv/``“; fertige und archivierte
Modelle werden nie gelöscht. Reste nach einem Absturz räumt
:func:`cleanup_partial` auf (App-Start, Beginn jedes Downloads/jeder Reparatur).

SPERRE: Download und Reparatur derselben Größe nutzen denselben
``.part``-Namen; ``flock`` auf ``ggml-<size>.bin.lock`` (0600, bleibt liegen)
verhindert parallele Vorgänge – auch zwischen Prozessen. Ein zweiter Versuch
endet mit :class:`DownloadBusy`.

REPARATUR: ``model_status()`` erkennt günstig (nur ``stat``) eine Datei mit
falscher Größe; ``verify()`` prüft die Prüfsummen nur auf Anforderung (liest die
ganze Datei). ``repair()`` lädt das Modell erneut in eine temporäre Datei im
Modellordner, aktiviert sie erst nach erfolgreicher Prüfung und benennt die alte
Datei vorher reversibel nach ``<models_dir>/_archiv/ggml-<size>.bin.defekt-<Zeit>``
um (nie löschen). Schlägt der Ersatz fehl, bleibt die alte Datei unverändert.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import logging
import os
import stat
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .errors import ModelDownloadError

log = logging.getLogger(__name__)

BASE_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
SUBDIR = "whisper-cpp"
# Weiterleitungen nur zu Hugging Face und dessen CDN.
# ⚠ Annahme: Hugging Face liefert über *.hf.co bzw. *.huggingface.co aus.
ALLOWED_HOSTS = ("huggingface.co",)
ALLOWED_HOST_SUFFIXES = (".huggingface.co", ".hf.co")
# Download-Obergrenze: Faktor auf die erwartete Größe
SIZE_LIMIT_FACTOR = 1.15


@dataclass(frozen=True)
class ModelInfo:
    size: str
    approx_mb: int     # Download-/Plattengröße (ungefähr)
    ram_hint: str      # grober RAM-Bedarf auf CPU
    note: str
    sha1: str          # whisper.cpp models/README.md
    sha256: str        # Hugging Face LFS-OID
    nbytes: int        # exakte Dateigröße

    @property
    def filename(self) -> str:
        return f"ggml-{self.size}.bin"

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.filename}"

    @property
    def max_bytes(self) -> int:
        return self.nbytes or int(self.approx_mb * SIZE_LIMIT_FACTOR * 1024 * 1024)


MODELS: dict[str, ModelInfo] = {
    "tiny": ModelInfo("tiny", 75, "ca. 0,2 GB", "sehr schnell, deutlich mehr Erkennungsfehler",
                      "bd577a113a864445d4c299885e0cb97d4ba92b5f",
                      "be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21", 77691713),
    "base": ModelInfo("base", 142, "ca. 0,3–0,4 GB",
                      "leistungsschwache Rechner, Befehle gut, Diktat mäßig",
                      "465707469ff3a37a2b9b8d8f89f2f99de7299dac",
                      "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe", 147951465),
    "small": ModelInfo("small", 466, "ca. 0,6–1 GB", "empfohlen ab 4 Kernen, gute Erkennung",
                       "55356645c2b361a969dfd0ef2c5a50d530afd8d5",
                       "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b", 487601967),
    "medium": ModelInfo("medium", 1530, "ca. 1,7–2,5 GB",
                        "beste Erkennung, nur auf starker Hardware flüssig",
                        "fd9727b6e1217c2f614f9b698455c4ffd82463b4",
                        "6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208", 1533763059),
}


def _info(size: str) -> ModelInfo:
    try:
        return MODELS[size]
    except KeyError:
        raise ValueError(f"Unbekannte Modellgröße: {size!r} (erlaubt: {', '.join(MODELS)})") from None


def model_file(models_dir: Path | str, size: str) -> Path:
    return Path(models_dir).expanduser() / SUBDIR / _info(size).filename


def is_installed(models_dir: Path | str, size: str) -> bool:
    return model_file(models_dir, size).is_file()


def installed_sizes(models_dir: Path | str) -> list[str]:
    return [s for s in MODELS if is_installed(models_dir, s)]


STATUS_MISSING = "missing"
STATUS_OK = "ok"
STATUS_SIZE_MISMATCH = "size_mismatch"


def model_status(models_dir: Path | str, size: str) -> str:
    """Günstige Zustandsprüfung ohne Lesen der Datei.

    ``"missing"`` (nicht installiert), ``"size_mismatch"`` (vorhanden, aber
    Größe weicht von der hinterlegten exakten Größe ab → reparaturbedürftig)
    oder ``"ok"`` (Größe stimmt; Prüfsummen prüft nur :func:`verify`).
    """
    info = _info(size)
    f = model_file(models_dir, size)
    try:
        st = f.stat()
    except OSError:
        return STATUS_MISSING
    if not f.is_file():
        return STATUS_MISSING
    if info.nbytes and st.st_size != info.nbytes:
        return STATUS_SIZE_MISMATCH
    return STATUS_OK


def needs_repair(models_dir: Path | str, size: str) -> bool:
    return model_status(models_dir, size) == STATUS_SIZE_MISMATCH


class DownloadCancelled(ModelDownloadError):
    """Download wurde vom Nutzer abgebrochen."""


class DownloadBusy(ModelDownloadError):
    """Für diese Modellgröße läuft bereits ein Download oder eine Reparatur."""


PART_SUFFIX = ".part"
LOCK_SUFFIX = ".lock"


def part_file(models_dir: Path | str, size: str) -> Path:
    """Temporäre Download-Datei ``ggml-<size>.bin.part`` (einziges Namensmuster)."""
    target = model_file(models_dir, size)
    return target.with_name(target.name + PART_SUFFIX)


def _lock_file(models_dir: Path | str, size: str) -> Path:
    target = model_file(models_dir, size)
    return target.with_name(target.name + LOCK_SUFFIX)


@contextlib.contextmanager
def _size_lock(models_dir: Path | str, size: str) -> Iterator[None]:
    """Exklusive, nicht blockierende Sperre je Modellgröße (``flock``).

    ``flock`` gilt je geöffneter Datei – also auch zwischen zwei Threads
    desselben Prozesses. Die Sperrdatei (0600, O_NOFOLLOW) bleibt liegen; der
    Kernel gibt die Sperre beim Schließen bzw. Prozessende frei (auch nach Absturz).
    """
    path = _lock_file(models_dir, size)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise ModelDownloadError(f"Modell {size}: Sperrdatei nicht nutzbar: {exc}") from exc
    try:
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise DownloadBusy(
                    f"Modell {size} wird bereits heruntergeladen oder repariert – "
                    "bitte warten, bis der laufende Vorgang abgeschlossen ist."
                ) from None
            raise ModelDownloadError(f"Modell {size}: Sperrdatei nicht nutzbar: {exc}") from exc
        yield
    finally:
        os.close(fd)  # gibt die Sperre frei


def _discard_part(part: Path, ident: tuple[int, int] | None = None) -> bool:
    """Eigene ``.part``-Datei entfernen (bewusste Ausnahme vom Grundsatz „nicht löschen“).

    Nur eine reguläre Datei (lstat, kein symbolischer Link) des eigenen Benutzers;
    mit ``ident`` (st_dev, st_ino) nur genau die selbst geschriebene Datei.
    Fehler werden protokolliert, nie weitergereicht (Aufräumen ist Nebensache).
    """
    try:
        st = os.lstat(part)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return False
        if ident is not None and (st.st_dev, st.st_ino) != ident:
            return False
        os.unlink(part)
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("Unvollständiger Download %s nicht entfernt: %s", part.name, exc)
        return False
    log.info("Unvollständigen Download entfernt: %s", part.name)
    return True


def cleanup_partial(models_dir: Path | str) -> list[Path]:
    """Veraltete ``.part``-Reste (z. B. nach Absturz mitten im Download) entfernen.

    Geprüft werden ausschließlich die Namen, die :func:`download`/:func:`repair`
    selbst erzeugen: ``<models_dir>/whisper-cpp/ggml-<size>.bin.part`` für jede
    bekannte Größe – keine Unterordner, nie ``_archiv/``, keine fertigen Modelle.
    Entfernt wird nur eine reguläre Datei (kein Link) des eigenen Benutzers und
    nur, wenn für diese Größe gerade kein Download läuft (Sperre frei). Legt keine
    Ordner an. Liefert die entfernten Pfade.
    """
    removed: list[Path] = []
    for size in MODELS:
        part = part_file(models_dir, size)
        try:
            st = os.lstat(part)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            continue
        try:
            with _size_lock(models_dir, size):
                if _discard_part(part, (st.st_dev, st.st_ino)):
                    removed.append(part)
        except DownloadBusy:
            log.debug("Modell %s wird gerade geladen – .part bleibt", size)
        except ModelDownloadError as exc:
            log.warning("Aufräumen für Modell %s übersprungen: %s", size, exc)
    return removed


ProgressCallback = Callable[[str], None]
Opener = Callable[[str], object]


def _hashes(path: Path) -> tuple[str, str]:
    h1 = hashlib.sha1()  # noqa: S324 - Vergleich mit veröffentlichter Prüfsumme, keine Signatur
    h256 = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h1.update(chunk)
            h256.update(chunk)
    return h1.hexdigest(), h256.hexdigest()


def verify(models_dir: Path | str, size: str) -> bool:
    """SHA-1 und SHA-256 der Modelldatei prüfen (liest die ganze Datei)."""
    f = model_file(models_dir, size)
    info = _info(size)
    if not f.is_file() or _hashes(f) != (info.sha1, info.sha256):
        log.warning("Prüfsumme falsch oder Datei fehlt: Modell %s", size)
        return False
    return True


def _archive_dest(models_dir: Path | str, name: str) -> Path:
    """Freien Zielnamen ``<models_dir>/_archiv/<name>[-n]`` liefern (Ordner 0700)."""
    archive = Path(models_dir).expanduser() / "_archiv"
    archive.mkdir(mode=0o700, parents=True, exist_ok=True)
    dest = archive / name
    n = 1
    while dest.exists():
        n += 1
        dest = archive / f"{name}-{n}"
    return dest


def _quarantine(models_dir: Path | str, src: Path) -> Path:
    """Datei nach ``<models_dir>/_archiv/`` verschieben (nicht löschen)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = _archive_dest(models_dir, f"{src.name}-{stamp}-pruefsumme-falsch")
    os.replace(src, dest)
    return dest


def archived_path(models_dir: Path | str, size: str) -> Path:
    """Zielname für eine beim Reparieren ersetzte Modelldatei (noch nicht angelegt)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return _archive_dest(models_dir, f"{_info(size).filename}.defekt-{stamp}")


def _host_allowed(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in ALLOWED_HOSTS or host.endswith(ALLOWED_HOST_SUFFIXES)


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Weiterleitungen (Hugging Face → CDN) nur auf HTTPS und nur zu Hugging Face."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        if not newurl.startswith("https://"):
            raise ModelDownloadError(f"Weiterleitung ohne HTTPS abgelehnt: {newurl}")
        if not _host_allowed(newurl):
            host = urllib.parse.urlsplit(newurl).hostname
            raise ModelDownloadError(f"Weiterleitung zu fremdem Server abgelehnt: {host}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener(url: str) -> object:
    """Minimale Anfrage: nur URL, User-Agent „Tracksy“; keine Cookies, keine Kennungen."""
    if not url.startswith("https://"):
        raise ModelDownloadError(f"Nur HTTPS erlaubt: {url}")
    if not _host_allowed(url):
        raise ModelDownloadError(f"Nur Hugging Face erlaubt: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Tracksy"})
    opener = urllib.request.build_opener(_HttpsOnlyRedirect)
    return opener.open(req, timeout=60)


def download(
    models_dir: Path | str,
    size: str,
    progress_cb: ProgressCallback | None = None,
    *,
    cancel: threading.Event | None = None,
    _opener: Opener | None = None,
) -> Path:
    """Lädt ``ggml-<size>.bin`` nach ``<models_dir>/whisper-cpp/``.

    NUR nach ausdrücklicher Zustimmung des Nutzers aufrufen (Netzwerkzugriff).
    ``progress_cb`` erhält kurze deutsche Statusmeldungen (aus dem
    aufrufenden Thread; GUI muss ggf. per GLib.idle_add marshallen).
    ``cancel``: wird es gesetzt, bricht der Download beim nächsten Block mit
    :class:`DownloadCancelled` ab. Bei Abbruch und jedem Fehler wird die eigene
    ``.part``-Datei entfernt (Ausnahme: vollständige Datei mit falscher Prüfsumme
    → ``_archiv/``). Läuft für diese Größe schon ein Download oder eine
    Reparatur, endet der Aufruf mit :class:`DownloadBusy`.
    """
    _info(size)
    target = model_file(models_dir, size)
    report = _reporter(progress_cb)

    if target.is_file():
        report(f"Modell {size} ist bereits vorhanden.")
        return target

    with _size_lock(models_dir, size):
        cleanup_partial(models_dir)  # Reste anderer Größen/Abstürze
        if target.is_file():  # parallel fertig geworden
            report(f"Modell {size} ist bereits vorhanden.")
            return target
        part, ident = _fetch(models_dir, size, report, cancel, _opener)
        try:
            os.replace(part, target)
        except OSError as exc:
            _discard_part(part, ident)
            raise ModelDownloadError(f"Modell {size}: Aktivieren fehlgeschlagen: {exc}") from exc
    report(f"Modell {size} installiert.")
    return target


def _reporter(progress_cb: ProgressCallback | None) -> ProgressCallback:
    def report(msg: str) -> None:
        log.info(msg)
        if progress_cb:
            progress_cb(msg)
    return report


def _fetch(
    models_dir: Path | str,
    size: str,
    report: ProgressCallback,
    cancel: threading.Event | None,
    _opener: Opener | None,
) -> tuple[Path, tuple[int, int]]:
    """Lädt das Modell in ``<ziel>.part`` und prüft Größe + SHA-1 + SHA-256.

    Nur unter :func:`_size_lock` aufrufen. Liefert die geprüfte ``.part``-Datei
    und ihre Kennung (st_dev, st_ino); das Aktivieren übernimmt der Aufrufer.
    Bei jedem Fehler (auch Abbruch) ist die eigene ``.part``-Datei danach
    entfernt – außer bei falscher Prüfsumme einer vollständigen Datei (Quarantäne).
    """
    info = _info(size)
    part = part_file(models_dir, size)
    part.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    report(f"Lade Modell {size} (ca. {info.approx_mb} MB) von huggingface.co …")
    ident: list[tuple[int, int]] = []  # gesetzt, sobald die .part-Datei uns gehört
    try:
        done, digests = _stream(info, part, cancel, report, _opener, ident)
    except BaseException:
        if ident:
            _discard_part(part, ident[0])
        raise
    if info.nbytes and done != info.nbytes:
        # Abgerissene Verbindung: nicht in Quarantäne (würde je Fehlversuch bis
        # 1,5 GB in _archiv/ anhäufen), sondern die eigene .part-Datei entfernen.
        _discard_part(part, ident[0])
        raise ModelDownloadError(
            f"Modell {size}: Download unvollständig ({done} von {info.nbytes} Bytes) – "
            "bitte erneut versuchen"
        )
    report(f"Prüfe Modell {size} …")
    if digests != (info.sha1, info.sha256):
        # Vollständige Datei, falscher Inhalt: Beleg für Manipulation → aufheben
        dest = _quarantine(models_dir, part)
        raise ModelDownloadError(
            f"Modell {size}: Prüfsumme stimmt nicht – Download verworfen und nach {dest} verschoben"
        )
    return part, ident[0]


def _stream(
    info: ModelInfo,
    part: Path,
    cancel: threading.Event | None,
    report: ProgressCallback,
    _opener: Opener | None,
    ident: list[tuple[int, int]],
) -> tuple[int, tuple[str, str]]:
    """Antwort blockweise in ``part`` schreiben; liefert (Bytes, (SHA-1, SHA-256))."""
    size = info.size
    h = hashlib.sha1()  # noqa: S324
    h256 = hashlib.sha256()
    try:
        with (_opener or _default_opener)(info.url) as resp:  # type: ignore[attr-defined]
            total = int(getattr(resp, "headers", {}).get("Content-Length") or 0)
            if total > info.max_bytes or (info.nbytes and total and total != info.nbytes):
                raise ModelDownloadError(f"Modell {size}: Datei hat unerwartete Größe ({total} Bytes)")
            done = last = 0
            # O_NOFOLLOW: kein Umlenken über einen symbolischen Link
            fd = os.open(part, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as out:
                st = os.fstat(fd)
                ident.append((st.st_dev, st.st_ino))  # ab hier gehört die .part-Datei uns
                os.fchmod(fd, 0o600)
                while chunk := resp.read(1024 * 1024):
                    if cancel is not None and cancel.is_set():
                        raise DownloadCancelled(f"Download von Modell {size} abgebrochen.")
                    if done + len(chunk) > info.max_bytes:
                        raise ModelDownloadError(f"Modell {size}: Download überschreitet die erwartete Größe")
                    out.write(chunk)
                    h.update(chunk)
                    h256.update(chunk)
                    done += len(chunk)
                    if total and done * 10 // total > last:
                        last = done * 10 // total
                        report(f"Modell {size}: {last * 10} %")
    except ModelDownloadError:
        raise
    except Exception as exc:
        raise ModelDownloadError(f"Download von {info.url} fehlgeschlagen: {exc}") from exc
    return done, (h.hexdigest(), h256.hexdigest())


@dataclass(frozen=True)
class RepairResult:
    path: Path                 # aktive (neue) Modelldatei
    archived: Path | None      # umbenannte alte Datei (None, wenn keine vorhanden war)


def repair(
    models_dir: Path | str,
    size: str,
    progress_cb: ProgressCallback | None = None,
    *,
    cancel: threading.Event | None = None,
    _opener: Opener | None = None,
) -> RepairResult:
    """Modell erneut herunterladen und eine vorhandene (ggf. beschädigte) Datei ersetzen.

    NUR nach ausdrücklicher Zustimmung des Nutzers aufrufen (Netzwerkzugriff).
    Ablauf: Download in ``<ziel>.part`` → Größe + SHA-1 + SHA-256 prüfen → alte
    Datei nach ``<models_dir>/_archiv/ggml-<size>.bin.defekt-<Zeit>`` umbenennen
    → neue Datei aktivieren. Bei Abbruch/Fehler bleibt die alte Datei unverändert;
    wie bei :func:`download` wird die eigene ``.part``-Datei entfernt, eine
    vollständig geladene Datei mit falscher Prüfsumme landet unter ``_archiv/``.
    Die alte Modelldatei wird nie gelöscht, nur archiviert. Läuft für diese
    Größe schon ein Download oder eine Reparatur: :class:`DownloadBusy`.
    """
    _info(size)
    target = model_file(models_dir, size)
    report = _reporter(progress_cb)
    with _size_lock(models_dir, size):
        cleanup_partial(models_dir)
        part, ident = _fetch(models_dir, size, report, cancel, _opener)  # alte Datei unberührt
        archived: Path | None = None
        try:
            if target.is_file() or target.is_symlink():
                archived = archived_path(models_dir, size)
                os.replace(target, archived)
            os.replace(part, target)
        except OSError as exc:
            # lexists: auch einen (ggf. verwaisten) symbolischen Link zurückholen
            if archived is not None and os.path.lexists(archived) and not os.path.lexists(target):
                os.replace(archived, target)  # alte Datei wiederherstellen
            _discard_part(part, ident)
            raise ModelDownloadError(f"Modell {size}: Ersetzen fehlgeschlagen: {exc}") from exc
    if archived is not None:
        log.info("Alte Modelldatei %s archiviert: %s", size, archived)
    report(f"Modell {size} repariert.")
    return RepairResult(target, archived)
