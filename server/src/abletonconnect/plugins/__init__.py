"""The plugin index: what's installed (disk.py), how the formats of one plugin merge into an entry
and what Live can load (index.py), what each plugin exposes inside Live (live.py), and the
`abletonconnect-scan-plugins` command (cli.py). The index is ~/.config/abletonconnect/plugins.json;
the user's own notes live in plugin_roles.yaml next to it and are merged in when it's read."""

SCAN_ON_WINDOWS = (
    "abletonconnect-scan-plugins only runs on macOS so far (it reads Mac plugin bundles and uses auval, lsof and "
    "screencapture), so there's no plugin index on Windows. Find plugins with browse_browser (path 'plugins') or "
    "search_browser, and load them with load_browser_item."
)
