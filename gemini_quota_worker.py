from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

CDP_BASE = "http://127.0.0.1:9222"
RATE_LIMIT_URL = "https://aistudio.google.com/u/1/rate-limit?timeRange=last-28-days"


def parse_amount(value: Any) -> float | int | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text == "-":
        return None
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*([KMB]?)", text, re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    factor = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2).upper()]
    result = number * factor
    return int(result) if result.is_integer() else result


def _model_key(value: Any) -> str:
    words = re.findall(r"[a-z0-9]+", str(value or "").lower().replace("models/", ""))
    ignored = {"preview", "latest", "experimental", "exp", "model", "models"}
    return "".join(word for word in words if word not in ignored)


def _parse_metric(metric: str, value: Any) -> dict[str, Any] | None:
    text = str(value or "").strip()
    if "/" not in text:
        return None
    used_text, limit_text = (part.strip() for part in text.split("/", 1))
    used = parse_amount(used_text)
    limit = parse_amount(limit_text)
    if used is None or limit is None or float(limit) <= 0:
        return None
    used_percent = max(0.0, min(100.0, float(used) / float(limit) * 100.0))
    return {
        "metric": metric,
        "used": used,
        "limit": limit,
        "used_text": used_text,
        "limit_text": limit_text,
        "used_percent": used_percent,
        "remaining_percent": max(0.0, 100.0 - used_percent),
    }


def parse_rate_limit_rows(
    rows: list[list[str]],
    model: Any,
    *,
    fetched_at: float | None = None,
) -> dict[str, Any]:
    wanted = _model_key(model)
    candidates: list[tuple[int, list[str]]] = []
    for cells in rows:
        if len(cells) < 6:
            continue
        label = str(cells[1] or "").strip()
        key = _model_key(label)
        if not key or not key.startswith("gemini"):
            continue
        if wanted and (key == wanted or key in wanted or wanted in key):
            candidates.append((len(key), cells))
    if not candidates:
        return {
            "available": False,
            "provider": "gemini",
            "model": str(model or ""),
            "limits": [],
            "fetched_at": fetched_at or time.time(),
            "error": "Gemini rate-limit data for this model is not available in Google AI Studio.",
        }

    cells = max(candidates, key=lambda item: item[0])[1]
    label = str(cells[1]).strip()
    metrics = [
        item
        for item in (
            _parse_metric("RPM", cells[3]),
            _parse_metric("TPM", cells[4]),
            _parse_metric("RPD", cells[5]),
        )
        if item is not None
    ]
    if not metrics:
        return {
            "available": False,
            "provider": "gemini",
            "model": label,
            "limits": [],
            "fetched_at": fetched_at or time.time(),
            "error": "Gemini rate-limit values are unavailable.",
        }

    primary = max(metrics, key=lambda item: float(item["used_percent"]))
    details = "\n".join(
        [f"{item['metric']} {item['used_text']} / {item['limit_text']}" for item in metrics]
        + ["Peak usage over the last 28 days"]
    )
    limit = {
        "id": f"gemini:{_model_key(label)}:{primary['metric'].lower()}",
        "kind": "model",
        "provider": "gemini",
        "group": label,
        "label": f"{primary['metric']} peak · 28d",
        "metric": primary["metric"],
        "used_percent": primary["used_percent"],
        "remaining_percent": primary["remaining_percent"],
        "usage_text": f"{primary['metric']} {primary['used_text']} / {primary['limit_text']}",
        "details": details,
        "period_label": "peak · 28d",
        "source": "google-ai-studio-rate-limit",
    }
    timestamp = fetched_at or time.time()
    return {
        "available": True,
        "provider": "gemini",
        "model": label,
        "limit": limit,
        "limits": [limit],
        "metrics": metrics,
        "fetched_at": timestamp,
        "source": "google-ai-studio-rate-limit",
    }


def _http_json(url: str, *, method: str = "GET", timeout: float = 15) -> Any:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _endpoint_ready() -> bool:
    try:
        payload = _http_json(f"{CDP_BASE}/json/version", timeout=2)
        return bool(isinstance(payload, dict) and payload.get("webSocketDebuggerUrl"))
    except Exception:
        return False


def _hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        return Path(configured)
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local_app_data / "hermes"


def _ensure_bob_browser() -> None:
    if _endpoint_ready():
        return
    launcher = _hermes_home() / "scripts" / "start-bob-browser.py"
    if not launcher.exists():
        raise RuntimeError("Bob Browser launcher is unavailable")
    subprocess.run(
        [sys.executable, str(launcher)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    deadline = time.time() + 12
    while time.time() < deadline:
        if _endpoint_ready():
            return
        time.sleep(0.25)
    raise RuntimeError("Bob Browser CDP endpoint did not start")


def _recv_exact(connection: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = connection.recv(count - len(data))
        if not chunk:
            raise RuntimeError("CDP WebSocket closed")
        data += chunk
    return data


class _CdpConnection:
    def __init__(self, websocket_url: str) -> None:
        parsed = urllib.parse.urlparse(websocket_url)
        self.connection = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=30)
        self.connection.settimeout(50)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.netloc}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self.connection.sendall(request)
        response = b""
        while b"\r\n\r\n" not in response:
            response += self.connection.recv(4096)
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError("CDP WebSocket handshake failed")
        self.next_id = 1

    def _send_frame(self, payload: bytes, opcode: int = 1) -> None:
        mask = os.urandom(4)
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length <= 0xFFFF:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.connection.sendall(header + mask + masked)

    def _receive_message(self) -> bytes:
        chunks: list[bytes] = []
        while True:
            first, second = _recv_exact(self.connection, 2)
            finished = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", _recv_exact(self.connection, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", _recv_exact(self.connection, 8))[0]
            mask = _recv_exact(self.connection, 4) if masked else b""
            payload = _recv_exact(self.connection, length)
            if masked:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 0x9:
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0x8:
                raise RuntimeError("CDP WebSocket closed")
            if opcode in (0x0, 0x1, 0x2):
                chunks.append(payload)
                if finished:
                    return b"".join(chunks)

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        message_id = self.next_id
        self.next_id += 1
        self._send_frame(json.dumps({"id": message_id, "method": method, "params": params or {}}).encode("utf-8"))
        while True:
            message = json.loads(self._receive_message().decode("utf-8"))
            if message.get("id") != message_id:
                continue
            if message.get("error"):
                raise RuntimeError("CDP command failed")
            return message.get("result", {})

    def close(self) -> None:
        try:
            self._send_frame(b"", opcode=0x8)
        except Exception:
            pass
        self.connection.close()


def _open_or_find_target() -> tuple[dict[str, Any], bool]:
    targets = _http_json(f"{CDP_BASE}/json/list")
    for target in targets:
        url = str(target.get("url") or "")
        if target.get("type") == "page" and "aistudio.google.com/u/1/rate-limit" in url:
            return target, False
    encoded = urllib.parse.quote(RATE_LIMIT_URL, safe="")
    target = _http_json(f"{CDP_BASE}/json/new?{encoded}", method="PUT", timeout=20)
    return target, True


def _close_target(target_id: str) -> None:
    try:
        _http_json(f"{CDP_BASE}/json/close/{urllib.parse.quote(target_id)}", timeout=5)
    except Exception:
        pass


def _fetch_rate_limit_rows(model: str) -> list[list[str]]:
    _ensure_bob_browser()
    target, created = _open_or_find_target()
    connection = _CdpConnection(str(target["webSocketDebuggerUrl"]))
    try:
        connection.call("Page.enable")
        # A target returned by /json/new can advertise the destination URL
        # before its first JavaScript execution context exists. Navigate
        # explicitly and give Chromium one geometry/event-loop turn before
        # evaluating the polling script.
        connection.call("Page.navigate", {"url": RATE_LIMIT_URL})
        time.sleep(1.0)
        wanted_model = json.dumps(_model_key(model))
        expression = r'''(async () => {
          const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
          const modelKey = (value) => clean(value).toLowerCase().replace(/models\//g, '')
            .split(/[^a-z0-9]+/).filter((word) => word && !['preview', 'latest', 'experimental', 'exp', 'model', 'models'].includes(word)).join('');
          const wanted = ''' + wanted_model + r''';
          const deadline = Date.now() + 45000;
          while (Date.now() < deadline) {
            const rows = [...document.querySelectorAll('tr')]
              .map((row) => [...row.querySelectorAll('th,td,[role="cell"],[role="columnheader"]')]
                .map((cell) => clean(cell.innerText || cell.textContent)).filter(Boolean))
              .filter((cells) => cells.length >= 6 && /^Gemini\b/i.test(cells[1] || ''));
            const wantedRow = rows.find((cells) => modelKey(cells[1]) === wanted);
            if (wantedRow && [3, 4, 5].every((index) => (wantedRow[index] || '').includes('/'))) {
              return {rows, title: document.title, url: location.href};
            }
            const body = clean(document.body?.innerText);
            if (/choose an account|sign in/i.test(body)) return {rows: [], authRequired: true};
            await new Promise((resolve) => setTimeout(resolve, 350));
          }
          return {rows: [], timedOut: true};
        })()'''
        result: dict[str, Any] | None = None
        for attempt in range(3):
            try:
                result = connection.call(
                    "Runtime.evaluate",
                    {"expression": expression, "awaitPromise": True, "returnByValue": True},
                )
                break
            except RuntimeError:
                if attempt == 2:
                    raise
                time.sleep(0.75)
        if result is None:
            raise RuntimeError("Gemini rate-limit page did not expose a runtime context")
        value = result.get("result", {}).get("value") or {}
        rows = value.get("rows") if isinstance(value, dict) else []
        return [list(map(str, row)) for row in rows if isinstance(row, list)]
    finally:
        connection.close()
        if created:
            _close_target(str(target.get("id") or ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    try:
        rows = _fetch_rate_limit_rows(args.model)
        payload = parse_rate_limit_rows(rows, args.model)
    except Exception:
        payload = {
            "available": False,
            "provider": "gemini",
            "model": args.model,
            "limits": [],
            "fetched_at": time.time(),
            "error": "Gemini quota sync is unavailable. Open Bob Browser and sign in to Google AI Studio.",
        }
    print(json.dumps(payload, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
