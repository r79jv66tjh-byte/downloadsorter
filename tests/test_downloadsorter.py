"""Tests mit einem temporären Testordner – der echte Downloads-Ordner wird nie angefasst."""
import io
import os
import sys
import tempfile
import time
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Protokoll/Config in einen Testordner umleiten, bevor das Modul benutzt wird
_HOME = tempfile.mkdtemp(prefix="ds_home_")
os.environ["DOWNLOADSORTER_HOME"] = _HOME

from downloadsorter import actions as A  # noqa: E402
from downloadsorter import categories as C  # noqa: E402
from downloadsorter import scanner as S  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
PDF = b"%PDF-1.7\n" + b"x" * 40
EXE = b"MZ" + b"\x90" * 100
MP3 = b"ID3\x04\x00" + b"\x00" * 60
MKV = b"\x1a\x45\xdf\xa3" + b"\x00" * 60
TORRENT = b"d8:announce35:udp://tracker.example:1337/announce4:infod" + b"e" * 10


def zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return buf.getvalue()


def write(path, data: bytes, age_days=0.0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    if age_days:
        t = time.time() - age_days * 86400
        os.utime(path, (t, t))


class TestFolder(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ds_test_")
        r = self.root
        write(os.path.join(r, "foto.png"), PNG)
        write(os.path.join(r, "bild_ohne_endung"), PNG + b"!")                 # Bild ohne Endung
        write(os.path.join(r, "rechnung.txt"), PDF)                     # PDF mit falscher Endung
        write(os.path.join(r, "setup.exe"), EXE)
        write(os.path.join(r, "lied.mp3"), MP3)
        write(os.path.join(r, "film.mkv"), MKV)
        write(os.path.join(r, "linux.torrent"), TORRENT)
        write(os.path.join(r, "skript.py"), b"print('hallo')\n")
        write(os.path.join(r, "notizen.md"), b"# Hallo\n")
        write(os.path.join(r, "brief.docx"), zip_bytes({"word/document.xml": "<x/>", "[Content_Types].xml": "x"}))
        write(os.path.join(r, "halb.crdownload"), b"abc", age_days=2)
        write(os.path.join(r, "laeuft_noch.part"), b"abc")             # frisch -> kein Vorschlag
        write(os.path.join(r, "foto (1).png"), PNG)                     # Duplikat
        write(os.path.join(r, "paket.zip"), zip_bytes({"paket/a.txt": "A", "paket/b.txt": "B"}))
        write(os.path.join(r, "paket", "a.txt"), b"A")                  # schon entpackt
        write(os.path.join(r, "paket", "b.txt"), b"B")
        write(os.path.join(r, "neu.zip"), zip_bytes({"x.txt": "X"}))    # nicht entpackt
        write(os.path.join(r, "unbekannt.xyz"), b"\x01\x02\x03")
        # Zone.Identifier wie vom Browser geschrieben
        with open(os.path.join(r, "setup.exe") + ":Zone.Identifier", "w") as f:
            f.write("[ZoneTransfer]\nZoneId=3\nReferrerUrl=https://example.org/download\n"
                    "HostUrl=https://cdn.example.org/setup.exe\n")
        self.res = S.scan(r, installer_age_days=0)
        self.files = {f.name: f for f in self.res.files}

    def test_categories(self):
        expect = {"foto.png": C.IMAGE, "bild_ohne_endung": C.IMAGE, "rechnung.txt": C.DOCUMENT,
                  "setup.exe": C.INSTALLER, "lied.mp3": C.MUSIC, "film.mkv": C.VIDEO,
                  "linux.torrent": C.TORRENT, "skript.py": C.CODE, "notizen.md": C.DOCUMENT,
                  "brief.docx": C.DOCUMENT, "paket.zip": C.ARCHIVE, "unbekannt.xyz": C.OTHER,
                  "halb.crdownload": C.OTHER}
        for name, cat in expect.items():
            self.assertEqual(self.files[name].category, cat, name)
        self.assertTrue(self.files["rechnung.txt"].ext_mismatch)
        self.assertFalse(self.files["brief.docx"].ext_mismatch)
        self.assertNotIn("a.txt", self.files)  # nicht rekursiv

    def test_origin(self):
        f = self.files["setup.exe"]
        self.assertEqual(f.host_url, "https://cdn.example.org/setup.exe")
        self.assertEqual(f.referrer_url, "https://example.org/download")
        self.assertEqual(f.origin, "cdn.example.org")
        self.assertEqual(f.zone_id, "3")

    def test_duplicates_and_suggestions(self):
        self.assertEqual(len(self.res.duplicates), 1)
        self.assertEqual({f.name for f in self.res.duplicates[0]}, {"foto.png", "foto (1).png"})
        kinds = {(s.kind, s.file.name) for s in self.res.suggestions}
        self.assertIn(("incomplete", "halb.crdownload"), kinds)
        self.assertNotIn(("incomplete", "laeuft_noch.part"), kinds)
        self.assertIn(("installer", "setup.exe"), kinds)
        self.assertIn(("extracted", "paket.zip"), kinds)
        self.assertNotIn(("extracted", "neu.zip"), kinds)
        self.assertEqual(sum(1 for k, _ in kinds if k == "duplicate"), 1)

    def test_sort_and_undo(self):
        cfg = A.load_config()
        cfg["targets"][C.MUSIC] = os.path.join(self.root, "_fest", "Musik")  # festes Ziel
        before = {os.path.join(dp, n) for dp, _, ns in os.walk(self.root) for n in ns}
        # Namenskonflikt erzwingen
        write(os.path.join(self.root, C.IMAGE, "foto.png"), b"anderes bild")
        items = [(f.path, A.target_dir(f.category, self.root, cfg)) for f in self.res.files]
        plan = A.plan_moves(items)
        # Vorschau ändert nichts
        self.assertTrue(os.path.exists(os.path.join(self.root, "foto.png")))
        renamed = [m for m in plan if m.renamed]
        self.assertTrue(renamed)
        dsts = [m.dst.lower() for m in plan if not m.problem]
        self.assertEqual(len(dsts), len(set(dsts)))  # keine Zielkollision
        self.assertNotIn(os.path.join(self.root, C.IMAGE, "foto.png").lower(), dsts)

        batch = A.execute_moves(plan, "Test")
        self.assertEqual(batch["errors"], [])
        self.assertTrue(os.path.isfile(os.path.join(self.root, "_fest", "Musik", "lied.mp3")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, C.DOCUMENT, "rechnung.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "setup.exe")))
        # Zone.Identifier wandert mit (gleiches Laufwerk)
        moved = S.read_zone_identifier(os.path.join(self.root, C.INSTALLER, "setup.exe"))
        self.assertEqual(moved.get("HostUrl"), "https://cdn.example.org/setup.exe")
        self.assertIn(batch["id"], [b["id"] for b in A.load_journal()])

        ok, errs = A.undo_batch(batch["id"])
        self.assertEqual(errs, [])
        self.assertEqual(ok, len(self.res.files))
        after = {os.path.join(dp, n) for dp, _, ns in os.walk(self.root) for n in ns}
        blocker = os.path.join(self.root, C.IMAGE, "foto.png")
        with open(blocker, "rb") as f:
            self.assertEqual(f.read(), b"anderes bild")  # vorhandene Datei nie überschrieben
        after.discard(blocker)
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(os.path.join(self.root, C.MUSIC)))  # leere Zielordner entfernt
        self.assertTrue(next(b for b in A.load_journal() if b["id"] == batch["id"])["undone"])
        self.assertEqual(A.undo_batch(batch["id"])[0], 0)  # doppelt geht nicht

    def test_undo_does_not_overwrite(self):
        src = os.path.join(self.root, "skript.py")
        batch = A.execute_moves(A.plan_moves([(src, os.path.join(self.root, "X"))]))
        write(src, b"neue Datei am alten Ort")
        ok, errs = A.undo_batch(batch["id"])
        self.assertEqual(ok, 0)
        self.assertEqual(len(errs), 1)
        with open(src, "rb") as f:
            self.assertEqual(f.read(), b"neue Datei am alten Ort")
        self.assertTrue(os.path.exists(os.path.join(self.root, "X", "skript.py")))

    @unittest.skipUnless(os.name == "nt", "Windows-Papierkorb")
    def test_trash(self):
        p = os.path.join(self.root, "unbekannt.xyz")
        done, errs = A.send_to_trash([p])
        self.assertEqual((len(done), errs), (1, []))
        self.assertFalse(os.path.exists(p))


if __name__ == "__main__":
    unittest.main()
