# AbletonConnect

Let Claude work inside Ableton Live. AbletonConnect is a Live Remote Script
(**LiveBridge**) plus an [MCP](https://modelcontextprotocol.io) server. Claude
can read your set, create tracks and clips, write MIDI, load instruments,
effects, plugins and samples, tweak device parameters and mix, and undo any of
it.

Everything stays on your machine: the only socket is on `127.0.0.1`, and there's
no telemetry and no outbound network traffic.

> **Status:** Phase 1 (bridge + core tools). Tested with Ableton Live 12.4.7
> Suite on macOS (Apple silicon). Not affiliated with or endorsed by Ableton.

## What Claude can do

19 tools, each returning compact JSON with the state after the change:

| Area | Tools |
|---|---|
| Session | `get_session_info`, `get_track_info`, `set_tempo`, `undo` |
| Tracks & mixer | `create_midi_track`, `create_audio_track`, `set_mixer` (volume raw or dB, pan, sends, mute, solo) |
| Clips & notes | `create_clip`, `add_notes`, `get_clip_notes`, `fire_clip`, `stop_clip` |
| Transport | `start_playback`, `stop_playback` |
| Sound design | `get_device_parameters`, `set_device_parameter` (by index or name; raw, normalized, or option name like `"Lowpass"`) |
| Browser | `browse_browser`, `search_browser`, `load_browser_item` (devices, plugins, presets, samples, user folders) |

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

5. **Check it:**

   ```bash
   scripts/install.sh --check
   ```

   This shows the install, LiveBridge's log lines since Live started, and
   whether the port is open. To exercise every tool against a new, empty Live
   set, run the smoke test. It adds two "LB smoke" tracks; close the set
   without saving afterwards.

   ```bash
   uv run --project server python scripts/smoke_test.py
   ```

## Use it

Open Live, start a new Claude Code session, and ask in plain language:

- "What's in my Live set?"
- "Make a MIDI track called Bass, load Operator, and write a 2-bar bassline in A minor."
- "Open up the filter on that synth and add a little resonance."
- "Search my user folders for a snare and put it in slot 2 of the audio track."
- "Drums to -6 dB, hats 20% right, keys to the reverb return at -12 dB."
- "Undo that."

If something's wrong, the tool's error says what to do, for example "Ableton
Live isn't running" or "LiveBridge isn't answering… set a Control Surface slot
to LiveBridge". `scripts/tail_log.sh` follows Live's log.

## Configuration

The port defaults to `9878`. To change it, create
`~/.config/abletonconnect/config.json`. Both sides read this file, and Live
needs a restart to pick up the change:

```json
{"port": 9878}
```

The MCP server also accepts the `ABLETONCONNECT_PORT` environment variable.

## Security notes

- LiveBridge binds to `127.0.0.1` only.
- Every connection must start with a JSON `hello`. Anything else, such as a
  web page POSTing to the port, is dropped before any command runs.
- Any program running as your user can still connect and control Live, just
  as it could control Live in other ways. Don't run LiveBridge on a shared
  account you don't trust.

## Limitations (Phase 1)

- Session View clips only. No Arrangement editing or automation yet.
- No deleting tracks, clips or devices. Changes can be undone.
- Devices inside rack chains aren't addressable yet; rack macros are.
- Browser search walks Live's browser by name and stops after ~15 s on huge
  folders.

## Roadmap

2. **Sample library index:** scan sample folders offline with librosa (BPM,
   key, brightness, length, one-shot or loop) and search them fast.
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

The first command runs the client tests against a fake LiveBridge socket, plus
end-to-end tests through the real bridge code. The second runs the Remote
Script tests on Live's Python version, against a fake Live API, including a
reproduction of Live's GIL behaviour.

[CLAUDE.md](CLAUDE.md) covers the architecture, protocol, threading rules and
the quirks of Live's embedded Python that shaped the design (GIL starvation of
background threads, `schedule_message` re-entrancy, macOS socket behaviour).

## License

[MIT](LICENSE)

Ableton and Ableton Live are trademarks of Ableton AG. This project is
independent and unofficial.
