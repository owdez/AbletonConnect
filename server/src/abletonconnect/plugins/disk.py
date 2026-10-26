"""Installed plugin bundles: VST3 (moduleinfo.json, else Info.plist), VST2 (Info.plist) and Audio
Units (`auval -a`, matched to their .component bundles for paths and versions)."""

from __future__ import annotations

import json
import plistlib
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

PLUGIN_ROOTS = (("/Library/Audio/Plug-Ins", "system"), ("~/Library/Audio/Plug-Ins", "user"))
FORMAT_DIRS = {"VST3": ("VST3", ".vst3"), "VST2": ("VST", ".vst"), "AU": ("Components", ".component")}

# Audio Unit types: aumf is an effect that also takes MIDI (Guitar Rig's MFX), aumi a MIDI effect.
AU_KINDS = {"aumu": "instrument", "aufx": "effect", "aumf": "effect", "aumi": "midi_effect"}
APPLE = "appl"

_AUVAL_LINE = re.compile(
    r"^(?P<type>[a-z]{4}) (?P<subtype>.{4}) (?P<manufacturer>.{4})  -  (?P<vendor>[^:]+): (?P<name>.+)$"
)
_LOG_TAIL = re.compile(r"\[\d{4}-\d{2}-\d{2}[ T]\d.*$")  # some plugins print log lines into auval's output


@dataclass
class Install:
    """One plugin in one format at one path. An AU bundle can hold several (Guitar Rig 7 FX and MFX)."""

    format: str
    path: str
    scope: str  # "system" (/Library) or "user" (~/Library)
    name: str
    vendor: str | None = None
    version: str | None = None
    bundle_id: str | None = None
    bundle_name: str | None = None
    kind: str | None = None
    categories: list[str] = field(default_factory=list)
    au_codes: str | None = None  # "aufx U3DY UADx"
    name_source: str = "plist"  # "moduleinfo", "au" or "plist" (a bundle name like "uaudio_ua_1176")
    modified: str | None = None
    registered: bool | None = None  # AU only: whether macOS's AU registry (auval) lists this copy

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, [], "")}


# ---- parsers (pure, tested with fixtures) ----------------------------------------------------------


def parse_auval(text: str) -> list[dict]:
    """`auval -a` lines like 'aufx U3DY UADx  -  Universal Audio (UADx): UADx 1176 FET Compressor'.
    Apple's own units are skipped; log noise some plugins print in between is ignored."""
    out = []
    for raw in text.splitlines():
        m = _AUVAL_LINE.match(raw.rstrip())
        if not m or m["manufacturer"] == APPLE:
            continue
        name = _LOG_TAIL.sub("", m["name"]).strip()
        out.append(
            {
                "type": m["type"],
                "subtype": m["subtype"],
                "manufacturer": m["manufacturer"],
                "vendor": m["vendor"].strip(),
                "name": name,
                "kind": AU_KINDS.get(m["type"]),
            }
        )
    return out


def loads_json5(text: str) -> dict:
    """moduleinfo.json is JSON5: trailing commas and // comments. Strip both outside strings."""
    out, i, n, in_string = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        elif c == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
            out.append(c)
        else:
            out.append(c)
        i += 1
    return json.loads("".join(out))


def vst3_from_moduleinfo(info: dict) -> dict:
    """Name, vendor, version, kind and categories from a VST3 moduleinfo.json."""
    classes = [c for c in info.get("Classes", []) if c.get("Category") == "Audio Module Class"]
    cls = classes[0] if classes else {}
    subs = [str(s) for s in cls.get("Sub Categories", [])]
    if "Instrument" in subs:
        kind = "instrument"
    elif "Fx" in subs:
        kind = "effect"
    else:
        kind = None
    return {
        "name": cls.get("Name") or info.get("Name"),
        "vendor": cls.get("Vendor") or (info.get("Factory Info") or {}).get("Vendor"),
        "version": cls.get("Version") or info.get("Version"),
        "kind": kind,
        "categories": [s for s in subs if s not in ("Fx", "Instrument")],
    }


def vendor_from_plist(plist: dict) -> str | None:
    """'Copyright © 2026 Native Instruments' or '10.5.0, Copyright © 2001-2024 iZotope, Inc.' -> the company."""
    for key in ("NSHumanReadableCopyright", "CFBundleGetInfoString"):
        text = str(plist.get(key) or "")
        m = re.search(r"(?:©|\(c\)|Copyright)\s*(?:©\s*)?(?:\d{4}(?:\s*-\s*\d{4})?\s+)?(.+)", text, re.I)
        if not m:
            continue
        name = re.split(r"[.,](?:\s|$)", m.group(1))[0].strip()
        name = re.sub(r"\s+(?:GmbH|Inc|LLC|Ltd|AG)\.?$", "", name)
        if name:
            return name
    return None


def au_components(plist: dict) -> list[dict]:
    """The AudioComponents entries of a .component Info.plist."""
    out = []
    for comp in plist.get("AudioComponents") or []:
        vendor, _, name = str(comp.get("name", "")).partition(": ")
        out.append(
            {
                "type": comp.get("type"),
                "subtype": comp.get("subtype"),
                "manufacturer": comp.get("manufacturer"),
                "vendor": vendor if name else None,
                "name": name or vendor,
            }
        )
    return out


# ---- reading bundles ----------------------------------------------------------------------------


def _plist(bundle: Path) -> dict:
    try:
        with open(bundle / "Contents" / "Info.plist", "rb") as f:
            return plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {}


def _modified(bundle: Path) -> str | None:
    binaries = list((bundle / "Contents" / "MacOS").glob("*"))
    target = binaries[0] if binaries else bundle
    try:
        return datetime.fromtimestamp(target.stat().st_mtime, UTC).strftime("%Y-%m-%d")
    except OSError:
        return None


def read_vst3(bundle: Path, scope: str) -> list[Install]:
    plist = _plist(bundle)
    base = dict(
        format="VST3",
        path=str(bundle),
        scope=scope,
        bundle_id=plist.get("CFBundleIdentifier"),
        bundle_name=plist.get("CFBundleName") or bundle.stem,
        modified=_modified(bundle),
    )
    info_path = bundle / "Contents" / "Resources" / "moduleinfo.json"
    if info_path.exists():
        try:
            info = vst3_from_moduleinfo(loads_json5(info_path.read_text(encoding="utf-8")))
            return [
                Install(
                    name=info["name"] or bundle.stem,
                    vendor=info["vendor"],
                    version=info["version"],
                    kind=info["kind"],
                    categories=info["categories"],
                    name_source="moduleinfo",
                    **base,
                )
            ]
        except (OSError, ValueError):
            pass
    return [
        Install(
            name=base["bundle_name"],
            vendor=vendor_from_plist(plist),
            version=plist.get("CFBundleShortVersionString"),
            **base,
        )
    ]


def read_vst2(bundle: Path, scope: str) -> list[Install]:
    plist = _plist(bundle)
    return [
        Install(
            format="VST2",
            path=str(bundle),
            scope=scope,
            name=plist.get("CFBundleName") or bundle.stem,
            vendor=vendor_from_plist(plist),
            version=plist.get("CFBundleShortVersionString"),
            bundle_id=plist.get("CFBundleIdentifier"),
            bundle_name=plist.get("CFBundleName") or bundle.stem,
            modified=_modified(bundle),
        )
    ]


def read_au(bundle: Path, scope: str) -> list[Install]:
    plist = _plist(bundle)
    out = []
    for comp in au_components(plist):
        if comp["manufacturer"] == APPLE:
            continue
        out.append(
            Install(
                format="AU",
                path=str(bundle),
                scope=scope,
                name=comp["name"],
                vendor=comp["vendor"],
                version=plist.get("CFBundleShortVersionString"),
                bundle_id=plist.get("CFBundleIdentifier"),
                bundle_name=plist.get("CFBundleName") or bundle.stem,
                kind=AU_KINDS.get(comp["type"] or ""),
                au_codes="{} {} {}".format(comp["type"], comp["subtype"], comp["manufacturer"]),
                name_source="au",
                modified=_modified(bundle),
            )
        )
    return out


READERS = {"VST3": read_vst3, "VST2": read_vst2, "AU": read_au}


def run_auval() -> str | None:
    try:
        return subprocess.run(["auval", "-a"], capture_output=True, text=True, timeout=180).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def scan_disk(roots=None, auval_text: str | None = None) -> list[Install]:
    """Every plugin bundle under the roots. With auval output, each AU copy is marked registered
    or not (macOS keeps one copy per type/subtype/manufacturer), and AUs that auval lists but no
    bundle provides (AUv3 app extensions) are added with no path."""
    installs: list[Install] = []
    for root, scope in roots or PLUGIN_ROOTS:
        base = Path(root).expanduser()
        for fmt, (folder, suffix) in FORMAT_DIRS.items():
            directory = base / folder
            if not directory.is_dir():
                continue
            for bundle in sorted(directory.iterdir()):
                if bundle.suffix.lower() == suffix:
                    installs.extend(READERS[fmt](bundle, scope))
    if auval_text is not None:
        registered = {(a["type"], a["subtype"], a["manufacturer"]): a for a in parse_auval(auval_text)}
        seen = set()
        for inst in installs:
            if inst.format != "AU" or not inst.au_codes:
                continue
            codes = tuple(inst.au_codes.split(" "))
            entry = registered.get(codes)
            inst.registered = entry is not None and entry["name"] == inst.name and entry["vendor"] == inst.vendor
            if entry is not None:
                inst.kind = inst.kind or entry["kind"]
                seen.add(codes)
        for codes, entry in registered.items():
            if codes not in seen:
                installs.append(
                    Install(
                        format="AU",
                        path="",
                        scope="registry",
                        name=entry["name"],
                        vendor=entry["vendor"],
                        kind=entry["kind"],
                        au_codes=" ".join(codes),
                        name_source="au",
                        registered=True,
                    )
                )
    return installs
