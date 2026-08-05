# Codex Usage Widget

A lightweight Windows popup and system-tray monitor for real ChatGPT/Codex quota and Hermes token usage.

![Windows](https://img.shields.io/badge/platform-Windows-0078D4)
![Python](https://img.shields.io/badge/python-3.11+-3776AB)
![License](https://img.shields.io/badge/license-MIT-green)

## What it shows

- Real Codex account quota windows and reset times
- Real SuperGrok weekly usage, remaining percentage, and reset time through Hermes's authenticated xAI OAuth
- Current Hermes session spend: processed, input, output, and API calls
- Historical totals for the most-used models
- A **weekly-only button** in the header that collapses the widget to the Codex and Grok weekly bars and remaining percentages
- A **GROK** button that opens the authoritative SuperGrok Usage page in your default browser
- System-tray controls for show/hide, refresh, weekly-only mode, always-on-top, startup, and quit

The expanded `API USAGE` view identifies Codex and Grok as the monitored services while preserving the original card layout. The compact view is intentionally not a redesign—it is the same widget collapsed to the one number most useful to leave on screen.

## Honest data semantics

- **Provider quota** comes from the authenticated ChatGPT/Codex account-usage response.
- **Session spend** and **model totals** come from Hermes's read-only local telemetry database.
- **SuperGrok quota** comes from the same authenticated Grok billing RPC used by `Settings → Usage`, using Hermes's local xAI OAuth token in a short-lived helper. The token never enters the widget process or repository.
- These values are not context-window occupancy and are never converted into a made-up “tokens remaining” number.
- Credentials never enter the UI, logs, repository, or packaged frontend.

## Lightweight design

The persistent widget is one process. It reads Hermes's SQLite telemetry directly instead of keeping a collector process alive.

| Data | Visible | Hidden in tray |
|---|---:|---:|
| Current session | 10 seconds | 60 seconds |
| Model aggregates | 2 minutes | 10 minutes |
| Provider quota | 5 minutes | 15 minutes |

The quota helper runs only during a provider refresh and exits immediately. The local build uses PyInstaller one-directory mode to avoid an extra one-file bootstrap process.

## Development

```bash
uv venv .venv --python 3.11
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
.venv/Scripts/python.exe -m unittest discover -s tests -v
.venv/Scripts/python.exe build.py
```

Install the Windows build:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -SourceDir .\dist\CodexUsageWidget
```

The optional Hermes Desktop pane is under `hermes-plugin/`. Install its files with:

```bash
python install-hermes-plugin.py
```

Then ensure `plugins.enabled` is a YAML list containing `codex-usage` and restart Hermes Desktop.

## License

MIT
