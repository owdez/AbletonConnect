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
   `from mcp.server.mcpserver import MCPServer`.) Since extended with
   scenes, track rename/arm/delete, song scale, display-value parameters
   and `arrange_scenes`, after a real song build hit those gaps. Then devices
   inside racks (`device_path`), Simpler's playback mode
   (`set_device_property`) and Live's own output meters (`get_meters`, an
   interim step before Phase 3, not a replacement), then clip envelopes
   (`set_clip_envelope`), which `arrange_scenes` carries onto the timeline.
   Then transport (`jump_to`, `start_playback` from a bar,
   `set_arrangement_loop`, `back_to_arrangement`), and `load_browser_item`
   putting back the selection and record arms it used to change.
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
  handlers/                 session, tracks (mixer), clips, envelopes, scenes, arrangement, devices, meters, browser
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
- **Undo steps.** A script's `begin_undo_step()` ... `end_undo_step()` groups
  only the changes it makes within one tick. Holding a step open across ticks
  doesn't merge changes from different ticks. `browser.load_item` is
  synchronous but records its own undo step and ignores any step the script
  has open. When the loaded sample is the first clip in the set and a loop,
  Live also sets the song tempo from it, inside the same call, as a second
  step. `ClipSlot.create_audio_clip(path)` makes the same clip (name, warp,
  loop, gain, track rename) in one step and leaves the tempo alone. Library
  samples have no file path: their uri is `query:Samples#FileId_N`. User
  folder items carry it: `userfolder:/Volumes/...#Sub:file.wav`.
- With a copy install (`scripts/install.sh --copy`), rerun it after every
  change under `remote_script/`, then restart Live.

## Scenes, tracks and the Arrangement

- `arrange_scenes` (`handlers/arrangement.py`) lays scenes out back to back.
  A looping MIDI clip becomes one timeline clip per section via
  `Track.create_midi_clip` (Live 12), with its notes repeated to fill it; audio
  clips go through `Track.duplicate_clip_to_arrangement` (Live 11) end to end.
  The API can't set an arrangement clip's length, which is why MIDI is
  rebuilt rather than copied. Positions are quarter-note beats:
  `beats_per_bar = numerator * 4 / denominator`.
- A MIDI clip with no notes inside its section (a riser at the end of a
  32-bar clip in a 24-bar section) is skipped, not placed empty, and listed
  in `skipped_empty`. It restores the playhead: Live moves it to each clip
  `create_midi_clip` makes.
- Timeline clips are addressed by their index in `track.arrangement_clips`
  (`arrangement_index` in `set_clip` / `delete_clip`, listed by
  `get_track_info`).
- It does everything in one handler call, one tick, so it's one undo step.
  Don't turn it into a multi-tick generator: changes from different ticks
  become separate undo steps.
- A MIDI clip with envelopes can't go through `create_midi_clip` (a timeline
  clip can't be given envelopes). It's built in a spare Session slot (the
  track's first empty slot, or a temporary scene that's deleted afterwards and
  the selected scene restored), with notes and envelopes repeated to fill the
  section, then copied with `duplicate_clip_to_arrangement` and the spare clip
  deleted, all in the same tick.
- `set_device_parameter` takes display text ("120 Hz", "1.2 kHz", "350 ms",
  "1/4"). Continuous parameters bisect `str_for_value` like the dB code;
  units are normalized (kHz -> Hz, s -> ms) so prefix changes don't break it.
- Live reports tempo-synced rates (Auto Pan "Rate", Delay synced times) as
  continuous 0-21 with no `value_items`. `lom.stepped_items` spots them (the
  display halfway between two steps matches one of them) and lists the
  options.
- Return tracks list a send to themselves in `mixer_device.sends`; it does
  nothing, so it's hidden and rejected.
- Only automatable parameters appear in `device.parameters`. Drift's Voice
  Mode and mod matrix routing aren't there.

## Clip envelopes (verified in Live 12.4.7)

`handlers/envelopes.py`. What the API turned out to allow:

- Session clips: `Clip.automation_envelope(param)` (None if there's none),
  `create_automation_envelope(param)`, `clear_envelope`, `clear_all_envelopes`,
  and `Clip.automation_envelopes` lists them (`env.parameter`). The param must
  be on the clip's track. A new envelope has no events and follows the
  parameter's own value.
- `Live.Envelope.Envelope` has `create_event(EnvelopeEvent(time, raw value))`,
  `events_in_range`, `delete_events_in_range`, `insert_step` and
  `value_at_time`. Times are beats from the clip start, like notes.
- Breakpoints are joined by straight lines in raw values, so ramps are exact.
  `EnvelopeEventControlCoefficients` (curves) are accepted and ignored, so
  curves are drawn as 16 straight pieces. Quantized parameters step.
- Two events at one time make a vertical jump (holds use this). At a jump, and
  at any event of a quantized parameter, `value_at_time(t)` returns the value
  reached at t; the new value starts just after t.
- **`events_in_range` reports values in Live's internal units**: linear gain for
  volume and sends (raw 0.8 reads 0.794), Hz for Operator's filter, raw for pan
  and most others. Always read values with `value_at_time`, which is raw.
- `events_in_range` and `delete_events_in_range` are inclusive at both ends and
  raise "Range out of bounds" past +-1576800 beats (Live's last beat).
  `create_event` raises "Value out of bounds" outside min-max; `insert_step`
  clamps, and a zero-length step does nothing.
- Arrangement clips: `automation_envelope` returns None and
  `create_automation_envelope` raises "Not a session clip or parameter belongs
  to another track". There's no `Track.automation_envelope`: track automation
  in the Arrangement isn't in the LOM at all.
- `duplicate_clip_to_arrangement` carries a Session clip's envelopes, even ones
  written in the same tick. On a track with no devices they stay in the
  timeline clip (`has_envelopes`, editable through `automation_envelopes`) and
  go when it's deleted. On a track with devices Live writes them as the
  track's arrangement automation over the clip's span, with the parameter's own
  value outside it; deleting the clip leaves that automation behind. Both play
  back the same. A parameter's `automation_state` can read 2 (overridden) while
  playback still follows the automation.
- Undo: one `set_clip_envelope` call (clear, create, events) is one undo step.
- Parameters inside racks take envelopes too (a Simpler inside the 909 Core
  Kit's pad rack, by `device_path`). `env.parameter == param` matches LOM
  parameters, which is how `get_clip_envelope` lists where each one lives.

## Racks, Simpler and meters (verified in Live 12.4.7)

- Racks have `can_have_chains`; `device.chains[i].devices[j]` reaches the
  devices inside, nested racks included. A `device_path` alternates device
  and chain indices: `[2, 0, 1]` is track device 2, chain 0, device 1.
  Drum Rack pads are its `chains`, and each has `in_note` (36 = C1). Core
  Library kits wrap each pad's Simpler in an Instrument Rack, so the 909
  Mid Tom's Simpler is `[0, 8, 0, 0, 0]`. `lom.device_brief` lists chains
  and each nested device's path (64 chains per rack, 4 rack levels).
- Simpler (`class_name` "OriginalSimpler", Python type `SimplerDevice`):
  `playback_mode` is a plain int property (0 Classic, 1 One-Shot,
  2 Slicing), and so is `slicing_playback_mode` (0 Mono, 1 Poly, 2 Thru).
  Setting one inside an undo step is one undo step. Switching modes doesn't
  change or disable any parameter, so nothing warns that One-Shot ignores
  the volume envelope: with Ve Attack at 20 s a 909 tom peaked at -29 dB in
  Classic and -2.7 dB in One-Shot. `retrigger` and `voices` are properties
  too but not exposed. Add properties in `lom.DEVICE_PROPERTIES`.
- `Track.output_meter_left/right/level` are **linear in dB**, not
  amplitude: reading = (dBFS + 70) / 76, so 0 is -70 dBFS or quieter,
  0.921 is 0 dBFS and 1 is +6 dBFS (`lom.meter_db`). Measured with sines of
  known level on audio tracks; returns and the master use the same scale.
  They're post-fader.
- left/right fall about 75 dB/s after a peak, so one reading per tick can
  miss ~7 dB of a drum hit. `output_meter_level` holds the highest peak
  for 2.0 s and then falls just as fast, so it catches every hit but also
  carries peaks from before the window. `get_meters` therefore counts
  left/right only after 1 s and held peaks only after 2.3 s and only when
  they aren't falling, which makes the shortest window 2.5 s.
- A muted track's meter keeps showing its level (the 909 kit read the same
  -22.9 dB peak after `mute`), so the meters read before the mute.
  `get_meters` reports the level and adds `"mute": true`.
- MIDI tracks with no instrument have `has_audio_output` False; their
  meters read 0.
- A rack macro mapped to one parameter displays that parameter's value, and
  `str_for_value` on the macro returns it too ("4.15 kHz" on Dark Corner
  Pad's Low Pass Filter, range 0-127), so display-text bisection works on
  macros without knowing the mapping. Unmapped macros show plain numbers.

## Transport and arm (verified in Live 12.4.7)

- `start_playing()` plays from the start marker (`song.start_time`), not
  from `current_song_time`. So a stopped jump sets both. Setting
  `current_song_time` while playing jumps at once. `continue_playing()`
  resumes from the playhead.
- `song.back_to_arranger` is True while Session clips override the
  Arrangement (the ▶≡ button next to the Main track's stop button lights).
  Setting it False stops those clips and the tracks play the timeline again.
- Loop brace changes (`loop`, `loop_start`, `loop_length`) aren't undo steps.
  `song.loop` reads back its new value only on the next tick. Setting
  `loop_start` past `song_length` raises "Cannot set the Loopstart behind the
  Songlength".
- **A script load can arm the track.** After the script moved the selection
  and called `browser.load_item`, Live often armed that track (`track.arm`,
  not `implicit_arm`) and disarmed the one it armed on an earlier load. It
  didn't when loading onto the already selected track, re-loading onto a
  track it had armed before, or while the user had any track armed. Clicking
  a track or loading from Live's own browser arms nothing. An instrument
  loaded onto an audio track goes onto a new MIDI track, which Live selects
  and arms. Since the rule isn't predictable, `load_browser_item` restores the
  selection and every arm after every load, matching tracks by identity (a
  new track shifts indices).
- Komplete_Kontrol_A (Control Surface slot 1 here) has Ableton's
  `AutoArmComponent`, which sets `implicit_arm` on the selected track. With
  its keyboard unplugged (Input None) it arms nothing.
- With no audio input device, `track.arm = True` on an audio track is
  silently ignored; `set_track` says so.

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
  Live doesn't need a restart for those. A session started before a server
  change keeps the old tool list: that, not a missing handler, is what a
  "the bridge has commands the server doesn't expose" report usually means.
- Debug with Log.txt: `scripts/tail_log.sh` (filters LiveBridge lines) and
  `scripts/install.sh --check` (install and whether a copy is older than the
  repo, log lines since Live started, and the port).
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
