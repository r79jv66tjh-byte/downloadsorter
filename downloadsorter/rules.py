"""Eigene Sortierregeln: Wenn eine Datei passt, wohin soll sie?"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import asdict, dataclass

from . import actions as A
from . import categories as C
from .scanner import FileInfo

ANY = "Alle"


@dataclass
class Rule:
    name: str = "Neue Regel"
    enabled: bool = True
    category: str = ANY          # Kategorie oder "Alle"
    extensions: str = ""         # z. B. "jar, zip" (leer = egal)
    name_pattern: str = ""       # Platzhalter wie *forge*.jar (leer = egal)
    origin_contains: str = ""    # Teil der Herkunfts-URL/Domain, z. B. forgecdn.net (leer = egal)
    target: str = ""             # Unterordner-Name oder absoluter Pfad

    def ext_set(self) -> set[str]:
        return {e.strip().lstrip(".").lower() for e in self.extensions.replace(";", ",").split(",") if e.strip()}

    def matches(self, f: FileInfo) -> bool:
        if not self.enabled or not self.target.strip():
            return False
        if self.category not in ("", ANY) and f.category != self.category:
            return False
        exts = self.ext_set()
        if exts and C.extension(f.name) not in exts:
            return False
        if self.name_pattern.strip() and not fnmatch.fnmatch(f.name.lower(), self.name_pattern.strip().lower()):
            return False
        o = self.origin_contains.strip().lower()
        if o and o not in (f.host_url + " " + f.referrer_url).lower():
            return False
        return True

    def describe(self) -> str:
        parts = []
        if self.category not in ("", ANY):
            parts.append(f"Kategorie {self.category}")
        if self.ext_set():
            parts.append("Endung " + ", ".join(sorted(self.ext_set())))
        if self.name_pattern.strip():
            parts.append(f"Name wie „{self.name_pattern.strip()}“")
        if self.origin_contains.strip():
            parts.append(f"Herkunft enthält „{self.origin_contains.strip()}“")
        return " und ".join(parts) or "jede Datei"


EXAMPLE_RULES = [
    Rule(name="Beispiel: Minecraft-Mods", enabled=False, category=ANY, extensions="jar",
         origin_contains="forgecdn.net", target=os.path.join("Mods", "Minecraft")),
]


def load_rules(cfg: dict) -> list[Rule]:
    raw = cfg.get("rules")
    if raw is None:
        return [Rule(**asdict(r)) for r in EXAMPLE_RULES]
    fields = Rule.__dataclass_fields__
    return [Rule(**{k: v for k, v in r.items() if k in fields}) for r in raw if isinstance(r, dict)]


def save_rules(cfg: dict, rules: list[Rule]) -> None:
    cfg["rules"] = [asdict(r) for r in rules]
    A.save_config(cfg)


def resolve_target(target: str, root: str) -> str:
    t = os.path.expandvars(os.path.expanduser(target.strip()))
    return t if os.path.isabs(t) else os.path.join(root, t)


def destination(f: FileInfo, rules: list[Rule], root: str, cfg: dict,
                fallback_category: bool) -> tuple[str, str] | None:
    """Liefert (Zielordner, Begründung) für eine Datei oder None, wenn nichts passt."""
    if f.incomplete:
        return None
    for r in rules:
        if r.matches(f):
            return resolve_target(r.target, root), f"Regel „{r.name}“"
    if fallback_category:
        return A.target_dir(f.category, root, cfg), f"Kategorie {f.category}"
    return None
