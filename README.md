# AbletonConnect

Let Claude work inside Ableton Live. AbletonConnect is a Live Remote Script
(**LiveBridge**) plus an [MCP](https://modelcontextprotocol.io) server. Claude
can read your set, create tracks and clips, write MIDI, load instruments,
effects, plugins and samples, tweak device parameters and mix, and undo any of
it.

Everything stays on your machine: the only socket is on `127.0.0.1`, and there's
no telemetry and no outbound network traffic.

> **Status:** Phase 1 (bridge + core tools) and Phase 2 (sample library index).
> Tested with Ableton Live 12.4.7 Suite on macOS (Apple silicon). Not affiliated
> with or endorsed by Ableton.

## What Claude can do

45 tools, each returning compact JSON with the state after the change:

| Area | Tools |
|---|---|
| Session | `get_session_info`, `get_track_info`, `set_tempo`, `set_scale`, `undo` |
| Tracks & mixer | `create_midi_track`, `create_audio_track`, `set_track` (rename, arm), `delete_track`, `set_mixer` (volume raw or dB, pan, sends, mute, solo) |
| Clips & notes | `create_clip`, `add_notes`, `get_clip_notes`, `fire_clip`, `stop_clip`, `set_clip` (rename), `delete_clip` (Session or timeline) |
| Automation | `set_clip_envelope` (a ramp, curve or breakpoints inside a Session clip for volume, pan, sends or any device parameter, racks included, in what Live displays, like `"200 Hz"` to `"8 kHz"`), `get_clip_envelope`, `clear_clip_envelope` |
| Scenes & Arrangement | `create_scene`, `set_scene` (name), `delete_scene`, `fire_scene`, `arrange_scenes` (lay scenes out on the timeline as song sections, clip envelopes included) |
| Transport | `start_playback` (from the start marker, the start, or any bar or beat), `stop_playback`, `jump_to` (a bar or beat, while playing or stopped), `set_arrangement_loop` (brace position, length, on/off), `back_to_arrangement` (Live's Back to Arrangement button) |
| Sound design | `get_device_parameters`, `set_device_parameter` (by index or name; what Live displays, like `"120 Hz"`, `"350 ms"` or `"1/4"`, or raw or normalized values), `set_device_property` (Simpler's playback mode: Classic, One-Shot, Slicing). Devices inside Instrument, Effect and Drum Racks are reached with a `device_path`; `get_track_info` lists every rack's chains and the path of each device in them |
| Metering | `get_meters` (peak and average level per track, return and master in dBFS, from Live's own meters while the set plays) |
| Browser | `browse_browser`, `search_browser`, `load_browser_item` (devices, plugins, presets, samples, user folders; leaves the selected track and every track's record arm as they were) |
| Sample library | `search_samples` (your sample folders by name, loop or one-shot, BPM, key, root note, brightness and length; instant, from a local index) |
| Claude Kits | `list_kits` (your racks by role, tags or text, from `kits.yaml`), `load_kit` (onto a track; returns its macros and what each does) |
| Your plugins | `list_plugins` (by kind, vendor, your role, text), `get_plugin_info` (formats, which one Live loads, parameters with their ranges, programs, your notes), `load_plugin` (by name), `get_plugin_programs`, `set_plugin_program` |

Every change is a single Live undo step, so `undo` (or Cmd+Z) reverts exactly
one tool call.

## How it works

```
Claude Code ──stdio──▶ MCP server (server/, Python, uv)
                          │  newline-delimited JSON over TCP 127.0.0.1:9878
                          ▼
                       LiveBridge Remote Script (remote_script/LiveBridge/)
                       inside Live's embedded Python 3.11
                          │  socket threads ─▶ queue ─▶ main-thread pump
                          ▼
                       Live Object Model
```

Socket threads only parse and queue requests. All Live API access runs on
Live's main thread in a pump scheduled with `schedule_message`. Every handler
is wrapped so an error becomes an error response and is logged to Live's
`Log.txt`; it never crashes Live.

## Requirements

- macOS and Ableton Live 12 (the Remote Script targets Live's bundled Python 3.11)
- [uv](https://docs.astral.sh/uv/)
- [Claude Code](https://claude.com/claude-code), or any other MCP client

## Install

1. **Clone the repo** and install the Remote Script into Live's User Library:

   ```bash
   git clone https://github.com/owdez/AbletonConnect.git
   cd AbletonConnect
   scripts/install.sh
   ```

   This symlinks `remote_script/LiveBridge` into `User Library/Remote Scripts/`.
   If LiveBridge doesn't show up in Live, use `scripts/install.sh --copy`
   instead, and rerun it after every change to `remote_script/`.

2. **Restart Live.** Live only loads Remote Scripts at startup.

3. In **Live → Settings → Link, Tempo & MIDI**, set an empty **Control
   Surface** slot to **LiveBridge**, with Input **None** and Output **None**.
   Live's status bar shows "LiveBridge listening on 127.0.0.1:9878".

4. **Register the MCP server** with Claude Code. Run this from the repo folder
   to make it available in all your projects:

   ```bash
   claude mcp add --scope user ableton -- "$(which uv)" --directory "$PWD/server" run abletonconnect-mcp
   ```

   For the **Claude desktop app** (its chats, not Claude Code), run:

   ```bash
   scripts/install.sh --desktop
   ```

   It backs up `~/Library/Application Support/Claude/claude_desktop_config.json`,
   adds an `ableton` server with uv's absolute path (the app doesn't see your
   shell's `PATH`) and keeps every other entry. Quit and reopen the app to load it.

5. **Check it:**

   ```bash
   scripts/install.sh --check
   ```

   This shows the install (and whether a copy install is older than the repo),
   where Claude Code and the desktop app run the server from, LiveBridge's log
   lines since Live started, and whether the port is open. A Claude session
   loads the MCP server when it starts, so a session that was already open
   keeps the old tools until it's restarted.

   To exercise every tool against a new, empty Live set, run the smoke test.
   It adds two "LB smoke" tracks; close the set without saving afterwards.

   ```bash
   uv run --project server python scripts/smoke_test.py
   ```

## Use it

Open Live, start a new Claude Code session, and ask in plain language:

- "What's in my Live set?"
- "Make a MIDI track called Bass, load Operator, and write a 2-bar bassline in A minor."
- "Open up the filter on that synth and add a little resonance."
- "Search my user folders for a snare and put it in slot 2 of the audio track."
- "Find a dark 808 in F and load it on a new MIDI track."
- "I need a 90 BPM break, something brighter than the last one."
- "Drums to -6 dB, hats 20% right, keys to the reverb return at -12 dB."
- "In the build clip, sweep the bass filter from 200 Hz to 8 kHz over 8 bars, slow at first, and ride the reverb send up to 0 dB in the last 2."
- "Name the scenes Intro, Build, Drop and Outro, then lay them out as 32 bars each on the timeline."
- "Undo that."

If something's wrong, the tool's error says what to do, for example "Ableton
Live isn't running" or "LiveBridge isn't answering… set a Control Surface slot
to LiveBridge". `scripts/tail_log.sh` follows Live's log.

## Sample library (Phase 2)

Live's browser is slow to search big sample folders, so AbletonConnect keeps
its own index. A scanner analyzes every file once, outside Live, and
`search_samples` queries the index instantly. Live doesn't need to be running
to search.

1. **List your sample folders** in `~/.config/abletonconnect/config.json`:

   ```json
   {"sample_folders": ["/Volumes/Samples Drive/Samples"]}
   ```

2. **Add the same folders to Live's browser** (drag them under Places, or use
   "Add Folder…"). Search results load through Live's browser, at
   `user_folders/<folder name>/…`.

3. **Scan:**

   ```bash
   uv run --project tools/sampleindex sampleindex scan
   ```

   The first scan of ~4,000 files takes about a minute and a half on an 8-core
   Mac. After that only new and changed files are analyzed, so rerun it
   whenever you add samples. A folder on an unmounted drive is skipped and its
   entries are kept; results from it are marked `"available": false`.

`sampleindex stats` shows what's indexed and `sampleindex search "dark kick"`
searches from the terminal.

What the index knows about each file:

| Field | Where it comes from |
|---|---|
| Loop or one-shot | File and folder names ("Loops", "One Shots", "Kick", "Fill"), checked against the audio |
| BPM (loops only) | The name ("90BPM", "Drum Loop 81") when present. Otherwise the loop's length, since loops are cut to whole bars; on a 4,000-file test library that matched the named BPM 93% of the time. Results from audio say `bpm_estimated`. |
| Key | Only the file name ("Am", "CMIN", "(F#)"). Estimating keys from audio was right less than half the time, so it isn't used. |
| Root note (one-shots) | Pitch detection on tonal sounds like 808s, toms and vocal notes, named the way Live names notes (C3 = MIDI 60). Noisy sounds (hats, claps) get none. |
| Brightness | Spectral centroid. "dark" and "bright" pick the darkest or brightest third of the other matches. |
| Tags | Drum names and abbreviations, so "kick" finds `BD0010.wav`. |

## Your plugins

Claude works better with your own plugins than with generic stock devices, so
AbletonConnect keeps an index of them: `~/.config/abletonconnect/plugins.json`,
which stays on your machine and out of the repo. Build it with Live open on a
new, empty set:

```bash
uv run --project server abletonconnect-scan-plugins
```

The scanner reads every VST3 (`moduleinfo.json`, else `Info.plist`), VST2 and
Audio Unit (`auval -a`, Apple's own skipped) under `/Library/Audio/Plug-Ins` and
`~/Library/Audio/Plug-Ins`, and merges the formats of each plugin into one entry,
noting duplicate installs. It then asks Live which of them it lists, records
the browser path for each format and, for anything Live doesn't list, the
likely reason (for example Audio Units switched off in Live's Settings >
Plug-Ins). Finally it loads each plugin on a scratch track, one at a time,
records its parameters (names, ranges, defaults, display text at each end) and
programs, and deletes the track. A plugin that hangs, wants activating, opens a
consent dialog or exposes no parameters is marked `needs_attention` with the
reason; the scanner never clicks anything in a plugin's windows. A rescan only
loads new or updated plugins (`--force` loads them all, `--only NAME` just
those, `--no-live` reads the disk only).

Your own notes go in `~/.config/abletonconnect/plugin_roles.yaml`, which the
scanner never changes once it exists. The first scan writes a draft with a
guessed role for each plugin, marked `draft: true`; confirm or fix each one and
delete its `draft` line:

```yaml
UADx 1176 FET Compressor:
  role: compressor
  tags: [fet, fast, vocals, drums]
  notes: Fast FET compression on vocals, drums and bass.
```

`list_plugins` and `get_plugin_info` merge these in, and `load_plugin` loads a
plugin by name. Live's API can't load a plugin's own preset files; to reuse a
sound, save it in a rack (see Claude Kits).

## Claude Kits

Kits are racks that hold your sounds and chains: your plugins with your
settings, and macros with clear names. They're how your sounds get reused,
because Live's API can't load a plugin's presets or save presets. They live in
`Claude Kits` in your User Library, one folder per role (Drums, Bass, Synths,
Pads, FX, Vocal Chains, Mix Bus), described by `Claude Kits/kits.yaml`.
`list_kits` reads the manifest and checks it against the folder and Live's
browser; `load_kit` loads a kit onto a track and returns its macros with their
values and what each does. Two starter kits show the idea, `UAD Lead Chain`
(610-A, 1176, LA-2A, Pultec EQP-1A) and `SLVC Dub Throw` (SLVC Dub Echo, SLVC
Reverb); they're marked `starter: true`, so replace them with your own.

### How to add a kit

1. **Build the sound** on a track: your instrument or chain, dialled in the way
   you like it.
2. **Rack it.** Click the first device's title bar, Shift-click the last one,
   and press Cmd+G.
3. **Name the macros.** Show the rack's macros (the top button on its left
   edge), unfold each plugin's parameters (the triangle in its title bar),
   right-click a parameter and choose Map to Macro *n*. Then click each macro's
   name, press Cmd+R and give it a name that says what it does ("Air", "Echo
   Time"). Give the rack itself a name the same way. Macros mapped to plugin
   parameters show 0 to 127 rather than the plugin's units, so say the range in
   the manifest.
4. **Save it in the right folder.** Click the rack's Save Preset button and press
   Return. Live saves it under User Library > Presets; move the `.adg` file into
   the matching folder of `Claude Kits` (in Finder, or by dragging it in Live's
   browser).
5. **Add a manifest entry** to `Claude Kits/kits.yaml`:

   ```yaml
   kits:
     Night Drive Bass:
       file: Bass/Night Drive Bass.adg
       role: bass
       tags: [dark, sub, analog]
       range: F# minor riffs, F#0 to F#2
       macros:
         Cutoff: Filter cutoff, closed to open
         Grit: Drive before the filter
   ```

   A rack file without an entry still shows up in `list_kits` as `unlisted`.

## Configuration

`~/.config/abletonconnect/config.json` holds every setting. Both LiveBridge
and the MCP server read it; Live needs a restart to pick up a port change:

```json
{
  "port": 9878,
  "sample_folders": [
    "/Volumes/Samples Drive/Samples",
    {"path": "~/Music/Breaks", "browser": "user_folders/Music/Breaks"}
  ],
  "sample_index": "~/Library/Application Support/AbletonConnect/samples.db"
}
```

- `port` defaults to `9878`. The MCP server also accepts the
  `ABLETONCONNECT_PORT` environment variable.
- `sample_folders` entries are paths, or objects with a `browser` path for a
  folder that sits inside a folder added to Live's browser.
- `sample_index` defaults to the path above.
- `kits_folder` overrides where Claude Kits live (default: `Claude Kits` in the
  User Library Live uses).

## Security notes

- LiveBridge binds to `127.0.0.1` only.
- Every connection must start with a JSON `hello`. Anything else, such as a
  web page POSTing to the port, is dropped before any command runs.
- Any program running as your user can still connect and control Live, just
  as it could control Live in other ways. Don't run LiveBridge on a shared
  account you don't trust.

## Limitations

- The Arrangement can be filled from scenes (`arrange_scenes`), and timeline
  clips can be renamed or deleted, but not moved or edited.
- Automation goes into Session clip envelopes, and `arrange_scenes` carries it
  onto the timeline. Live's API has no access to track automation in the
  Arrangement, and timeline clips can't be given new envelopes, so automation
  can't be drawn or erased on the timeline directly. On tracks with devices,
  Live turns carried envelopes into track automation, which stays when those
  timeline clips are deleted; undo the layout instead.
- Tracks, scenes and clips can be deleted; devices can't yet.
- Live's API has no way to save the set, so Claude reminds you to save
  (Cmd+S in Live) after building something.
- Live doesn't record Arrangement loop changes for undo, so `undo` doesn't
  revert `set_arrangement_loop`. The loop can't start past the end of the song.
- No input, output or sidechain routing yet.
- Only parameters Live exposes for automation can be set. Some device
  settings, like Drift's Voice Mode, have no parameter.
- `get_meters` reads Live's peak meters. It isn't RMS or LUFS; the Phase 3
  analyzer plugin will measure those.
- Devices inside racks and Drum Rack pads are reachable by `device_path`, and
  rack macros take their target's units (`"150 Hz"`), but rack chains' own
  volume, pan and sends can't be set yet.
- Browser search walks Live's browser by name and stops after ~15 s on huge
  folders. Use `search_samples` for sample folders.
- The sample index needs a manual rescan after you add samples.

## Roadmap

3. **Analyzer plugin (JUCE):** stream LUFS, RMS, peak, spectrum and
   correlation per track over a local socket, plus mix-analysis tools.
4. **.als parser:** read-only overviews across your projects.

## Development

```bash
cd server && uv run pytest
```

```bash
uv run --no-project --python 3.11 --with pytest -- python -m pytest remote_script/tests
```

```bash
cd tools/sampleindex && uv run pytest
```

The first command runs the client tests against a fake LiveBridge socket, plus
end-to-end tests through the real bridge code. The second runs the Remote
Script tests on Live's Python version, against a fake Live API, including a
reproduction of Live's GIL behaviour. The third runs the sample indexer's tests,
including librosa on generated audio with a known tempo and pitch.

[CLAUDE.md](CLAUDE.md) covers the architecture, protocol, threading rules and
the quirks of Live's embedded Python that shaped the design (GIL starvation of
background threads, `schedule_message` re-entrancy, macOS socket behaviour).

## Moving the repo

The scripts quote every path, so the repo works from any folder, including one
whose name has spaces (`scripts/install.sh --check` warns about a folder name
that ends in one, since that's easy to break by hand). To move or rename it:

1. Quit the Claude desktop app and close Claude Code sessions running in the
   repo, so nothing has files open in it.
2. Move it, quoting both paths:

   ```bash
   mv "$HOME/Developer/AbletonConnect " "$HOME/Developer/AbletonConnect"
   ```

3. From the new folder, reconnect the git worktrees. They live inside the repo,
   so both ends moved; passing their new paths repairs both directions:

   ```bash
   git worktree repair .claude/worktrees/*
   ```

4. Rebuild the uv environments, whose editable installs record the old path:

   ```bash
   rm -rf server/.venv tools/sampleindex/.venv && uv sync --project server && uv sync --project tools/sampleindex
   ```

5. Register the MCP server again, in Claude Code (`claude mcp remove --scope
   user ableton`, then the `claude mcp add` command from Install) and in the
   desktop app (`scripts/install.sh --desktop`).
6. With a symlink install, run `scripts/install.sh` again and restart Live. A
   copy install doesn't depend on where the repo is.
7. `scripts/install.sh --check` should show both registrations pointing at the
   new `server` folder.

## License

[MIT](LICENSE)

Ableton and Ableton Live are trademarks of Ableton AG. This project is
independent and unofficial.
