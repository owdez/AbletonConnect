"""Claude Kits: racks holding the user's own sounds, in "<User Library>/Claude Kits/<role folder>/",
described by kits.yaml in that folder. Live's API can't load plugin presets or save presets, so a
rack saved with its plugins inside is how a sound gets reused."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

from .config import CONFIG_PATH

FOLDER = "Claude Kits"
SUBFOLDERS = ("Drums", "Bass", "Synths", "Pads", "FX", "Vocal Chains", "Mix Bus")
RACK_SUFFIXES = (".adg", ".adv", ".agr")  # racks and device presets
BROWSER_ROOT = "user_library/" + FOLDER
KIT_KEYS = ("file", "role", "tags", "range", "macros", "starter", "notes")


def user_library() -> Path:
    """The User Library from Live's newest Library.cfg, else Live's default location."""
    prefs = sorted(
        Path("~/Library/Preferences/Ableton").expanduser().glob("Live *"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for folder in prefs:
        try:
            text = (folder / "Library.cfg").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        block = re.search(r"<UserLibrary>(.*?)</UserLibrary>", text, re.S)
        path = re.search(r'<ProjectPath Value="([^"]*)"', block.group(1)) if block else None
        name = re.search(r'<ProjectName Value="([^"]*)"', block.group(1)) if block else None
        if path and name and (Path(path.group(1)) / name.group(1)).is_dir():
            return Path(path.group(1)) / name.group(1)
    return Path("~/Music/Ableton/User Library").expanduser()


def kits_dir() -> Path:
    override = os.environ.get("ABLETONCONNECT_KITS")
    if override:
        return Path(override).expanduser()
    try:
        configured = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("kits_folder")
    except (OSError, ValueError, AttributeError):
        configured = None
    return Path(configured).expanduser() if configured else user_library() / FOLDER


def manifest_path(folder: Path | None = None) -> Path:
    return (folder or kits_dir()) / "kits.yaml"


def load_manifest(folder: Path | None = None) -> dict[str, dict[str, Any]]:
    """{kit name: entry} from kits.yaml. Missing file: no kits."""
    path = manifest_path(folder)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as e:
        raise ValueError(f"{path} isn't valid YAML: {e}") from e
    kits = data.get("kits", data) if isinstance(data, dict) else None
    if not isinstance(kits, dict):
        raise ValueError(f"{path} should map kit names to their details under 'kits:'.")
    return {str(name): {k: v for k, v in (entry or {}).items() if k in KIT_KEYS} for name, entry in kits.items()}


def files_on_disk(folder: Path | None = None) -> list[str]:
    """Rack files under the kits folder, relative to it."""
    root = folder or kits_dir()
    if not root.is_dir():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.suffix.lower() in RACK_SUFFIXES)


def browser_path(relative: str) -> str:
    """Where Live's browser shows a kit file ('Vocal Chains/UAD Lead Chain.adg')."""
    parts = [p.replace("\\", "\\\\").replace("/", "\\/") for p in Path(relative).parts]
    return "/".join([BROWSER_ROOT, *parts])


def catalog(folder: Path | None = None, browser_files: list[str] | None = None) -> list[dict[str, Any]]:
    """Every kit in the manifest, plus rack files in the folder the manifest doesn't list yet.
    browser_files: the files Live's browser shows (relative paths), or None when Live wasn't asked."""
    root = folder or kits_dir()
    manifest = load_manifest(root)
    disk = set(files_on_disk(root))
    out = []
    listed_files = set()
    for name, entry in manifest.items():
        kit = {"name": name, **entry}
        rel = entry.get("file")
        if not rel:
            kit["problem"] = "The manifest entry has no file."
        else:
            listed_files.add(rel)
            if rel not in disk:
                kit["problem"] = f"{rel} isn't in {root}."
            elif browser_files is not None and rel not in browser_files:
                kit["problem"] = "Live's browser doesn't show it yet; it appears once Live rescans the User Library."
            else:
                kit["browser_path"] = browser_path(rel)
        out.append(kit)
    for rel in sorted(disk - listed_files):
        out.append(
            {
                "name": Path(rel).stem,
                "file": rel,
                "role": Path(rel).parts[0] if len(Path(rel).parts) > 1 else None,
                "browser_path": browser_path(rel),
                "unlisted": True,
            }
        )
    return out


def matches(
    kit: dict[str, Any], role: str | None = None, tags: list[str] | None = None, text: str | None = None
) -> bool:
    def norm(s: Any) -> str:
        return " ".join(str(s).casefold().split())

    if role and norm(role) not in (
        norm(kit.get("role") or ""),
        norm(Path(kit.get("file") or "").parts[0] if kit.get("file") else ""),
    ):
        return False
    kit_tags = {norm(t) for t in kit.get("tags") or []}
    if tags and not all(norm(t) in kit_tags for t in tags):
        return False
    if text:
        hay = norm(
            " ".join(
                [
                    kit["name"],
                    kit.get("role") or "",
                    kit.get("range") or "",
                    kit.get("notes") or "",
                    " ".join(kit_tags),
                    " ".join((kit.get("macros") or {}).keys()),
                ]
            )
        )
        if not all(w in hay for w in norm(text).split()):
            return False
    return True


def find(kits: list[dict[str, Any]], name: str) -> dict[str, Any]:
    key = " ".join(name.casefold().split())
    exact = [k for k in kits if " ".join(k["name"].casefold().split()) == key]
    if exact:
        return exact[0]
    partial = [k for k in kits if key in k["name"].casefold()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        raise LookupError(
            f"'{name}' matches {len(partial)} kits: {', '.join(k['name'] for k in partial)}. Use the full name."
        )
    raise LookupError(f"No kit called '{name}'. list_kits shows them.")


def create_folders(folder: Path | None = None) -> Path:
    root = folder or kits_dir()
    for sub in SUBFOLDERS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


MACRO_SKIP = ("Device On", "Chain Selector")


def macros(parameters: list[dict[str, Any]], described: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """A loaded rack's macros with their names, values and what the manifest says they do. Unmapped
    macros keep Live's default name ('Macro 5') and are left out."""
    out = []
    for p in parameters:
        if p["name"] in MACRO_SKIP or re.fullmatch(r"Macro \d+", p["name"]):
            continue
        macro = {"index": p["index"], "name": p["name"], "value": p["value"], "display": p["display"]}
        if described and p["name"] in described:
            macro["does"] = described[p["name"]]
        out.append(macro)
    return out
