from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..config import ConfigSnapshot, DataPaths, ModelSelection
from ..domain import ErrorInfo, SourceSnapshot
from ..storage.repository import Repository, dumps
from .cache import ExtractionCache, extraction_cache_key
from .extraction import extract
from .ollama import OllamaClient, OllamaError
from .policy import evaluate
from .readers import read_bytes


class AnalysisService:
    def __init__(self, repository: Repository):
        self.repo = repository
        self.cache = ExtractionCache(repository.paths.cache / "extract")

    def prepare_bytes(self, *, data: bytes, media_type: str | None, filename: str, snapshot: ConfigSnapshot) -> dict[str, Any]:
        read = read_bytes(data, media_type, filename)
        digest = hashlib.sha256(data).hexdigest()
        if read.status != "readable":
            return {"digest": digest, "read": read, "state": read.status, "items": [], "model_items": [], "cache_hit": False, "completion": {}}
        selection = snapshot.model
        if selection is None:
            return {"digest": digest, "read": read, "state": "failed", "error": ErrorInfo("model_unselected", "Select an installed Ollama model before analyzing a cache miss.", False, "analyze"), "items": [], "model_items": [], "cache_hit": False, "completion": {}}
        key = extraction_cache_key(read.text, model_name=selection.name, model_digest=selection.digest, parameters={"temperature": 0, "max_tokens": 4096}, prompt_hash=__import__('agenda_app.analysis.extraction', fromlist=['PROMPT_HASH']).PROMPT_HASH)
        cached = self.cache.get(key)
        inventory_state = self.repo.models_state()
        if inventory_state.get("connection") == "online" and inventory_state.get("selection") in {"removed", "digest_changed"}:
            return {"digest": digest, "read": read, "state": "failed", "error": ErrorInfo("model_changed", "The saved model name or digest is no longer the installed model.", False, "analyze"), "items": [], "model_items": [], "cache_hit": False, "completion": {}}
        if cached:
            extracted = cached["items"]
            result_state, cache_hit = "success" if extracted else "empty", True
            completion = cached["completion"]
            model_items = [(i["title"], i["original_text"], i["priority"], i.get("reason"), i.get("anchor", {"status": "exact"})) for i in extracted]
        else:
            client = OllamaClient(selection.base_url, timeout=float(snapshot.values.get("inference_timeout_seconds", 120)))
            try:
                result = extract(read, selection, lambda chunk, prompt: client.chat(selection, chunk, prompt))
            except OllamaError as exc:
                return {"digest": digest, "read": read, "state": "failed", "error": ErrorInfo(exc.code, str(exc), exc.retryable, "analyze"), "items": [], "model_items": [], "cache_hit": False, "completion": {}}
            if result.state == "failed":
                return {"digest": digest, "read": read, "state": "failed", "error": result.error, "items": [], "model_items": [], "cache_hit": False, "completion": result.completion}
            model_items = [(i.title, i.original_text, i.priority, i.reason, i.anchor) for i in result.items]
            result_state, cache_hit, completion = result.state, False, result.completion
            self.cache.put(key, {"cache_key": key, "items": [{"title": x[0], "original_text": x[1], "priority": x[2], "reason": x[3], "anchor": x[4]} for x in model_items], "completion": {"finish_reason": "stop", **completion}, "model": {"name": selection.name, "digest": selection.digest}})
        return {"digest": digest, "read": read, "state": result_state, "error": None, "model_items": model_items, "cache_hit": cache_hit, "completion": completion, "extraction_key": key,
                "model_name": selection.name, "model_digest": selection.digest,
                "prompt_hash": __import__('agenda_app.analysis.extraction', fromlist=['PROMPT_HASH']).PROMPT_HASH}

    def persist_prepared(self, conn, *, run_id: str, source: SourceSnapshot, document_id: str, data: bytes,
                         media_type: str | None, filename: str, snapshot: ConfigSnapshot, prepared: dict[str, Any],
                         fetched_url: str | None = None, blob_relpath: str | None = None) -> dict[str, Any]:
        read = prepared["read"]
        version_id, _ = self.repo.ensure_document_version(conn, document_id, sha256=prepared["digest"], blob_relpath=blob_relpath, byte_size=len(data), media_type=media_type,
                                                           fetched_url=fetched_url, retained_text=read.text, read_status=read.status, read_error=read.error.as_dict() if read.error else None, reader_version=read.reader_version)
        if prepared["state"] not in {"success", "empty"}:
            return {"document_version_id": version_id, "read": read, "state": prepared["state"], "error": prepared.get("error"), "items": []}
        analysis_id = self.repo.create_analysis(conn, version_id=version_id, extraction_key=prepared["extraction_key"], model_name=prepared.get("model_name"), model_digest=prepared.get("model_digest"), reader_version=read.reader_version, extractor_version="extractor-v1", prompt_hash=prepared["prompt_hash"], parameters={"temperature": 0, "max_tokens": 4096}, snapshot=__import__('json').loads(snapshot.json()), state=prepared["state"], cache_hit=prepared["cache_hit"])
        items = []
        policy = snapshot.policy
        for ordinal, (title, original, model_priority, reason, anchor) in enumerate(prepared.get("model_items", [])):
            item_version_id = self.repo.create_item_version(conn, document_id, version_id, original_text=original, title=title, priority=model_priority, reason=reason, anchor=anchor, analysis_id=analysis_id, ordinal=ordinal, model_priority=model_priority)
            decision = evaluate(original, model_priority, policy)
            self.repo.add_run_item(conn, run_id=run_id, item_version_id=item_version_id, analysis_id=analysis_id, policy_version_id=snapshot.policy_version_id,
                                   policy_priority=decision.policy_priority, proposed_priority=decision.proposed_priority, decision_source=decision.decision_source,
                                   matched_rules=list(decision.matched_rules), included=decision.included, exclusion_reason=decision.exclusion_reason)
            items.append({"item_version_id": item_version_id, "decision": decision})
        return {"document_version_id": version_id, "analysis_id": analysis_id, "read": read, "state": prepared["state"], "cache_hit": prepared["cache_hit"], "items": items}

    def analyze_bytes(self, conn, *, run_id: str, source: SourceSnapshot, document_id: str, data: bytes,
                      media_type: str | None, filename: str, snapshot: ConfigSnapshot, fetched_url: str | None = None,
                      blob_relpath: str | None = None) -> dict[str, Any]:
        prepared = self.prepare_bytes(data=data, media_type=media_type, filename=filename, snapshot=snapshot)
        return self.persist_prepared(conn, run_id=run_id, source=source, document_id=document_id, data=data, media_type=media_type,
                                     filename=filename, snapshot=snapshot, prepared=prepared, fetched_url=fetched_url, blob_relpath=blob_relpath)
