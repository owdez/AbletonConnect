"""Merge installs into one entry per plugin, match them to Live's browser, and read and write
plugins.json with the user's plugin_roles.yaml merged in at read time."""

from __future__ import annotations

import json
import os
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ..config import config_file
from .disk import Install

INDEX_VERSION = 1
FORMAT_ORDER = ("VST3", "AU", "VST2")  # load preference when the caller doesn't ask for one
LIVE_FORMATS = {"VST3": "VST3", "VST": "VST2", "VST2": "VST2", "AU": "AU", "AUv2": "AU", "AUv3": "AU"}
SETTING_FOR = {
    "AU": "Use Audio Units v2/v3",
    "VST2": "Use VST2 Plug-In System Folders",
    "VST3": "Use VST3 Plug-In System Folders",
}
ROLE_KEYS = ("role", "tags", "notes", "draft")
# What a scan learns by loading a plugin in Live; kept across rescans while the plugin is unchanged.
CAPTURE_KEYS = (
    "parameters",
    "parameter_count",
    "programs",
    "captured",
    "needs_attention",
    "live_kind",
    "loaded_install",
)


def index_path() -> Path:
    return config_file("plugins.json", "ABLETONCONNECT_PLUGIN_INDEX")


def roles_path() -> Path:
    return config_file("plugin_roles.yaml", "ABLETONCONNECT_PLUGIN_ROLES")


def norm(name: str) -> str:
    return " ".join(str(name).casefold().split())


# ---- merging installs ---------------------------------------------------------------------------


def merge(installs: list[Install]) -> list[dict]:
    """One entry per plugin, its formats keyed VST3/AU/VST2, each a list of installs (two when the
    same format is installed twice, like a plugin in both /Library and ~/Library).

    A VST3 or VST2 bundle without moduleinfo.json only has its bundle name ("uaudio_ua_1176"), so it
    takes the name of the AU in the same bundle (same identifier or file name). An AU whose name
    differs from its bundle's ("Guitar Rig 7 FX" in "Guitar Rig 7.component") joins the plugin
    named like the bundle when there is one."""
    au_by_bundle: dict[str, list[Install]] = {}
    for inst in installs:
        if inst.format == "AU":
            for key in (inst.bundle_id, Path(inst.path).stem if inst.path else None):
                if key:
                    au_by_bundle.setdefault(key, []).append(inst)
    for inst in installs:
        if inst.format != "AU" and inst.name_source == "plist":
            twins = au_by_bundle.get(inst.bundle_id or "") or au_by_bundle.get(Path(inst.path).stem) or []
            names = {t.name for t in twins}
            if len(names) == 1:
                inst.name = names.pop()
                inst.name_source = "au"

    groups: dict[str, list[Install]] = {}
    for inst in installs:
        if inst.format != "AU":
            groups.setdefault(norm(inst.name), []).append(inst)
    for inst in installs:
        if inst.format != "AU":
            continue
        key = norm(inst.name)
        if key not in groups and inst.bundle_name and norm(inst.bundle_name) in groups:
            key = norm(inst.bundle_name)
        groups.setdefault(key, []).append(inst)
    return [_entry(group) for _, group in sorted(groups.items())]


def _first(values):
    return next((v for v in values if v), None)


def _name(group: list[Install]) -> str:
    """A VST name from moduleinfo or borrowed from its AU, else any VST name (Guitar Rig 7, whose
    AU bundle holds 'Guitar Rig 7 FX' and 'MFX'), else the AU's."""
    for wanted in (lambda i: i.format != "AU" and i.name_source != "plist", lambda i: i.format != "AU", lambda i: True):
        names = [i.name for i in group if wanted(i)]
        if names:
            return names[0]
    raise ValueError("empty group")


def _entry(group: list[Install]) -> dict:
    by_format: dict[str, list[dict]] = {}
    for inst in group:
        by_format.setdefault(inst.format, []).append(inst.to_dict())
    newest = sorted(group, key=lambda i: (i.modified or "", i.scope == "user"), reverse=True)
    vendors = [i.vendor for i in newest if i.format == "VST3" and i.name_source == "moduleinfo"]
    vendors += [i.vendor for i in newest if i.format == "AU"] + [i.vendor for i in newest]
    kinds = [i.kind for i in group if i.format == "VST3"] + [i.kind for i in group]
    categories = sorted({c for i in group for c in i.categories})
    entry: dict[str, Any] = {
        "name": _name(newest),
        "vendor": _first(vendors),
        "version": _first(i.version for i in newest),
        "kind": _first(kinds),
        "categories": categories,
        "formats": {f: by_format[f] for f in FORMAT_ORDER if f in by_format},
    }
    paths = {f: sorted({i["path"] for i in insts if i.get("path")}) for f, insts in entry["formats"].items()}
    dupes = [{"format": f, "paths": p} for f, p in paths.items() if len(p) > 1]
    if dupes:
        entry["duplicates"] = dupes
    return entry


# ---- Live's browser -----------------------------------------------------------------------------


def parse_plugin_uri(uri: str) -> dict | None:
    """'query:Plugins#VST3:Universal%20Audio%20(UADx):UADx%201176%20FET%20Compressor' -> format, vendor, name."""
    if "#" not in uri:
        return None
    parts = uri.split("#", 1)[1].split(":")
    if len(parts) < 2:
        return None
    fmt = LIVE_FORMATS.get(parts[0], parts[0])
    name = urllib.parse.unquote(parts[-1])
    vendor = urllib.parse.unquote(":".join(parts[1:-1])) or None
    return {"format": fmt, "vendor": vendor, "name": name}


def match_live(entries: list[dict], browser_items: list[dict]) -> dict:
    """Mark what Live lists: each install Live can load gets live_path; each entry gets loadable,
    load_path (VST3, then AU, then VST2) and, per format Live doesn't list, the likely reason.
    browser_items: [{"name", "path", "uri"}] under Live's plugins category. Returns a summary."""
    parsed = []
    for item in browser_items:
        info = parse_plugin_uri(item.get("uri") or "") or {"format": None, "vendor": None, "name": item["name"]}
        parsed.append({**item, **info})
    formats_in_live = sorted({p["format"] for p in parsed if p["format"]})
    by_name: dict[str, list[dict]] = {}
    for p in parsed:
        by_name.setdefault(norm(p["name"]), []).append(p)
    unmatched = {id(p): p for p in parsed}

    for entry in entries:
        names = {norm(entry["name"])} | {norm(i["name"]) for insts in entry["formats"].values() for i in insts}
        live_items = [p for n in names for p in by_name.get(n, [])]
        entry["live"] = {}
        entry.pop("missing", None)
        for fmt, insts in entry["formats"].items():
            items = [p for p in live_items if p["format"] == fmt]
            for p in items:
                unmatched.pop(id(p), None)
                same_vendor = [i for i in insts if p["vendor"] and norm(i.get("vendor") or "") == norm(p["vendor"])]
                for inst in same_vendor or insts:
                    inst["live_path"] = p["path"]
            if items:
                entry["live"][fmt] = [p["path"] for p in items]
            else:
                entry.setdefault("missing", {})[fmt] = _missing_reason(fmt, formats_in_live)
        order = [f for f in FORMAT_ORDER if f in entry["live"]]
        entry["loadable"] = bool(order)
        entry["load_path"] = _preferred_path(entry, order[0]) if order else None
        if entry.get("vendor") is None:
            entry["vendor"] = _first(p["vendor"] for p in live_items)
    return {
        "formats": formats_in_live,
        "items": len(parsed),
        "unmatched": [p["path"] for p in unmatched.values()],
    }


def _preferred_path(entry: dict, fmt: str) -> str:
    """With two installs of one format (each listed by Live under its own vendor), the newest."""
    insts = [i for i in entry["formats"][fmt] if i.get("live_path")]
    insts.sort(key=lambda i: (i.get("modified") or "", i.get("scope") == "user"), reverse=True)
    return insts[0]["live_path"] if insts else entry["live"][fmt][0]


def _missing_reason(fmt: str, formats_in_live: list[str]) -> str:
    if fmt not in formats_in_live:
        return f"Live lists no {fmt} plugins at all: '{SETTING_FOR.get(fmt, fmt)}' is probably off in Live's Settings > Plug-Ins."
    return f"Live lists other {fmt} plugins but not this one: rescan in Live's Settings > Plug-Ins, or Live failed to validate it."


# ---- the file -----------------------------------------------------------------------------------


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path: Path | None = None) -> dict:
    path = path or index_path()
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(index: dict, path: Path | None = None) -> Path:
    path = path or index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
    return path


def carry_over(entries: list[dict], previous: dict | None) -> None:
    """Keep parameters and programs captured by an earlier scan while the plugin's version and
    load path are unchanged, so a rescan only loads new or updated plugins."""
    if not previous:
        return
    old = {norm(e["name"]): e for e in previous.get("plugins", [])}
    for entry in entries:
        before = old.get(norm(entry["name"]))
        if (
            before
            and before.get("version") == entry.get("version")
            and before.get("load_path") == entry.get("load_path")
        ):
            for key in CAPTURE_KEYS:
                if key in before:
                    entry[key] = before[key]


# ---- the user's notes ---------------------------------------------------------------------------


def load_roles(path: Path | None = None) -> dict[str, dict]:
    path = path or roles_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as e:
        raise ValueError(f"{path} isn't valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} should map plugin names to notes.")
    return {norm(k): v for k, v in data.items() if isinstance(v, dict)}


def with_roles(entries: list[dict], roles: dict[str, dict]) -> list[dict]:
    out = []
    for entry in entries:
        note = roles.get(norm(entry["name"]))
        if note:
            entry = {**entry, **{k: note[k] for k in ROLE_KEYS if k in note}}
        out.append(entry)
    return out


def read(index_file: Path | None = None, roles_file: Path | None = None) -> dict:
    """The index with the user's roles merged in, for the MCP tools."""
    index = load(index_file)
    index["plugins"] = with_roles(index.get("plugins", []), load_roles(roles_file))
    return index


# ---- queries for the tools ----------------------------------------------------------------------


def find(entries: list[dict], name: str) -> dict:
    """An entry by exact name, else the only one whose name contains the text."""
    key = norm(name)
    exact = [e for e in entries if norm(e["name"]) == key]
    if exact:
        return exact[0]
    partial = [e for e in entries if key in norm(e["name"])]
    if len(partial) == 1:
        return partial[0]
    if partial:
        names = ", ".join(e["name"] for e in partial[:12])
        raise LookupError(f"'{name}' matches {len(partial)} plugins: {names}. Use the full name.")
    raise LookupError(
        f"No plugin called '{name}' in the index. list_plugins shows what's there; run abletonconnect-scan-plugins after installing new ones."
    )


def annotate_devices(result: Any, entries: list[dict]) -> Any:
    """Add the vendor and exact format to every plugin device in a bridge result, including the
    devices in rack chains. The bridge only knows "VST" or "AU" from the device's class."""
    by_name = {norm(e["name"]): e for e in entries}

    def visit(devices: Any) -> None:
        for device in devices or ():
            if not isinstance(device, dict):
                continue
            entry = by_name.get(norm(device.get("name", ""))) if "plugin" in device else None
            if entry:
                if entry.get("vendor"):
                    device["vendor"] = entry["vendor"]
                vst = [f for f in (entry.get("live") or {}) if f != "AU"]
                if device["plugin"] == "VST" and len(vst) == 1:
                    device["plugin"] = vst[0]
            for chain in device.get("chains") or ():
                visit(chain.get("devices"))

    if isinstance(result, dict):
        visit(result.get("devices"))
    return result


def summary(entry: dict) -> dict:
    out = {k: entry[k] for k in ("name", "vendor", "kind") if entry.get(k)}
    for key in ("role", "tags", "categories"):
        if entry.get(key):
            out[key] = entry[key]
    if entry.get("draft"):
        out["role_draft"] = True
    out["formats"] = list(entry.get("formats", {}))
    out["loadable"] = bool(entry.get("loadable"))
    if "parameter_count" in entry:
        out["parameters"] = entry["parameter_count"]
    if entry.get("programs"):
        out["programs"] = len(entry["programs"])
    if entry.get("needs_attention"):
        out["needs_attention"] = entry["needs_attention"]
    return out


def filter_entries(entries, kind=None, vendor=None, role=None, text=None, loadable_only=False):
    out = []
    for e in entries:
        if kind and (e.get("live_kind") or e.get("kind")) != kind:
            continue
        if vendor and norm(vendor) not in norm(e.get("vendor") or ""):
            continue
        if role and norm(role) != norm(e.get("role") or ""):
            continue
        if loadable_only and not e.get("loadable"):
            continue
        if text:
            hay = norm(
                " ".join(
                    [
                        e["name"],
                        e.get("vendor") or "",
                        e.get("notes") or "",
                        " ".join(e.get("categories", [])),
                        " ".join(map(str, e.get("tags") or [])),
                    ]
                )
            )
            if not all(word in hay for word in norm(text).split()):
                continue
        out.append(e)
    return out
