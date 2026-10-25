"""search_samples through the MCP layer, against a small index built without librosa."""

import json

import pytest
from mcp import Client
from sampleindex import config, db, names

import abletonconnect.server as mcp_server


@pytest.fixture
def index(tmp_path, monkeypatch):
    path = tmp_path / "samples.db"
    conn = db.connect_writer(path)
    root = db.upsert_root(conn, config.SampleFolder(str(tmp_path), "user_folders/Samples"), mounted=True)
    for rel, result in [
        ("Kicks/BD0010.wav", {"duration": 0.4, "kind": "oneshot", "note_pc": 9, "note_midi": 33, "brightness": 300.0}),
        ("Loops/Break 45.wav", {"duration": 5.333, "kind": "loop", "bpm": 90.0, "bpm_source": "audio"}),
    ]:
        db.write_sample(conn, root, rel, 1, 1, 1, result, names.parse(rel))
    conn.commit()
    conn.close()
    monkeypatch.setenv(config.INDEX_ENV, str(path))
    return path


async def call(**args):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("search_samples", args)
    text = result.content[0].text
    return {"error": text} if result.is_error else json.loads(text)


async def test_search_samples_returns_loadable_paths(index):
    out = await call(query="kick")
    assert out["results"] == [
        {
            "name": "BD0010.wav",
            "kind": "oneshot",
            "length": 0.4,
            "note": "A0",
            "brightness_hz": 300,
            "browser_path": "user_folders/Samples/Kicks/BD0010.wav",
        }
    ]
    loop = (await call(bpm=90, kind="loop"))["results"][0]
    assert loop["bpm"] == 90 and loop["bpm_estimated"] is True


async def test_search_samples_errors_say_what_to_do(index, tmp_path, monkeypatch):
    assert "Can't read" in (await call(key="purple"))["error"]
    monkeypatch.setenv(config.INDEX_ENV, str(tmp_path / "missing.db"))
    out = await call(query="kick")
    assert "hasn't been built yet" in out["error"] and "sampleindex scan" in out["error"]
