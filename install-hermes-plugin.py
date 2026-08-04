from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


def hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        return Path(configured)
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "hermes"


def main() -> int:
    source = Path(__file__).resolve().parent / "hermes-plugin"
    home = hermes_home()
    copies = (
        (source / "desktop-plugins" / "codex-usage", home / "desktop-plugins" / "codex-usage"),
        (source / "plugins" / "codex-usage", home / "plugins" / "codex-usage"),
    )
    installed: list[str] = []
    for source_dir, target_dir in copies:
        if not source_dir.is_dir():
            raise FileNotFoundError(source_dir)
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)
        installed.append(str(target_dir))
    print(json.dumps({
        "installed": installed,
        "next": "Ensure plugins.enabled is a YAML list containing codex-usage, then restart Hermes Desktop.",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
