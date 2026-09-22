from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Any

from ..config import ModelSelection, validate_loopback_url


class OllamaError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True, code: str = "ollama_unavailable"):
        self.retryable = retryable; self.code = code; super().__init__(message)


class OllamaClient:
    def __init__(self, base_url: str, timeout: float = 3.0): self.base_url = validate_loopback_url(base_url).rstrip("/"); self.timeout = timeout

    def _json(self, path: str, payload: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = Request(self.base_url + path, data=data, headers={"Content-Type": "application/json"} if data else {})
        try:
            with urlopen(req, timeout=timeout or self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise OllamaError(str(exc)) from exc

    def tags(self) -> list[dict[str, Any]]:
        data = self._json("/api/tags")
        result = []
        for model in data.get("models", []):
            digest = model.get("digest") or model.get("details", {}).get("digest")
            result.append({"name": model.get("name") or model.get("model"), "digest": digest, "size": model.get("size"), "details": model.get("details", {})})
        return [m for m in result if m["name"] and m["digest"]]

    def check_selection(self, selection: ModelSelection | None) -> dict[str, Any]:
        models = self.tags()
        if not selection: return {"connection": "online", "selection": "none", "models": models}
        match = next((m for m in models if m["name"] == selection.name), None)
        if not match: state = "removed"
        elif match["digest"] != selection.digest: state = "digest_changed"
        else: state = "available"
        return {"connection": "online", "selection": state, "models": models}

    def chat(self, selection: ModelSelection, prompt: str, system: str) -> dict[str, Any]:
        before = {m["name"]: m["digest"] for m in self.tags()}.get(selection.name)
        if before != selection.digest: raise OllamaError("selected model digest is not installed", retryable=False, code="model_changed")
        response = self._json("/api/chat", {"model": selection.name, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}], "format": "json", "stream": False, "options": {"temperature": 0, "top_p": 1, "num_predict": 4096}}, timeout=self.timeout)
        message = response.get("message", {})
        done = response.get("done") is True
        finish = response.get("done_reason") or response.get("finish_reason") or ("stop" if done else "length")
        after = {m["name"]: m["digest"] for m in self.tags()}.get(selection.name)
        if after != selection.digest: raise OllamaError("model changed during completion", retryable=False, code="model_changed")
        return {"content": message.get("content", ""), "completion": {"finish_reason": finish, "done": done}}
