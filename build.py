from __future__ import annotations

import shutil
import hashlib
import json
import subprocess
import sys
import os
from pathlib import Path

from app import VERSION, _tray_image

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
DIST = ROOT / "dist"
APP_DIR = DIST / "CodexUsageWidget"


def main() -> int:
    ASSETS.mkdir(exist_ok=True)
    icon_path = ASSETS / "codex-usage.ico"
    _tray_image(75).save(
        icon_path,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    if APP_DIR.exists():
        shutil.rmtree(APP_DIR)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        "CodexUsageWidget",
        "--icon",
        str(icon_path),
        "--add-data",
        f"{ROOT / 'quota_worker.py'};.",
        "--add-data",
        f"{ROOT / 'grok_quota_worker.py'};.",
        "--add-data",
        f"{ROOT / 'gemini_quota_worker.py'};.",
        "--add-data",
        f"{ROOT / 'hermes-plugin' / 'plugins' / 'codex-usage' / 'dashboard' / 'plugin_api.py'};hermes-plugin/plugins/codex-usage/dashboard",
        "--hidden-import",
        "pystray._win32",
        "--exclude-module",
        "numpy",
        "--exclude-module",
        "psutil",
        "--exclude-module",
        "matplotlib",
        "--exclude-module",
        "scipy",
        str(ROOT / "app.py"),
    ]
    clean_environment = os.environ.copy()
    clean_environment.pop("PYTHONPATH", None)
    subprocess.run(command, cwd=ROOT, check=True, env=clean_environment)
    output = APP_DIR / "CodexUsageWidget.exe"
    if not output.exists():
        raise RuntimeError("PyInstaller did not produce CodexUsageWidget.exe")
    for name in ("LICENSE", "README.md", "CHANGELOG.md", "install.ps1"):
        shutil.copy2(ROOT / name, APP_DIR / name)
    manifest = {"version": VERSION, "files": [
        {"path": path.relative_to(APP_DIR).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in sorted(APP_DIR.rglob("*")) if path.is_file()
    ]}
    (APP_DIR / "build-info.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    archive = Path(shutil.make_archive(str(DIST / "CodexUsageWidget-windows-x64"), "zip", root_dir=APP_DIR))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(output)
    print(archive)
    print(f"SHA256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
