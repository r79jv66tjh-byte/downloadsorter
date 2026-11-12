"""DownloadSorter – Kommandozeile für Claude/ToolHub.

Jeder Befehl gibt JSON auf stdout aus. Fehler: Meldung auf stderr, Exit-Code 1.
Befehle, die Dateien verändern (sort, undo), unterstützen --dry-run (nur Vorschau).
Gelöscht wird über diese Schnittstelle nie etwas.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from downloadsorter import actions as A  # noqa: E402
from downloadsorter import categories as C  # noqa: E402
from downloadsorter import rules as R  # noqa: E402
from downloadsorter import scanner as S  # noqa: E402


class CliError(Exception):
    pass


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))


def file_json(f: S.FileInfo, root: str | None = None) -> dict:
    d = {"name": f.name, "path": f.path, "category": f.category, "type": f.type_desc,
         "size_bytes": f.size, "size": S.fmt_size(f.size), "age_days": round(f.age_days, 1),
         "downloaded": iso(f.ctime), "modified": iso(f.mtime), "origin": f.origin or None}
    if root:
        d["relative_path"] = os.path.relpath(f.path, root)
    if f.ext_mismatch:
        d["extension_mismatch"] = True
    if f.incomplete:
        d["incomplete_download"] = True
    if f.notes:
        d["notes"] = f.notes
    return d


def resolve_folder(folder: str | None) -> str:
    root = os.path.abspath(os.path.expandvars(os.path.expanduser(folder))) if folder else S.default_downloads()
    if not os.path.isdir(root):
        raise CliError(f"Ordner nicht gefunden: {root}")
    return root


def do_scan(a, dups=True) -> S.ScanResult:
    cfg = A.load_config()
    age = getattr(a, "installer_age", None)
    if age is None:
        age = cfg.get("installer_age_days", 30)
    return S.scan(resolve_folder(a.folder), recursive=getattr(a, "recursive", False),
                  find_duplicates=dups, installer_age_days=age)


# ------------------------------------------------------------------ lesende Befehle

def cmd_scan(a):
    res = do_scan(a)
    total = sum(f.size for f in res.files)
    cats = []
    for cat, fl in res.by_category().items():
        size = sum(f.size for f in fl)
        cats.append({"category": cat, "count": len(fl), "size_bytes": size, "size": S.fmt_size(size),
                     "share_percent": round(100 * size / total, 1) if total else 0,
                     "avg_age_days": round(sum(f.age_days for f in fl) / len(fl), 1) if fl else None,
                     "oldest_age_days": round(max(f.age_days for f in fl), 1) if fl else None})
    ages = []
    for _, label in S.AGE_BUCKETS:
        fl = [f for f in res.files if S.age_bucket(f.age_days) == label]
        ages.append({"age": label, "count": len(fl), "size_bytes": sum(f.size for f in fl)})
    wasted = sum(f.size for g in res.duplicates for f in g[1:])
    return {"folder": res.root, "file_count": len(res.files), "subfolder_count": len(res.folders),
            "total_size_bytes": total, "total_size": S.fmt_size(total), "categories": cats, "age_buckets": ages,
            "suggestion_count": len(res.suggestions), "duplicate_groups": len(res.duplicates),
            "duplicate_wasted_bytes": wasted, "duplicate_wasted": S.fmt_size(wasted),
            "extension_mismatches": sum(1 for f in res.files if f.ext_mismatch),
            "files_with_origin": sum(1 for f in res.files if f.origin), "unreadable": res.errors[:50]}


def _select(res: S.ScanResult, a) -> list[S.FileInfo]:
    fl = res.files
    if getattr(a, "category", None):
        cat = _category(a.category)
        fl = [f for f in fl if f.category == cat]
    if getattr(a, "name", None):
        q = a.name.lower()
        fl = [f for f in fl if q in f.name.lower()]
    if getattr(a, "file", None):
        fl = [f for f in fl if _same_file(f, a.file, res.root)]
        if not fl:
            raise CliError(f"Datei nicht im Ordner gefunden: {a.file}")
    return fl


def _same_file(f: S.FileInfo, spec: str, root: str) -> bool:
    p = spec if os.path.isabs(spec) else os.path.join(root, spec)
    return os.path.normcase(os.path.abspath(p)) == os.path.normcase(f.path)


def _category(name: str) -> str:
    for c in C.CATEGORIES:
        if c.lower() == name.strip().lower():
            return c
    raise CliError(f"Unbekannte Kategorie „{name}“. Erlaubt: {', '.join(C.CATEGORIES)}")


def cmd_files(a):
    res = do_scan(a, dups=False)
    fl = _select(res, a)
    key = {"size": lambda f: -f.size, "age": lambda f: -f.age_days, "name": lambda f: f.name.lower(),
           "newest": lambda f: f.age_days}[a.sort]
    fl = sorted(fl, key=key)
    limit = max(1, a.limit)
    return {"folder": res.root, "matched": len(fl), "returned": min(limit, len(fl)),
            "files": [file_json(f, res.root) for f in fl[:limit]]}


def cmd_suggestions(a):
    res = do_scan(a)
    groups: dict[str, dict] = {}
    for s in res.suggestions:
        g = groups.setdefault(s.kind, {"kind": s.kind, "title": s.title, "count": 0, "size_bytes": 0, "files": []})
        g["count"] += 1
        g["size_bytes"] += s.file.size
        g["files"].append({**file_json(s.file, res.root), "reason": s.reason})
    for g in groups.values():
        g["size"] = S.fmt_size(g["size_bytes"])
    total = sum(g["size_bytes"] for g in groups.values())
    return {"folder": res.root, "suggestion_count": len(res.suggestions), "reclaimable_bytes": total,
            "reclaimable": S.fmt_size(total), "groups": list(groups.values()),
            "note": "Vorschläge zum Aufräumen. Diese Schnittstelle löscht nichts; Löschen (Papierkorb) "
                    "nur in der DownloadSorter-Oberfläche."}


def cmd_duplicates(a):
    res = do_scan(a)
    groups = []
    for g in res.duplicates:
        groups.append({"sha256": g[0].sha256, "size_bytes": g[0].size, "size": S.fmt_size(g[0].size),
                       "copies": len(g), "wasted_bytes": g[0].size * (len(g) - 1),
                       "keep": file_json(g[0], res.root), "duplicates": [file_json(f, res.root) for f in g[1:]]})
    wasted = sum(x["wasted_bytes"] for x in groups)
    return {"folder": res.root, "group_count": len(groups), "wasted_bytes": wasted, "wasted": S.fmt_size(wasted),
            "groups": groups}


def cmd_origin(a):
    p = a.file
    if not os.path.isabs(p):
        p = os.path.join(resolve_folder(a.folder), p)
    if not os.path.isfile(p):
        raise CliError(f"Datei nicht gefunden: {p}")
    f = S.analyze(os.path.abspath(p))
    return {**file_json(f), "host_url": f.host_url or None, "referrer_url": f.referrer_url or None,
            "zone_id": f.zone_id or None, "zone": S.ZONE_NAMES.get(f.zone_id) if f.zone_id else None,
            "origin_known": bool(f.host_url or f.referrer_url)}


def cmd_history(a):
    out = []
    for b in reversed(A.load_journal()[-max(1, a.limit):]):
        out.append({"id": b["id"], "time": iso(b["time"]), "kind": b.get("kind"), "label": b.get("label"),
                    "file_count": len(b.get("ops", [])), "undone": bool(b.get("undone")),
                    "undoable": b.get("kind") == "move" and not b.get("undone") and bool(b.get("ops")),
                    "errors": b.get("errors", []), "moves": b.get("ops", [])[:200]})
    return {"journal": A.journal_path(), "entries": out}


def cmd_rules(a):
    cfg = A.load_config()
    rules = R.load_rules(cfg)
    from downloadsorter import watcher as W
    auto = W.auto_cfg(cfg)
    return {"rules": [{"position": i + 1, "name": r.name, "enabled": r.enabled, "condition": r.describe(),
                       "category": r.category, "extensions": sorted(r.ext_set()), "name_pattern": r.name_pattern,
                       "origin_contains": r.origin_contains, "target": r.target} for i, r in enumerate(rules)],
            "category_targets": {c: A.target_dir(c, "<Ordner>", cfg) for c in C.CATEGORIES},
            "automation": {"enabled": auto["enabled"], "folder": auto["folder"] or None,
                           "sort_rest_by_category": auto["fallback_category"], "only_new": auto["only_new"],
                           "background_running": W.background_running(),
                           "autostart": W.autostart_enabled()}}


# ------------------------------------------------------------------ schreibende Befehle

def cmd_sort(a):
    if not (a.category or a.file or a.name or a.all):
        raise CliError("Bitte eingrenzen: --category, --file, --name oder ausdrücklich --all")
    res = do_scan(a, dups=False)
    cfg = A.load_config()
    files = [f for f in _select(res, a) if not f.incomplete]
    items, unmatched = [], []
    for f in files:
        if a.target:
            t = os.path.expandvars(os.path.expanduser(a.target))
            items.append((f.path, t if os.path.isabs(t) else os.path.join(res.root, t)))
        elif a.mode == "rules":
            dest = R.destination(f, R.load_rules(cfg), res.root, cfg, fallback_category=False)
            if dest:
                items.append((f.path, dest[0]))
            else:
                unmatched.append(f.name)
        else:
            items.append((f.path, A.target_dir(f.category, res.root, cfg)))
    plan = A.plan_moves(items)
    preview = [{"source": m.src, "destination": m.dst, "renamed": m.renamed, "skipped": m.problem or None}
               for m in plan]
    n = sum(1 for m in plan if not m.problem)
    out = {"folder": res.root, "dry_run": a.dry_run, "planned_moves": n, "moves": preview}
    if unmatched:
        out["no_matching_rule"] = unmatched
    if a.dry_run or n == 0:
        return out
    batch = A.execute_moves(plan, "Claude: " + ("Regeln" if a.mode == "rules" and not a.target else "Einsortieren"))
    out.update({"moved": len(batch["ops"]), "errors": batch["errors"], "undo_id": batch["id"]})
    return out


def cmd_undo(a):
    entries = A.load_journal()
    if a.id:
        batch = next((b for b in entries if b.get("id") == a.id), None)
        if batch is None:
            raise CliError(f"Protokolleintrag nicht gefunden: {a.id}")
    else:
        batch = next((b for b in reversed(entries)
                      if b.get("kind") == "move" and not b.get("undone") and b.get("ops")), None)
        if batch is None:
            raise CliError("Keine Verschiebung zum Rückgängigmachen vorhanden")
    if batch.get("kind") != "move":
        raise CliError("Papierkorb-Aktionen bitte über den Windows-Papierkorb wiederherstellen")
    if batch.get("undone"):
        raise CliError("Diese Aktion wurde bereits rückgängig gemacht")
    preview = []
    for op in batch["ops"]:
        problem = None
        if not os.path.exists(op["dst"]):
            problem = "Datei ist nicht mehr am Ziel"
        elif os.path.exists(op["src"]):
            problem = "am Ursprungsort liegt inzwischen eine andere Datei"
        preview.append({"from": op["dst"], "back_to": op["src"], "skipped": problem})
    out = {"id": batch["id"], "label": batch.get("label"), "time": iso(batch["time"]), "dry_run": a.dry_run,
           "planned": sum(1 for p in preview if not p["skipped"]), "moves": preview}
    if a.dry_run:
        return out
    ok, errs = A.undo_batch(batch["id"])
    out.update({"restored": ok, "errors": errs})
    return out


# ------------------------------------------------------------------ Parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cli.py", description="DownloadSorter-Kommandozeile (JSON-Ausgabe)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, func, help_, folder=True, recursive=True):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(func=func)
        sp.add_argument("--json", action="store_true", help="Ausgabe als JSON (immer aktiv)")
        if folder:
            sp.add_argument("--folder", help="Ordner (Standard: Downloads)")
        if recursive:
            sp.add_argument("--recursive", action="store_true", help="Unterordner einbeziehen")
        return sp

    add("scan", cmd_scan, "Übersicht pro Kategorie")
    sp = add("files", cmd_files, "Dateien auflisten")
    sp.add_argument("--category")
    sp.add_argument("--name", help="Teil des Dateinamens")
    sp.add_argument("--sort", choices=["size", "age", "name", "newest"], default="size")
    sp.add_argument("--limit", type=int, default=100)
    sp = add("suggestions", cmd_suggestions, "Aufräum-Vorschläge")
    sp.add_argument("--installer-age", type=int, dest="installer_age")
    add("duplicates", cmd_duplicates, "Duplikate")
    sp = add("origin", cmd_origin, "Herkunft einer Datei", recursive=False)
    sp.add_argument("--file", required=True)
    sp = add("history", cmd_history, "Protokoll", folder=False, recursive=False)
    sp.add_argument("--limit", type=int, default=20)
    add("rules", cmd_rules, "Regeln anzeigen", folder=False, recursive=False)
    sp = add("sort", cmd_sort, "Dateien einsortieren")
    sp.add_argument("--category")
    sp.add_argument("--file")
    sp.add_argument("--name")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--mode", choices=["category", "rules"], default="category")
    sp.add_argument("--target")
    sp.add_argument("--dry-run", action="store_true", dest="dry_run")
    sp = add("undo", cmd_undo, "Verschiebung rückgängig machen", folder=False, recursive=False)
    sp.add_argument("--id")
    sp.add_argument("--dry-run", action="store_true", dest="dry_run")
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    try:
        a = parser.parse_args(argv)
    except SystemExit as ex:  # argparse schreibt die Meldung selbst auf stderr
        return 0 if ex.code == 0 else 2
    try:
        result = a.func(a)
    except CliError as ex:
        print(f"Fehler: {ex}", file=sys.stderr)
        return 1
    except Exception as ex:  # noqa: BLE001
        print(f"Fehler: {type(ex).__name__}: {ex}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
