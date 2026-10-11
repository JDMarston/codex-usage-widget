from __future__ import annotations

import json
import math
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
EMPTY_GRPC_WEB_FRAME = b"\x00\x00\x00\x00\x02\x08\x00"
MAX_RESPONSE_BYTES = 1_048_576
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
        if shift == 63 and byte > 1:
            raise ValueError("overflowing protobuf varint")
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, index
        shift += 7
    raise ValueError("truncated protobuf varint")


def _parse_fields(data: bytes) -> dict[int, list[tuple[str, Any]]]:
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("oversized protobuf")
    fields: dict[int, list[tuple[str, Any]]] = {}
    index = 0
    while index < len(data):
        key, index = _read_varint(data, index)
        field_number, wire_type = key >> 3, key & 7
        if not 1 <= field_number <= 536_870_911:
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
    if len(values) != 1 or values[0][0] != "fixed32":
        raise ValueError("invalid percentage field")
    return _percentage(struct.unpack("<f", values[0][1])[0])


def _percentage(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("invalid percentage")
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError("invalid percentage")
    return value


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
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("oversized gRPC response")
    frames: list[bytes] = []
    index = 0
    trailer_seen = False
    while index < len(data):
        if index + 5 > len(data) or trailer_seen:
            raise ValueError("truncated or trailing gRPC frame")
        flags = data[index]
        length = int.from_bytes(data[index + 1:index + 5], "big")
        start, end = index + 5, index + 5 + length
        if end > len(data) or flags not in (0, 128):
            raise ValueError("invalid gRPC frame")
        body = data[start:end]
        if flags == 128:
            trailer_seen = True
            trailers = dict(line.split(":", 1) for line in body.decode("ascii").splitlines() if ":" in line)
            status = trailers.get("grpc-status", "").strip()
            if status != "0":
                raise ValueError("gRPC request failed")
        else:
            frames.append(body)
        index = end
    if len(frames) != 1:
        raise ValueError("expected one gRPC data frame")
    return frames


def parse_credits_response(message: bytes) -> dict[str, Any]:
    top = _parse_fields(message)
    config_message = _first_message(top.get(1))
    if config_message is None:
        raise ValueError("SuperGrok response did not include a usage config")
    config = _parse_fields(config_message)

    used = _first_float(config.get(1))
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

    return _snapshot(used, period_start, reset_at, products, "grok-grpc-web")


def _snapshot(used: float | None, start: Any, end: Any, products: list, source: str) -> dict[str, Any]:
    if used is None and not (start and end and end > start):
        raise ValueError("response contains neither usage nor a complete period")
    duration = end - start if start and end and end > start else None
    label = "Weekly" if duration and 6 * 86400 <= duration <= 8 * 86400 else "Monthly" if duration and 27 * 86400 <= duration <= 32 * 86400 else "Usage window"
    result: dict[str, Any] = {
        "available": used is not None, "status": "ok" if used is not None else "not_reported",
        "plan": "SuperGrok", "period_start_at": start, "reset_at": end,
        "period_label": label, "source": source, "fetched_at": time.time(),
    }
    if used is None:
        # Proto3 cannot distinguish an omitted zero from an unpublished meter.
        # Never claim 100% headroom when neither endpoint publishes a number.
        result["notice"] = "Grok returned the billing period, but no usage percentage."
    else:
        result["limit"] = {"id": "supergrok-weekly", "kind": "account", "group": "SuperGrok",
                           "label": label, "used_percent": used, "remaining_percent": 100 - used,
                           "period_start_at": start, "reset_at": end, "window_seconds": duration}
        if products and abs(sum(p["used_percent"] for p in products) - used) <= 1:
            result["products"] = products
            result["limit"]["details"] = "\n".join(f"{p['label']}: {p['used_percent']:g}%" for p in products)
    return result


def _request(url: str, access_token: str, *, grpc: bool) -> bytes:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "*/*" if grpc else "application/json",
        "User-Agent": "UsageWidget/2",
        "Cache-Control": "no-cache",
    }
    data: bytes | None = None
    if grpc:
        data = EMPTY_GRPC_WEB_FRAME
        headers.update(
            {
                "Content-Type": "application/grpc-web+proto",
                "X-Grpc-Web": "1",
                "Connect-Protocol-Version": "1",
                "X-User-Agent": "connect-es/2.1.1",
                "Origin": "https://grok.com",
                "Referer": "https://grok.com/?_s=usage",
            }
        )
    else:
        headers["X-Grok-Client-Mode"] = "cli"
        headers["X-XAI-Token-Auth"] = "xai-grok-cli"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if grpc else "GET")
    with urllib.request.urlopen(request, timeout=8) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("oversized billing response")
        return body


def _parse_cli_billing(body: bytes) -> dict[str, Any]:
    value = json.loads(body)
    if not isinstance(value, dict) or not isinstance(value.get("config"), dict):
        raise ValueError("missing usage config")
    root = value["config"]
    used = _percentage(root["creditUsagePercent"]) if "creditUsagePercent" in root else None
    period = root.get("currentPeriod") or {}
    if not isinstance(period, dict):
        raise ValueError("invalid current period")

    def epoch(value: Any) -> float | None:
        if not isinstance(value, str):
            return None
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp() if dt.tzinfo else None
        except (ValueError, OverflowError):
            return None

    start = epoch(period.get("start")) or epoch(root.get("billingPeriodStart"))
    end = epoch(period.get("end")) or epoch(root.get("billingPeriodEnd"))
    products = []
    for raw in root.get("productUsage") or []:
        if not isinstance(raw, dict) or "usagePercent" not in raw:
            continue
        identifier = raw.get("product", 0)
        products.append({"id": identifier, "label": PRODUCT_NAMES.get(identifier, str(identifier)),
                         "used_percent": _percentage(raw["usagePercent"])})
    return _snapshot(used, start, end, products, "grok-cli-billing")


def fetch_with_token(access_token: str) -> dict[str, Any]:
    period_only = None
    auth_failed = False
    for grpc in (False, True):
        try:
            body = _request(BILLING_URL if grpc else CLI_BILLING_URL, access_token, grpc=grpc)
            result = parse_credits_response(_grpc_data_frames(body)[0]) if grpc else _parse_cli_billing(body)
            if result["available"]:
                return result
            if period_only is None:
                period_only = result
        except urllib.error.HTTPError as error:
            auth_failed = auth_failed or error.code in {401, 403}
        except (ValueError, OSError, TimeoutError):
            pass
    if period_only is not None:
        return period_only
    return {"available": False, "status": "auth_required" if auth_failed else "error",
            "error": "Reconnect xAI OAuth in Hermes." if auth_failed else "Grok usage is temporarily unavailable."}


def fetch_snapshot() -> dict[str, Any]:
    sys.path.insert(0, str(hermes_home() / "hermes-agent"))
    try:
        from hermes_cli.auth import resolve_xai_oauth_runtime_credentials
        credentials = resolve_xai_oauth_runtime_credentials()
        access_token = str(credentials.get("api_key") or "").strip()
        if access_token:
            return fetch_with_token(access_token)
    except Exception:
        pass
    return {"available": False, "status": "auth_required", "error": "Connect xAI OAuth in Hermes to check SuperGrok usage."}


def main() -> int:
    print(json.dumps(fetch_snapshot(), separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
