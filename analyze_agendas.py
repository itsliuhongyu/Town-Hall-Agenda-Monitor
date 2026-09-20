"""Compatibility helpers for the pre-stage-2 analyzer.

New runs use :mod:`agenda_app.analysis.service`; these functions remain only
for callers that imported the old pure cleaning helpers.  They do not read or
write the production SQLite publication and do not own policy state.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agenda_app.analysis.extraction import _parse, chunks
from agenda_app.analysis.readers import read_file

SYSTEM_PROMPT = object()
SYSTEM_PROMPT_FALLBACK = object()
CACHE_DIR = os.path.join(os.getcwd(), ".analysis_cache")
_keyword_scores_cache = None
_removal_keywords_cache = None


@dataclass
class AnalysisResult:
    status: str
    output_dir: str = ""
    files: list[str] | None = None


def should_drop_agenda_text(line: str) -> bool:
    value = " ".join((line or "").split()).casefold()
    stripped = re.sub(r"^\d{1,3}[.)]\s*", "", value)
    return bool(re.search(r"\b\d{3,5}[^\n]*\b\d{5}\b.*\b\d{1,2}:\d{2}\b", value)) or value.endswith(" agenda") or stripped in {"chairperson zoning administrator", "call to order", "affirm open meeting notice has been given"}


def filter_text_before_llm(content: str, removal_keywords: list[str] | None = None) -> str:
    removal_keywords = [str(x).casefold() for x in (removal_keywords or [])]
    lines = []
    for line in content.splitlines():
        normalized = " ".join(line.split()).casefold()
        if should_drop_agenda_text(line) or any(keyword and keyword in normalized for keyword in removal_keywords): continue
        if normalized: lines.append(line)
    return "\n".join(lines)


def extract_agenda_items_by_rules(content: str) -> list[dict[str, Any]]:
    result = []
    current = None
    pattern = re.compile(r"^\s*(\d{1,3})[.)]\s*(.+?)\s*$")
    for raw in content.splitlines():
        line = " ".join(raw.split()).strip()
        if not line or should_drop_agenda_text(line): continue
        match = pattern.match(line)
        if match:
            if current: result.append(current)
            current = {"item": match.group(2), "importance": "low", "original_text": line}
        elif current:
            current["original_text"] += " " + line; current["item"] += " " + line
    if current: result.append(current)
    return result


def build_llm_input(content: str) -> str: return content


def parse_llm_response(raw: str) -> list[dict[str, Any]]:
    data = json.loads(raw)
    converted = {"items": [{"title": row.get("title", row.get("item", "")), "importance": row.get("importance"), "original_text": row.get("original_text", ""), "reason": row.get("reason", "")} for row in data.get("items", [])]}
    return [{"item": item.title, "importance": item.priority, "original_text": item.original_text, "reason": item.reason} for item in _parse(json.dumps(converted))]


def classify_importance(text: str, keyword_scores: list[dict[str, Any]] | None = None) -> str:
    matches = []
    for row in keyword_scores or []:
        keyword = str(row.get("keyword", "")).strip()
        if keyword and re.search(r"(?<!\w)" + re.escape(keyword.casefold()) + r"(?!\w)", text.casefold()): matches.append(int(float(row.get("priority_score", 0))))
    score = max(matches, default=0)
    return "high" if score >= 80 else "medium" if score >= 40 else "low"


def dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []; seen = set()
    for item in items:
        key = " ".join(str(item.get("original_text") or item.get("item") or "").split()).casefold()
        if not key or key in seen: continue
        seen.add(key); result.append(dict(item))
    return result


def call_llm(filename: str, content: str, system_prompt=None):
    import urllib.request
    from agenda_app.config import validate_loopback_url
    base = validate_loopback_url(os.environ.get("AGENDA_OLLAMA_URL", "http://127.0.0.1:11434"))
    model = os.environ.get("AGENDA_MODEL", "")
    if not model: raise RuntimeError("AGENDA_MODEL is not configured")
    body = json.dumps({"model": model, "messages": [{"role": "system", "content": "Return JSON agenda items."}, {"role": "user", "content": content}], "format": "json", "stream": False}).encode()
    request = urllib.request.Request(base + "/api/chat", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response: payload = json.loads(response.read().decode())
    return payload.get("message", {}).get("content", "")


def analyze_agenda(filename: str, content: str) -> list[dict[str, Any]]:
    try:
        raw = call_llm(filename, content, SYSTEM_PROMPT); return dedupe_items(parse_llm_response(raw))
    except Exception:
        all_items = []
        for piece in chunks(content, size=2500, overlap=300):
            raw = call_llm(filename, piece, SYSTEM_PROMPT_FALLBACK); all_items.extend(parse_llm_response(raw))
        if not all_items: raise
        return dedupe_items(all_items)


def read_file_content(filepath: str) -> str:
    result = read_file(filepath)
    if result.status != "readable": raise RuntimeError(result.error.message if result.error else result.status)
    return result.text


def ensure_ollama_model_ready(model=None):
    from agenda_app.analysis.ollama import OllamaClient
    model = model or os.environ.get("AGENDA_MODEL")
    if not model: return False
    return any(row["name"] == model for row in OllamaClient(os.environ.get("AGENDA_OLLAMA_URL", "http://127.0.0.1:11434")).tags())


def _config_fingerprint() -> str:
    values = []
    for filename in ("keyword_scores.csv", "removal_keywords.csv"):
        path = Path(filename); values.append(path.read_bytes() if path.exists() else b"")
    return hashlib.sha256(b"\0".join(values)).hexdigest()


def _analyze_single_file(filename: str, temps_dir: str, metadata: dict[str, Any] | None = None):
    metadata = metadata or {}; path = Path(filename); path = path if path.is_absolute() else Path(temps_dir) / path.name
    content = read_file_content(str(path)); key = hashlib.sha256((content + _config_fingerprint()).encode()).hexdigest()
    cache_path = Path(CACHE_DIR); cache_path.mkdir(parents=True, exist_ok=True); cache_file = cache_path / f"{key}.json"
    try: cached = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError): cached = None
    hit = cached is not None; items = cached if hit else analyze_agenda(path.name, content)
    if not hit: cache_file.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    rows = []
    for item in items:
        row = {"filename": path.name, "item": item.get("item", ""), "importance": item.get("importance", "low"), "original_text": item.get("original_text", "")}; row.update(metadata); rows.append(row)
    return path.name, rows, hit


def main(output_dir: str = ".", temps_dir: str = "./temps") -> AnalysisResult:
    out = Path(output_dir); out.mkdir(parents=True, exist_ok=True); temp = Path(temps_dir)
    files = sorted(p for p in temp.iterdir() if p.is_file() and p.name != "agenda_manifest.json") if temp.exists() else []
    all_rows = []
    for path in files:
        try: all_rows.extend(_analyze_single_file(str(path), str(temp))[1])
        except Exception: return AnalysisResult("failed", str(out))
    fields = ["filename", "item", "importance", "original_text", "gov_body", "meeting_date", "download_url", "source_url"]
    for priority in ("high", "medium", "low"):
        with (out / f"{priority}.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(row for row in all_rows if row.get("importance") == priority)
    return AnalysisResult("success" if all_rows else "empty_success", str(out), [f"{p}.csv" for p in ("high", "medium", "low")])


if __name__ == "__main__":
    result = main(); raise SystemExit(0 if result.status in {"success", "empty_success"} else 1)
