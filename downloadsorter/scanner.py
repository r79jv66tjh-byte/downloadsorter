"""Scannt einen Ordner, kategorisiert Dateien und erzeugt Aufräum-Vorschläge."""
from __future__ import annotations

import hashlib
import os
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlparse

from . import categories as C

DAY = 86400


@dataclass
class FileInfo:
    path: str
    name: str
    size: int
    mtime: float
    ctime: float
    category: str
    type_desc: str
    ext_mismatch: bool = False
    incomplete: bool = False
    host_url: str = ""
    referrer_url: str = ""
    zone_id: str = ""
    sha256: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def age_days(self) -> float:
        # "Alter" = seit wann die Datei im Ordner liegt (Erstellzeit auf NTFS = Download-Zeitpunkt)
        return max(0.0, (time.time() - max(self.ctime, 0)) / DAY)

    @property
    def origin(self) -> str:
        url = self.host_url or self.referrer_url
        if not url:
            return ""
        try:
            return urlparse(url).hostname or url
        except ValueError:
            return url


@dataclass
class Suggestion:
    kind: str          # "installer", "incomplete", "extracted", "duplicate"
    title: str         # Gruppenüberschrift
    reason: str        # Begründung für genau diese Datei
    file: FileInfo
    group: int = 0     # bei Duplikaten: Gruppen-Nr.


@dataclass
class ScanResult:
    root: str
    files: list[FileInfo]
    folders: list[str]
    duplicates: list[list[FileInfo]]
    suggestions: list[Suggestion]
    errors: list[str]

    def by_category(self) -> dict[str, list[FileInfo]]:
        out: dict[str, list[FileInfo]] = {c: [] for c in C.CATEGORIES}
        for f in self.files:
            out.setdefault(f.category, []).append(f)
        return out


def default_downloads() -> str:
    """Ermittelt den echten Downloads-Ordner (auch wenn er verschoben wurde)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            import uuid

            guid = uuid.UUID("{374DE290-123F-4565-9164-39C4925E467B}")
            buf = ctypes.c_wchar_p()
            gid = (ctypes.c_byte * 16).from_buffer_copy(guid.bytes_le)
            if ctypes.windll.shell32.SHGetKnownFolderPath(gid, 0, None, ctypes.byref(buf)) == 0:
                p = buf.value
                ctypes.windll.ole32.CoTaskMemFree(buf)
                if p and os.path.isdir(p):
                    return p
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), "Downloads")


def read_zone_identifier(path: str) -> dict[str, str]:
    """Liest den Alternate Data Stream Zone.Identifier (Herkunft des Downloads)."""
    out: dict[str, str] = {}
    if os.name != "nt":
        return out
    try:
        with open(path + ":Zone.Identifier", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


ZONE_NAMES = {"0": "Lokaler Computer", "1": "Intranet", "2": "Vertrauenswürdig", "3": "Internet", "4": "Eingeschränkt"}


def file_hash(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _iter_files(root: str, recursive: bool, errors: list[str]):
    folders = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if d == root:
                                folders.append(e.path)
                            if recursive:
                                stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            yield e, None
                    except OSError as ex:
                        errors.append(f"{e.path}: {ex}")
        except OSError as ex:
            errors.append(f"{d}: {ex}")
    yield None, folders


def analyze(path: str, st: os.stat_result | None = None) -> FileInfo:
    """Kategorisiert eine einzelne Datei inkl. Herkunft."""
    st = st or os.stat(path)
    name = os.path.basename(path)
    cat, desc, mismatch = C.classify(path, name)
    fi = FileInfo(path=path, name=name, size=st.st_size, mtime=st.st_mtime,
                  ctime=getattr(st, "st_birthtime", st.st_ctime), category=cat,
                  type_desc=desc, ext_mismatch=mismatch,
                  incomplete=C.extension(name) in C.INCOMPLETE_EXT)
    zone = read_zone_identifier(path)
    fi.host_url = zone.get("HostUrl", "")
    fi.referrer_url = zone.get("ReferrerUrl", "")
    fi.zone_id = zone.get("ZoneId", "")
    if mismatch:
        fi.notes.append(f"Endung passt nicht zum Inhalt ({desc})")
    return fi


def scan(root: str, recursive: bool = False, find_duplicates: bool = True,
         installer_age_days: int = 30, progress=None, cancel=None) -> ScanResult:
    """Scannt `root`. progress(text, done, total) wird optional aufgerufen."""
    errors: list[str] = []
    files: list[FileInfo] = []
    folders: list[str] = []
    entries = []
    for e, fl in _iter_files(root, recursive, errors):
        if e is None:
            folders = fl
        else:
            entries.append(e)

    total = len(entries)
    for i, e in enumerate(entries):
        if cancel and cancel():
            break
        if progress and i % 25 == 0:
            progress(f"Analysiere {e.name}", i, total)
        try:
            st = e.stat()
        except OSError as ex:
            errors.append(f"{e.path}: {ex}")
            continue
        if e.name.lower() == "desktop.ini":
            continue
        files.append(analyze(e.path, st))

    duplicates = _find_duplicates(files, progress, cancel, errors) if find_duplicates else []
    suggestions = _suggest(files, folders, duplicates, installer_age_days)
    if progress:
        progress("Fertig", total, total)
    return ScanResult(root=root, files=files, folders=folders, duplicates=duplicates,
                      suggestions=suggestions, errors=errors)


def _find_duplicates(files, progress, cancel, errors) -> list[list[FileInfo]]:
    by_size: dict[int, list[FileInfo]] = defaultdict(list)
    for f in files:
        if f.size > 0 and not f.incomplete:
            by_size[f.size].append(f)
    candidates = [g for g in by_size.values() if len(g) > 1]
    todo = sum(len(g) for g in candidates)
    done = 0
    groups: list[list[FileInfo]] = []
    for g in candidates:
        by_hash: dict[str, list[FileInfo]] = defaultdict(list)
        for f in g:
            if cancel and cancel():
                return groups
            if progress:
                progress(f"Prüfe Duplikat: {f.name}", done, todo)
            try:
                f.sha256 = file_hash(f.path)
                by_hash[f.sha256].append(f)
            except OSError as ex:
                errors.append(f"{f.path}: {ex}")
            done += 1
        groups += [sorted(v, key=lambda x: x.ctime) for v in by_hash.values() if len(v) > 1]
    groups.sort(key=lambda grp: -grp[0].size * (len(grp) - 1))
    return groups


def _archive_extracted_into(fi: FileInfo, folders: list[str]) -> str | None:
    """Prüft, ob der Inhalt eines Archivs schon entpackt daneben liegt. Liefert den Ordner oder None."""
    base = fi.name
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
            break
    else:
        base = os.path.splitext(base)[0]
    parent = os.path.dirname(fi.path)
    lower_folders = {os.path.basename(p).lower(): p for p in folders if os.path.dirname(p) == parent}

    if C.extension(fi.name) == "zip":
        try:
            with zipfile.ZipFile(fi.path) as z:
                names = [n for n in z.namelist() if not n.endswith("/")][:2000]
        except Exception:
            names = []
        if names:
            # Kandidaten: Ordner mit Archivnamen oder der Archivordner selbst (Entpacken "hierhin")
            for target in [lower_folders.get(base.lower()), parent]:
                if not target:
                    continue
                hits = sum(1 for n in names if os.path.exists(os.path.join(target, *n.split("/"))))
                if hits >= max(1, int(len(names) * 0.9)):
                    return target
            # Häufig: Zip enthält einen Wurzelordner, der neben dem Archiv entpackt wurde
            return None
        return None
    # Andere Archive (rar, 7z, ...): Ordner gleichen Namens daneben, nicht leer
    p = lower_folders.get(base.lower())
    if p:
        try:
            if any(os.scandir(p)):
                return p
        except OSError:
            pass
    return None


INCOMPLETE_MIN_AGE = 3600  # Sekunden; jüngere Teil-Downloads laufen evtl. noch


def _suggest(files, folders, duplicates, installer_age_days) -> list[Suggestion]:
    out: list[Suggestion] = []
    now = time.time()
    for f in files:
        if f.incomplete:
            idle = now - f.mtime
            if idle >= INCOMPLETE_MIN_AGE:
                out.append(Suggestion("incomplete", "Abgebrochene Downloads",
                                      f"seit {_fmt_age(idle)} unverändert", f))
        elif f.category == C.INSTALLER and C.extension(f.name) not in ("dll",) and f.age_days >= installer_age_days:
            out.append(Suggestion("installer", f"Installer älter als {installer_age_days} Tage",
                                  f"{f.age_days:.0f} Tage alt", f))
        if f.category == C.ARCHIVE and not f.incomplete:
            target = _archive_extracted_into(f, folders)
            if target:
                where = "daneben" if target == os.path.dirname(f.path) else f"in „{os.path.basename(target)}“"
                out.append(Suggestion("extracted", "Archive, die schon entpackt sind",
                                      f"Inhalt liegt bereits {where}", f))
    for i, grp in enumerate(duplicates, 1):
        keep = grp[0]
        for f in grp[1:]:
            out.append(Suggestion("duplicate", "Duplikate (älteste Kopie bleibt)",
                                  f"identisch mit „{keep.name}“", f, group=i))
    return out


def _fmt_age(seconds: float) -> str:
    if seconds < 3600:
        return f"{seconds / 60:.0f} Min."
    if seconds < DAY:
        return f"{seconds / 3600:.0f} Std."
    return f"{seconds / DAY:.0f} Tagen"


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_age_days(d: float) -> str:
    if d < 1:
        return "heute"
    if d < 2:
        return "gestern"
    if d < 60:
        return f"{d:.0f} Tage"
    if d < 730:
        return f"{d / 30.4:.0f} Monate"
    return f"{d / 365:.1f} Jahre"


AGE_BUCKETS = [(7, "< 1 Woche"), (30, "< 1 Monat"), (180, "< 6 Monate"), (365, "< 1 Jahr"), (float("inf"), "älter")]


def age_bucket(d: float) -> str:
    for limit, label in AGE_BUCKETS:
        if d < limit:
            return label
    return AGE_BUCKETS[-1][1]
