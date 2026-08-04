from __future__ import annotations

import shutil
import subprocess
import sys
import os
from pathlib import Path

from app import _tray_image

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
        f"{ROOT / 'hermes-plugin'};hermes-plugin",
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
    archive = shutil.make_archive(str(DIST / "CodexUsageWidget-windows-x64"), "zip", root_dir=APP_DIR)
    print(output)
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
