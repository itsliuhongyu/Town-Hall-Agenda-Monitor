from __future__ import annotations

import json
from typing import Any


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def success(value: Any, status: int = 200) -> tuple[int, dict[str, str], bytes]:
    return status, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}, json_bytes(value)


def error(code: str, message: str, status: int, *, details: dict[str, Any] | None = None, retryable: bool = False) -> tuple[int, dict[str, str], bytes]:
    return status, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}, json_bytes({"error": {"code": code, "message": message, "fields": {}, "retryable": retryable, "details": details or {}}})
