"""tkinter-Oberfläche für DownloadSorter."""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import actions as A
from . import categories as C
from . import rules as R
from . import scanner as S
from . import watcher as W

CHECK, UNCHECK = "☑", "☐"


def make_tree(parent, columns, headings, widths, show="headings", height=12):
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=columns, show=show, height=height, selectmode="extended")
    for c, h, w in zip(columns, headings, widths):
        tree.heading(c, text=h)
        tree.column(c, width=w, stretch=(w >= 200), anchor="e" if h in ("Größe", "Anzahl", "Anteil") else "w")
    ys = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    xs = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
    tree.grid(row=0, column=0, sticky="nsew")
    ys.grid(row=0, column=1, sticky="ns")
    xs.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return frame, tree


def reveal_in_explorer(path: str) -> None:
    if os.path.exists(path):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif os.path.isdir(os.path.dirname(path)):
        os.startfile(os.path.dirname(path))


def fmt_time(t: float) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(t))


# ====================================================================== Vorschau-Dialoge

class PreviewDialog(tk.Toplevel):
    """Zeigt geplante Änderungen; erst mit Klick auf 'Ausführen' passiert etwas."""

    def __init__(self, master, title, intro, columns, headings, widths, rows, ok_text):
        super().__init__(master)
        self.title(title)
        self.transient(master)
        self.result = False
        self.geometry("1000x520")
        ttk.Label(self, text=intro, wraplength=960, justify="left").pack(fill="x", padx=10, pady=(10, 4))
        frame, tree = make_tree(self, columns, headings, widths)
        frame.pack(fill="both", expand=True, padx=10, pady=4)
        tree.tag_configure("warn", foreground="#b36b00")
        tree.tag_configure("bad", foreground="#a00000")
        for values, tag in rows:
            tree.insert("", "end", values=values, tags=(tag,) if tag else ())
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=10)
        ttk.Button(bar, text="Abbrechen", command=self.destroy).pack(side="right")
        b = ttk.Button(bar, text=ok_text, command=self._ok)
        b.pack(side="right", padx=6)
        if not any(tag != "bad" for _, tag in rows):
            b.state(["disabled"])
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.wait_window()

    def _ok(self):
        self.result = True
        self.destroy()


class SettingsDialog(tk.Toplevel):
    def __init__(self, master, cfg: dict):
        super().__init__(master)
        self.title("Einstellungen – Sortierziele")
        self.transient(master)
        self.cfg = cfg
        self.saved = False
        ttk.Label(self, text="Ziel je Kategorie. Leer = Unterordner mit dem Kategorienamen im gescannten Ordner;\n"
                             "ein Name = Unterordner mit diesem Namen; ein absoluter Pfad = festes Ziel "
                             "(z. B. D:\\Bilder).", justify="left").grid(row=0, column=0, columnspan=3,
                                                                          sticky="w", padx=10, pady=10)
        self.vars = {}
        for i, cat in enumerate(C.CATEGORIES, 1):
            ttk.Label(self, text=cat).grid(row=i, column=0, sticky="w", padx=10, pady=2)
            v = tk.StringVar(value=cfg["targets"].get(cat, ""))
            self.vars[cat] = v
            ttk.Entry(self, textvariable=v, width=60).grid(row=i, column=1, sticky="ew", pady=2)
            ttk.Button(self, text="…", width=3,
                       command=lambda v=v: self._browse(v)).grid(row=i, column=2, padx=(4, 10))
        r = len(C.CATEGORIES) + 1
        ttk.Label(self, text="Installer gelten als alt ab (Tage):").grid(row=r, column=0, sticky="w", padx=10, pady=8)
        self.age = tk.IntVar(value=cfg.get("installer_age_days", 30))
        ttk.Spinbox(self, from_=1, to=3650, textvariable=self.age, width=8).grid(row=r, column=1, sticky="w")
        bar = ttk.Frame(self)
        bar.grid(row=r + 1, column=0, columnspan=3, sticky="e", padx=10, pady=10)
        ttk.Button(bar, text="Speichern", command=self._save).pack(side="left", padx=4)
        ttk.Button(bar, text="Abbrechen", command=self.destroy).pack(side="left")
        self.columnconfigure(1, weight=1)
        self.grab_set()
        self.wait_window()

    def _browse(self, var):
        d = filedialog.askdirectory(parent=self, title="Zielordner wählen")
        if d:
            var.set(os.path.normpath(d))

    def _save(self):
        for cat, v in self.vars.items():
            self.cfg["targets"][cat] = v.get().strip()
        try:
            self.cfg["installer_age_days"] = max(1, int(self.age.get()))
        except (tk.TclError, ValueError):
            pass
        A.save_config(self.cfg)
        self.saved = True
        self.destroy()


class RuleDialog(tk.Toplevel):
    def __init__(self, master, rule: R.Rule):
        super().__init__(master)
        self.title("Regel bearbeiten")
        self.transient(master)
        self.rule = None
        self.v = {
            "name": tk.StringVar(value=rule.name), "enabled": tk.BooleanVar(value=rule.enabled),
            "category": tk.StringVar(value=rule.category or R.ANY), "extensions": tk.StringVar(value=rule.extensions),
            "name_pattern": tk.StringVar(value=rule.name_pattern),
            "origin_contains": tk.StringVar(value=rule.origin_contains), "target": tk.StringVar(value=rule.target),
        }
        rows = [("Name der Regel", "name", None), ("Kategorie", "category", [R.ANY] + C.CATEGORIES),
                ("Endungen (z. B. jar, zip)", "extensions", None),
                ("Dateiname wie (z. B. *forge*.jar)", "name_pattern", None),
                ("Herkunft enthält (z. B. forgecdn.net)", "origin_contains", None),
                ("Ziel: Unterordner oder fester Pfad", "target", None)]
        ttk.Label(self, text="Alle ausgefüllten Bedingungen müssen passen. Leere Felder = egal.\n"
                             "Die erste passende Regel (von oben) gewinnt.", justify="left"
                  ).grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=10)
        for i, (label, key, values) in enumerate(rows, 1):
            ttk.Label(self, text=label).grid(row=i, column=0, sticky="w", padx=10, pady=3)
            if values:
                ttk.Combobox(self, textvariable=self.v[key], values=values, state="readonly", width=20
                             ).grid(row=i, column=1, sticky="w")
            else:
                ttk.Entry(self, textvariable=self.v[key], width=50).grid(row=i, column=1, sticky="ew")
        ttk.Button(self, text="…", width=3, command=self._browse).grid(row=len(rows), column=2, padx=(4, 10))
        ttk.Checkbutton(self, text="Regel aktiv", variable=self.v["enabled"]).grid(
            row=len(rows) + 1, column=1, sticky="w", pady=6)
        bar = ttk.Frame(self)
        bar.grid(row=len(rows) + 2, column=0, columnspan=3, sticky="e", padx=10, pady=10)
        ttk.Button(bar, text="OK", command=self._ok).pack(side="left", padx=4)
        ttk.Button(bar, text="Abbrechen", command=self.destroy).pack(side="left")
        self.columnconfigure(1, weight=1)
        self.grab_set()
        self.wait_window()

    def _browse(self):
        d = filedialog.askdirectory(parent=self, title="Zielordner wählen")
        if d:
            self.v["target"].set(os.path.normpath(d))

    def _ok(self):
        if not self.v["target"].get().strip():
            messagebox.showwarning("Ziel fehlt", "Bitte ein Ziel angeben.", parent=self)
            return
        vals = {k: v.get() for k, v in self.v.items()}
        self.rule = R.Rule(**{k: (v.strip() if isinstance(v, str) else v) for k, v in vals.items()})
        self.destroy()


# ====================================================================== Hauptfenster

class App(tk.Tk):
    def __init__(self, folder: str | None = None):
        super().__init__()
        self.title("DownloadSorter – Downloads aufräumen")
        self.geometry("1180x720")
        self.minsize(900, 500)
        try:
            ttk.Style(self).theme_use("vista")
        except tk.TclError:
            pass
        self.cfg = A.load_config()
        self.result: S.ScanResult | None = None
        self.by_iid: dict[str, S.FileInfo] = {}
        self.q: queue.Queue = queue.Queue()
        self.scanning = False

        self.folder = tk.StringVar(value=folder or self.cfg.get("last_folder") or S.default_downloads())
        self.recursive = tk.BooleanVar(value=bool(self.cfg.get("recursive")))
        self._build_top()
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self._build_overview()
        self._build_files()
        self._build_cleanup()
        self._build_duplicates()
        self._build_journal()
        self._build_rules()
        self.status = tk.StringVar(value="Bereit. Ordner wählen und „Scannen“ klicken.")
        ttk.Label(self, textvariable=self.status, anchor="w").pack(fill="x", padx=10, pady=(0, 6))
        self.after(100, self.scan)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(300, self._auto_resume)
        self.after(500, self._poll_auto)

    # ------------------------------------------------------------------ Kopfzeile
    def _build_top(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Label(top, text="Ordner:").pack(side="left")
        ttk.Entry(top, textvariable=self.folder).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(top, text="Wählen…", command=self.choose_folder).pack(side="left")
        ttk.Checkbutton(top, text="Unterordner einbeziehen", variable=self.recursive).pack(side="left", padx=8)
        self.scan_btn = ttk.Button(top, text="Scannen", command=self.scan)
        self.scan_btn.pack(side="left")
        ttk.Button(top, text="Einstellungen…", command=self.settings).pack(side="left", padx=4)
        self.pb = ttk.Progressbar(top, length=160, mode="determinate")
        self.pb.pack(side="left", padx=4)

    def choose_folder(self):
        d = filedialog.askdirectory(initialdir=self.folder.get(), title="Ordner zum Scannen wählen")
        if d:
            self.folder.set(os.path.normpath(d))
            self.scan()

    def settings(self):
        dlg = SettingsDialog(self, self.cfg)
        if dlg.saved and self.result:
            self.scan()

    # ------------------------------------------------------------------ Scan
    def scan(self):
        if self.scanning:
            return
        root = self.folder.get().strip()
        if not os.path.isdir(root):
            messagebox.showerror("Ordner fehlt", f"Ordner nicht gefunden:\n{root}")
            return
        self.cfg["last_folder"] = root
        self.cfg["recursive"] = self.recursive.get()
        A.save_config(self.cfg)
        self.scanning = True
        self.scan_btn.state(["disabled"])
        self.status.set("Scanne …")
        rec, age = self.recursive.get(), self.cfg.get("installer_age_days", 30)

        def work():
            try:
                res = S.scan(root, recursive=rec, installer_age_days=age,
                             progress=lambda t, d, n: self.q.put(("progress", t, d, n)))
                self.q.put(("done", res))
            except Exception as ex:  # noqa: BLE001
                self.q.put(("error", str(ex)))

        threading.Thread(target=work, daemon=True).start()
        self.after(50, self._poll)

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "progress":
                    _, text, done, total = msg
                    self.pb["maximum"] = max(total, 1)
                    self.pb["value"] = done
                    self.status.set(text)
                elif msg[0] == "done":
                    self.scanning = False
                    self.scan_btn.state(["!disabled"])
                    self._show(msg[1])
                    return
                elif msg[0] == "error":
                    self.scanning = False
                    self.scan_btn.state(["!disabled"])
                    messagebox.showerror("Fehler beim Scannen", msg[1])
                    return
        except queue.Empty:
            pass
        self.after(50, self._poll)

    def _show(self, res: S.ScanResult):
        self.result = res
        self.pb["value"] = 0
        self._fill_overview()
        self._fill_files()
        self._fill_cleanup()
        self._fill_duplicates()
        self._fill_journal()
        total = sum(f.size for f in res.files)
        wasted = sum(f.size for g in res.duplicates for f in g[1:])
        msg = (f"{len(res.files)} Dateien, {S.fmt_size(total)} in {res.root}  ·  "
               f"{len(res.suggestions)} Aufräum-Vorschläge  ·  Duplikate belegen {S.fmt_size(wasted)}")
        if res.errors:
            msg += f"  ·  {len(res.errors)} Dateien nicht lesbar"
        self.status.set(msg)

    # ------------------------------------------------------------------ Übersicht
    def _build_overview(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="Übersicht")
        cols = ("cat", "n", "size", "share", "avg", "oldest")
        f1, self.ov = make_tree(tab, cols, ("Kategorie", "Anzahl", "Größe", "Anteil", "Ø Alter", "Älteste Datei"),
                                (160, 80, 100, 80, 110, 160), height=11)
        f1.pack(fill="x", padx=6, pady=6)
        self.ov.bind("<Double-1>", self._ov_open)
        ttk.Label(tab, text="Doppelklick auf eine Kategorie zeigt ihre Dateien.").pack(anchor="w", padx=8)
        ttk.Label(tab, text="Alter der Dateien (seit dem Download):", font=("Segoe UI", 9, "bold")).pack(
            anchor="w", padx=8, pady=(10, 0))
        f2, self.ages = make_tree(tab, ("bucket", "n", "size"), ("Alter", "Anzahl", "Größe"), (160, 80, 100), height=6)
        f2.pack(fill="x", padx=6, pady=6)

    def _fill_overview(self):
        res = self.result
        self.ov.delete(*self.ov.get_children())
        total = sum(f.size for f in res.files) or 1
        for cat, fl in res.by_category().items():
            size = sum(f.size for f in fl)
            avg = sum(f.age_days for f in fl) / len(fl) if fl else 0
            oldest = max(fl, key=lambda f: f.age_days) if fl else None
            self.ov.insert("", "end", iid="cat:" + cat, values=(
                cat, len(fl), S.fmt_size(size), f"{100 * size / total:.0f} %",
                S.fmt_age_days(avg) if fl else "–", S.fmt_age_days(oldest.age_days) if oldest else "–"))
        self.ov.insert("", "end", values=("Gesamt", len(res.files), S.fmt_size(sum(f.size for f in res.files)),
                                          "100 %", "", ""))
        self.ages.delete(*self.ages.get_children())
        for _, label in S.AGE_BUCKETS:
            fl = [f for f in res.files if S.age_bucket(f.age_days) == label]
            self.ages.insert("", "end", values=(label, len(fl), S.fmt_size(sum(f.size for f in fl))))

    def _ov_open(self, _e):
        iid = self.ov.focus()
        if iid.startswith("cat:"):
            self.cat_filter.set(iid[4:])
            self._fill_files()
            self.nb.select(1)

    # ------------------------------------------------------------------ Dateien
    FILE_COLS = ("name", "cat", "type", "size", "age", "origin", "note")

    def _build_files(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="Dateien")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", padx=6, pady=6)
        ttk.Label(bar, text="Kategorie:").pack(side="left")
        self.cat_filter = tk.StringVar(value="Alle")
        cb = ttk.Combobox(bar, textvariable=self.cat_filter, values=["Alle"] + C.CATEGORIES, state="readonly", width=14)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self._fill_files())
        ttk.Label(bar, text="Suche:").pack(side="left", padx=(10, 0))
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *a: self._fill_files())
        ttk.Entry(bar, textvariable=self.search, width=28).pack(side="left", padx=4)
        self.file_count = tk.StringVar()
        ttk.Label(bar, textvariable=self.file_count).pack(side="left", padx=10)

        pw = ttk.PanedWindow(tab, orient="vertical")
        pw.pack(fill="both", expand=True, padx=6)
        f, self.ft = make_tree(pw, self.FILE_COLS,
                               ("Name", "Kategorie", "Erkannter Typ", "Größe", "Alter", "Herkunft", "Hinweis"),
                               (330, 90, 170, 80, 80, 160, 220))
        pw.add(f, weight=4)
        self.ft.tag_configure("warn", foreground="#b36b00")
        self.ft.bind("<<TreeviewSelect>>", self._file_detail)
        self.ft.bind("<Double-1>", lambda e: self._open_selected())
        self.sort_state = ("age", True)
        for c in self.FILE_COLS:
            self.ft.heading(c, command=lambda c=c: self._sort_files(c))
        self.detail = tk.Text(pw, height=6, wrap="word", relief="flat", background=self.cget("background"))
        pw.add(self.detail, weight=1)

        btns = ttk.Frame(tab)
        btns.pack(fill="x", padx=6, pady=6)
        ttk.Button(btns, text="Markierte einsortieren…", command=lambda: self.sort_files(self._sel_files())
                   ).pack(side="left")
        ttk.Button(btns, text="Alle angezeigten einsortieren…",
                   command=lambda: self.sort_files([self.by_iid[i] for i in self.ft.get_children()])).pack(side="left", padx=4)
        ttk.Button(btns, text="Markierte verschieben nach…", command=self.move_to_folder).pack(side="left")
        ttk.Button(btns, text="Markierte in den Papierkorb…",
                   command=lambda: self.trash_files(self._sel_files())).pack(side="left", padx=4)
        ttk.Button(btns, text="Im Explorer zeigen", command=self._reveal_selected).pack(side="right")
        ttk.Button(btns, text="Öffnen", command=self._open_selected).pack(side="right", padx=4)

    def _visible_files(self):
        if not self.result:
            return []
        cat, q = self.cat_filter.get(), self.search.get().strip().lower()
        out = [f for f in self.result.files if (cat == "Alle" or f.category == cat)
               and (not q or q in f.name.lower() or q in f.origin.lower() or q in f.type_desc.lower())]
        key, rev = self.sort_state
        keyf = {"name": lambda f: f.name.lower(), "cat": lambda f: f.category, "type": lambda f: f.type_desc,
                "size": lambda f: f.size, "age": lambda f: f.age_days, "origin": lambda f: f.origin,
                "note": lambda f: ";".join(f.notes)}[key]
        return sorted(out, key=keyf, reverse=rev)

    def _sort_files(self, col):
        key, rev = self.sort_state
        self.sort_state = (col, not rev if key == col else col in ("size", "age"))
        self._fill_files()

    def _fill_files(self):
        self.ft.delete(*self.ft.get_children())
        self.by_iid = {}
        files = self._visible_files()
        for i, f in enumerate(files):
            iid = f"f{i}"
            self.by_iid[iid] = f
            name = os.path.relpath(f.path, self.result.root) if self.recursive.get() else f.name
            self.ft.insert("", "end", iid=iid, tags=("warn",) if f.notes else (), values=(
                name, f.category, f.type_desc, S.fmt_size(f.size), S.fmt_age_days(f.age_days), f.origin,
                "; ".join(f.notes)))
        self.file_count.set(f"{len(files)} Dateien, {S.fmt_size(sum(f.size for f in files))}")

    def _sel_files(self):
        return [self.by_iid[i] for i in self.ft.selection() if i in self.by_iid]

    def _file_detail(self, _e=None):
        sel = self._sel_files()
        self.detail.delete("1.0", "end")
        if len(sel) != 1:
            if sel:
                self.detail.insert("end", f"{len(sel)} Dateien markiert, zusammen {S.fmt_size(sum(f.size for f in sel))}")
            return
        f = sel[0]
        lines = [f"Pfad: {f.path}",
                 f"Typ: {f.type_desc}   ·   Größe: {S.fmt_size(f.size)} ({f.size:,} Bytes)".replace(",", "."),
                 f"Heruntergeladen/erstellt: {fmt_time(f.ctime)}   ·   Geändert: {fmt_time(f.mtime)}"]
        if f.host_url or f.referrer_url or f.zone_id:
            lines.append(f"Quelle (HostUrl): {f.host_url or '–'}")
            lines.append(f"Verweisende Seite (ReferrerUrl): {f.referrer_url or '–'}")
            if f.zone_id:
                lines.append(f"Zone: {S.ZONE_NAMES.get(f.zone_id, f.zone_id)}")
        else:
            lines.append("Herkunft: von Windows nicht gespeichert")
        if f.sha256:
            lines.append(f"SHA-256: {f.sha256}")
        for n in f.notes:
            lines.append(f"Hinweis: {n}")
        self.detail.insert("end", "\n".join(lines))

    def _open_selected(self):
        sel = self._sel_files()
        if len(sel) == 1 and messagebox.askyesno(
                "Öffnen", f"„{sel[0].name}“ mit dem Standardprogramm öffnen?\n"
                          "(Bei Programmen/Installern startet das die Datei!)"):
            os.startfile(sel[0].path)

    def _reveal_selected(self):
        sel = self._sel_files()
        if sel:
            reveal_in_explorer(sel[0].path)

    # ------------------------------------------------------------------ Aufräumen
    def _build_cleanup(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="Aufräumen")
        ttk.Label(tab, text="Vorschläge – Klick auf ☐/☑ markiert. Nichts passiert ohne Vorschau. "
                            "Gelöscht wird nur in den Papierkorb.").pack(anchor="w", padx=8, pady=6)
        f, self.ct = make_tree(tab, ("chk", "name", "size", "age", "reason"),
                               ("", "Datei", "Größe", "Alter", "Grund"), (40, 380, 90, 90, 380), show="tree headings")
        self.ct.column("#0", width=260, stretch=False)
        self.ct.column("chk", anchor="center")
        self.ct.heading("#0", text="Vorschlag")
        f.pack(fill="both", expand=True, padx=6)
        self.ct.bind("<Button-1>", self._toggle_check)
        self.clean_items: dict[str, S.Suggestion] = {}
        btns = ttk.Frame(tab)
        btns.pack(fill="x", padx=6, pady=6)
        ttk.Button(btns, text="Alle markieren", command=lambda: self._check_all(True)).pack(side="left")
        ttk.Button(btns, text="Keine", command=lambda: self._check_all(False)).pack(side="left", padx=4)
        self.clean_sum = tk.StringVar()
        ttk.Label(btns, textvariable=self.clean_sum).pack(side="left", padx=10)
        ttk.Button(btns, text="Markierte in den Papierkorb…",
                   command=lambda: self.trash_files(self._checked())).pack(side="right")
        ttk.Button(btns, text="Markierte einsortieren…",
                   command=lambda: self.sort_files(self._checked())).pack(side="right", padx=4)

    def _fill_cleanup(self):
        self.ct.delete(*self.ct.get_children())
        self.clean_items = {}
        groups: dict[str, str] = {}
        for i, s in enumerate(self.result.suggestions):
            if s.title not in groups:
                groups[s.title] = self.ct.insert("", "end", text=s.title, values=(UNCHECK, "", "", "", ""), open=True)
            iid = self.ct.insert(groups[s.title], "end", iid=f"s{i}", values=(
                UNCHECK, s.file.name, S.fmt_size(s.file.size), S.fmt_age_days(s.file.age_days), s.reason))
            self.clean_items[iid] = s
        for title, gid in groups.items():
            kids = self.ct.get_children(gid)
            size = sum(self.clean_items[k].file.size for k in kids)
            self.ct.item(gid, text=f"{title} ({len(kids)}, {S.fmt_size(size)})")
        if not groups:
            self.ct.insert("", "end", text="Keine Vorschläge – alles aufgeräumt.", values=("", "", "", "", ""))
        self._update_clean_sum()

    def _toggle_check(self, e):
        if self.ct.identify_column(e.x) != "#1":
            return
        iid = self.ct.identify_row(e.y)
        if not iid:
            return
        new = CHECK if self.ct.set(iid, "chk") != CHECK else UNCHECK
        self.ct.set(iid, "chk", new)
        for k in self.ct.get_children(iid):
            self.ct.set(k, "chk", new)
        self._update_clean_sum()
        return "break"

    def _check_all(self, on):
        for gid in self.ct.get_children():
            self.ct.set(gid, "chk", CHECK if on else UNCHECK)
            for k in self.ct.get_children(gid):
                self.ct.set(k, "chk", CHECK if on else UNCHECK)
        self._update_clean_sum()

    def _checked(self):
        return [s.file for iid, s in self.clean_items.items() if self.ct.set(iid, "chk") == CHECK]

    def _update_clean_sum(self):
        fl = self._checked()
        self.clean_sum.set(f"{len(fl)} markiert, {S.fmt_size(sum(f.size for f in fl))}" if fl else "")

    # ------------------------------------------------------------------ Duplikate
    def _build_duplicates(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="Duplikate")
        ttk.Label(tab, text="Inhaltlich identische Dateien (SHA-256). Die älteste Kopie ist als „behalten“ "
                            "markiert.").pack(anchor="w", padx=8, pady=6)
        f, self.dt = make_tree(tab, ("name", "size", "date", "keep"),
                               ("Datei", "Größe", "Heruntergeladen", "Status"), (520, 90, 140, 110),
                               show="tree headings")
        self.dt.column("#0", width=200, stretch=False)
        self.dt.heading("#0", text="Gruppe")
        f.pack(fill="both", expand=True, padx=6)
        self.dup_items: dict[str, S.FileInfo] = {}
        btns = ttk.Frame(tab)
        btns.pack(fill="x", padx=6, pady=6)
        ttk.Button(btns, text="Markierte in den Papierkorb…", command=lambda: self.trash_files(
            [self.dup_items[i] for i in self.dt.selection() if i in self.dup_items])).pack(side="right")
        ttk.Button(btns, text="Im Explorer zeigen", command=lambda: [
            reveal_in_explorer(self.dup_items[i].path) for i in self.dt.selection()[:1] if i in self.dup_items]
                   ).pack(side="right", padx=4)

    def _fill_duplicates(self):
        self.dt.delete(*self.dt.get_children())
        self.dup_items = {}
        for gi, grp in enumerate(self.result.duplicates, 1):
            gid = self.dt.insert("", "end", text=f"Gruppe {gi}: {len(grp)}×", open=True,
                                 values=("", S.fmt_size(grp[0].size * (len(grp) - 1)) + " doppelt", "", ""))
            for fi, f in enumerate(grp):
                iid = self.dt.insert(gid, "end", values=(
                    os.path.relpath(f.path, self.result.root), S.fmt_size(f.size), fmt_time(f.ctime),
                    "behalten" if fi == 0 else "Kopie"))
                self.dup_items[iid] = f
        if not self.result.duplicates:
            self.dt.insert("", "end", text="Keine Duplikate gefunden.")

    # ------------------------------------------------------------------ Protokoll
    def _build_journal(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="Protokoll / Rückgängig")
        ttk.Label(tab, text="Alle Verschiebungen werden protokolliert und lassen sich rückgängig machen. "
                            "Papierkorb-Aktionen stellst du über den Windows-Papierkorb wieder her."
                  ).pack(anchor="w", padx=8, pady=6)
        f, self.jt = make_tree(tab, ("time", "label", "n", "state"), ("Zeit", "Aktion", "Anzahl", "Status"),
                               (140, 260, 80, 260), show="tree headings")
        self.jt.column("#0", width=30, stretch=False)
        f.pack(fill="both", expand=True, padx=6)
        btns = ttk.Frame(tab)
        btns.pack(fill="x", padx=6, pady=6)
        ttk.Button(btns, text="Markierte Aktion rückgängig machen", command=self.undo).pack(side="right")
        ttk.Button(btns, text="Papierkorb öffnen",
                   command=lambda: subprocess.Popen(["explorer", "shell:RecycleBinFolder"])).pack(side="right", padx=4)

    def _fill_journal(self):
        self.jt.delete(*self.jt.get_children())
        for b in reversed(A.load_journal()[-200:]):
            if b.get("kind") == "trash":
                state = "im Papierkorb"
            elif b.get("undone"):
                state = "rückgängig gemacht"
            elif b.get("undo_errors"):
                state = f"teilweise rückgängig ({len(b['undo_errors'])} offen)"
            else:
                state = "ausgeführt" + (f", {len(b['errors'])} Fehler" if b.get("errors") else "")
            bid = self.jt.insert("", "end", iid=b["id"], values=(fmt_time(b["time"]), b.get("label", ""),
                                                                  len(b.get("ops", [])), state))
            for op in b.get("ops", [])[:500]:
                txt = f"{op['src']}  →  {op['dst']}" if "dst" in op else op["src"]
                self.jt.insert(bid, "end", values=("", txt, "", ""))

    def undo(self):
        sel = [i for i in self.jt.selection() if self.jt.parent(i) == ""] or \
              [self.jt.parent(i) for i in self.jt.selection()]
        if not sel:
            messagebox.showinfo("Rückgängig", "Bitte eine Aktion in der Liste markieren.")
            return
        bid = sel[0]
        batch = next((b for b in A.load_journal() if b["id"] == bid), None)
        if not batch or batch.get("kind") != "move" or batch.get("undone") or not batch.get("ops"):
            messagebox.showinfo("Rückgängig", "Diese Aktion kann hier nicht rückgängig gemacht werden.")
            return
        rows = [((os.path.basename(op["dst"]), op["dst"], op["src"]), "") for op in batch["ops"]]
        dlg = PreviewDialog(self, "Vorschau: Rückgängig", f"{len(rows)} Dateien werden an ihren Ursprungsort "
                            "zurückverschoben.", ("n", "from", "to"), ("Datei", "Von", "Zurück nach"),
                            (200, 380, 380), rows, "Zurückverschieben")
        if not dlg.result:
            return
        ok, errs = A.undo_batch(bid)
        msg = f"{ok} Dateien zurückverschoben."
        if errs:
            msg += "\n\nNicht möglich:\n" + "\n".join(errs[:20])
        messagebox.showinfo("Rückgängig", msg)
        self.scan()

    # ------------------------------------------------------------------ Regeln & Automatik
    def _build_rules(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="Regeln & Automatik")
        self.rule_list = R.load_rules(self.cfg)
        lf = ttk.LabelFrame(tab, text="Eigene Regeln (die erste passende gewinnt)")
        lf.pack(fill="both", expand=True, padx=6, pady=6)
        f, self.rt = make_tree(lf, ("on", "name", "cond", "target"), ("Aktiv", "Name", "Wenn", "Dann nach"),
                               (50, 200, 420, 300), height=7)
        self.rt.column("on", anchor="center")
        f.pack(fill="both", expand=True, padx=4, pady=4)
        self.rt.bind("<Double-1>", lambda e: self._rule_edit())
        b = ttk.Frame(lf)
        b.pack(fill="x", padx=4, pady=4)
        for text, cmd in (("Neu…", self._rule_new), ("Bearbeiten…", self._rule_edit), ("Löschen", self._rule_delete),
                          ("▲", lambda: self._rule_move(-1)), ("▼", lambda: self._rule_move(1))):
            ttk.Button(b, text=text, command=cmd).pack(side="left", padx=2)
        ttk.Button(b, text="Regeln jetzt auf vorhandene Dateien anwenden (Vorschau)…",
                   command=self._rules_apply_now).pack(side="right")

        af = ttk.LabelFrame(tab, text="Automatik – neue Downloads selbstständig einsortieren")
        af.pack(fill="both", expand=True, padx=6, pady=6)
        a = W.auto_cfg(self.cfg)
        self.auto_on = tk.BooleanVar(value=bool(a["enabled"]))
        self.auto_fallback = tk.BooleanVar(value=bool(a["fallback_category"]))
        self.auto_only_new = tk.BooleanVar(value=bool(a["only_new"]))
        self.autostart = tk.BooleanVar(value=W.autostart_enabled())
        row = ttk.Frame(af)
        row.pack(fill="x", padx=4, pady=4)
        ttk.Checkbutton(row, text="Automatik aktiv", variable=self.auto_on, command=self._auto_toggle
                        ).pack(side="left")
        ttk.Checkbutton(row, text="Ohne passende Regel nach Kategorie einsortieren", variable=self.auto_fallback,
                        command=self._auto_save).pack(side="left", padx=12)
        ttk.Checkbutton(row, text="Nur neue Downloads (vorhandene bleiben liegen)", variable=self.auto_only_new,
                        command=self._auto_save).pack(side="left")
        row2 = ttk.Frame(af)
        row2.pack(fill="x", padx=4)
        ttk.Checkbutton(row2, text="Beim Windows-Start im Hintergrund starten (ohne Fenster)",
                        variable=self.autostart, command=self._autostart_toggle).pack(side="left")
        ttk.Button(row2, text="Hintergrund-Log öffnen", command=self._open_bg_log).pack(side="right")
        self.auto_status = tk.StringVar(value="Automatik aus.")
        ttk.Label(af, textvariable=self.auto_status, font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=6, pady=4)
        ttk.Label(af, text="Sortiert werden nur fertige Downloads (keine .crdownload/.part, einige Sekunden "
                           "unverändert, nicht gesperrt). Jede Verschiebung steht im Protokoll und lässt sich "
                           "rückgängig machen. Gelöscht wird nie.", wraplength=1050, justify="left"
                  ).pack(anchor="w", padx=6)
        self.auto_log = tk.Text(af, height=6, wrap="none", relief="flat", background=self.cget("background"))
        self.auto_log.pack(fill="both", expand=True, padx=4, pady=4)
        self.watcher = None
        self.lock = None
        self.aq = queue.Queue()
        self._fill_rules()

    def _fill_rules(self):
        self.rt.delete(*self.rt.get_children())
        for i, r in enumerate(self.rule_list):
            self.rt.insert("", "end", iid=str(i), values=(CHECK if r.enabled else UNCHECK, r.name, r.describe(),
                                                         r.target))

    def _rules_changed(self):
        R.save_rules(self.cfg, self.rule_list)
        self._fill_rules()

    def _rule_sel(self):
        s = self.rt.selection()
        return int(s[0]) if s else None

    def _rule_new(self):
        d = RuleDialog(self, R.Rule())
        if d.rule:
            self.rule_list.append(d.rule)
            self._rules_changed()

    def _rule_edit(self):
        i = self._rule_sel()
        if i is None:
            return
        d = RuleDialog(self, self.rule_list[i])
        if d.rule:
            self.rule_list[i] = d.rule
            self._rules_changed()
            self.rt.selection_set(str(i))

    def _rule_delete(self):
        i = self._rule_sel()
        if i is not None and messagebox.askyesno("Regel löschen", f"Regel „{self.rule_list[i].name}“ löschen?"):
            del self.rule_list[i]
            self._rules_changed()

    def _rule_move(self, d):
        i = self._rule_sel()
        if i is None or not 0 <= i + d < len(self.rule_list):
            return
        self.rule_list[i], self.rule_list[i + d] = self.rule_list[i + d], self.rule_list[i]
        self._rules_changed()
        self.rt.selection_set(str(i + d))

    def _rules_apply_now(self):
        if not self.result:
            return
        items = []
        for f in self.result.files:
            dest = R.destination(f, self.rule_list, self.result.root, self.cfg, self.auto_fallback.get())
            if dest:
                items.append((f.path, dest[0]))
        if not items:
            messagebox.showinfo("Regeln", "Keine vorhandene Datei passt zu einer aktiven Regel.")
            return
        self._move_with_preview(items, "Regeln angewendet")

    # Automatik steuern
    def _auto_save(self):
        a = self.cfg.setdefault("auto", {})
        a.update({"enabled": self.auto_on.get(), "fallback_category": self.auto_fallback.get(),
                  "only_new": self.auto_only_new.get(), "folder": self.folder.get().strip()})
        A.save_config(self.cfg)

    def _auto_log(self, text):
        self.auto_log.insert("end", time.strftime("%H:%M:%S ") + text + "\n")
        self.auto_log.see("end")

    def _auto_start(self) -> bool:
        if W.background_running():
            self.auto_status.set("Automatik läuft im Hintergrundprozess (ohne Fenster). Ausschalten beendet ihn.")
            return True
        root = self.folder.get().strip()
        if not os.path.isdir(root):
            return False
        self.lock = W.SingleInstance()
        if not self.lock.acquire():
            self.lock = None
            return False
        self.watcher = W.Watcher(root, self.cfg, on_event=lambda k, t: self.aq.put((k, t)))
        self.watcher.start()
        n = sum(1 for r in self.rule_list if r.enabled)
        self.auto_status.set(f"Automatik läuft für {root} – {n} aktive Regel(n)"
                             + (", Rest nach Kategorie" if self.auto_fallback.get() else ""))
        self._auto_log("Automatik gestartet")
        return True

    def _auto_stop(self, stop_background=True):
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
            self._auto_log("Automatik gestoppt")
        if self.lock:
            self.lock.release()
            self.lock = None
        elif stop_background and W.background_running():
            W.request_background_stop()
            self._auto_log("Hintergrundprozess wird beendet")
        self.auto_status.set("Automatik aus.")

    def _auto_toggle(self):
        if self.auto_on.get():
            if not any(r.enabled for r in self.rule_list) and not self.auto_fallback.get():
                messagebox.showinfo("Automatik", "Es gibt keine aktive Regel und „nach Kategorie einsortieren“ "
                                                 "ist aus – die Automatik würde nichts tun.\n"
                                                 "Lege eine Regel an oder aktiviere die Kategorie-Sortierung.")
            self._auto_save()
            if not self._auto_start():
                self.auto_on.set(False)
                self._auto_save()
                messagebox.showerror("Automatik", "Automatik konnte nicht gestartet werden.")
        else:
            self._auto_save()
            self._auto_stop()

    def _auto_resume(self):
        if self.auto_on.get():
            self._auto_start()

    def _poll_auto(self):
        moved = False
        try:
            while True:
                kind, text = self.aq.get_nowait()
                prefix = {"moved": "✔ ", "error": "⚠ "}.get(kind, "· ")
                self._auto_log(prefix + text)
                moved = moved or kind == "moved"
        except queue.Empty:
            pass
        if moved:
            self._fill_journal()
            self.status.set("Automatik hat Dateien einsortiert – „Scannen“ aktualisiert die Ansicht.")
        self.after(500, self._poll_auto)

    def _autostart_toggle(self):
        try:
            W.set_autostart(self.autostart.get())
        except Exception as ex:  # noqa: BLE001
            messagebox.showerror("Autostart", f"Konnte Autostart nicht ändern:\n{ex}")
        self.autostart.set(W.autostart_enabled())
        if self.autostart.get() and not self.auto_on.get():
            messagebox.showinfo("Autostart", "Autostart ist eingerichtet. Damit beim Windows-Start etwas "
                                             "sortiert wird, muss auch „Automatik aktiv“ angehakt sein.")

    def _open_bg_log(self):
        p = W.log_path()
        if not os.path.exists(p):
            open(p, "a").close()
        os.startfile(p)

    def on_close(self):
        if self.watcher and self.watcher.running:
            ans = messagebox.askyesnocancel(
                "Automatik", "Die Automatik läuft. Soll sie nach dem Schließen im Hintergrund "
                             "(ohne Fenster) weiterlaufen?\n\nJa = weiterlaufen, Nein = beenden")
            if ans is None:
                return
            self._auto_stop(stop_background=False)
            if ans:
                W.spawn_background()
        self.destroy()

    # ------------------------------------------------------------------ Aktionen
    def sort_files(self, files):
        if not files:
            messagebox.showinfo("Einsortieren", "Keine Dateien markiert.")
            return
        root = self.result.root
        items = [(f.path, A.target_dir(f.category, root, self.cfg)) for f in files]
        self._move_with_preview(items, "Einsortieren nach Kategorie")

    def move_to_folder(self):
        files = self._sel_files()
        if not files:
            messagebox.showinfo("Verschieben", "Keine Dateien markiert.")
            return
        d = filedialog.askdirectory(title="Zielordner wählen", initialdir=self.result.root)
        if d:
            self._move_with_preview([(f.path, os.path.normpath(d)) for f in files],
                                    f"Verschieben nach {os.path.basename(d)}")

    def _move_with_preview(self, items, label):
        plan = A.plan_moves(items)
        rows = []
        for m in plan:
            tag = "bad" if m.problem else ("warn" if m.renamed else "")
            note = m.problem or ("umbenannt (Name existiert schon)" if m.renamed else "")
            rows.append(((os.path.basename(m.src), m.dst, note), tag))
        n = sum(1 for m in plan if not m.problem)
        dlg = PreviewDialog(self, "Vorschau: Verschieben",
                            f"{n} Dateien werden verschoben. Die Aktion wird protokolliert und kann unter "
                            "„Protokoll / Rückgängig“ zurückgenommen werden.",
                            ("n", "to", "note"), ("Datei", "Neues Ziel", "Hinweis"), (260, 520, 200), rows,
                            f"{n} Dateien verschieben")
        if not dlg.result:
            return
        batch = A.execute_moves(plan, label)
        msg = f"{len(batch['ops'])} Dateien verschoben."
        if batch["errors"]:
            msg += "\n\nFehler:\n" + "\n".join(batch["errors"][:20])
        messagebox.showinfo("Verschieben", msg)
        self.scan()

    def trash_files(self, files):
        if not files:
            messagebox.showinfo("Papierkorb", "Keine Dateien markiert.")
            return
        rows = [((f.name, S.fmt_size(f.size), f.category, os.path.dirname(f.path)), "") for f in files]
        total = S.fmt_size(sum(f.size for f in files))
        dlg = PreviewDialog(self, "Vorschau: In den Papierkorb",
                            f"{len(files)} Dateien ({total}) werden in den Windows-Papierkorb verschoben – "
                            "nicht endgültig gelöscht. Wiederherstellen geht über den Papierkorb.",
                            ("n", "size", "cat", "dir"), ("Datei", "Größe", "Kategorie", "Ordner"),
                            (320, 90, 100, 400), rows, "In den Papierkorb")
        if not dlg.result:
            return
        done, errs = A.send_to_trash([f.path for f in files])
        msg = f"{len(done)} Dateien in den Papierkorb verschoben."
        if errs:
            msg += "\n\nNicht verschoben:\n" + "\n".join(errs[:20])
        messagebox.showinfo("Papierkorb", msg)
        self.scan()


def main(argv=None):
    import sys
    argv = sys.argv[1:] if argv is None else argv
    if "--hintergrund" in argv:
        W.run_headless()
        return
    if os.name == "nt":
        import ctypes
        # Available since Windows 8.1; failure is a return code, and the UI is then merely blurry.
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    App(argv[0] if argv else None).mainloop()
