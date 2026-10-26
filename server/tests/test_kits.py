"""Claude Kits: the manifest, what's on disk and in Live's browser, filters and macro reporting."""

import pytest

from abletonconnect import kits

MANIFEST = """\
kits:
  SLVC Dub Throw:
    file: FX/SLVC Dub Throw.adg
    role: fx
    tags: [dub, throw, delay]
    range: Any source.
    macros:
      Echo Time: Synced echo time.
      Feedback: Echo feedback.
    starter: true
  UAD Lead Chain:
    file: Vocal Chains/UAD Lead Chain.adg
    role: vocal chain
    tags: [lead vocal, warm]
  Gone Pad:
    file: Pads/Gone Pad.adg
    role: pad
"""


@pytest.fixture
def folder(tmp_path, monkeypatch):
    root = kits.create_folders(tmp_path / "Claude Kits")
    (root / "FX" / "SLVC Dub Throw.adg").write_bytes(b"\x1f\x8b")
    (root / "Vocal Chains" / "UAD Lead Chain.adg").write_bytes(b"\x1f\x8b")
    (root / "Drums" / "909 Kit.adg").write_bytes(b"\x1f\x8b")  # not in the manifest yet
    (root / "kits.yaml").write_text(MANIFEST)
    monkeypatch.setenv("ABLETONCONNECT_KITS", str(root))
    return root


def by_name(found):
    return {k["name"]: k for k in found}


def test_folders_and_manifest(folder):
    assert sorted(p.name for p in folder.iterdir() if p.is_dir()) == sorted(kits.SUBFOLDERS)
    assert kits.kits_dir() == folder
    manifest = kits.load_manifest()
    assert manifest["SLVC Dub Throw"]["macros"] == {"Echo Time": "Synced echo time.", "Feedback": "Echo feedback."}


def test_catalog_merges_manifest_disk_and_browser(folder):
    found = by_name(kits.catalog(browser_files=["FX/SLVC Dub Throw.adg", "Drums/909 Kit.adg"]))
    assert found["SLVC Dub Throw"]["browser_path"] == "user_library/Claude Kits/FX/SLVC Dub Throw.adg"
    assert found["SLVC Dub Throw"]["starter"] is True
    assert "doesn't show it yet" in found["UAD Lead Chain"]["problem"]  # on disk, Live hasn't rescanned
    assert "isn't in" in found["Gone Pad"]["problem"]
    assert found["909 Kit"] == {
        "name": "909 Kit",
        "file": "Drums/909 Kit.adg",
        "role": "Drums",
        "browser_path": "user_library/Claude Kits/Drums/909 Kit.adg",
        "unlisted": True,
    }
    without_live = by_name(kits.catalog())
    assert "browser_path" in without_live["UAD Lead Chain"]  # Live not asked: the file on disk is enough


def test_filters_and_find(folder):
    found = kits.catalog()
    assert [k["name"] for k in found if kits.matches(k, role="vocal chain")] == ["UAD Lead Chain"]
    assert [k["name"] for k in found if kits.matches(k, role="drums")] == ["909 Kit"]  # by its folder
    assert [k["name"] for k in found if kits.matches(k, tags=["Dub", "throw"])] == ["SLVC Dub Throw"]
    assert [k["name"] for k in found if kits.matches(k, text="echo time")] == ["SLVC Dub Throw"]
    assert kits.find(found, "dub")["name"] == "SLVC Dub Throw"
    with pytest.raises(LookupError, match="matches 2 kits"):
        kits.find(found, "a")  # UAD Lead Chain and Gone Pad
    with pytest.raises(LookupError, match="No kit called 'Bass'"):
        kits.find(found, "Bass")


def test_bad_manifest(folder):
    (folder / "kits.yaml").write_text("- a list\n")
    with pytest.raises(ValueError, match="should map kit names"):
        kits.load_manifest()
    (folder / "kits.yaml").write_text("kits: [\n")
    with pytest.raises(ValueError, match="isn't valid YAML"):
        kits.load_manifest()


def test_macros_leave_out_unmapped_ones():
    params = [
        {"index": 0, "name": "Device On", "value": 1, "display": "On"},
        {"index": 1, "name": "Echo Time", "value": 64.0, "display": "64"},
        {"index": 2, "name": "Macro 2", "value": 0.0, "display": "0"},
        {"index": 9, "name": "Chain Selector", "value": 0.0, "display": "0"},
    ]
    assert kits.macros(params, {"Echo Time": "Synced echo time."}) == [
        {"index": 1, "name": "Echo Time", "value": 64.0, "display": "64", "does": "Synced echo time."}
    ]


def test_user_library_comes_from_lives_library_cfg(tmp_path, monkeypatch):
    home = tmp_path / "home"
    prefs = home / "Library" / "Preferences" / "Ableton" / "Live 12.4.7"
    prefs.mkdir(parents=True)
    library = tmp_path / "Elsewhere" / "My Library"
    library.mkdir(parents=True)
    (prefs / "Library.cfg").write_text(
        f'<Ableton><ContentLibrary><UserLibrary><LibraryProject><ProjectPath Value="{library.parent}" />'
        '<ProjectName Value="My Library" /></LibraryProject></UserLibrary></ContentLibrary></Ableton>'
    )
    monkeypatch.setenv("HOME", str(home))
    assert kits.user_library() == library
