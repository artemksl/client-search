"""Small, dependency-free model helpers used across outreach modules."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4()}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


def json_loads(value: str | None, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    return json.loads(value)


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_phone(value: str) -> str:
    raw = value.strip().lower().replace("whatsapp", "")
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        return f"+{digits[2:]}"
    if digits.startswith("44"):
        return f"+{digits}"
    if digits.startswith("0") and len(digits) >= 10:
        return f"+44{digits[1:]}"
    return f"+{digits}" if digits else ""


def normalize_destination(channel: str, value: str) -> str:
    channel = channel.strip().lower()
    if channel == "email":
        return normalize_email(value)
    if channel in {"phone", "sms", "whatsapp"}:
        return normalize_phone(value)
    return value.strip().lower()


def destination_family(channel: str) -> str:
    normalized = channel.strip().lower()
    return "phone" if normalized in {"phone", "sms", "whatsapp"} else normalized


def destination_key(channel: str, normalized_value: str) -> str:
    return f"{destination_family(channel)}:{normalized_value}"


def row_dict(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
