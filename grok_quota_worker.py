from __future__ import annotations

import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BILLING_URL = "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"
CLI_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
EMPTY_GRPC_WEB_FRAME = b"\x00\x00\x00\x00\x00"
PRODUCT_NAMES = {
    0: "Unspecified",
    1: "API",
    2: "Grok Build",
    3: "Grok Plugins",
    4: "Grok Chat",
    5: "Grok Imagine",
    6: "Grok Voice",
    7: "Grok App Builder",
}


def hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        return Path(configured)
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "hermes"


def _read_varint(data: bytes, index: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    while index < len(data) and shift < 64:
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, index
        shift += 7
    raise ValueError("truncated protobuf varint")


def _parse_fields(data: bytes) -> dict[int, list[tuple[str, Any]]]:
    fields: dict[int, list[tuple[str, Any]]] = {}
    index = 0
    while index < len(data):
        key, index = _read_varint(data, index)
        field_number, wire_type = key >> 3, key & 7
        if field_number == 0:
            raise ValueError("invalid protobuf field")
        if wire_type == 0:
            value, index = _read_varint(data, index)
            item: tuple[str, Any] = ("varint", value)
        elif wire_type == 1:
            if index + 8 > len(data):
                raise ValueError("truncated protobuf fixed64")
            item = ("fixed64", data[index : index + 8])
            index += 8
        elif wire_type == 2:
            length, index = _read_varint(data, index)
            end = index + length
            if end > len(data):
                raise ValueError("truncated protobuf bytes")
            item = ("bytes", data[index:end])
            index = end
        elif wire_type == 5:
            if index + 4 > len(data):
                raise ValueError("truncated protobuf fixed32")
            item = ("fixed32", data[index : index + 4])
            index += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        fields.setdefault(field_number, []).append(item)
    return fields


def _first_varint(values: list[tuple[str, Any]] | None) -> int | None:
    if values and values[0][0] == "varint":
        return int(values[0][1])
    return None


def _first_float(values: list[tuple[str, Any]] | None) -> float | None:
    if not values:
        return None
    kind, value = values[0]
    if kind == "fixed32":
        return float(struct.unpack("<f", value)[0])
    if kind == "varint":
        return float(value)
    return None


def _first_message(values: list[tuple[str, Any]] | None) -> bytes | None:
    if values and values[0][0] == "bytes":
        return bytes(values[0][1])
    return None


def _timestamp(values: list[tuple[str, Any]] | None) -> int | None:
    message = _first_message(values)
    if message is None:
        return None
    return _first_varint(_parse_fields(message).get(1))


def _grpc_data_frames(data: bytes) -> list[bytes]:
    frames: list[bytes] = []
    index = 0
    while index + 5 <= len(data):
        flags = data[index]
        length = int.from_bytes(data[index + 1 : index + 5], "big")
        start, end = index + 5, index + 5 + length
        if end > len(data):
            raise ValueError("truncated gRPC-web frame")
        if flags & 0x80 == 0:
            frames.append(data[start:end])
        index = end
    return frames


def parse_credits_response(message: bytes) -> dict[str, Any]:
    top = _parse_fields(message)
    config_message = _first_message(top.get(1))
    if config_message is None:
        raise ValueError("SuperGrok response did not include a usage config")
    config = _parse_fields(config_message)

    used = max(0.0, min(100.0, _first_float(config.get(1)) or 0.0))
    period_start = _timestamp(config.get(4))
    reset_at = _timestamp(config.get(5))

    current_period = _first_message(config.get(8))
    if current_period is not None:
        period = _parse_fields(current_period)
        period_start = _timestamp(period.get(2)) or period_start
        reset_at = _timestamp(period.get(3)) or reset_at

    products: list[dict[str, Any]] = []
    for kind, raw in config.get(7, []):
        if kind != "bytes":
            continue
        product = _parse_fields(bytes(raw))
        product_id = _first_varint(product.get(1)) or 0
        product_used = _first_float(product.get(2))
        if product_used is None:
            continue
        products.append(
            {
                "id": product_id,
                "label": PRODUCT_NAMES.get(product_id, f"Product {product_id}"),
                "used_percent": max(0.0, min(100.0, product_used)),
            }
        )

    return {
        "available": True,
        "plan": "SuperGrok",
        "limit": {
            "id": "supergrok-weekly",
            "kind": "account",
            "group": "SuperGrok",
            "label": "Weekly",
            "used_percent": used,
            "remaining_percent": max(0.0, 100.0 - used),
            "period_start_at": period_start,
            "reset_at": reset_at,
        },
        "products": products,
        "source": "grok-grpc-web",
        "collected_at": time.time(),
    }


def _request(url: str, access_token: str, *, grpc: bool) -> bytes:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "*/*" if grpc else "application/json",
        "User-Agent": "CodexUsageWidget/1.1",
    }
    data: bytes | None = None
    if grpc:
        data = EMPTY_GRPC_WEB_FRAME
        headers.update(
            {
                "Content-Type": "application/grpc-web+proto",
                "X-Grpc-Web": "1",
                "X-User-Agent": "connect-es/2.1.1",
                "Origin": "https://grok.com",
                "Referer": "https://grok.com/?_s=usage",
            }
        )
    else:
        headers["X-Grok-Client-Mode"] = "cli"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if grpc else "GET")
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read()


def _parse_cli_billing(body: bytes) -> dict[str, Any]:
    value = json.loads(body.decode("utf-8"))
    root = value.get("config", value) if isinstance(value, dict) else {}
    used = float(root.get("creditUsagePercent") or 0.0)
    products = []
    for raw in root.get("productUsage") or []:
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("product") or "Other")
        products.append({"id": 0, "label": label, "used_percent": float(raw.get("usagePercent") or 0.0)})
    reset_at = root.get("billingPeriodEnd") or (root.get("currentPeriod") or {}).get("end")
    period_start = root.get("billingPeriodStart") or (root.get("currentPeriod") or {}).get("start")

    def epoch(value: Any) -> int | None:
        if not value:
            return None
        try:
            from datetime import datetime

            return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError):
            return None

    used = max(0.0, min(100.0, used))
    return {
        "available": True,
        "plan": "SuperGrok",
        "limit": {
            "id": "supergrok-weekly",
            "kind": "account",
            "group": "SuperGrok",
            "label": "Weekly",
            "used_percent": used,
            "remaining_percent": max(0.0, 100.0 - used),
            "period_start_at": epoch(period_start),
            "reset_at": epoch(reset_at),
        },
        "products": products,
        "source": "grok-cli-billing",
        "collected_at": time.time(),
    }


def fetch_snapshot() -> dict[str, Any]:
    home = hermes_home()
    sys.path.insert(0, str(home / "hermes-agent"))
    try:
        from hermes_cli.auth import resolve_xai_oauth_runtime_credentials

        credentials = resolve_xai_oauth_runtime_credentials()
        access_token = str(credentials.get("api_key") or "").strip()
    except Exception:
        return {"available": False, "error": "Connect xAI Grok OAuth in Hermes to show SuperGrok usage."}
    if not access_token:
        return {"available": False, "error": "Connect xAI Grok OAuth in Hermes to show SuperGrok usage."}

    first_error = ""
    try:
        frames = _grpc_data_frames(_request(BILLING_URL, access_token, grpc=True))
        if not frames:
            raise ValueError("empty SuperGrok quota response")
        return parse_credits_response(frames[0])
    except urllib.error.HTTPError as error:
        first_error = f"HTTP {error.code}"
    except Exception as error:
        first_error = str(error)

    try:
        return _parse_cli_billing(_request(CLI_BILLING_URL, access_token, grpc=False))
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            return {"available": False, "error": "xAI OAuth cannot read SuperGrok usage. Reconnect xAI in Hermes."}
        return {"available": False, "error": f"SuperGrok usage is temporarily unavailable (HTTP {error.code})."}
    except Exception:
        return {"available": False, "error": f"SuperGrok usage is temporarily unavailable ({first_error or 'request failed'})."}


def main() -> int:
    print(json.dumps(fetch_snapshot(), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
