"""Claude-Schnittstelle: jeder Befehl aus toolhub.json liefert gültiges JSON (nur Testordner)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.test_downloadsorter import EXE, PDF, PNG, zip_bytes, write  # noqa: E402

MANIFEST = json.load(open(os.path.join(ROOT, "toolhub.json"), encoding="utf-8"))
COMMANDS = {c["name"]: c for c in MANIFEST["commands"]}


def run(cmd: str, *extra: str, home: str):
    argv = list(COMMANDS[cmd]["run"])
    if argv[0] == "python":
        argv[0] = sys.executable
    env = dict(os.environ, DOWNLOADSORTER_HOME=home, PYTHONIOENCODING="utf-8")
    p = subprocess.run(argv + list(extra), cwd=ROOT, capture_output=True, env=env, timeout=120)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


class TestCli(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ds_cli_home_")
        self.dir = tempfile.mkdtemp(prefix="ds_cli_")
        d = self.dir
        write(os.path.join(d, "foto.png"), PNG)
        write(os.path.join(d, "foto (1).png"), PNG)
        write(os.path.join(d, "rechnung.pdf"), PDF)
        write(os.path.join(d, "setup.exe"), EXE)
        write(os.path.join(d, "alt.crdownload"), b"x", age_days=3)
        write(os.path.join(d, "paket.zip"), zip_bytes({"a.txt": "A"}))
        write(os.path.join(d, "paket", "a.txt"), b"A")
        with open(os.path.join(d, "setup.exe") + ":Zone.Identifier", "w") as f:
            f.write("[ZoneTransfer]\nZoneId=3\nHostUrl=https://example.org/setup.exe\n")
        self.snapshot = self._files()

    def _files(self):
        return sorted(os.path.relpath(os.path.join(dp, n), self.dir) for dp, _, ns in os.walk(self.dir) for n in ns)

    def ok(self, cmd, *extra):
        code, out, err = run(cmd, *extra, home=self.home)
        self.assertEqual(code, 0, f"{cmd}: {err}")
        return json.loads(out)

    def test_manifest_shape(self):
        self.assertEqual(MANIFEST["name"], "DownloadSorter")
        for c in MANIFEST["commands"]:
            self.assertTrue(c["description"])
            self.assertEqual(c["run"][:2], ["python", "cli.py"])
            for a in c["args"]:
                self.assertIn(a["type"], {"string", "integer", "number", "boolean", "array"})
                self.assertTrue(a["flag"].startswith("--"))
            if not c["readonly"]:
                self.assertIn("--dry-run", [a["flag"] for a in c["args"]], c["name"])
        toolhub = Path(ROOT).parent / "ToolHub"
        if (toolhub / "toolhub" / "manifest.py").exists():  # mit dem echten ToolHub-Lader prüfen
            sys.path.insert(0, str(toolhub))
            try:
                from toolhub import manifest as M
                m = M.load(Path(ROOT) / "toolhub.json")
                self.assertEqual(len(m.commands), len(COMMANDS))
            finally:
                sys.path.remove(str(toolhub))

    def test_every_command_returns_json(self):
        sample = {"folder": self.dir, "file": "setup.exe", "category": "Bilder", "limit": "5",
                  "installer_age": "0", "sort": "age", "name": "foto", "mode": "category",
                  "target": "Ziel", "id": None}
        for name, c in COMMANDS.items():
            extra = []
            for a in c["args"]:
                if a["type"] == "boolean":
                    if a["name"] == "dry_run":
                        extra.append(a["flag"])     # schreibende Befehle nur als Vorschau
                    continue
                v = sample.get(a["name"])
                if v is not None and (a["required"] or name != "sort" or a["name"] in ("folder", "category")):
                    extra += [a["flag"], v]
            if name == "undo":
                code, out, err = run(name, *extra, home=self.home)
                self.assertEqual(code, 1)          # noch nichts zum Rückgängigmachen
                self.assertTrue(err and not out)
                continue
            data = self.ok(name, *extra)
            self.assertIsInstance(data, dict, name)
        self.assertEqual(self._files(), self.snapshot)  # nichts verändert

    def test_read_results(self):
        scan = self.ok("scan", "--folder", self.dir)
        cats = {c["category"]: c["count"] for c in scan["categories"]}
        self.assertEqual((cats["Bilder"], cats["Installer"], cats["Dokumente"]), (2, 1, 1))
        self.assertEqual(scan["duplicate_groups"], 1)
        sug = self.ok("suggestions", "--folder", self.dir, "--installer-age", "0")
        kinds = {g["kind"] for g in sug["groups"]}
        self.assertEqual(kinds, {"installer", "incomplete", "extracted", "duplicate"})
        dup = self.ok("duplicates", "--folder", self.dir)
        self.assertEqual(dup["groups"][0]["copies"], 2)
        org = self.ok("origin", "--folder", self.dir, "--file", "setup.exe")
        self.assertEqual(org["host_url"], "https://example.org/setup.exe")
        self.assertEqual(org["zone"], "Internet")
        files = self.ok("files", "--folder", self.dir, "--category", "bilder")
        self.assertEqual(files["matched"], 2)
        rules = self.ok("rules")
        self.assertIn("automation", rules)

    def test_sort_dry_run_then_real_then_undo(self):
        prev = self.ok("sort", "--folder", self.dir, "--category", "Bilder", "--dry-run")
        self.assertEqual(prev["planned_moves"], 2)
        self.assertEqual(self._files(), self.snapshot)
        done = self.ok("sort", "--folder", self.dir, "--category", "Bilder")
        self.assertEqual(done["moved"], 2)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "Bilder", "foto.png")))
        hist = self.ok("history")
        self.assertEqual(hist["entries"][0]["id"], done["undo_id"])
        self.assertTrue(hist["entries"][0]["undoable"])
        # festes Ziel
        t = self.ok("sort", "--folder", self.dir, "--file", "rechnung.pdf", "--target", "Belege")
        self.assertEqual(t["moved"], 1)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "Belege", "rechnung.pdf")))
        # Rückgängig: Vorschau ändert nichts, dann echt (neueste zuerst, dann per ID)
        pv = self.ok("undo", "--dry-run")
        self.assertEqual(pv["planned"], 1)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "Belege", "rechnung.pdf")))
        self.assertEqual(self.ok("undo")["restored"], 1)
        self.assertEqual(self.ok("undo", "--id", done["undo_id"])["restored"], 2)
        self.assertEqual(self._files(), self.snapshot)

    def test_errors(self):
        for args in (("origin", "--folder", self.dir, "--file", "gibtsnicht.exe"),
                     ("sort", "--folder", self.dir),                       # nicht eingegrenzt
                     ("scan", "--folder", os.path.join(self.dir, "fehlt")),
                     ("files", "--folder", self.dir, "--category", "Quatsch"),
                     ("undo", "--id", "unbekannt")):
            code, out, err = run(*args, home=self.home)
            self.assertNotEqual(code, 0, args)
            self.assertEqual(out, "", args)
            self.assertIn("Fehler", err, args)
        code, out, err = run("sort", "--unbekannt", home=self.home)
        self.assertNotEqual(code, 0)
        self.assertEqual(self._files(), self.snapshot)


if __name__ == "__main__":
    unittest.main()
