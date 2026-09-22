from __future__ import annotations

import json
import os
import re
import sys
from types import MappingProxyType
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT_DIR = Path(__file__).resolve().parent.parent
LEGACY_ROOT = ROOT_DIR


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def ensure_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown IANA timezone: {value}") from None
    return value


def local_date_window(zone: str, days_back: int = 0, days_forward: int = 14, now: datetime | None = None) -> tuple[date, date]:
    ensure_timezone(zone)
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(zone)).date()
    return local.fromordinal(local.toordinal() - days_back), local.fromordinal(local.toordinal() + days_forward)


def canonical_url(value: str, *, keep_query: bool = True) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("URL must be an absolute http(s) URL without credentials")
    port = parsed.port
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)
    netloc = parsed.hostname.lower()
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    if port and not default_port:
        netloc += f":{port}"
    path = parsed.path or "/"
    query = parsed.query if keep_query else ""
    return urlunparse((parsed.scheme.lower(), netloc, path, "", query, ""))


def normalize_text(value: str) -> str:
    import unicodedata

    return " ".join(unicodedata.normalize("NFC", value or "").split())


def identity_key(*parts: str) -> str:
    return "|".join(normalize_text(str(part)).casefold() for part in parts)


def validate_loopback_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Ollama URL must be an HTTP loopback URL")
    if parsed.port is None or not 1 <= parsed.port <= 65535:
        raise ValueError("Ollama URL must include a valid port")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Ollama URL cannot contain credentials, path, query, or fragment")
    host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
    return f"http://{host}:{parsed.port}"


@dataclass(frozen=True)
class DataPaths:
    root: Path

    @classmethod
    def from_env(cls, data_dir: str | os.PathLike[str] | None = None) -> "DataPaths":
        raw = data_dir or os.environ.get("AGENDA_DATA_DIR") or str(ROOT_DIR / "data")
        root = Path(raw).expanduser().resolve()
        return cls(root)

    @property
    def database(self) -> Path: return self.root / "agenda.sqlite3"
    @property
    def blobs(self) -> Path: return self.root / "blobs" / "sha256"
    @property
    def exports(self) -> Path: return self.root / "exports"
    @property
    def work(self) -> Path: return self.root / "work"
    @property
    def cache(self) -> Path: return self.root / "cache"
    @property
    def locks(self) -> Path: return self.root / "locks"
    @property
    def backups(self) -> Path: return self.root / "backups"

    def ensure(self) -> "DataPaths":
        for path in (self.root, self.blobs, self.exports, self.work, self.cache, self.locks, self.backups):
            path.mkdir(parents=True, exist_ok=True)
        return self


@dataclass(frozen=True)
class ModelSelection:
    name: str
    digest: str
    base_url: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "digest": self.digest, "base_url": self.base_url}


@dataclass(frozen=True)
class ConfigSnapshot:
    values: dict[str, Any]
    policy: dict[str, Any]
    revision: int
    policy_version_id: str
    captured_at: str = field(default_factory=utc_now)

    def __post_init__(self):
        object.__setattr__(self, "values", _freeze(self.values))
        object.__setattr__(self, "policy", _freeze(self.policy))

    @property
    def model(self) -> ModelSelection | None:
        selected = self.values.get("selected_model")
        if not selected:
            return None
        return ModelSelection(str(selected["name"]), str(selected["digest"]), str(self.values["ollama_base_url"]))

    def json(self) -> str:
        return json.dumps({"values": _thaw(self.values), "policy": _thaw(self.policy), "revision": self.revision,
                           "policy_version_id": self.policy_version_id, "captured_at": self.captured_at}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _freeze(value):
    if isinstance(value, dict): return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list): return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, MappingProxyType): return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple): return [_thaw(item) for item in value]
    return value


def default_settings() -> dict[str, Any]:
    try:
        ui_timezone = datetime.now().astimezone().tzinfo
        ui_timezone = getattr(ui_timezone, "key", None) or "UTC"
        ensure_timezone(ui_timezone)
    except Exception:
        ui_timezone = "UTC"
    return {
        "ollama_base_url": "http://127.0.0.1:11434",
        "selected_model": None,
        "days_back": 0,
        "days_forward": 14,
        "download_workers": 1,
        "analysis_workers": 2,
        "run_timeout_minutes": 60,
        "inference_timeout_seconds": 120,
        "ui_timezone": ui_timezone,
        # BoardDocs may require a normal visible browser window on hosts where
        # its headless response is an empty shell or a CloudFront denial.
        # Keep this opt-in so ordinary runs remain unattended/headless.
        "allow_visible_browser": False,
    }


def validate_settings(values: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("settings values must be an object")
    allowed = set(default_settings())
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    result = default_settings()
    result.update(values)
    if type(result["allow_visible_browser"]) is not bool:
        raise ValueError("allow_visible_browser must be a boolean")
    for name in ("days_back", "days_forward"):
        if type(result[name]) is not int or not 0 <= result[name] <= 365:
            raise ValueError(f"{name} must be an integer between 0 and 365")
    if result["days_back"] + result["days_forward"] > 366:
        raise ValueError("date window cannot exceed 366 days")
    for name in ("download_workers", "analysis_workers"):
        if type(result[name]) is not int or not 1 <= result[name] <= 4:
            raise ValueError(f"{name} must be an integer between 1 and 4")
    if type(result["run_timeout_minutes"]) is not int or not 1 <= result["run_timeout_minutes"] <= 180:
        raise ValueError("run_timeout_minutes must be an integer between 1 and 180")
    if type(result["inference_timeout_seconds"]) is not int or not 3 <= result["inference_timeout_seconds"] <= 600:
        raise ValueError("inference_timeout_seconds must be an integer between 3 and 600")
    result["ollama_base_url"] = validate_loopback_url(str(result["ollama_base_url"]))
    result["ui_timezone"] = ensure_timezone(str(result["ui_timezone"]))
    selected = result.get("selected_model")
    if selected is not None:
        if not isinstance(selected, dict) or set(selected) != {"name", "digest", "verified_at"}:
            raise ValueError("selected_model must contain name, digest, and verified_at")
        if not selected["name"] or not selected["digest"]:
            raise ValueError("selected_model name and digest are required")
    return result
