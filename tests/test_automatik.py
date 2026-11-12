"""Regeln + Hintergrundmodus, nur in temporären Testordnern."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.test_downloadsorter import EXE, PNG, write  # noqa: E402  (setzt DOWNLOADSORTER_HOME)

from downloadsorter import actions as A  # noqa: E402
from downloadsorter import categories as C  # noqa: E402
from downloadsorter import rules as R  # noqa: E402
from downloadsorter import scanner as S  # noqa: E402
from downloadsorter import watcher as W  # noqa: E402

JAR = b"PK\x05\x06" + b"\x00" * 18


def zone(path, host):
    with open(path + ":Zone.Identifier", "w") as f:
        f.write(f"[ZoneTransfer]\nZoneId=3\nHostUrl={host}\n")


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class TestRules(unittest.TestCase):
    def test_matching(self):
        root = tempfile.mkdtemp(prefix="ds_rule_")
        p = os.path.join(root, "coolmod-1.20.jar")
        write(p, JAR)
        zone(p, "https://mediafilez.forgecdn.net/files/coolmod-1.20.jar")
        f = S.analyze(p)
        mod = R.Rule(name="Mods", extensions="jar", origin_contains="forgecdn.net", target="Mods")
        self.assertTrue(mod.matches(f))
        self.assertFalse(R.Rule(extensions="zip", target="X").matches(f))
        self.assertFalse(R.Rule(origin_contains="github.com", target="X").matches(f))
        self.assertFalse(R.Rule(extensions="jar", target="X", enabled=False).matches(f))
        self.assertTrue(R.Rule(name_pattern="COOL*.jar", target="X").matches(f))
        self.assertFalse(R.Rule(extensions="jar", target="").matches(f))  # ohne Ziel nie
        dest = R.destination(f, [R.Rule(extensions="jar", target="", name="leer"), mod], root, {}, False)
        self.assertEqual(dest[0], os.path.join(root, "Mods"))
        # Laden/Speichern
        cfg = {}
        self.assertEqual(R.load_rules(cfg)[0].enabled, False)  # Beispielregel ist aus
        R.save_rules(cfg, [mod])
        self.assertEqual(R.load_rules(A.load_config())[0], mod)


class TestWatcher(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ds_watch_")
        write(os.path.join(self.root, "alt.exe"), EXE)          # vorher schon da
        self.cfg = {"targets": {}, "rules": [
            {"name": "Mods", "extensions": "jar", "origin_contains": "forgecdn", "target": "Mods"}],
            "auto": {"settle_seconds": 5, "only_new": True, "fallback_category": False}}
        self.clock = Clock()
        self.events = []
        self.w = W.Watcher(self.root, self.cfg, on_event=lambda k, t: self.events.append((k, t)), clock=self.clock)

    def tick(self, dt=3):
        self.clock.t += dt
        return self.w.step()

    def test_new_files_sorted_after_settle(self):
        p = os.path.join(self.root, "mod.jar")
        write(p, JAR)
        zone(p, "https://mediafilez.forgecdn.net/mod.jar")
        write(os.path.join(self.root, "bild.png"), PNG)                     # keine Regel -> bleibt
        write(os.path.join(self.root, "halb.jar.crdownload"), b"x")         # unfertig -> bleibt
        self.assertEqual(self.tick(), [])          # erstmals gesehen
        self.assertEqual(self.tick(), [])          # erst 3 s stabil
        batches = self.tick()                      # 6 s stabil -> sortieren
        self.assertEqual(len(batches), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "Mods", "mod.jar")))
        for n in ("alt.exe", "bild.png", "halb.jar.crdownload"):
            self.assertTrue(os.path.isfile(os.path.join(self.root, n)), n)
        self.assertIn("skip", [k for k, _ in self.events])
        self.assertEqual(self.tick(10), [])        # nichts doppelt
        # Rückgängig über das normale Protokoll
        ok, errs = A.undo_batch(batches[0]["id"])
        self.assertEqual((ok, errs), (1, []))
        self.assertTrue(os.path.isfile(p))
        # zurückgelegte Datei wird nicht erneut einsortiert (ist bekannt)
        self.tick(); self.tick(); self.assertEqual(self.tick(), [])
        self.assertTrue(os.path.isfile(p))

    def test_growing_file_waits(self):
        p = os.path.join(self.root, "gross.jar")
        write(p, JAR)
        zone(p, "https://forgecdn.net/x")
        for i in range(4):
            with open(p, "ab") as f:              # Datei wächst noch
                f.write(b"\x00" * 10)
            os.utime(p, (self.clock.t + i, self.clock.t + i))
            self.assertEqual(self.tick(), [])
        self.tick()
        self.assertEqual(len(self.tick()), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "Mods", "gross.jar")))

    def test_fallback_and_conflict(self):
        self.cfg["auto"]["fallback_category"] = True
        write(os.path.join(self.root, C.IMAGE, "bild.png"), b"vorhanden")
        write(os.path.join(self.root, "bild.png"), PNG)
        for _ in range(3):
            self.tick()
        self.assertTrue(os.path.isfile(os.path.join(self.root, C.IMAGE, "bild (1).png")))
        with open(os.path.join(self.root, C.IMAGE, "bild.png"), "rb") as f:
            self.assertEqual(f.read(), b"vorhanden")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "alt.exe")))  # only_new

    @unittest.skipUnless(os.name == "nt", "Windows-Sperre")
    def test_locked_file_waits(self):
        p = os.path.join(self.root, "offen.jar")
        write(p, JAR)
        zone(p, "https://forgecdn.net/x")
        import msvcrt  # noqa: F401
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = ctypes.c_void_p
        h = k32.CreateFileW(p, 0x80000000, 0x1, None, 3, 0x80, None)  # lesen, ohne FILE_SHARE_DELETE
        try:
            for _ in range(4):
                self.assertEqual(self.tick(), [])
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
        self.assertEqual(len(self.tick()), 1)

    def test_single_instance(self):
        a, b = W.SingleInstance(), W.SingleInstance()
        self.assertTrue(a.acquire())
        try:
            if os.name == "nt":
                self.assertFalse(b.acquire())
                self.assertTrue(W.background_running())
        finally:
            a.release()
        self.assertFalse(W.background_running())


if __name__ == "__main__":
    unittest.main()
