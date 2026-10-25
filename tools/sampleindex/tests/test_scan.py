"""Scanning with a stand-in analyzer, so these run without decoding audio."""

import os
import sqlite3

import pytest

from sampleindex import config, scan, search


def fake_analyzer(path, rel):
    """Module-level so worker processes can import it. The file's text decides the result."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if text.startswith("broken"):
        return {"error": "LibsndfileError: Format not recognised."}
    kind = "loop" if "loop" in text else "oneshot"
    return {"duration": 1.0, "kind": kind, "bpm": 100.0 if kind == "loop" else None, "brightness": float(len(text))}


def write(root, rel, text="hit"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "Samples"
    write(root, "Kicks/Kick 1.wav")
    write(root, "Kicks/Kick 2.WAV")
    write(root, "Loops/Break 90BPM.aif", "loop")
    write(root, "Loops/Break 90BPM.asd", "Live analysis file, not audio")
    write(root, ".hidden/Kick.wav")
    write(root, "Loops/._Break.wav", "AppleDouble junk")
    return root


def run(folders, index, **kw):
    return scan.scan(
        folders, index, analyzer=fake_analyzer, analyzer_version=kw.pop("version", 1), jobs=kw.pop("jobs", 1), **kw
    )


def rows(index):
    with sqlite3.connect(index) as conn:
        return dict(conn.execute("SELECT rel, COALESCE(error, kind) FROM samples").fetchall())


def test_first_scan_finds_audio_only(library, tmp_path):
    index = tmp_path / "samples.db"
    report = run([config.make_folder(str(library))], index)
    assert rows(index) == {
        "Kicks/Kick 1.wav": "oneshot",
        "Kicks/Kick 2.WAV": "oneshot",
        "Loops/Break 90BPM.aif": "loop",
    }
    fr = report.folders[0]
    assert (fr.found, fr.analyzed, fr.unchanged, fr.failed) == (3, 3, 0, 0)
    hit = search.search(index, text="break")["results"][0]
    assert hit["browser_path"] == "user_folders/Samples/Loops/Break 90BPM.aif"


def test_rescan_only_analyzes_changes(library, tmp_path):
    index = tmp_path / "samples.db"
    folder = config.make_folder(str(library))
    run([folder], index)
    assert run([folder], index).folders[0].analyzed == 0

    write(library, "Kicks/Kick 1.wav", "a longer hit")  # changed size
    write(library, "Kicks/Kick 3.wav")  # new
    (library / "Kicks/Kick 2.WAV").unlink()  # gone
    fr = run([folder], index).folders[0]
    assert (fr.analyzed, fr.unchanged, fr.removed) == (2, 1, 1)
    assert set(rows(index)) == {"Kicks/Kick 1.wav", "Kicks/Kick 3.wav", "Loops/Break 90BPM.aif"}
    assert search.search(index, text="kick 2")["total"] == 0  # the full-text row went too


def test_new_analyzer_version_reanalyzes_everything(library, tmp_path):
    index = tmp_path / "samples.db"
    folder = config.make_folder(str(library))
    run([folder], index)
    assert run([folder], index, version=2).folders[0].analyzed == 3
    assert run([folder], index, force=True, version=2).folders[0].analyzed == 3


def test_failures_are_recorded_and_retried_on_request(library, tmp_path):
    index = tmp_path / "samples.db"
    folder = config.make_folder(str(library))
    write(library, "Kicks/Bad.wav", "broken")
    fr = run([folder], index).folders[0]
    assert fr.failed == 1 and rows(index)["Kicks/Bad.wav"].startswith("LibsndfileError")
    assert run([folder], index).folders[0].analyzed == 0  # not retried until it changes
    assert run([folder], index, retry_failed=True).folders[0].analyzed == 1


def test_unmounted_folder_keeps_its_entries(library, tmp_path):
    index = tmp_path / "samples.db"
    run([config.make_folder(str(library))], index)
    moved = tmp_path / "Unplugged"
    os.rename(library, moved)
    fr = run([config.make_folder(str(library))], index).folders[0]
    assert not fr.mounted and fr.kept_offline == 3
    out = search.search(index, text="kick")
    assert out["total"] == 2 and out["offline"] == [str(library)]


def test_empty_folder_does_not_wipe_the_index(library, tmp_path):
    index = tmp_path / "samples.db"
    folder = config.make_folder(str(library))
    run([folder], index)
    for path in list(library.rglob("*")):
        if path.is_file():
            path.unlink()
    fr = run([folder], index).folders[0]
    assert fr.found == 0 and fr.removed == 0 and len(rows(index)) == 3


def test_folders_dropped_from_config_are_forgotten(library, tmp_path):
    index = tmp_path / "samples.db"
    other = tmp_path / "Other"
    write(other, "Snare.wav")
    run([config.make_folder(str(library)), config.make_folder(str(other))], index)
    report = run([config.make_folder(str(other))], index)
    assert report.forgotten == 3 and set(rows(index)) == {"Snare.wav"}
    assert search.search(index, text="kick")["total"] == 0


def test_worker_processes_give_the_same_result(library, tmp_path):
    index = tmp_path / "samples.db"
    seen = []
    run([config.make_folder(str(library))], index, jobs=2, progress=lambda done, total: seen.append((done, total)))
    assert len(rows(index)) == 3
    assert seen[0] == (0, 3) and seen[-1] == (3, 3)


def crashing_analyzer(path, rel):
    """Dies like a decoder segfault on files named 'crash'."""
    if "crash" in rel:
        os._exit(1)
    return fake_analyzer(path, rel)


def test_a_crashed_worker_keeps_finished_work(library, tmp_path):
    index = tmp_path / "samples.db"
    write(library, "Zzz/crash.wav")
    report = scan.scan(
        [config.make_folder(str(library))], index, analyzer=crashing_analyzer, analyzer_version=1, jobs=2
    )
    assert report.crashed
    assert "Zzz/crash.wav" not in rows(index)  # not recorded as a failure, so the next scan tries again
