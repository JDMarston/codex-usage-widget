from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path


def hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        return Path(configured)
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "hermes"


def main() -> int:
    home = hermes_home()
    source_root = home / "hermes-agent"
    # Use the version shipped and tested with this build, never an old installed plugin.
    backend_path = Path(__file__).resolve().parent / "hermes-plugin" / "plugins" / "codex-usage" / "dashboard" / "plugin_api.py"
    if not backend_path.exists():
        print(json.dumps({"available": False, "limits": [], "error": "Codex quota plugin is not installed."}))
        return 0
    sys.path.insert(0, str(source_root))
    spec = importlib.util.spec_from_file_location("hermes_usage_quota_backend", backend_path)
    if spec is None or spec.loader is None:
        return 1
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    print(json.dumps(module._quota_snapshot(), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
