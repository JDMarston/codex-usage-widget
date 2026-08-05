from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
import webbrowser
import winreg
from pathlib import Path
from typing import Any, Callable

import pystray
from PIL import Image, ImageDraw, ImageFont

from data_client import HermesUsageData

APP_NAME = "Codex Usage Widget"
APP_ID = "CodexUsageWidget"
IPC_PORT = 47631
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SIZES = {"full": (382, 710), "small": (330, 205)}
GROK_USAGE_URL = "https://grok.com/?_s=usage"

LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
APP_DATA = LOCAL_APP_DATA / APP_ID
SETTINGS_PATH = APP_DATA / "settings.json"
LOG_PATH = APP_DATA / "widget.log"
HERMES_HOME = Path(os.environ.get("HERMES_HOME", LOCAL_APP_DATA / "hermes"))
APP_DATA.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=LOG_PATH, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


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
        return f"{round(float(value))}%"
    except (TypeError, ValueError):
        return "—"


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
    for limit in limits:
        group = _model_key(limit.get("group"))
        if limit.get("kind") == "model" and wanted and group and (wanted in group or group in wanted):
            return limit
    return next((item for item in limits if item.get("kind") == "account"), limits[0] if limits else None)


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
    }
    try:
        stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            settings.update(stored)
    except (OSError, ValueError):
        pass
    if settings.get("layout") not in SIZES:
        settings["layout"] = "full"
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


def _work_area() -> tuple[int, int, int, int]:
    class Rect(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rect = Rect()
    try:
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
    draw.text((32, 31), "C", fill="#f2f4fa", font=font, anchor="mm")
    return image


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
                    command = connection.recv(32).decode("ascii", errors="ignore").strip().upper()
                    if command in {"SHOW", "QUIT"}:
                        self.callback(command.lower())
        except OSError:
            logging.exception("IPC server stopped")
        finally:
            server.close()

    def close(self) -> None:
        self.stop_event.set()
        if self.socket:
            try:
                self.socket.close()
            except OSError:
                pass


def signal_existing_instance(command: str = "SHOW") -> bool:
    try:
        with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=0.4) as connection:
            connection.sendall(command.encode("ascii") + b"\n")
        return True
    except OSError:
        return False


class UsageWidget:
    def __init__(self, root: tk.Tk, *, start_hidden: bool) -> None:
        self.root = root
        self.settings = _load_settings()
        self.layout = str(self.settings["layout"])
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stop_event = threading.Event()
        self.refresh_event = threading.Event()
        self.force_all = True
        self.visible = not start_hidden
        self.quitting = False
        self.drag_origin: tuple[int, int, int, int] | None = None
        self.state: dict[str, Any] = {"quota": None, "grok_quota": None, "session": None, "models": None}
        self.last_updated: float | None = None
        self.last_error: str | None = None
        self.tray: pystray.Icon | None = None
        self.data = HermesUsageData(HERMES_HOME, resource_path("quota_worker.py"))

        dark = _system_uses_dark_mode()
        self.colors = {
            "window": "#11131a" if dark else "#f4f5f8",
            "card": "#191c26" if dark else "#ffffff",
            "card_alt": "#151821" if dark else "#f8f9fb",
            "text": "#f1f3f8" if dark else "#181a20",
            "muted": "#9ba1b2" if dark else "#626979",
            "faint": "#6e7485" if dark else "#8990a0",
            "border": "#2b3040" if dark else "#d8dce5",
            "accent": "#8c7cf0",
            "green": "#46c785",
            "orange": "#f0a64a",
            "red": "#e65c67",
            "track": "#2b3040" if dark else "#e0e3ea",
        }

        self.root.title(APP_NAME)
        self.root.overrideredirect(True)
        self.root.configure(bg=self.colors["window"])
        self.root.attributes("-topmost", bool(self.settings["always_on_top"]))
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.bind("<Escape>", lambda _event: self.hide())

        self._set_geometry(initial=True)
        self._build_ui()
        self._create_tray()
        self.root.after(120, self._drain_events)
        threading.Thread(target=self._poll_loop, daemon=True, name="codex-usage-poller").start()
        if start_hidden:
            self.root.withdraw()
        else:
            self.show()

    def _set_geometry(self, *, initial: bool = False) -> None:
        old_width = self.root.winfo_width() if not initial else 0
        old_height = self.root.winfo_height() if not initial else 0
        old_x = self.root.winfo_x() if not initial else None
        old_y = self.root.winfo_y() if not initial else None
        width, height = SIZES[self.layout]
        left, top, right, bottom = _work_area()
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
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.settings.update({"layout": self.layout, "window_x": x, "window_y": y})
        _save_settings(self.settings)
        self.root.update_idletasks()
        try:
            preference = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                self.root.winfo_id(), 33, ctypes.byref(preference), ctypes.sizeof(preference)
            )
        except Exception:
            pass

    def _label(self, parent: tk.Misc, text: str = "", **kwargs: Any) -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            bg=kwargs.pop("bg", parent.cget("bg")),
            fg=kwargs.pop("fg", self.colors["text"]),
            font=kwargs.pop("font", _font(10)),
            borderwidth=0,
            highlightthickness=0,
            **kwargs,
        )

    def _section_title(self, parent: tk.Misc, text: str) -> tk.Label:
        return self._label(parent, text.upper(), fg=self.colors["muted"], font=_font(8, "bold"), anchor="w")

    def _bind_drag(self, widget: tk.Misc) -> None:
        widget.bind("<ButtonPress-1>", self._begin_drag)
        widget.bind("<B1-Motion>", self._drag)
        widget.bind("<ButtonRelease-1>", self._end_drag)

    def _build_ui(self) -> None:
        for child in self.root.winfo_children():
            child.destroy()

        outer = tk.Frame(
            self.root,
            bg=self.colors["window"],
            highlightbackground=self.colors["border"],
            highlightthickness=1,
        )
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=self.colors["window"], height=58, cursor="fleur")
        header.pack(fill="x", padx=15, pady=(12, 5))
        header.pack_propagate(False)
        self._bind_drag(header)

        title_box = tk.Frame(header, bg=self.colors["window"])
        title_box.pack(side="left", fill="y")
        self._bind_drag(title_box)
        title = self._label(title_box, "API USAGE", font=_font(11, "bold"), anchor="w")
        title.pack(anchor="w")
        self._bind_drag(title)

        status_row = tk.Frame(title_box, bg=self.colors["window"])
        status_row.pack(anchor="w", pady=(4, 0))
        self._bind_drag(status_row)
        monitored_models = tk.Frame(
            status_row,
            bg=self.colors["card"],
            highlightbackground=self.colors["border"],
            highlightthickness=1,
        )
        monitored_models.pack(side="left")
        monitored_label = self._label(
            monitored_models,
            "CODEX · GROK",
            bg=self.colors["card"],
            fg=self.colors["muted"],
            font=_font(7, "bold"),
        )
        monitored_label.pack(padx=5, pady=2)
        self._bind_drag(monitored_models)
        self._bind_drag(monitored_label)
        self.status_dot = tk.Canvas(status_row, width=8, height=8, bg=self.colors["window"], highlightthickness=0)
        self.status_dot.pack(side="left", padx=(7, 0), pady=(2, 0))
        self.status_dot.create_oval(1, 1, 7, 7, fill=self.colors["orange"], outline="", tags="dot")
        self.status_var = tk.StringVar(value="Connecting to Hermes…")
        status_label = self._label(status_row, textvariable=self.status_var, fg=self.colors["muted"], font=_font(8))
        status_label.pack(side="left", padx=(6, 0))
        self._bind_drag(status_label)

        close_button = tk.Button(
            header,
            text="×",
            command=self.hide,
            bg=self.colors["window"],
            fg=self.colors["muted"],
            activebackground=self.colors["card"],
            activeforeground=self.colors["text"],
            borderwidth=0,
            highlightthickness=0,
            font=_font(15),
            cursor="hand2",
            width=2,
        )
        close_button.pack(side="right", anchor="n")

        size_button = tk.Button(
            header,
            text="−" if self.layout == "full" else "□",
            command=self.toggle_size,
            bg=self.colors["window"],
            fg=self.colors["muted"],
            activebackground=self.colors["card"],
            activeforeground=self.colors["text"],
            borderwidth=0,
            highlightthickness=0,
            font=_font(14, "bold"),
            cursor="hand2",
            width=2,
        )
        size_button.pack(side="right", anchor="n", padx=(0, 2))

        if self.layout == "full":
            grok_button = tk.Button(
                header,
                text="GROK",
                command=self.open_grok_usage,
                bg=self.colors["window"],
                fg=self.colors["accent"],
                activebackground=self.colors["card"],
                activeforeground=self.colors["text"],
                borderwidth=0,
                highlightthickness=0,
                font=_font(8, "bold"),
                cursor="hand2",
                width=5,
            )
            grok_button.pack(side="right", anchor="n", padx=(0, 3), pady=(3, 0))

        content = tk.Frame(outer, bg=self.colors["window"])
        content.pack(fill="both", expand=True, padx=15, pady=(0, 10 if self.layout == "full" else 8))

        if self.layout == "small":
            self.quota_container = tk.Frame(content, bg=self.colors["window"])
            self.quota_container.pack(fill="x")
            self.session_model_var = tk.StringVar(value="—")
            self.metric_vars = {}
            self.models_total_var = tk.StringVar(value="")
            self.models_container = None
            self.footer_var = tk.StringVar(value="")
            self._render_quota()
            self._sync_status()
            return

        self._section_title(content, "Account quota").pack(fill="x", pady=(0, 5))
        self.quota_container = tk.Frame(content, bg=self.colors["window"])
        self.quota_container.pack(fill="x")
        self._render_loading_quota()

        session_header = tk.Frame(content, bg=self.colors["window"])
        session_header.pack(fill="x", pady=(12, 5))
        self._section_title(session_header, "Current session spend").pack(side="left")
        self.session_model_var = tk.StringVar(value="—")
        self._label(
            session_header,
            textvariable=self.session_model_var,
            fg=self.colors["faint"],
            font=_font(8),
        ).pack(side="right")

        session_card = tk.Frame(
            content,
            bg=self.colors["card"],
            highlightbackground=self.colors["border"],
            highlightthickness=1,
        )
        session_card.pack(fill="x")
        metrics = tk.Frame(session_card, bg=self.colors["card"])
        metrics.pack(fill="x", padx=10, pady=9)
        self.metric_vars: dict[str, tk.StringVar] = {}
        for index, (key, label) in enumerate(
            (("total", "PROCESSED"), ("input", "INPUT"), ("output", "OUTPUT"), ("calls", "CALLS"))
        ):
            cell = tk.Frame(metrics, bg=self.colors["card"])
            cell.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 5, 0))
            metrics.grid_columnconfigure(index, weight=1)
            variable = tk.StringVar(value="—")
            self.metric_vars[key] = variable
            self._label(cell, textvariable=variable, bg=self.colors["card"], font=_font(12, "bold"), anchor="w").pack(anchor="w")
            self._label(
                cell,
                label,
                bg=self.colors["card"],
                fg=self.colors["faint"],
                font=_font(7, "bold"),
                anchor="w",
            ).pack(anchor="w", pady=(2, 0))

        models_header = tk.Frame(content, bg=self.colors["window"])
        models_header.pack(fill="x", pady=(12, 3))
        self._section_title(models_header, "Models · all tracked").pack(side="left")
        self.models_total_var = tk.StringVar(value="")
        self._label(models_header, textvariable=self.models_total_var, fg=self.colors["faint"], font=_font(8)).pack(side="right")
        self.models_container = tk.Frame(content, bg=self.colors["window"])
        self.models_container.pack(fill="x")
        self._label(
            self.models_container,
            "Loading model telemetry…",
            fg=self.colors["muted"],
            font=_font(9),
            anchor="w",
        ).pack(fill="x", pady=8)

        footer = tk.Frame(outer, bg=self.colors["window"])
        footer.pack(fill="x", padx=15, pady=(0, 12))
        self.footer_var = tk.StringVar(value="Quota from ChatGPT + Grok · tokens from Hermes")
        self._label(footer, textvariable=self.footer_var, fg=self.colors["faint"], font=_font(7), anchor="w").pack(side="left")
        tk.Button(
            footer,
            text="↻",
            command=self.refresh_now,
            bg=self.colors["window"],
            fg=self.colors["muted"],
            activebackground=self.colors["card"],
            activeforeground=self.colors["text"],
            borderwidth=0,
            highlightthickness=0,
            font=_font(12, "bold"),
            cursor="hand2",
            width=2,
        ).pack(side="right")

        self._render_all()
        self._sync_status()

    def _sync_status(self) -> None:
        if self.last_error:
            self.status_dot.itemconfigure("dot", fill=self.colors["red"])
            self.status_var.set(self.last_error)
            return
        if self.state.get("quota") or self.state.get("session"):
            self.status_dot.itemconfigure("dot", fill=self.colors["green"])
            plan = (self.state.get("quota") or {}).get("plan")
            self.status_var.set(f"Live · {plan}" if plan else "Live")
            return
        self.status_dot.itemconfigure("dot", fill=self.colors["orange"])
        self.status_var.set("Connecting to Hermes…")

    def _render_loading_quota(self) -> None:
        for child in self.quota_container.winfo_children():
            child.destroy()
        self._label(
            self.quota_container,
            "Checking live account limits…",
            fg=self.colors["muted"],
            font=_font(9),
            anchor="w",
        ).pack(fill="x", pady=6)

    def _quota_color(self, remaining: float) -> str:
        if remaining < 10:
            return self.colors["red"]
        if remaining < 30:
            return self.colors["orange"]
        return self.colors["green"]

    def _draw_bar(self, canvas: tk.Canvas, used: float) -> None:
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        canvas.create_rectangle(0, 0, width, 4, fill=self.colors["track"], outline="")
        canvas.create_rectangle(0, 0, width * max(0.0, min(100.0, used)) / 100, 4, fill=self.colors["accent"], outline="")

    def _quota_card(self, parent: tk.Misc, limit: dict[str, Any], *, small: bool) -> None:
        card = tk.Frame(
            parent,
            bg=self.colors["card"],
            highlightbackground=self.colors["border"],
            highlightthickness=1,
        )
        card.pack(fill="x", pady=(0, 7))
        top = tk.Frame(card, bg=self.colors["card"])
        top.pack(fill="x", padx=10, pady=(8, 4))
        group = str(limit.get("group") or "Codex account")
        label = str(limit.get("label") or "Weekly")
        remaining = float(limit.get("remaining_percent") or 0)
        used = float(limit.get("used_percent") or 0)
        self._label(top, f"{group} · {label}", bg=self.colors["card"], font=_font(8, "bold"), anchor="w").pack(side="left")
        self._label(
            top,
            f"{percent(remaining)} left",
            bg=self.colors["card"],
            fg=self._quota_color(remaining),
            font=_font(10, "bold"),
        ).pack(side="right")
        bar = tk.Canvas(card, height=4, bg=self.colors["track"], highlightthickness=0)
        bar.pack(fill="x", padx=10, pady=(2, 8 if small else 3))
        bar.bind("<Configure>", lambda _event, widget=bar, amount=used: self._draw_bar(widget, amount))
        if not small:
            bottom = tk.Frame(card, bg=self.colors["card"])
            bottom.pack(fill="x", padx=10, pady=(0, 7))
            self._label(bottom, f"{percent(used)} used", bg=self.colors["card"], fg=self.colors["faint"], font=_font(7)).pack(side="left")
            self._label(bottom, reset_text(limit.get("reset_at")), bg=self.colors["card"], fg=self.colors["faint"], font=_font(7)).pack(side="right")

    def _render_quota(self) -> None:
        for child in self.quota_container.winfo_children():
            child.destroy()
        quota = self.state.get("quota") or {}
        session = self.state.get("session") or {}
        if self.layout == "small":
            account = next((item for item in quota.get("limits", []) if item.get("kind") == "account"), None)
            grok_quota = self.state.get("grok_quota") or {}
            grok_limit = grok_quota.get("limit") if grok_quota.get("available") else None
            if account:
                self._quota_card(self.quota_container, account, small=True)
            if isinstance(grok_limit, dict):
                self._quota_card(self.quota_container, grok_limit, small=True)
            if not account and not isinstance(grok_limit, dict):
                self._label(self.quota_container, "Weekly quotas unavailable", fg=self.colors["muted"], font=_font(9), anchor="w").pack(fill="x", pady=8)
            return
        limits = visible_limits(quota, session.get("model"), maximum=2)
        for limit in limits:
            self._quota_card(self.quota_container, limit, small=False)
        grok_quota = self.state.get("grok_quota") or {}
        grok_limit = grok_quota.get("limit") if grok_quota.get("available") else None
        if isinstance(grok_limit, dict):
            self._quota_card(self.quota_container, grok_limit, small=False)
        elif limits:
            message = str(grok_quota.get("error") or "SuperGrok weekly quota unavailable")
            self._label(self.quota_container, message, fg=self.colors["faint"], font=_font(8), anchor="w").pack(fill="x", pady=(0, 5))
        else:
            message = str(quota.get("error") or grok_quota.get("error") or "No account quota was returned.")
            self._label(self.quota_container, message, fg=self.colors["muted"], font=_font(9), anchor="w").pack(fill="x", pady=6)

    def _render_session(self) -> None:
        if self.layout != "full":
            return
        session = self.state.get("session") or {}
        if not session.get("available"):
            self.session_model_var.set("No active session")
            for variable in self.metric_vars.values():
                variable.set("—")
            return
        self.session_model_var.set(str(session.get("model") or "unknown"))
        self.metric_vars["total"].set(compact_number(session.get("total_tokens")))
        self.metric_vars["input"].set(compact_number(session.get("input_tokens")))
        self.metric_vars["output"].set(compact_number(session.get("output_tokens")))
        self.metric_vars["calls"].set(compact_number(session.get("api_calls")))

    def _render_models(self) -> None:
        if self.layout != "full" or self.models_container is None:
            return
        for child in self.models_container.winfo_children():
            child.destroy()
        models_snapshot = self.state.get("models") or {}
        models = list(models_snapshot.get("models") or [])[:3]
        total = sum(int(item.get("total_tokens") or 0) for item in (models_snapshot.get("models") or []))
        self.models_total_var.set(f"{compact_number(total)} tracked" if total else "")
        if not models:
            self._label(self.models_container, "No model telemetry yet.", fg=self.colors["muted"], font=_font(9), anchor="w").pack(fill="x", pady=8)
            return
        for index, item in enumerate(models):
            row = tk.Frame(
                self.models_container,
                bg=self.colors["card_alt"],
                highlightbackground=self.colors["border"],
                highlightthickness=1,
            )
            row.pack(fill="x", pady=(0, 5))
            left = tk.Frame(row, bg=self.colors["card_alt"])
            left.pack(side="left", fill="both", expand=True, padx=9, pady=7)
            model_name = str(item.get("model") or "unknown")
            if len(model_name) > 34:
                model_name = model_name[:31] + "…"
            self._label(left, model_name, bg=self.colors["card_alt"], font=_font(8, "bold"), anchor="w").pack(anchor="w")
            details = f"in {compact_number(item.get('input_tokens'))} · out {compact_number(item.get('output_tokens'))} · cache {compact_number(item.get('cache_tokens'))}"
            self._label(left, details, bg=self.colors["card_alt"], fg=self.colors["faint"], font=_font(7), anchor="w").pack(anchor="w", pady=(2, 0))
            self._label(row, compact_number(item.get("total_tokens")), bg=self.colors["card_alt"], font=_font(10, "bold")).pack(side="right", padx=9)

    def _render_all(self) -> None:
        self._render_quota()
        self._render_session()
        self._render_models()

    def _create_tray(self) -> None:
        def command(name: str) -> Callable[[pystray.Icon, pystray.MenuItem], None]:
            def handler(_icon: pystray.Icon, _item: pystray.MenuItem) -> None:
                self.events.put(("command", name))
            return handler

        menu = pystray.Menu(
            pystray.MenuItem("Show / hide", command("toggle"), default=True),
            pystray.MenuItem("Refresh now", command("refresh")),
            pystray.MenuItem("Weekly only", command("size"), checked=lambda _item: self.layout == "small"),
            pystray.MenuItem("Open Grok usage", command("grok")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Always on top", command("topmost"), checked=lambda _item: bool(self.settings["always_on_top"])),
            pystray.MenuItem("Start with Windows", command("startup"), checked=lambda _item: startup_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", command("quit")),
        )
        self.tray = pystray.Icon(APP_ID, _tray_image(None), APP_NAME, menu)
        self.tray.run_detached()

    def _poll_loop(self) -> None:
        next_session = next_models = next_quota = next_grok_quota = 0.0
        while not self.stop_event.is_set():
            now = time.monotonic()
            forced = self.force_all
            self.force_all = False
            snapshot: dict[str, Any] = {}
            try:
                if forced or now >= next_session:
                    snapshot["session"] = self.data.fetch_session()
                    next_session = now + (10.0 if self.visible else 60.0)
                if forced or now >= next_models:
                    snapshot["models"] = self.data.fetch_models()
                    next_models = now + (120.0 if self.visible else 600.0)
                if forced or now >= next_quota:
                    snapshot["quota"] = self.data.fetch_quota()
                    next_quota = now + (300.0 if self.visible else 900.0)
                if forced or now >= next_grok_quota:
                    snapshot["grok_quota"] = self.data.fetch_grok_quota()
                    next_grok_quota = now + (300.0 if self.visible else 900.0)
                if snapshot:
                    snapshot["collected_at"] = time.time()
                    self.events.put(("snapshot", snapshot))
            except Exception:
                logging.exception("Usage polling failed")
                self.events.put(("error", "Collector offline — retrying"))
            wait_until = min(next_session, next_models, next_quota, next_grok_quota)
            timeout = max(0.5, min(60.0, wait_until - time.monotonic()))
            self.refresh_event.wait(timeout)
            self.refresh_event.clear()

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "snapshot":
                    for key in ("quota", "grok_quota", "session", "models"):
                        if key in payload:
                            self.state[key] = payload[key]
                    self.last_updated = float(payload.get("collected_at") or time.time())
                    self.last_error = None
                    self._sync_status()
                    if self.layout == "full":
                        timestamp = time.strftime("%I:%M %p", time.localtime(self.last_updated)).lstrip("0")
                        self.footer_var.set(f"Quota from ChatGPT + Grok · tokens from Hermes · {timestamp}")
                    self._render_all()
                    self._update_tray()
                elif kind == "error":
                    self.last_error = str(payload)
                    self.status_dot.itemconfigure("dot", fill=self.colors["red"])
                    self.status_var.set(self.last_error)
                elif kind == "command":
                    self._handle_command(str(payload))
        except queue.Empty:
            pass
        if not self.quitting:
            self.root.after(150, self._drain_events)

    def _update_tray(self) -> None:
        if not self.tray:
            return
        limit = choose_current_limit(self.state.get("quota"), (self.state.get("session") or {}).get("model"))
        remaining = float(limit.get("remaining_percent")) if limit and limit.get("remaining_percent") is not None else None
        self.tray.icon = _tray_image(remaining)
        self.tray.title = f"Codex Usage — {percent(remaining)} left" if remaining is not None else APP_NAME

    def _handle_command(self, command: str) -> None:
        if command == "toggle":
            self.hide() if self.visible else self.show()
        elif command == "refresh":
            self.refresh_now()
        elif command == "size":
            self.toggle_size()
        elif command == "grok":
            self.open_grok_usage()
        elif command == "topmost":
            self.settings["always_on_top"] = not bool(self.settings["always_on_top"])
            self.root.attributes("-topmost", bool(self.settings["always_on_top"]))
            _save_settings(self.settings)
            if self.tray:
                self.tray.update_menu()
        elif command == "startup":
            try:
                set_startup(not startup_enabled())
            except OSError:
                logging.exception("Could not update startup setting")
            if self.tray:
                self.tray.update_menu()
        elif command == "show":
            self.show()
        elif command == "quit":
            self.quit()

    def toggle_size(self) -> None:
        self.layout = "small" if self.layout == "full" else "full"
        self._set_geometry()
        self._build_ui()
        if self.tray:
            self.tray.update_menu()

    def open_grok_usage(self) -> None:
        if not launch_grok_usage():
            self.last_error = "Could not open Grok Usage"
            self._sync_status()

    def refresh_now(self) -> None:
        self.status_dot.itemconfigure("dot", fill=self.colors["orange"])
        self.status_var.set("Refreshing…")
        self.force_all = True
        self.refresh_event.set()

    def show(self) -> None:
        self.visible = True
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", bool(self.settings["always_on_top"]))
        self.force_all = True
        self.refresh_event.set()

    def hide(self) -> None:
        self.visible = False
        self.root.withdraw()

    def request_ipc(self, command: str) -> None:
        self.events.put(("command", command))

    def _begin_drag(self, event: tk.Event[Any]) -> None:
        self.drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _drag(self, event: tk.Event[Any]) -> None:
        if self.drag_origin is None:
            return
        start_x, start_y, window_x, window_y = self.drag_origin
        self.root.geometry(f"+{window_x + event.x_root - start_x}+{window_y + event.y_root - start_y}")

    def _end_drag(self, _event: tk.Event[Any]) -> None:
        if self.drag_origin is None:
            return
        self.drag_origin = None
        self.settings["window_x"] = self.root.winfo_x()
        self.settings["window_y"] = self.root.winfo_y()
        _save_settings(self.settings)

    def quit(self) -> None:
        if self.quitting:
            return
        self.quitting = True
        self.stop_event.set()
        self.refresh_event.set()
        self.settings.update({"window_x": self.root.winfo_x(), "window_y": self.root.winfo_y()})
        _save_settings(self.settings)
        if self.tray:
            self.tray.stop()
        self.root.destroy()


def main() -> int:
    parser = argparse.ArgumentParser(description="Lightweight Codex usage widget")
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
