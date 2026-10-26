"""The plugin index: parsers on real auval and moduleinfo.json output, the merge and Live matching on
a fake plugin folder built from the plists found on a real machine, and the user's roles."""

import argparse
import asyncio
import json
import os
import plistlib
from pathlib import Path

import pytest
import yaml

from abletonconnect.plugins import cli, disk, index
from abletonconnect.plugins.roles_seed import seed_roles

FIXTURES = Path(__file__).parent / "fixtures" / "plugins"
AUVAL = (FIXTURES / "auval.txt").read_text()
DUB_ECHO_INFO = (FIXTURES / "moduleinfo_dub_echo.json5").read_text()


def bundle(folder: Path, name: str, plist: dict, moduleinfo: str | None = None, mtime: float | None = None) -> Path:
    path = folder / name
    (path / "Contents" / "MacOS").mkdir(parents=True)
    with open(path / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(plist, f)
    binary = path / "Contents" / "MacOS" / plist.get("CFBundleExecutable", Path(name).stem)
    binary.write_bytes(b"\0")
    if mtime:
        os.utime(binary, (mtime, mtime))
    if moduleinfo:
        (path / "Contents" / "Resources").mkdir()
        (path / "Contents" / "Resources" / "moduleinfo.json").write_text(moduleinfo)
    return path


SPECTRAFORM_INFO = """{"Name": "SpectraForm", "Version": "0.1.0", "Factory Info": {"Vendor": "VENDOR",},
 "Classes": [{"Category": "Audio Module Class", "Name": "SpectraForm", "Vendor": "VENDOR", "Version": "0.1.0",
   "Sub Categories": ["Instrument", "Synth",],},],}"""


def spectraform_info(vendor: str) -> str:
    return SPECTRAFORM_INFO.replace("VENDOR", vendor)


def au(name: str, type_: str, subtype: str, manufacturer: str) -> dict:
    return {"name": name, "type": type_, "subtype": subtype, "manufacturer": manufacturer, "version": 1}


@pytest.fixture
def plugin_roots(tmp_path):
    """Bundles shaped like the ones on the machine this was built on (fields copied from their plists)."""
    system, user = tmp_path / "Library" / "Audio" / "Plug-Ins", tmp_path / "home" / "Library" / "Audio" / "Plug-Ins"
    for root in (system, user):
        for sub in ("VST3", "Components", "VST"):
            (root / sub).mkdir(parents=True)
    uad = {
        "CFBundleName": "uaudio_ua_1176",
        "CFBundleIdentifier": "com.uaudio.effects.U3DY",
        "CFBundleShortVersionString": "1.0.3",
        "NSHumanReadableCopyright": "Copyright © 2022 Universal Audio. All rights reserved.",
    }
    bundle(system / "VST3", "uaudio_ua_1176.vst3", uad)
    bundle(
        system / "Components",
        "uaudio_ua_1176.component",
        {**uad, "AudioComponents": [au("Universal Audio (UADx): UADx 1176 FET Compressor", "aufx", "U3DY", "UADx")]},
    )
    gr = {
        "CFBundleName": "Guitar Rig 7",
        "CFBundleShortVersionString": "7.0.2",
        "CFBundleGetInfoString": "7.0.2 (R0), Copyright © 2024 Native Instruments GmbH",
    }
    bundle(system / "VST3", "Guitar Rig 7.vst3", {**gr, "CFBundleIdentifier": "Guitar Rig 7.vst3"})
    bundle(
        system / "Components",
        "Guitar Rig 7.component",
        {
            **gr,
            "CFBundleIdentifier": "Guitar Rig 7.MusicDevice.component",
            "AudioComponents": [
                au("Native Instruments: Guitar Rig 7 FX", "aufx", "Ni$T", "-NI-"),
                au("Native Instruments: Guitar Rig 7 MFX", "aumf", "Ni$T", "-NI-"),
            ],
        },
    )
    rx = {
        "CFBundleShortVersionString": "10.5.0",
        "CFBundleGetInfoString": "10.5.0, Copyright © 2001-2024 iZotope, Inc.",
    }
    bundle(
        system / "VST3",
        "RX 10 De-click.vst3",
        {**rx, "CFBundleName": "RX 10 De-click", "CFBundleIdentifier": "com.izotope.VST3.RXPRODCLICK"},
    )
    bundle(
        system / "Components",
        "iZRX10De-clickAUHook.component",
        {
            **rx,
            "CFBundleName": "iZRX10De-clickAUHook",
            "CFBundleIdentifier": "com.izotope.AU.RXPRODCLICK",
            "AudioComponents": [au("iZotope: RX 10 De-click", "aufx", "rZK0", "iZtp")],
        },
    )
    mx = {
        "CFBundleName": "Massive X",
        "CFBundleShortVersionString": "1.7.1",
        "NSHumanReadableCopyright": "Copyright © 2026 Native Instruments",
    }
    bundle(system / "VST3", "Massive X.vst3", {**mx, "CFBundleIdentifier": "com.native-instruments.MassiveX.vst3"})
    bundle(system / "VST", "Massive X.vst", {**mx, "CFBundleIdentifier": "com.native-instruments.MassiveX.vst"})
    bundle(
        system / "Components",
        "AUBandpass.component",
        {"CFBundleName": "AUBandpass", "AudioComponents": [au("Apple: AUBandpass", "aufx", "bpas", "appl")]},
    )
    sf = {
        "CFBundleName": "SpectraForm",
        "CFBundleIdentifier": "com.slvcstudios.spectraform",
        "CFBundleShortVersionString": "0.1.0",
    }
    bundle(system / "VST3", "SpectraForm.vst3", sf, spectraform_info("SLVC Studios"), mtime=1_789_000_000)
    bundle(user / "VST3", "SpectraForm.vst3", sf, spectraform_info("SLVC"), mtime=1_792_000_000)
    bundle(
        system / "Components",
        "SpectraForm.component",
        {**sf, "AudioComponents": [au("SLVC Studios: SpectraForm", "aumu", "SPF1", "SLVc")]},
    )
    bundle(
        user / "Components",
        "SpectraForm.component",
        {**sf, "AudioComponents": [au("SLVC: SpectraForm", "aumu", "SPF1", "SLVc")]},
    )
    bundle(
        user / "VST3",
        "SLVC Dub Echo.vst3",
        {"CFBundleName": "SLVC Dub Echo", "CFBundleIdentifier": "com.slvc.dubecho"},
        DUB_ECHO_INFO,
    )
    return ((str(system), "system"), (str(user), "user"))


def live_item(vendor, name, fmt="VST3"):
    quote = lambda s: s.replace(" ", "%20")  # noqa: E731
    return {
        "name": name,
        "path": f"plugins/{vendor}/{name}",
        "uri": f"query:Plugins#{fmt}:{quote(vendor)}:{quote(name)}",
    }


LIVE_ITEMS = [
    live_item("Universal Audio (UADx)", "UADx 1176 FET Compressor"),
    live_item("Native Instruments", "Guitar Rig 7"),
    live_item("iZotope", "RX 10 De-click"),
    live_item("SLVC", "SpectraForm"),
    live_item("SLVC Studios", "SpectraForm"),
    live_item("SLVC", "SLVC Dub Echo"),
    live_item("Mystery", "Mystery Synth"),
]


@pytest.fixture
def entries(plugin_roots):
    return index.merge(disk.scan_disk(plugin_roots, auval_text=AUVAL))


def by_name(entries):
    return {e["name"]: e for e in entries}


# ---- parsers ------------------------------------------------------------------------------------


def test_parse_auval_skips_apple_and_log_noise():
    units = disk.parse_auval(AUVAL)
    assert [(u["type"], u["name"]) for u in units] == [
        ("aufx", "Guitar Rig 7 FX"),
        ("aufx", "SLVC Dub Echo"),
        ("aufx", "UADx 1176 FET Compressor"),
        ("aufx", "RX 10 De-click"),
        ("aumf", "Guitar Rig 7 MFX"),
        ("aumu", "Komplete Kontrol"),  # its log line printed onto the same line is cut off
        ("aumu", "SpectraForm"),
    ]
    assert units[2]["vendor"] == "Universal Audio (UADx)" and units[2]["manufacturer"] == "UADx"
    assert [u["kind"] for u in units[4:6]] == ["effect", "instrument"]


def test_moduleinfo_is_json5():
    info = disk.loads_json5(DUB_ECHO_INFO)
    assert disk.vst3_from_moduleinfo(info) == {
        "name": "SLVC Dub Echo",
        "vendor": "SLVC",
        "version": "0.1.0",
        "kind": "effect",
        "categories": ["Delay"],
    }
    tricky = '{"a": "x, ]", // a comment, with a comma\n "url": "http://e.com", /* block */ "b": [1, 2,],}'
    assert disk.loads_json5(tricky) == {"a": "x, ]", "url": "http://e.com", "b": [1, 2]}


@pytest.mark.parametrize(
    "text, vendor",
    [
        ("Copyright © 2022 Universal Audio. All rights reserved.", "Universal Audio"),
        ("10.5.0, Copyright © 2001-2024 iZotope, Inc.", "iZotope"),
        ("7.0.2 (R0), Copyright © 2024 Native Instruments GmbH", "Native Instruments"),
        ("1.7.1, Copyright © 2026 Native Instruments", "Native Instruments"),
        ("", None),
    ],
)
def test_vendor_from_plist(text, vendor):
    assert disk.vendor_from_plist({"NSHumanReadableCopyright": text}) == vendor


# ---- disk scan and merge ------------------------------------------------------------------------


def test_scan_disk_reads_every_format_and_marks_the_registered_au(plugin_roots):
    installs = disk.scan_disk(plugin_roots, auval_text=AUVAL)
    formats = sorted({i.format for i in installs})
    assert formats == ["AU", "VST2", "VST3"]
    assert not any(i.vendor == "Apple" or (i.au_codes or "").endswith("appl") for i in installs)
    spectra_aus = {i.vendor: i.registered for i in installs if i.format == "AU" and i.name == "SpectraForm"}
    assert spectra_aus == {"SLVC Studios": False, "SLVC": True}  # macOS registers one copy per code
    registry_only = {i.name for i in installs if i.scope == "registry"}
    assert registry_only == {"SLVC Dub Echo", "Komplete Kontrol"}  # auval lists them; no bundle here


def test_merge_names_formats_and_duplicates(entries):
    plugins = by_name(entries)
    assert set(plugins) == {
        "UADx 1176 FET Compressor",
        "Guitar Rig 7",
        "RX 10 De-click",
        "SpectraForm",
        "SLVC Dub Echo",
        "Massive X",
        "Komplete Kontrol",
    }
    uad = plugins["UADx 1176 FET Compressor"]  # the VST3 bundle only says "uaudio_ua_1176"
    assert (
        list(uad["formats"]) == ["VST3", "AU"] and uad["vendor"] == "Universal Audio (UADx)" and uad["kind"] == "effect"
    )
    gr = plugins["Guitar Rig 7"]  # its AU bundle holds "Guitar Rig 7 FX" and "MFX"
    assert [i["name"] for i in gr["formats"]["AU"]] == [
        "Guitar Rig 7 FX",
        "Guitar Rig 7 MFX",
    ] and "duplicates" not in gr
    assert (
        list(plugins["Massive X"]["formats"]) == ["VST3", "VST2"]
        and plugins["Massive X"]["vendor"] == "Native Instruments"
    )
    assert plugins["RX 10 De-click"]["vendor"] == "iZotope" and list(plugins["RX 10 De-click"]["formats"]) == [
        "VST3",
        "AU",
    ]
    sf = plugins["SpectraForm"]
    assert sf["vendor"] == "SLVC" and sf["kind"] == "instrument" and sf["categories"] == ["Synth"]  # the newer build
    assert [d["format"] for d in sf["duplicates"]] == ["VST3", "AU"]


def test_match_live_records_paths_reasons_and_the_newest_duplicate(entries):
    summary = index.match_live(entries, LIVE_ITEMS)
    assert summary["formats"] == ["VST3"] and summary["unmatched"] == ["plugins/Mystery/Mystery Synth"]
    plugins = by_name(entries)
    uad = plugins["UADx 1176 FET Compressor"]
    assert uad["loadable"] and uad["load_path"] == "plugins/Universal Audio (UADx)/UADx 1176 FET Compressor"
    assert "Use Audio Units" in uad["missing"]["AU"]
    sf = plugins["SpectraForm"]
    assert {i["vendor"]: i["live_path"] for i in sf["formats"]["VST3"]} == {
        "SLVC Studios": "plugins/SLVC Studios/SpectraForm",
        "SLVC": "plugins/SLVC/SpectraForm",
    }
    assert sf["load_path"] == "plugins/SLVC/SpectraForm"
    mx = plugins["Massive X"]
    assert mx["loadable"] is False and mx["load_path"] is None
    assert "lists other VST3 plugins but not this one" in mx["missing"]["VST3"] and "VST2" in mx["missing"]["VST2"]
    assert index.parse_plugin_uri("query:Plugins#AUv2:Native%20Instruments:Massive%20X") == {
        "format": "AU",
        "vendor": "Native Instruments",
        "name": "Massive X",
    }


def test_rescans_keep_captures_of_unchanged_plugins(entries):
    index.match_live(entries, LIVE_ITEMS)
    previous = {"plugins": [dict(e) for e in entries]}
    for e in previous["plugins"]:
        e.update(parameters=[{"name": "Input"}], parameter_count=1, captured={"at": "then"})
    previous["plugins"][0]["version"] = "0.0.1"  # this one was updated since
    fresh = [{k: v for k, v in e.items() if k not in index.CAPTURE_KEYS} for e in entries]
    index.carry_over(fresh, previous)
    kept = [e["name"] for e in fresh if "captured" in e]
    assert previous["plugins"][0]["name"] not in kept and len(kept) == len(fresh) - 1


# ---- the user's notes and the queries -----------------------------------------------------------


def test_roles_merge_filter_and_find(entries, tmp_path):
    index.match_live(entries, LIVE_ITEMS)
    roles = tmp_path / "plugin_roles.yaml"
    roles.write_text(
        "UADx 1176 FET Compressor:\n  role: compressor\n  tags: [fet, vocals]\n  notes: Fast. Vocals and drums.\nSpectraForm: {role: synth, draft: true}\n"
    )
    merged = index.with_roles(entries, index.load_roles(roles))
    assert by_name(merged)["UADx 1176 FET Compressor"]["role"] == "compressor"
    assert [e["name"] for e in index.filter_entries(merged, role="Compressor")] == ["UADx 1176 FET Compressor"]
    assert [e["name"] for e in index.filter_entries(merged, kind="instrument", loadable_only=True)] == ["SpectraForm"]
    assert [e["name"] for e in index.filter_entries(merged, text="fast vocals")] == ["UADx 1176 FET Compressor"]
    assert {e["name"] for e in index.filter_entries(merged, vendor="native")} == {
        "Guitar Rig 7",
        "Massive X",
        "Komplete Kontrol",
    }
    assert index.summary(by_name(merged)["SpectraForm"])["role_draft"] is True
    assert index.find(merged, "1176")["name"] == "UADx 1176 FET Compressor"
    with pytest.raises(
        LookupError, match=r"'x' matches 3 plugins: Massive X, RX 10 De-click, UADx 1176 FET Compressor\."
    ):
        index.find(merged, "x")
    with pytest.raises(LookupError, match="No plugin called 'Serum'"):
        index.find(merged, "Serum")
    roles.write_text("- just a list\n")
    with pytest.raises(ValueError, match="should map plugin names"):
        index.load_roles(roles)


def test_seeded_roles_are_drafts_and_never_overwrite(entries, tmp_path):
    path = tmp_path / "plugin_roles.yaml"
    assert seed_roles(entries, path) == path
    data = yaml.safe_load(path.read_text())
    assert data["UADx 1176 FET Compressor"] == {
        "role": "compressor",
        "tags": ["fet", "fast", "vocals", "drums"],
        "notes": "Fast FET compression: vocals, drums, bass.",
        "draft": True,
    }
    assert data["SLVC Dub Echo"]["role"] == "delay" and all(v["draft"] for v in data.values())
    path.write_text("Mine: {role: mine}\n")
    assert seed_roles(entries, path) is None and path.read_text() == "Mine: {role: mine}\n"


def test_annotate_devices_adds_vendor_and_format(entries):
    index.match_live(entries, LIVE_ITEMS)
    result = {
        "devices": [
            {"name": "Rack", "chains": [{"devices": [{"name": "UADx 1176 FET Compressor", "plugin": "VST"}]}]},
            {"name": "EQ Eight"},
        ]
    }
    nested = index.annotate_devices(result, entries)["devices"][0]["chains"][0]["devices"][0]
    assert nested == {"name": "UADx 1176 FET Compressor", "plugin": "VST3", "vendor": "Universal Audio (UADx)"}
    assert result["devices"][1] == {"name": "EQ Eight"}


def test_cli_without_live_writes_the_index_and_draft_roles(plugin_roots, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(disk, "PLUGIN_ROOTS", plugin_roots)
    monkeypatch.setattr(disk, "run_auval", lambda: AUVAL)
    monkeypatch.setenv("ABLETONCONNECT_PLUGIN_ROLES", str(tmp_path / "roles.yaml"))
    out = tmp_path / "plugins.json"
    args = argparse.Namespace(
        no_live=True,
        index=str(out),
        only=None,
        force=False,
        yes=True,
        timeout=30,
        settle=1,
        screenshots=None,
        flag=["Massive X=Needs its content updated."],
    )
    assert asyncio.run(cli.scan(args)) == 0
    data = json.loads(out.read_text())
    assert data["version"] == index.INDEX_VERSION and len(data["plugins"]) == 7
    assert by_name(data["plugins"])["Massive X"]["needs_attention"] == "Needs its content updated."
    assert (tmp_path / "roles.yaml").exists() and "draft roles" in capsys.readouterr().out
