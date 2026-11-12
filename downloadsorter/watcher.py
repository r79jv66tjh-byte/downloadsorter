"""Hintergrundmodus: sortiert neue Downloads automatisch nach eigenen Regeln.

Sicherheit:
- Nur Dateien, die nach dem Start neu dazukommen (vorhandene bleiben liegen, außer only_new=False).
- Nur fertige Downloads: keine .crdownload/.part/..., Größe und Änderungszeit eine Weile stabil,
  Datei nicht von einem anderen Programm gesperrt.
- Jede Verschiebung landet im Protokoll und ist unter „Protokoll / Rückgängig“ zurücknehmbar.
- Nie löschen, nie überschreiben.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import traceback

from . import actions as A
from . import categories as C
from . import rules as R
from . import scanner as S

MUTEX_NAME = "Local\\DownloadSorterAutomatik"
DEFAULT_AUTO = {"enabled": False, "folder": "", "fallback_category": False, "only_new": True,
                "settle_seconds": 8, "interval_seconds": 3}


def auto_cfg(cfg: dict) -> dict:
    a = dict(DEFAULT_AUTO)
    a.update(cfg.get("auto") or {})
    return a


def _ignored_name(name: str) -> bool:
    low = name.lower()
    return (low == "desktop.ini" or low.startswith("~$") or low.startswith(".")
            or C.extension(name) in C.INCOMPLETE_EXT)


def _locked(path: str) -> bool:
    """True, wenn ein anderes Programm die Datei noch geöffnet hält (Rename auf sich selbst schlägt fehl)."""
    try:
        os.rename(path, path)
        return False
    except OSError:
        return True


class Watcher:
    def __init__(self, root: str, cfg: dict, on_event=None, clock=time.time):
        self.root = root
        self.cfg = cfg
        self.on_event = on_event or (lambda kind, text: None)
        self.clock = clock
        self.ignored: set[str] = set()
        self.pending: dict[str, tuple[tuple[int, float], float]] = {}  # pfad -> (signatur, stabil seit)
        # schon behandelte Dateien (Name, Größe, Änderungszeit): wer eine Automatik-Verschiebung
        # rückgängig macht, bekommt die Datei nicht sofort wieder wegsortiert
        self.handled: set[tuple[str, int, float]] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        a = auto_cfg(cfg)
        if a["only_new"]:
            self.ignored = {e.path for e in self._entries()}

    # -------------------------------------------------------------- Kern (synchron testbar)
    def _entries(self):
        try:
            with os.scandir(self.root) as it:
                return [e for e in it if e.is_file(follow_symlinks=False)]
        except OSError:
            return []

    def step(self) -> list[dict]:
        """Ein Durchlauf. Liefert die ausgeführten Verschiebe-Batches."""
        a = auto_cfg(self.cfg)
        rules = R.load_rules(self.cfg)
        now = self.clock()
        present = set()
        batches = []
        for e in self._entries():
            present.add(e.path)
            if e.path in self.ignored or _ignored_name(e.name):
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            sig = (st.st_size, st.st_mtime)
            if (e.name.lower(), *sig) in self.handled:
                self.ignored.add(e.path)
                continue
            old = self.pending.get(e.path)
            if old is None or old[0] != sig:
                self.pending[e.path] = (sig, now)
                continue
            if now - old[1] < a["settle_seconds"] or _locked(e.path):
                continue
            del self.pending[e.path]
            self.ignored.add(e.path)  # egal was passiert: nicht noch einmal anfassen
            self.handled.add((e.name.lower(), *sig))
            try:
                fi = S.analyze(e.path)
                dest = R.destination(fi, rules, self.root, self.cfg, a["fallback_category"])
                if not dest:
                    self.on_event("skip", f"{e.name}: keine Regel passt – bleibt liegen")
                    continue
                target_dir, reason = dest
                plan = A.plan_moves([(e.path, target_dir)])
                if plan[0].problem:
                    self.on_event("skip", f"{e.name}: {plan[0].problem}")
                    continue
                batch = A.execute_moves(plan, f"Automatisch ({reason})")
                batches.append(batch)
                if batch["ops"]:
                    self.on_event("moved", f"{e.name} → {batch['ops'][0]['dst']}  [{reason}]")
                for err in batch["errors"]:
                    self.on_event("error", err)
            except Exception as ex:  # noqa: BLE001 - Hintergrund darf nicht abstürzen
                self.on_event("error", f"{e.name}: {ex}")
        # verschwundene Dateien vergessen (gleicher Name kann später neu kommen)
        self.ignored &= present
        for p in list(self.pending):
            if p not in present:
                del self.pending[p]
        return batches

    # -------------------------------------------------------------- Thread
    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            self.step()
            self._stop.wait(auto_cfg(self.cfg)["interval_seconds"])

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


# ------------------------------------------------------------------ Einzelinstanz

class SingleInstance:
    """Benannter Mutex: verhindert, dass GUI-Automatik und Hintergrundprozess gleichzeitig sortieren."""

    def __init__(self):
        self.handle = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        h = k32.CreateMutexW(None, False, MUTEX_NAME)
        if not h:
            return False
        if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            k32.CloseHandle(ctypes.c_void_p(h))
            return False
        self.handle = h
        return True

    def release(self):
        if self.handle and os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.handle))
        self.handle = None


def background_running() -> bool:
    lock = SingleInstance()
    if lock.acquire():
        lock.release()
        return False
    return True


# ------------------------------------------------------------------ Kopfloser Hintergrundprozess

def stop_flag() -> str:
    return os.path.join(A.app_dir(), "hintergrund-stoppen")


def log_path() -> str:
    return os.path.join(A.app_dir(), "hintergrund.log")


def _log(text: str):
    try:
        if os.path.exists(log_path()) and os.path.getsize(log_path()) > 1_000_000:
            os.replace(log_path(), log_path() + ".alt")
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


def run_headless():
    lock = SingleInstance()
    if not lock.acquire():
        _log("Automatik läuft bereits – zweiter Start beendet")
        return
    try:
        if os.path.exists(stop_flag()):
            os.remove(stop_flag())
        cfg = A.load_config()
        a = auto_cfg(cfg)
        root = a["folder"] or cfg.get("last_folder") or S.default_downloads()
        if not os.path.isdir(root):
            _log(f"Ordner fehlt: {root}")
            return
        _log(f"Hintergrundmodus gestartet für {root}")
        w = Watcher(root, cfg, on_event=lambda kind, text: _log(f"[{kind}] {text}"))
        cfg_path = os.path.join(A.app_dir(), "config.json")
        cfg_mtime = os.path.getmtime(cfg_path) if os.path.exists(cfg_path) else 0
        while not os.path.exists(stop_flag()):
            # Regeländerungen aus der Oberfläche übernehmen
            m = os.path.getmtime(cfg_path) if os.path.exists(cfg_path) else 0
            if m != cfg_mtime:
                cfg_mtime = m
                w.cfg = A.load_config()
                if not auto_cfg(w.cfg)["enabled"]:
                    _log("Automatik in den Einstellungen ausgeschaltet – beende")
                    break
            w.step()
            time.sleep(auto_cfg(w.cfg)["interval_seconds"])
        _log("Hintergrundmodus beendet")
    except Exception:  # noqa: BLE001
        _log("Absturz:\n" + traceback.format_exc())
    finally:
        try:
            os.remove(stop_flag())
        except OSError:
            pass
        lock.release()


def request_background_stop():
    with open(stop_flag(), "w") as f:
        f.write("stop")


def _launcher() -> tuple[str, str]:
    exe = sys.executable
    w = os.path.join(os.path.dirname(exe), "pythonw.exe")
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "DownloadSorter.pyw")
    return (w if os.path.exists(w) else exe), script


def spawn_background():
    exe, script = _launcher()
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    subprocess.Popen([exe, script, "--hintergrund"], creationflags=flags, close_fds=True,
                     cwd=os.path.dirname(script))


# ------------------------------------------------------------------ Autostart (Verknüpfung im Autostart-Ordner)

def startup_shortcut() -> str:
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs",
                        "Startup", "DownloadSorter Automatik.lnk")


def autostart_enabled() -> bool:
    return os.path.exists(startup_shortcut())


def set_autostart(on: bool):
    lnk = startup_shortcut()
    if not on:
        if os.path.exists(lnk):
            os.remove(lnk)  # nur unsere eigene Verknüpfung
        return
    exe, script = _launcher()
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:DS_LNK);"
          "$s.TargetPath=$env:DS_EXE;$s.Arguments='\"'+$env:DS_SCRIPT+'\" --hintergrund';"
          "$s.WorkingDirectory=$env:DS_DIR;$s.Description='DownloadSorter: neue Downloads automatisch sortieren';"
          "$s.Save()")
    env = dict(os.environ, DS_LNK=lnk, DS_EXE=exe, DS_SCRIPT=script, DS_DIR=os.path.dirname(script))
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env, check=True,
                   creationflags=0x08000000 if os.name == "nt" else 0)  # CREATE_NO_WINDOW
