from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..domain import ExtractedItem


def extraction_cache_key(text: str, *, model_name: str, model_digest: str, extractor_version: str = "extractor-v1",
                        prompt_hash: str = "", parameters: dict[str, Any] | None = None, context_hash: str = "") -> str:
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    payload = {"text_hash": text_hash, "extractor_version": extractor_version, "prompt_hash": prompt_hash,
               "schema": "agenda-items-v1", "model_name": model_name, "model_digest": model_digest,
               "parameters": parameters or {}, "chunking": "chunk-v1", "context_hash": context_hash}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ExtractionCache:
    def __init__(self, root: str | Path): self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path: return self.root / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self.path(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.validate(data, key)
            return data
        except (OSError, ValueError, TypeError, KeyError):
            try: path.unlink()
            except OSError: pass
            return None

    def put(self, key: str, payload: dict[str, Any]) -> None:
        self.validate(payload, key)
        temp = self.path(key).with_suffix(f".tmp-{__import__('os').getpid()}")
        temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temp.replace(self.path(key))

    @staticmethod
    def validate(payload: dict[str, Any], key: str) -> None:
        if not isinstance(payload, dict) or payload.get("cache_key") != key:
            raise ValueError("invalid extraction cache")
        completion = payload.get("completion")
        if not isinstance(completion, dict) or completion.get("finish_reason") not in {"stop", "complete", "cached"}:
            raise ValueError("invalid cache completion")
        model = payload.get("model")
        if not isinstance(model, dict) or not isinstance(model.get("name"), str) or not isinstance(model.get("digest"), str):
            raise ValueError("invalid cache model")
        if not isinstance(payload.get("items"), list): raise ValueError("cache items missing")
        for item in payload["items"]:
            if not isinstance(item, dict) or not item.get("title") or not item.get("original_text") or item.get("priority") not in {"low", "medium", "high"}:
                raise ValueError("invalid cached item")
            if not isinstance(item.get("title"), str) or not isinstance(item.get("original_text"), str): raise ValueError("invalid cached text")
            anchor = item.get("anchor")
            if not isinstance(anchor, dict) or anchor.get("status") not in {"exact", "ambiguous", "unverified"}: raise ValueError("invalid cached anchor")
            if anchor.get("status") in {"exact", "ambiguous"} and (type(anchor.get("start")) is not int or type(anchor.get("end")) is not int or anchor["start"] < 0 or anchor["end"] < anchor["start"]):
                raise ValueError("invalid cached anchor offsets")
            if "reason" in item and item["reason"] is not None and not isinstance(item["reason"], str): raise ValueError("invalid cached reason")
