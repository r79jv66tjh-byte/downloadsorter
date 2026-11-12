"""Verschieben mit Vorschau + Protokoll/Rückgängig, Löschen nur in den Papierkorb."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass

from . import categories as C


def app_dir() -> str:
    base = os.environ.get("DOWNLOADSORTER_HOME") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "DownloadSorter")
    os.makedirs(base, exist_ok=True)
    return base


# ---------------------------------------------------------------- Einstellungen

DEFAULT_CONFIG = {
    # Ziel je Kategorie: leer = Unterordner im gescannten Ordner mit dem Kategorienamen,
    # relativer Name = Unterordner, absoluter Pfad = festes Ziel
    "targets": {c: "" for c in C.CATEGORIES},
    "installer_age_days": 30,
    "last_folder": "",
    "recursive": False,
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(os.path.join(app_dir(), "config.json"), encoding="utf-8") as f:
            data = json.load(f)
        cfg.update({k: v for k, v in data.items() if k != "targets"})
        cfg["targets"].update(data.get("targets", {}))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    path = os.path.join(app_dir(), "config.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def target_dir(category: str, root: str, cfg: dict) -> str:
    t = (cfg.get("targets") or {}).get(category, "").strip()
    if not t:
        return os.path.join(root, category)
    t = os.path.expandvars(os.path.expanduser(t))
    return t if os.path.isabs(t) else os.path.join(root, t)


# ---------------------------------------------------------------- Vorschau

@dataclass
class PlannedMove:
    src: str
    dst: str
    renamed: bool = False   # Zielname wegen Namenskonflikt geändert
    problem: str = ""       # nicht ausführbar (z. B. Quelle fehlt)


def _free_name(path: str, taken: set[str]) -> str:
    if not os.path.exists(path) and path.lower() not in taken:
        return path
    stem, ext = os.path.splitext(path)
    i = 1
    while True:
        cand = f"{stem} ({i}){ext}"
        if not os.path.exists(cand) and cand.lower() not in taken:
            return cand
        i += 1


def plan_moves(items: list[tuple[str, str]]) -> list[PlannedMove]:
    """items: [(quelle, zielordner)]. Löst Namenskonflikte, ändert nichts auf der Platte."""
    plan: list[PlannedMove] = []
    taken: set[str] = set()
    for src, dst_dir in items:
        want = os.path.join(dst_dir, os.path.basename(src))
        if not os.path.isfile(src):
            plan.append(PlannedMove(src, want, problem="Quelle existiert nicht mehr"))
            continue
        if os.path.normcase(os.path.dirname(os.path.abspath(src))) == os.path.normcase(os.path.abspath(dst_dir)):
            plan.append(PlannedMove(src, want, problem="liegt schon im Zielordner"))
            continue
        dst = _free_name(want, taken)
        taken.add(dst.lower())
        plan.append(PlannedMove(src, dst, renamed=(dst != want)))
    return plan


# ---------------------------------------------------------------- Protokoll

def journal_path() -> str:
    return os.path.join(app_dir(), "protokoll.json")


def load_journal() -> list[dict]:
    try:
        with open(journal_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_journal(entries: list[dict]) -> None:
    path = journal_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


_JOURNAL_LOCK = threading.RLock()  # GUI und Automatik-Thread schreiben ins selbe Protokoll


def _append_batch(batch: dict) -> None:
    with _JOURNAL_LOCK:
        entries = load_journal()
        entries.append(batch)
        _save_journal(entries)


def _update_batch(batch: dict) -> None:
    with _JOURNAL_LOCK:
        entries = load_journal()
        for i, b in enumerate(entries):
            if b.get("id") == batch["id"]:
                entries[i] = batch
                break
        else:
            entries.append(batch)
        _save_journal(entries)


def execute_moves(plan: list[PlannedMove], label: str = "Sortieren") -> dict:
    """Führt die Verschiebungen aus und protokolliert jede einzelne (auch bei Abbruch)."""
    batch = {"id": uuid.uuid4().hex, "time": time.time(), "kind": "move", "label": label,
             "undone": False, "ops": [], "errors": []}
    _append_batch(batch)  # vorab anlegen, damit auch ein Absturz nachvollziehbar bleibt
    for m in plan:
        if m.problem:
            continue
        try:
            os.makedirs(os.path.dirname(m.dst), exist_ok=True)
            if os.path.exists(m.dst):  # zwischen Vorschau und Ausführung entstanden
                m.dst = _free_name(m.dst, set())
            shutil.move(m.src, m.dst)
            batch["ops"].append({"src": m.src, "dst": m.dst})
        except OSError as ex:
            batch["errors"].append(f"{m.src}: {ex}")
        _update_batch(batch)
    _update_batch(batch)
    return batch


def undo_batch(batch_id: str) -> tuple[int, list[str]]:
    """Macht eine Verschiebe-Aktion rückgängig. Liefert (anzahl_ok, fehler)."""
    entries = load_journal()
    batch = next((b for b in entries if b.get("id") == batch_id), None)
    if batch is None:
        return 0, ["Eintrag nicht gefunden"]
    if batch.get("kind") != "move":
        return 0, ["Papierkorb-Aktionen bitte über den Windows-Papierkorb wiederherstellen"]
    if batch.get("undone"):
        return 0, ["Wurde bereits rückgängig gemacht"]
    ok, errs = 0, []
    remaining = []
    for op in reversed(batch["ops"]):
        src, dst = op["src"], op["dst"]
        try:
            if not os.path.exists(dst):
                raise OSError("Datei ist nicht mehr am Ziel")
            if os.path.exists(src):
                raise OSError("am Ursprungsort liegt inzwischen eine andere Datei")
            os.makedirs(os.path.dirname(src), exist_ok=True)
            shutil.move(dst, src)
            ok += 1
            _remove_empty_dir(os.path.dirname(dst))
        except OSError as ex:
            errs.append(f"{os.path.basename(src)}: {ex}")
            remaining.append(op)
    batch["ops"] = list(reversed(remaining))
    batch["undone"] = not remaining
    batch["undo_errors"] = errs
    _update_batch(batch)
    return ok, errs


def _remove_empty_dir(d: str) -> None:
    try:
        os.rmdir(d)  # schlägt fehl, wenn nicht leer -> gewollt
    except OSError:
        pass


# ---------------------------------------------------------------- Papierkorb

def send_to_trash(paths: list[str]) -> tuple[list[str], list[str]]:
    """Verschiebt Dateien in den Windows-Papierkorb. Niemals endgültig löschen.
    Liefert (erfolgreich, fehler)."""
    if os.name != "nt":
        raise RuntimeError("Papierkorb wird nur unter Windows unterstützt")
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE = 3
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040            # = Papierkorb
    FOF_NOERRORUI = 0x0400
    FOF_WANTNUKEWARNING = 0x4000      # warnt, falls der Papierkorb die Datei nicht aufnehmen kann

    done, errs = [], []
    batch = {"id": uuid.uuid4().hex, "time": time.time(), "kind": "trash",
             "label": "In den Papierkorb", "undone": False, "ops": [], "errors": []}
    for p in paths:
        p = os.path.abspath(p)
        if not os.path.exists(p):
            errs.append(f"{p}: existiert nicht")
            continue
        op = SHFILEOPSTRUCTW()
        op.wFunc = FO_DELETE
        op.pFrom = p + "\0"  # doppelt nullterminiert
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if rc == 0 and not op.fAnyOperationsAborted and not os.path.exists(p):
            done.append(p)
            batch["ops"].append({"src": p})
        else:
            errs.append(f"{p}: Papierkorb fehlgeschlagen (Code {rc})")
    batch["errors"] = errs
    if batch["ops"] or errs:
        _append_batch(batch)
    return done, errs
