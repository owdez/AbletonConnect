# AbletonConnect

A custom MCP server that lets Claude work inside Ableton Live on macOS.
**Local only. No telemetry. Every socket binds to 127.0.0.1.**

Long-term goal: Claude can edit tracks, pick sounds, search and use the sample
library, load plugins, do sound design through device parameters, read real
meter and analysis data, and propose and apply mix fixes. We build it one phase
at a time. Don't start a phase until the one before it works.

## Architecture

```
Claude Code ──stdio──▶ MCP server (server/, mcp SDK MCPServer, uv, Python 3.12+)
                          │  newline-delimited JSON over TCP 127.0.0.1:<port>
                          ▼
                       LiveBridge Remote Script (remote_script/LiveBridge/)
                       runs inside Live's embedded Python 3.11
                          │  socket thread ─▶ queue.Queue ─▶ main-thread pump
                          ▼
                       Live Object Model (Song, Track, Clip, Device, Browser)
```

Phase 3 adds a JUCE analyzer plugin (analyzer/) on each track. It streams
meter data to the MCP server over its own 127.0.0.1 socket. Phase 4 adds a
read-only .als parser (tools/) that never talks to Live.

## Phases

1. **Bridge + core tools.** LiveBridge Remote Script and an MCP server with
   19 tools: session, track, clip, note, transport, device-parameter, mixer,
   browser and undo. (FastMCP is called `MCPServer` in mcp 2.x:
   `from mcp.server.mcpserver import MCPServer`.)
2. **Sample library index.** Scan the sample folders and analyze each file
   with librosa (BPM, key, brightness, length, one-shot or loop type) into a
   local index, then add a search tool. This runs outside Live. Live's browser
   is too slow to search big folders. Sample folders are configured per
   machine (see `CLAUDE.local.md`) and may be on drives that aren't mounted.
   Built: `tools/sampleindex` and the `search_samples` tool (see below).
3. **JUCE analyzer plugin.** Streams LUFS, RMS, peak, spectrum and
   correlation per track over a local socket, plus mix-analysis tools.
4. **Read-only .als parser.** Overviews across projects (.als is gzipped
   XML). It never writes .als files.

## Repo layout

```
remote_script/LiveBridge/   Remote Script, loaded by Live (Python 3.11, stdlib only)
  bridge.py                 ControlSurface subclass: starts the server, runs the pump
  server.py                 TCP server and connections (no Live imports)
  dispatch.py               @command registry, undo steps, multi-tick jobs
  lom.py                    LOM -> compact dict helpers, dB bisection
  handlers/                 session, tracks (mixer), clips, devices, browser
remote_script/tests/        pytest on Python 3.11 with a fake Live (fake_live.py)
server/                     MCP server (uv project)
  src/abletonconnect/       client.py (bridge client), server.py (MCP tools)
  tests/                    client vs fake socket; end-to-end through the real bridge code
analyzer/                   Phase 3: JUCE plugin (not yet)
tools/sampleindex/          Phase 2: sample indexer (own uv project, Python 3.12)
  src/sampleindex/          names.py (what names say), analyze.py (librosa), scan.py,
                            db.py (SQLite + FTS5), search.py, config.py, cli.py
  tests/                    names, config, search and scan (fake analyzer), librosa on generated audio
tools/                      Phase 4: .als parser (not yet)
scripts/                    install.sh, tail_log.sh, smoke_test.py
```

New commands: add a handler with `@command()` (or `@command(undo=True)` if
it changes the set) in `remote_script/LiveBridge/handlers/`, then a matching
tool in `server/src/abletonconnect/server.py` with `mutating=` set correctly.
Handlers that need more than one tick are generators (see `dispatch.py`).
`search_samples` is the exception: the server answers it from the sample
index and never talks to LiveBridge.

## Sample index (Phase 2)

- `sampleindex` the package is **stdlib only**. The server depends on it as an
  editable path dependency for search (`server/pyproject.toml`). librosa is in
  its `analyze` dependency group, which `uv run --project tools/sampleindex`
  installs by default and the server never gets.
- Settings live in `~/.config/abletonconnect/config.json`: `sample_folders`
  (paths, or `{"path", "browser"}`) and `sample_index` (default
  `~/Library/Application Support/AbletonConnect/samples.db`; the
  `ABLETONCONNECT_SAMPLE_INDEX` env var overrides it, which the tests use).
- A folder maps to `user_folders/<folder name>` in Live's browser, so results
  load with the existing `load_browser_item`. Escaping matches
  `handlers/browser.py`; keep the two in step.
- Scans are incremental (size + mtime + `ANALYZER_VERSION`). Bump
  `ANALYZER_VERSION` in `analyze.py` whenever analysis or name parsing
  changes results. A missing folder (unmounted drive) keeps its rows. A mounted
  folder with zero audio files keeps them too, in case the drive is half
  mounted.
- The server opens the index read-only (WAL), so a scan can run while Claude
  searches.

Measured on a 4,220-file library (DECAP, breaks packs, analogue drums), with
file names as ground truth:

- **Loop length beats librosa for tempo.** Loops are cut to whole bars, so
  `240 / duration * 2^k` gives the tempo up to an octave. That matched the
  named BPM on 93% of loops; `librosa.feature.tempo` matched 18%. The octave
  is folded into 70-140 BPM, unless a bare number in the name agrees with the
  length.
- **Audio key estimation doesn't work here.** Krumhansl/Temperley/Aarden/
  Albrecht profiles on CQT/CENS chroma matched named keys 35-45% of the time,
  so keys come only from names.
- **pyin reports a pitch in noise.** Root notes are skipped when median
  spectral flatness is above 0.01. Pitched kicks, toms and 808s sit near 0,
  hats and claps above 0.02. Notes use Live's naming (C3 = MIDI 60).
- **Loop or one-shot is a vote.** Name words, the nearest folder name and the
  audio each add points. Break packs name single hits "Break14 Hit 100BPM",
  so no single rule works.
- librosa 1.0's first call in a process JIT-compiles with numba (~30 s); numba
  caches it, so later runs start fast. A full scan takes ~40-90 s with 7
  workers.

## Environment facts (verified with Live 12.4.7 Suite on macOS)

Machine-specific details (repo path, sample folders, JUCE checkout, which
Control Surface slot holds LiveBridge, install mode) live in `CLAUDE.local.md`.
That file is git-ignored; read it when it exists.

- Live: `/Applications/Ableton Live 12 Suite.app` (12.4.7 tested). The
  process name is `Live`.
- Remote Script Python is **3.11**. Live's bundled `.pyc` magic number is
  3495. Remote Script code must run on 3.11 and use only the stdlib Live
  bundles. Test it with `uv run --python 3.11`.
- Log: `~/Library/Preferences/Ableton/Live 12.4.7/Log.txt`. Python `logging`
  output and `ControlSurface.log_message` both end up there.
- Install target: `<User Library>/Remote Scripts/LiveBridge`. `scripts/install.sh`
  reads the User Library location from Live's `Library.cfg` and symlinks the
  script there, or copies it with `--copy`.
- Quote every path in scripts and docs. Repo checkouts and Live's folders can
  contain spaces.

## Bridge protocol (v1)

- TCP on `127.0.0.1`, default port **9878**. Override it in
  `~/.config/abletonconnect/config.json` (`{"port": 9878}`), which both sides
  read. The server also accepts the `ABLETONCONNECT_PORT` environment variable.
  Live is launched from the Dock and never sees shell environment variables,
  which is why the Remote Script reads a file.
- One JSON object per line, UTF-8, `\n` terminated, 8 MiB max per line.
- Request: `{"id": "<str>", "cmd": "<name>", "args": {...}}`
- Response: `{"id": "<same>", "ok": true, "result": {...}}` or
  `{"id": "<same>", "ok": false, "error": {"type": "...", "message": "...", "where": "..."}}`
- The first message on a connection must be
  `{"id":..., "cmd":"hello", "args":{"protocol":1}}`. If the first line isn't
  valid JSON (for example a browser's `POST / HTTP/1.1`), the connection is
  closed. This blocks cross-protocol requests from web pages.
- `hello` returns the protocol version, script version and Live version. If
  the protocol version doesn't match, the server reports it and tells you to
  reinstall and restart Live.

## Threading rules (Remote Script) — do not break these

- **All Live Object Model access happens on Live's main thread.** Socket
  threads parse JSON, enqueue the request and write responses. Nothing else.
- Socket threads never call `schedule_message`. It changes `ControlSurface`
  state (a plain list and a `TaskGroup`) that the main thread iterates, so
  calling it from another thread is a race. Instead the main thread runs a
  pump that reschedules itself with `schedule_message(1, ...)` (one tick,
  about 100 ms) and drains a `queue.Queue`.
- The pump has a time budget for each tick so a burst of requests can't freeze
  Live's UI. Long operations, such as browser search, also have their own
  limits.
- The main thread never blocks on a socket. Connection sockets are
  non-blocking. `Connection.send()` writes directly, and a writer thread sends
  only what didn't fit.

## Hard-won facts about Live's Python (verified in 12.4.7)

- **Live's main thread holds the GIL almost all the time.** Background Python
  threads only run when it lets go. Without help, a `hello` answered purely by
  socket threads took 0.2–1 s. The pump starts every tick with
  `time.sleep(0.001)` to hand the GIL over, and replies are sent from the main
  thread rather than queued for a writer thread.
  `remote_script/tests/test_bridge.py::test_socket_threads_get_the_gil_every_tick`
  reproduces this.
- **`schedule_message(0, ...)` from inside a scheduled callback would hang
  Live.** `TaskGroup.do_update` iterates its live task list, so a task added
  with no delay runs again in the same update, forever. Always reschedule with
  a delay of 1.
- **`send(..., MSG_DONTWAIT)` on a blocking socket still blocks on macOS.**
  Use real non-blocking sockets. Wait with `selectors` (kqueue), not
  `select()`, because Live raises its file limit to 8192 and `select()` fails
  above fd 1024.
- **`Live.Clip.MidiNoteSpecification` is write-only.** You can't read
  `start_time` back from it, so keep the validated fields yourself.
- Python `logging` and `log_message` both reach Log.txt. Lines look like
  `RemoteScriptMessage: (LiveBridge) LiveBridge: ...`.
- Clip stops are quantized: right after `stop_clip`, the clip still reports
  playing until the next bar.
- With a copy install (`scripts/install.sh --copy`), rerun it after every
  change under `remote_script/`, then restart Live.

## Conventions

- **Never crash Live.** Every handler, listener, thread body and pump tick is
  wrapped in try/except. Exceptions become error responses and are logged to
  Log.txt with a traceback. `disconnect()` closes the sockets and stops the
  threads so Live can quit cleanly.
- Indices are 0-based everywhere and match `song.tracks`, `track.clip_slots`
  and `track.devices`. Tracks are addressed with
  `track_index` + optional `track_type` (`"track"` | `"return"` | `"master"`).
- Every tool returns compact JSON that shows the state after the change, such
  as the new track summary or the parameter's new value and display string,
  so Claude can see what changed without a second call. Keep payloads small
  and don't dump whole objects.
- Each mutating command is one Live undo step
  (`song.begin_undo_step()` / `end_undo_step()`), so one `undo` reverts one
  tool call.
- The server retries a command after a reconnect only if it is read-only. It
  never resends a mutating command when it can't tell whether Live already
  applied it.
- Error messages are for the user. They say what failed and what to do next,
  for example "Live isn't running" or "LiveBridge isn't selected as a Control
  Surface".
- Remote Script: stdlib only, no third-party imports, Python 3.11 syntax.
  Server: typed Python, `ruff` for lint and format, `pytest` for tests.
- No telemetry, no analytics, no outbound network calls.

## Workflow

- **Live loads Remote Scripts only at startup.** After any change under
  `remote_script/`, reinstall if it's a copy install and tell the user that
  Live needs a restart. Python modules are cached, so re-selecting the
  Control Surface does not reload the code. The Control Surface choice
  persists across restarts.
- MCP registration (user scope, all projects):
  `claude mcp add --scope user ableton -- "$(which uv)" --directory "<repo>/server" run abletonconnect-mcp`.
  Server-side changes take effect when Claude Code restarts the MCP server.
  Live doesn't need a restart for those.
- Debug with Log.txt: `scripts/tail_log.sh` (filters LiveBridge lines) and
  `scripts/install.sh --check` (install, log lines since Live started, port).
- Sample index: `uv run --project tools/sampleindex sampleindex scan` (add
  `--force` to re-analyze everything), `sampleindex stats`, and
  `sampleindex search "dark kick" --kind oneshot`.
- Tests:
  - `cd server && uv run pytest`: client against a fake LiveBridge socket, plus
    end-to-end through the real bridge code and a fake Live.
  - `uv run --no-project --python 3.11 --with pytest -- python -m pytest remote_script/tests`:
    the Remote Script on Live's Python version.
  - `cd tools/sampleindex && uv run pytest`: the indexer, including librosa on
    generated audio.
- Smoke test with Live open on an empty set: `uv run --project server python scripts/smoke_test.py`.
- Lint: `cd server && uv run ruff check src tests && uv run ruff format src tests`,
  and the same in `tools/sampleindex`.
