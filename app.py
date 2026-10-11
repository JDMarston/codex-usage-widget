from __future__ import annotations

import argparse
import ctypes
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
import webbrowser
import winreg
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pystray
from PIL import Image, ImageDraw, ImageFont

from data_client import HermesUsageData, merge_provider
from usage_ui import UsageView

APP_NAME = "API Usage"
VERSION = "2.0.0"
APP_ID = "CodexUsageWidget"
IPC_PORT = 47631
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SIZES = {"full": (410, 500), "small": (340, 330)}
GROK_USAGE_URL = "https://grok.com/?_s=usage"
RESIZE_BORDER = 6
MIN_SIZES = {"full": (320, 480), "small": (280, 180)}
RESIZE_CURSORS = {
    "n": "size_ns",
    "s": "size_ns",
    "e": "size_we",
    "w": "size_we",
    "ne": "size_ne_sw",
    "nw": "size_nw_se",
    "se": "size_nw_se",
    "sw": "size_ne_sw",
}

LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
APP_DATA = LOCAL_APP_DATA / APP_ID
SETTINGS_PATH = APP_DATA / "settings.json"
LOG_PATH = APP_DATA / "widget.log"
HERMES_HOME = Path(os.environ.get("HERMES_HOME", LOCAL_APP_DATA / "hermes"))
APP_DATA.mkdir(parents=True, exist_ok=True)
logging.basicConfig(handlers=[RotatingFileHandler(LOG_PATH, maxBytes=200_000, backupCount=2)],
                    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def compact_number(value: Any) -> str:
    number = float(value or 0)
    magnitude = abs(number)
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if magnitude >= threshold:
            text = f"{number / threshold:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return f"{int(number):,}"


def percent(value: Any) -> str:
    try:
        number = float(value)
        if not math.isfinite(number):
            return "—"
        if 0 < number < 0.1:
            return "<0.1%"
        if 99.9 < number < 100:
            return ">99.9%"
        return f"{number:.1f}".rstrip("0").rstrip(".") + "%"
    except (TypeError, ValueError):
        return "—"


def local_datetime_text(epoch: Any) -> str | None:
    try:
        moment = datetime.fromtimestamp(float(epoch)).astimezone()
    except (OSError, OverflowError, TypeError, ValueError):
        return None
    clock = moment.strftime("%I:%M %p").lstrip("0")
    zone = moment.tzname() or "local time"
    return f"{moment.strftime('%b')} {moment.day}, {moment.year} at {clock} {zone}"


def quota_tooltip_text(
    limit: dict[str, Any],
    *,
    formatter: Callable[[Any], str | None] = local_datetime_text,
) -> str:
    lines = [f"{percent(limit.get('used_percent'))} used"]
    details = str(limit.get("details") or "").strip()
    if details:
        lines.extend(line for line in details.splitlines() if line.strip())
    reset_at = limit.get("reset_at")
    period_start = limit.get("period_start_at")
    if period_start is None and reset_at is not None:
        try:
            window_seconds = float(limit.get("window_seconds"))
            if window_seconds > 0:
                period_start = float(reset_at) - window_seconds
        except (TypeError, ValueError):
            pass
    last_reset = formatter(period_start) if period_start is not None else None
    next_reset = formatter(reset_at) if reset_at is not None else None
    if last_reset:
        lines.append(f"Last reset: {last_reset}")
    if next_reset:
        lines.append(f"Next reset: {next_reset}")
    return "\n".join(lines)


def reset_text(epoch: Any, now: float | None = None) -> str:
    try:
        remaining = float(epoch) - (time.time() if now is None else now)
    except (TypeError, ValueError):
        return "reset unknown"
    if remaining <= 0:
        return "resets now"
    minutes = max(1, int((remaining + 59) // 60))
    if minutes < 60:
        return f"resets in {minutes}m"
    hours, leftover_minutes = divmod(minutes, 60)
    if hours < 24:
        return f"resets in {hours}h {leftover_minutes}m"
    days, leftover_hours = divmod(hours, 24)
    return f"resets in {days}d {leftover_hours}h"


def _model_key(value: Any) -> str:
    return "".join(character for character in str(value or "").lower() if character.isalnum())


def choose_current_limit(quota: dict[str, Any] | None, model: Any) -> dict[str, Any] | None:
    limits = list((quota or {}).get("limits") or [])
    wanted = _model_key(model)
    matched = [item for item in limits if item.get("kind") == "model" and wanted
               and _model_key(item.get("group")) and
               (wanted in _model_key(item.get("group")) or _model_key(item.get("group")) in wanted)]
    account = [item for item in limits if item.get("kind") == "account"]
    candidates = matched + account or limits
    # The first window is not necessarily the one that will block the user.
    return max(candidates, key=lambda item: float(item.get("used_percent") or 0)) if candidates else None


def visible_limits(quota: dict[str, Any] | None, model: Any, maximum: int = 2) -> list[dict[str, Any]]:
    limits = list((quota or {}).get("limits") or [])
    current = choose_current_limit(quota, model)
    selected: list[dict[str, Any]] = []
    if current:
        selected.append(current)
    for item in limits:
        if item not in selected:
            selected.append(item)
        if len(selected) >= maximum:
            break
    return selected


def monitored_provider_labels(state: dict[str, Any]) -> list[str]:
    labels: list[str] = []

    def add(label: str) -> None:
        normalized = label.strip().upper()
        if normalized and normalized not in labels:
            labels.append(normalized)

    quota_snapshot = state.get("provider_quotas")
    if isinstance(quota_snapshot, dict):
        for provider in quota_snapshot.get("providers") or []:
            if isinstance(provider, dict):
                add(str(provider.get("label") or provider.get("id") or ""))
        return labels

    # Before the first credential-backed quota snapshot arrives, show only
    # the current session provider. Historical model rows are deliberately
    # excluded so a removed provider cannot remain in the header forever.
    item = state.get("session") or {}
    kind = str(item.get("provider_kind") or "").lower()
    if kind == "codex":
        add("Codex")
    elif kind == "grok":
        add("Grok")
    elif kind == "gemini":
        add("Gemini")
    else:
        for provider in item.get("providers") or []:
            add(str(provider))
    return labels


def monitored_provider_text(state: dict[str, Any]) -> str:
    labels = monitored_provider_labels(state)
    if labels:
        return " · ".join(labels)
    return "DETECTING…" if state.get("provider_quotas") is None else "NONE CONNECTED"


def tray_usage_title(state: dict[str, Any]) -> str:
    parts = []
    for provider in (state.get("provider_quotas") or {}).get("providers") or []:
        limit = choose_current_limit(provider, (state.get("session") or {}).get("model"))
        label = str(provider.get("label") or "Provider")
        if limit and limit.get("remaining_percent") is not None:
            parts.append(f"{label} {percent(limit['remaining_percent'])} left" + (" (stale)" if provider.get("stale") else ""))
        else:
            parts.append(f"{label}: " + {"not_reported": "not reported", "loading": "checking", "auth_required": "sign-in needed"}.get(provider.get("status"), "unavailable"))
    return (f"API Usage — {' · '.join(parts)}" if parts else "API Usage")[:127]


def normalize_window_sizes(raw: Any) -> dict[str, list[int]]:
    sizes = {name: [width, height] for name, (width, height) in SIZES.items()}
    if not isinstance(raw, dict):
        return sizes
    for name, default in SIZES.items():
        value = raw.get(name)
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            continue
        try:
            width = int(value[0])
            height = int(value[1])
        except (TypeError, ValueError):
            continue
        min_width, min_height = MIN_SIZES[name]
        sizes[name] = [max(min_width, width), max(min_height, height)]
    return sizes


def layout_dimensions(layout: str, sizes: Any | None = None) -> tuple[int, int]:
    normalized = normalize_window_sizes(sizes)
    name = layout if layout in SIZES else "full"
    width, height = normalized[name]
    return int(width), int(height)


def compute_resized_geometry(
    edge: str,
    *,
    start_x: int,
    start_y: int,
    start_w: int,
    start_h: int,
    pointer_dx: int,
    pointer_dy: int,
    min_w: int,
    min_h: int,
    work_area: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    left, top, right, bottom = work_area
    x, y, width, height = int(start_x), int(start_y), int(start_w), int(start_h)
    edge = edge.lower()

    if "e" in edge:
        width = start_w + pointer_dx
    if "s" in edge:
        height = start_h + pointer_dy
    if "w" in edge:
        width = start_w - pointer_dx
        x = start_x + pointer_dx
    if "n" in edge:
        height = start_h - pointer_dy
        y = start_y + pointer_dy

    if width < min_w:
        if "w" in edge:
            x -= min_w - width
        width = min_w
    if height < min_h:
        if "n" in edge:
            y -= min_h - height
        height = min_h

    max_width = max(min_w, right - left)
    max_height = max(min_h, bottom - top)
    width = min(width, max_width)
    height = min(height, max_height)
    x = min(max(left, x), max(left, right - width))
    y = min(max(top, y), max(top, bottom - height))
    return x, y, width, height


def snapshot_render_targets(state: dict[str, Any], snapshot: dict[str, Any]) -> set[str]:
    changed = {
        key
        for key in ("provider_quotas", "session", "models")
        if key in snapshot and state.get(key) != snapshot[key]
    }
    if not changed:
        return set()

    targets = {"providers"}
    if "session" in changed:
        targets.add("session")
    if "models" in changed:
        targets.add("models")
    if "provider_quotas" in changed:
        targets.update({"quota", "tray"})

    if "session" in changed:
        old_model = _model_key((state.get("session") or {}).get("model"))
        new_model = _model_key((snapshot.get("session") or {}).get("model"))
        if old_model != new_model:
            targets.update({"quota", "models", "tray"})
    return targets


def ordered_model_rows(models_snapshot: dict[str, Any] | None, active_model: Any) -> list[dict[str, Any]]:
    """Keep every tracked model while placing the current one first.

    ``fetch_models`` already returns every model Hermes has recorded.  The UI
    must not truncate that source just because a newly selected model has less
    historical usage than older entries.
    """
    models = [item for item in ((models_snapshot or {}).get("models") or []) if isinstance(item, dict)]
    active_key = _model_key(active_model)
    indexed = list(enumerate(models))
    indexed.sort(
        key=lambda pair: (
            0 if active_key and _model_key(pair[1].get("model")) == active_key else 1,
            pair[0],
        )
    )
    return [item for _, item in indexed]


def launch_grok_usage(opener: Callable[..., Any] | None = None) -> bool:
    try:
        return bool((opener or webbrowser.open)(GROK_USAGE_URL, new=2))
    except Exception:
        logging.exception("Could not open Grok usage")
        return False


def resource_path(name: str) -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / name


def _font(size: int, weight: str = "normal") -> tuple[str, int, str]:
    return "Segoe UI", size, weight


def _system_uses_dark_mode() -> bool:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
    except OSError:
        return True


def _load_settings() -> dict[str, Any]:
    settings: dict[str, Any] = {
        "always_on_top": True,
        "layout": "full",
        "window_x": None,
        "window_y": None,
        "sizes": {name: [width, height] for name, (width, height) in SIZES.items()},
    }
    try:
        stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            settings.update(stored)
    except (OSError, ValueError):
        pass
    if settings.get("layout") not in SIZES:
        settings["layout"] = "full"
    settings["sizes"] = normalize_window_sizes(settings.get("sizes"))
    return settings


def _save_settings(settings: dict[str, Any]) -> None:
    temporary = SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    os.replace(temporary, SETTINGS_PATH)


def _launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable)}" --tray'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    executable = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{executable}" "{Path(__file__).resolve()}" --tray'


def startup_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_ID)
        return True
    except OSError:
        return False


def set_startup(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_ID, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_ID)
            except FileNotFoundError:
                pass


def _work_area(x=None, y=None) -> tuple[int, int, int, int]:
    class Rect(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rect = Rect()
    try:
        if isinstance(x, int) and isinstance(y, int):
            from ctypes import wintypes
            class MonitorInfo(ctypes.Structure):
                _fields_ = [("size", wintypes.DWORD), ("monitor", Rect), ("work", Rect), ("flags", wintypes.DWORD)]
            user32 = ctypes.windll.user32
            user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
            user32.MonitorFromPoint.restype = wintypes.HANDLE
            user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            monitor = user32.MonitorFromPoint(wintypes.POINT(x, y), 2)
            info = MonitorInfo(size=ctypes.sizeof(MonitorInfo))
            if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                return info.work.left, info.work.top, info.work.right, info.work.bottom
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return 0, 0, 1920, 1080


def _tray_image(remaining: float | None) -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    remaining_value = 0.0 if remaining is None else max(0.0, min(100.0, remaining))
    color = "#46c785" if remaining_value >= 30 else "#f0a64a" if remaining_value >= 10 else "#e65c67"
    draw.ellipse((3, 3, 61, 61), fill="#171923", outline="#3c4152", width=3)
    if remaining is not None:
        draw.arc((8, 8, 56, 56), -90, -90 + int(360 * remaining_value / 100), fill=color, width=7)
    try:
        font = ImageFont.truetype(str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeuib.ttf"), 25)
    except OSError:
        font = ImageFont.load_default()
    draw.text((32, 31), "U", fill="#f2f4fa", font=font, anchor="mm")
    return image


class _DaemonTrayIcon(pystray.Icon):
    """Keep the Win32 tray loop from holding the process after Tk exits."""

    def _run_detached(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="codex-usage-tray").start()


class IpcServer:
    def __init__(self, callback: Callable[[str], None]) -> None:
        self.callback = callback
        self.stop_event = threading.Event()
        self.socket: socket.socket | None = None
        self.thread = threading.Thread(target=self._run, daemon=True, name="codex-usage-ipc")

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket = server
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            server.bind(("127.0.0.1", IPC_PORT))
            server.listen(2)
            server.settimeout(0.5)
            while not self.stop_event.is_set():
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(0.5)
                    try:
                        command = connection.recv(32).decode("ascii", errors="ignore").strip().upper()
                    except socket.timeout:
                        continue
                    if command in {"SHOW", "QUIT", "REFRESH", "SIZE", "HIDE", "HISTORY", "QUOTA"}:
                        self.callback(command.lower())
        except OSError:
            if not self.stop_event.is_set():
                logging.exception("IPC server stopped unexpectedly")
        finally:
            server.close()

    def close(self) -> None:
        self.stop_event.set()
        if self.socket:
            try:
                self.socket.close()
            except OSError:
                pass
        if self.thread.ident != threading.current_thread().ident:
            self.thread.join(timeout=1.5)


def signal_existing_instance(command: str = "SHOW") -> bool:
    try:
        with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=0.4) as connection:
            connection.sendall(command.encode("ascii") + b"\n")
        return True
    except OSError:
        return False


class UsageWidget:
    def __init__(self, root: tk.Tk, *, start_hidden: bool, start_polling: bool = True, create_tray: bool = True) -> None:
        self.root = root
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.settings = _load_settings()
        if self.settings.get("layout_version") != 2:
            self.settings.update(layout_version=2, sizes={k: [self.px(w), self.px(h)] for k, (w, h) in SIZES.items()})
        self.layout = self.settings["layout"]
        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.refresh_event = threading.Event()
        self.force_all = True
        self.visible = not start_hidden
        self.quitting = False
        self.refreshing = False
        self.drag_origin = self.resize_origin = None
        self.tooltip_window = None
        self.tray = None
        self.last_error = None
        self.state: dict[str, Any] = {"provider_quotas": None, "session": None, "models": None}
        self.data = HermesUsageData(HERMES_HOME, resource_path("quota_worker.py"))
        self._quota_thread = None
        self._generation = 0
        self._next_quota = 0.0
        self._context = None
        self._tray_signature = None
        dark = _system_uses_dark_mode()
        self.colors = {
            "window": "#10151d" if dark else "#f3f5f7", "card": "#19212c" if dark else "#ffffff",
            "text": "#ecf0f5" if dark else "#172532", "muted": "#a7b4c5" if dark else "#536375",
            "faint": "#8190a4" if dark else "#69788a", "border": "#2a3645" if dark else "#dbe2e9",
            "track": "#303c4c" if dark else "#e5ebf0", "accent": "#74d8c0" if dark else "#16735f",
            "green": "#74d8c0" if dark else "#16735f", "orange": "#f0be72" if dark else "#945a0b",
            "red": "#f48d95" if dark else "#b43349",
        }
        root.title(APP_NAME)
        root.overrideredirect(True)
        root.configure(bg=self.colors["window"])
        root.attributes("-topmost", bool(self.settings["always_on_top"]))
        root.protocol("WM_DELETE_WINDOW", self.hide)
        root.bind("<Escape>", lambda _e: self.hide())
        self._set_geometry(initial=True)
        self.ui = UsageView(self)
        if create_tray:
            self._create_tray()
        root.after(120, self._drain_events)
        root.after(1000, self._tick)
        if start_hidden:
            root.withdraw()
        if start_polling:
            threading.Thread(target=self._poll_loop, daemon=True, name="usage-poller").start()

    def px(self, value):
        return max(1, round(value * self.scale))

    number = staticmethod(compact_number)
    format_percent = staticmethod(percent)

    def model_rows(self):
        return ordered_model_rows(self.state.get("models"), (self.state.get("session") or {}).get("model"))

    def card_limits(self, provider, compact):
        model = (self.state.get("session") or {}).get("model")
        if compact:
            limit = choose_current_limit(provider, model)
            return [limit] if limit else []
        return visible_limits(provider, model, maximum=20)

    def _quota_color(self, remaining):
        return self.colors["red"] if remaining < 10 else self.colors["orange"] if remaining < 30 else self.colors["green"]

    def freshness(self, provider):
        if provider.get("status") == "loading":
            return "Checking…"
        timestamp = provider.get("fetched_at")
        if not timestamp:
            return "Check failed"
        age = max(0, int(time.time() - timestamp))
        prefix = "Stale · " if provider.get("stale") else "Checked "
        text = "now" if age < 60 else f"{age // 60}m ago" if age < 3600 else f"{age // 3600}h ago"
        return prefix + text

    def quota_tooltip(self, limit, provider):
        return quota_tooltip_text(limit) + "\n" + self.freshness(provider)

    def reset_caption(self, limit):
        if limit.get("usage_text"):
            return str(limit["usage_text"])
        end = limit.get("reset_at")
        if end is None:
            return "Reset time not reported"
        if float(end) <= time.time():
            return "Reset due · awaiting provider update"
        return reset_text(end).capitalize()

    def _tick(self):
        if self.quitting:
            return
        if self.visible:
            self.ui.tick()
        self.root.after(1000 if self.visible else 15000, self._tick)

    def _poll_loop(self):
        next_session = next_models = 0.0
        latest_session, latest_models = {}, {}
        while not self.stop_event.is_set():
            now = time.monotonic()
            force = self.force_all
            self.force_all = False
            self.refresh_event.clear()
            try:
                snapshot = {}
                if force or now >= next_session:
                    latest_session = self.data.fetch_session()
                    snapshot["session"] = latest_session
                    next_session = now + (10 if self.visible else 60)
                if force or now >= next_models:
                    latest_models = self.data.fetch_models()
                    snapshot["models"] = latest_models
                    next_models = now + (60 if self.visible else 180)
                if snapshot:
                    self.events.put(("snapshot", snapshot))
                roster = self.data.configured_provider_ids()
                context = tuple((p, model if p == "gemini" else "") for p, model in self.data.provider_context_key(latest_session, latest_models))
                if context != self._context:
                    self._context = context
                    self._generation += 1
                    self._next_quota = 0
                    self.events.put(("roster", (self._generation, roster)))
                busy = self._quota_thread is not None and self._quota_thread.is_alive()
                if (force or now >= self._next_quota) and not busy:
                    generation = self._generation
                    self.events.put(("refresh_start", None))
                    self._quota_thread = threading.Thread(target=self._collect_quotas,
                        args=(generation, latest_session, latest_models), daemon=True, name="usage-refresh")
                    self._quota_thread.start()
            except Exception:
                logging.exception("Usage poll failed")
                self.events.put(("error", "Local telemetry unavailable"))
            self.refresh_event.wait(1 if force else (5 if self.visible else 30))

    def _collect_quotas(self, generation, session, models):
        try:
            snapshot = self.data.fetch_provider_quotas(session, models,
                on_provider=lambda item: self.events.put(("provider", (generation, item))))
            retry = any(p.get("status") in {"error", "auth_required"} for p in snapshot["providers"])
            interval = 60 if retry else (180 if self.visible else 600)
            future_resets = [limit["reset_at"] - time.time() + 2 for provider in snapshot["providers"]
                             for limit in provider.get("limits", [])
                             if isinstance(limit.get("reset_at"), (int, float)) and limit["reset_at"] > time.time()]
            if future_resets:
                interval = min(interval, min(future_resets))
            self._next_quota = time.monotonic() + interval if generation == self._generation else 0
        except Exception:
            logging.exception("Quota refresh failed")
            self._next_quota = time.monotonic() + 60
        finally:
            self.events.put(("refresh_done", generation))

    def _drain_events(self):
        redraw = False
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "snapshot":
                    changed = snapshot_render_targets(self.state, payload)
                    self.state.update(payload)
                    redraw |= self.ui.view == "history" and bool(changed & {"models", "session"})
                    if "quota" in changed:
                        redraw = True
                elif kind == "roster":
                    _generation, roster = payload
                    previous = {p["id"]: p for p in (self.state.get("provider_quotas") or {}).get("providers", [])}
                    self.state["provider_quotas"] = {"providers": [previous.get(p) or {"id": p, "label": p.title(), "status": "loading", "limits": []} for p in roster]}
                    redraw = True
                elif kind == "provider":
                    generation, provider = payload
                    if generation != self._generation:
                        continue
                    providers = (self.state.get("provider_quotas") or {}).get("providers", [])
                    for index, prior in enumerate(providers):
                        if prior["id"] == provider["id"]:
                            providers[index] = merge_provider(prior, provider)
                            redraw = self.ui.view == "quota" or redraw
                            break
                elif kind == "refresh_start":
                    self.refreshing = True
                elif kind == "refresh_done":
                    self.refreshing = False
                elif kind == "command":
                    self._handle_command(payload)
                elif kind == "error":
                    self.last_error = payload
        except queue.Empty:
            pass
        if not self.quitting:
            if redraw:
                self.ui.render()
            self._update_tray()
            self.ui.tick()
            self.root.after(200 if self.visible else 1000, self._drain_events)

    def _update_tray(self):
        if not self.tray:
            return
        title = tray_usage_title(self.state)
        providers = (self.state.get("provider_quotas") or {}).get("providers", [])
        limits = [choose_current_limit(p, (self.state.get("session") or {}).get("model")) for p in providers if not p.get("stale")]
        values = [x["remaining_percent"] for x in limits if x and x.get("remaining_percent") is not None]
        remaining = min(values) if values else None
        signature = (title, remaining)
        if signature != self._tray_signature:
            self.tray.title = title
            self.tray.icon = _tray_image(remaining)
            self._tray_signature = signature

    def _handle_command(self, command):
        if command == "toggle":
            self.hide() if self.visible else self.show()
        elif command == "refresh":
            self.refresh_now()
        elif command == "size":
            self.toggle_size()
        elif command in {"history", "quota"}:
            self.ui.switch(command)
        elif command == "grok":
            self.open_grok_usage()
        elif command == "topmost":
            self.settings["always_on_top"] = not bool(self.settings["always_on_top"])
            self.root.attributes("-topmost", self.settings["always_on_top"])
            _save_settings(self.settings)
        elif command == "startup":
            try:
                set_startup(not startup_enabled())
            except OSError:
                logging.exception("Startup preference failed")
        elif command == "show":
            self.show()
        elif command == "hide":
            self.hide()
        elif command == "quit":
            self.quit()
        if self.tray:
            self.tray.update_menu()

    def toggle_size(self):
        self.layout = "small" if self.layout == "full" else "full"
        self._set_geometry()
        self.ui.build()
        if self.tray:
            self.tray.update_menu()

    def refresh_now(self):
        if self.refreshing:
            return
        self.refreshing = True
        self.ui.tick()
        self.force_all = True
        self.refresh_event.set()

    def show(self):
        self.visible = True
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", bool(self.settings["always_on_top"]))
        # Showing a fresh popup does not force another request. A stale hidden
        # cadence does switch immediately back to the visible refresh budget.
        providers = (self.state.get("provider_quotas") or {}).get("providers", [])
        if any(time.time() - (p.get("checked_at") or 0) >= 180 for p in providers):
            self._next_quota = 0
        self.refresh_event.set()
        self.ui.tick()

    def hide(self):
        self.visible = False
        self._hide_tooltip()
        self.root.withdraw()

    def request_ipc(self, command):
        self.events.put(("command", command))

    def _sync_status(self):
        self.ui.tick()

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        self._hide_tooltip()
        self.stop_event.set()
        self.refresh_event.set()
        self.data.close()
        self.settings.update(window_x=self.root.winfo_x(), window_y=self.root.winfo_y())
        _save_settings(self.settings)
        if self.tray:
            self.tray.stop()
        for job in self.root.tk.call("after", "info"):
            self.root.after_cancel(job)
        self.root.destroy()

    def _set_geometry(self, *, initial: bool = False) -> None:
        old_width = self.root.winfo_width() if not initial else 0
        old_height = self.root.winfo_height() if not initial else 0
        old_x = self.root.winfo_x() if not initial else None
        old_y = self.root.winfo_y() if not initial else None
        width, height = layout_dimensions(self.layout, self.settings.get("sizes"))
        left, top, right, bottom = _work_area(self.settings.get("window_x"), self.settings.get("window_y"))
        saved_x = self.settings.get("window_x")
        saved_y = self.settings.get("window_y")
        if initial and isinstance(saved_x, int) and isinstance(saved_y, int):
            x, y = saved_x, saved_y
        elif old_x is not None and old_y is not None:
            x = old_x + old_width - width
            y = old_y + old_height - height
        else:
            x, y = right - width - 18, bottom - height - 18
        x = min(max(left, int(x)), max(left, right - width))
        y = min(max(top, int(y)), max(top, bottom - height))
        self.root.geometry(f"{width}x{height}{x:+d}{y:+d}")
        sizes = normalize_window_sizes(self.settings.get("sizes"))
        sizes[self.layout] = [width, height]
        self.settings.update({"layout": self.layout, "window_x": x, "window_y": y, "sizes": sizes})
        _save_settings(self.settings)
        self.root.update_idletasks()
        try:
            preference = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                self.root.winfo_id(), 33, ctypes.byref(preference), ctypes.sizeof(preference)
            )
        except Exception:
            pass

    def _bind_drag(self, widget: tk.Misc) -> None:
        widget.bind("<ButtonPress-1>", self._begin_drag)
        widget.bind("<B1-Motion>", self._drag)
        widget.bind("<ButtonRelease-1>", self._end_drag)

    def _install_resize_handles(self, parent: tk.Misc) -> None:
        specs: list[tuple[str, dict[str, Any]]] = [
            ("n", {"relx": 0, "rely": 0, "relwidth": 1, "height": RESIZE_BORDER}),
            ("s", {"relx": 0, "rely": 1, "relwidth": 1, "height": RESIZE_BORDER, "anchor": "sw"}),
            ("e", {"relx": 1, "rely": 0, "relheight": 1, "width": RESIZE_BORDER, "anchor": "ne"}),
            ("w", {"relx": 0, "rely": 0, "relheight": 1, "width": RESIZE_BORDER}),
            ("nw", {"relx": 0, "rely": 0, "width": RESIZE_BORDER + 4, "height": RESIZE_BORDER + 4}),
            ("ne", {"relx": 1, "rely": 0, "width": RESIZE_BORDER + 4, "height": RESIZE_BORDER + 4, "anchor": "ne"}),
            ("sw", {"relx": 0, "rely": 1, "width": RESIZE_BORDER + 4, "height": RESIZE_BORDER + 4, "anchor": "sw"}),
            ("se", {"relx": 1, "rely": 1, "width": RESIZE_BORDER + 8, "height": RESIZE_BORDER + 8, "anchor": "se"}),
        ]
        for edge, placement in specs:
            handle = tk.Frame(
                parent,
                bg=self.colors["window"],
                cursor=RESIZE_CURSORS[edge],
                borderwidth=0,
                highlightthickness=0,
            )
            handle.place(**placement)
            handle.bind("<ButtonPress-1>", lambda event, name=edge: self._begin_resize(event, name))
            handle.bind("<B1-Motion>", self._resize)
            handle.bind("<ButtonRelease-1>", self._end_resize)
            handle.lift()

    def _show_tooltip(self, text: str) -> None:
        self._hide_tooltip()
        window = tk.Toplevel(self.root)
        self.tooltip_window = window
        window.withdraw()
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        label = tk.Label(
            window,
            text=text,
            bg="#252a38",
            fg="#f1f3f8",
            font=_font(8, "bold"),
            padx=7,
            pady=4,
            borderwidth=1,
            relief="solid",
        )
        label.pack()
        window.update_idletasks()
        x = min(self.root.winfo_pointerx() + 12, self.root.winfo_screenwidth() - window.winfo_reqwidth() - 4)
        y = min(self.root.winfo_pointery() + 14, self.root.winfo_screenheight() - window.winfo_reqheight() - 4)
        window.geometry(f"+{max(0, x)}+{max(0, y)}")
        window.deiconify()

    def _hide_tooltip(self) -> None:
        if self.tooltip_window is None:
            return
        try:
            self.tooltip_window.destroy()
        except tk.TclError:
            pass
        self.tooltip_window = None

    def _create_tray(self) -> None:
        def command(name: str) -> Callable[[pystray.Icon, pystray.MenuItem], None]:
            def handler(_icon: pystray.Icon, _item: pystray.MenuItem) -> None:
                self.events.put(("command", name))
            return handler

        menu = pystray.Menu(
            pystray.MenuItem("Show / hide", command("toggle"), default=True),
            pystray.MenuItem("Refresh now", command("refresh")),
            pystray.MenuItem("Compact view", command("size"), checked=lambda _item: self.layout == "small"),
            pystray.MenuItem("Open Grok usage", command("grok")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Always on top", command("topmost"), checked=lambda _item: bool(self.settings["always_on_top"])),
            pystray.MenuItem("Start with Windows", command("startup"), checked=lambda _item: startup_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", command("quit")),
        )
        self.tray = _DaemonTrayIcon(APP_ID, _tray_image(None), APP_NAME, menu)
        self.tray.run_detached()

    def _begin_drag(self, event: tk.Event[Any]) -> None:
        self.resize_origin = None
        self.drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _drag(self, event: tk.Event[Any]) -> None:
        if self.drag_origin is None:
            return
        start_x, start_y, window_x, window_y = self.drag_origin
        self.root.geometry(f"{window_x + event.x_root - start_x:+d}{window_y + event.y_root - start_y:+d}")

    def _end_drag(self, _event: tk.Event[Any]) -> None:
        if self.drag_origin is None:
            return
        self.drag_origin = None
        self.settings["window_x"] = self.root.winfo_x()
        self.settings["window_y"] = self.root.winfo_y()
        _save_settings(self.settings)

    def _begin_resize(self, event: tk.Event[Any], edge: str) -> None:
        self.drag_origin = None
        self.resize_origin = (
            edge,
            event.x_root,
            event.y_root,
            self.root.winfo_x(),
            self.root.winfo_y(),
            max(1, self.root.winfo_width()),
            max(1, self.root.winfo_height()),
        )

    def _resize(self, event: tk.Event[Any]) -> None:
        if self.resize_origin is None:
            return
        edge, start_px, start_py, start_x, start_y, start_w, start_h = self.resize_origin
        min_w, min_h = (self.px(v) for v in MIN_SIZES.get(self.layout, MIN_SIZES["full"]))
        x, y, width, height = compute_resized_geometry(
            edge,
            start_x=start_x,
            start_y=start_y,
            start_w=start_w,
            start_h=start_h,
            pointer_dx=event.x_root - start_px,
            pointer_dy=event.y_root - start_py,
            min_w=min_w,
            min_h=min_h,
            work_area=_work_area(start_x, start_y),
        )
        self.root.geometry(f"{width}x{height}{x:+d}{y:+d}")

    def _end_resize(self, _event: tk.Event[Any]) -> None:
        if self.resize_origin is None:
            return
        self.resize_origin = None
        width = max(1, self.root.winfo_width())
        height = max(1, self.root.winfo_height())
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        sizes = normalize_window_sizes(self.settings.get("sizes"))
        sizes[self.layout] = [width, height]
        self.settings.update({"window_x": x, "window_y": y, "sizes": sizes, "layout": self.layout})
        _save_settings(self.settings)

    def open_grok_usage(self) -> None:
        if not launch_grok_usage():
            self.last_error = "Could not open Grok Usage"
            self._sync_status()



def main() -> int:
    parser = argparse.ArgumentParser(description="Lightweight provider quota monitor")
    parser.add_argument("--tray", action="store_true", help="Start hidden in the system tray")
    args = parser.parse_args()
    if signal_existing_instance():
        return 0
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    app = UsageWidget(root, start_hidden=args.tray)
    ipc = IpcServer(app.request_ipc)
    ipc.start()
    try:
        root.mainloop()
    finally:
        ipc.close()
        if not app.quitting:
            app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
