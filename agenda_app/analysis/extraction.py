from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable

from ..config import ModelSelection, normalize_text
from ..domain import ErrorInfo, ExtractedItem, ExtractionResult, ReadResult
from .cache import extraction_cache_key

EXTRACTOR_VERSION = "extractor-v1"
PROMPT = """Extract actionable agenda items from the supplied public-meeting agenda text. Ignore navigation, headers, attendance, call to order, routine approval of prior minutes, and boilerplate notices unless the text contains a substantive decision. Return only JSON object {items:[{title,importance,original_text,reason}]}. Each original_text must be an exact contiguous quote from the supplied text. Importance is a model suggestion: high means likely material contract, land use, policy, budget, public safety, legal, or other consequential decision; medium means a meaningful operational or administrative decision; low means routine or informational business. Use the full agenda context, do not infer a high priority from one keyword alone, and keep reason concise."""
PROMPT_HASH = hashlib.sha256(PROMPT.encode()).hexdigest()


def _parse(raw: str) -> list[ExtractedItem]:
    try: data = json.loads(raw)
    except json.JSONDecodeError as exc: raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), list): raise ValueError("response must contain items list")
    items = []
    for row in data["items"]:
        if not isinstance(row, dict) or not isinstance(row.get("title"), str) or not isinstance(row.get("original_text"), str): raise ValueError("invalid item shape")
        priority = str(row.get("importance", "")).lower()
        if priority not in {"low", "medium", "high"}: raise ValueError("invalid importance")
        quote = row["original_text"]
        if not quote.strip(): raise ValueError("original_text must not be blank")
        items.append(ExtractedItem(normalize_text(row["title"]), quote, priority, str(row.get("reason", ""))[:500] or None, {"status": "unverified"}, "llm"))
    return items


def chunks(text: str, size: int = 9000, overlap: int = 500) -> list[str]:
    if len(text) <= size: return [text]
    result = []; start = 0
    while start < len(text):
        end = min(len(text), start + size); result.append(text[start:end])
        if end == len(text): break
        start = max(start + 1, end - overlap)
    return result


def _anchor(text: str, quote: str, occurrence: int = 0, used_spans: set[tuple[int, int]] | None = None) -> dict[str, Any]:
    """Return coordinates into the retained, unnormalised text."""
    used_spans = used_spans or set()
    if not quote:
        return {"status": "unverified", "occurrence": occurrence}
    positions = [(match.start(), match.end()) for match in re.finditer(re.escape(quote), text)]
    available = [span for span in positions if span not in used_spans]
    if available:
        start, end = available[0 if len(available) == 1 else min(occurrence, len(available) - 1)]
        if len(positions) == 1:
            return {"status": "exact", "start": start, "end": end}
        return {"status": "ambiguous", "start": start, "end": end, "occurrence": occurrence, "matches": len(positions)}
    # A model can repeat an item because adjacent chunks overlap. There is no
    # remaining source occurrence to represent, so the caller drops it.
    if positions:
        return {"status": "duplicate", "occurrence": occurrence, "matches": len(positions)}
    # Do not call a whitespace-normalized or case-insensitive match exact: its
    # offsets cannot safely slice the retained source.
    return {"status": "unverified", "occurrence": occurrence}


def extract(read: ReadResult, selection: ModelSelection, call_model: Callable[[str, str], dict[str, Any]], *, parameters: dict[str, Any] | None = None) -> ExtractionResult:
    if read.status != "readable":
        return ExtractionResult((), "failed" if read.status not in {"empty"} else "empty", selection.name, selection.digest, "", PROMPT_HASH, {}, read.error)
    parameters = parameters or {"temperature": 0, "max_tokens": 4096}
    key = extraction_cache_key(read.text, model_name=selection.name, model_digest=selection.digest, parameters=parameters, prompt_hash=PROMPT_HASH)
    all_items: list[ExtractedItem] = []; completions = []
    for chunk in chunks(read.text):
        response = call_model(chunk, PROMPT)
        completion = response.get("completion", {}) if isinstance(response, dict) else {}
        completions.append(completion)
        finish = completion.get("finish_reason", "length")
        raw = response.get("content", "") if isinstance(response, dict) else ""
        try:
            parsed = _parse(raw)
            if finish not in {"stop", "complete"}:
                raise ValueError("non-terminal model completion")
            all_items.extend(parsed)
        except ValueError as exc:
            # A length-truncated or malformed primary response is not an
            # empty result. Re-run the complete chunk as overlapping smaller
            # pieces so unique items near the tail remain observable.
            fallback_items: list[ExtractedItem] = []
            fallback_failed = None
            for piece in chunks(chunk, size=2500, overlap=300):
                try:
                    fallback = call_model(piece, PROMPT)
                    fallback_completion = fallback.get("completion", {}) if isinstance(fallback, dict) else {}
                    fallback_finish = fallback_completion.get("finish_reason", "length")
                    if fallback_finish not in {"stop", "complete"}:
                        raise ValueError("fallback completion was truncated")
                    fallback_items.extend(_parse(fallback.get("content", "")))
                except ValueError as fallback_error:
                    fallback_failed = fallback_error; break
            if fallback_failed is not None:
                return ExtractionResult(tuple(all_items), "failed", selection.name, selection.digest, key, PROMPT_HASH, {"finish_reason": finish, "chunks": completions}, ErrorInfo("extraction_invalid", str(fallback_failed), False, "analyze", {"finish_reason": finish, "fallback": True}))
            all_items.extend(fallback_items)
    final = []
    seen = set()
    occurrences: dict[str, int] = {}; used_spans: set[tuple[int, int]] = set()
    for item in all_items:
        norm = normalize_text(item.original_text).casefold()
        occurrence = occurrences.get(norm, 0); occurrences[norm] = occurrence + 1
        anchor = _anchor(read.text, item.original_text, occurrence, used_spans)
        if anchor.get("status") == "duplicate":
            continue
        if anchor.get("status") in {"exact", "ambiguous"}:
            used_spans.add((anchor["start"], anchor["end"]))
        span_key = (norm, anchor.get("start"), anchor.get("end")) if "start" in anchor else (norm, "unverified", occurrence)
        if span_key in seen: continue
        seen.add(span_key); final.append(ExtractedItem(item.title, item.original_text, item.priority, item.reason, anchor, item.source))
    return ExtractionResult(tuple(final), "success" if final else "empty", selection.name, selection.digest, key, PROMPT_HASH, {"finish_reason": "stop", "chunks": completions})
