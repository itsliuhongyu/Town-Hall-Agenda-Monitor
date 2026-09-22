from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from .config import identity_key, normalize_text, utc_now


PRIORITIES = {"low", "medium", "high"}


def new_id() -> str:
    return str(uuid4())


@dataclass(frozen=True)
class SourceSnapshot:
    source_id: str
    platform: str
    name: str
    collection_url: str
    timezone: str
    config: dict[str, Any] = field(default_factory=dict)

    def json_dict(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "platform": self.platform, "name": self.name,
                "collection_url": self.collection_url, "timezone": self.timezone, "config": self.config}


@dataclass(frozen=True)
class DateWindow:
    start: date
    end: date


@dataclass(frozen=True)
class AgendaCandidate:
    meeting_heading: str
    local_date: date | None
    original_url: str | None
    source_url: str
    document_kind: str = "agenda"
    native_meeting_key: str | None = None
    native_document_key: str | None = None
    native_item_keys: tuple[str, ...] = ()
    raw_date: str = ""
    suggested_filename: str = "agenda.pdf"
    local_datetime: str | None = None
    # Some public portals render a complete agenda only after an interaction
    # and expose no stable download URL.  Adapters may retain the bounded
    # public response here; the worker writes it through the same verified
    # download/blob/read path as URL-backed documents.
    captured_bytes: bytes | None = None
    captured_media_type: str | None = None

    @property
    def locator_key(self) -> str:
        return identity_key(self.native_document_key or "", self.original_url or "", self.document_kind, self.meeting_heading)


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    message: str
    retryable: bool = False
    stage: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable,
                "stage": self.stage, "details": self.details}


@dataclass(frozen=True)
class DiscoveryResult:
    recognized: bool
    explicit_empty: bool
    candidates: tuple[AgendaCandidate, ...] = ()
    warnings: tuple[ErrorInfo, ...] = ()
    page_recognized: bool = False


@dataclass(frozen=True)
class ReadResult:
    status: str
    text: str
    pages: tuple[dict[str, Any], ...] = ()
    quality: str = "unknown"
    error: ErrorInfo | None = None
    reader_version: str = "reader-v1"


@dataclass(frozen=True)
class ExtractedItem:
    title: str
    original_text: str
    priority: str | None
    reason: str | None = None
    anchor: dict[str, Any] = field(default_factory=dict)
    source: str = "llm"


@dataclass(frozen=True)
class ExtractionResult:
    items: tuple[ExtractedItem, ...]
    state: str
    model_name: str | None
    model_digest: str | None
    extraction_key: str
    prompt_hash: str
    completion: dict[str, Any]
    error: ErrorInfo | None = None


@dataclass(frozen=True)
class PolicyDecision:
    included: bool
    exclusion_reason: str | None
    policy_priority: str
    proposed_priority: str
    decision_source: str
    matched_rules: tuple[dict[str, Any], ...] = ()


def fallback_meeting_identity(source_id: str, local_date: date | None, heading: str, local_datetime: str | None, locator: str) -> str:
    return identity_key(source_id, local_date.isoformat() if local_date else "", heading, local_datetime or "", locator)


def exact_item_identity(original_text: str, occurrence: int) -> str:
    return identity_key(normalize_text(original_text), str(occurrence))
