from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests


@dataclass(frozen=True)
class DownloadArtifact:
    url: str
    status: str
    path: Path | None = None
    sha256: str | None = None
    size: int = 0
    media_type: str = ""
    error: dict[str, Any] | None = None
    attempts: int = 0


RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
MAX_CAPTURE_BYTES = 50 * 1024 * 1024


class _NonRetryable(RuntimeError):
    pass


def download(url: str, destination: str | Path, *, filename: str, session: Any = requests, max_retries: int = 3) -> DownloadArtifact:
    destination = Path(destination); destination.mkdir(parents=True, exist_ok=True)
    final_path = destination / Path(filename).name
    for attempt in range(1, max_retries + 1):
        part = destination / f".{final_path.name}.part-{os.getpid()}-{attempt}"
        try:
            response = session.get(url, stream=True, timeout=(5, 30))
            status = getattr(response, "status_code", 200)
            if status in RETRYABLE_STATUS and attempt < max_retries:
                retry_after = min(int(response.headers.get("Retry-After", "0") or 0), 30)
                time.sleep(retry_after or (1 if attempt == 1 else 2)); continue
            if status >= 400:
                if status not in RETRYABLE_STATUS: raise _NonRetryable(f"HTTP {status}")
                raise RuntimeError(f"HTTP {status}")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            length = response.headers.get("Content-Length")
            total = 0; digest = hashlib.sha256()
            with part.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 128):
                    if not chunk: continue
                    total += len(chunk)
                    if total > 50 * 1024 * 1024: raise RuntimeError("download exceeds 50 MiB")
                    handle.write(chunk); digest.update(chunk)
                handle.flush(); os.fsync(handle.fileno())
            if total == 0: raise _NonRetryable("empty response body")
            if length and length.isdigit() and int(length) != total: raise _NonRetryable("truncated response body")
            if "text/html" in content_type and not Path(filename).suffix.lower() in {".html", ".htm"}:
                raise _NonRetryable("HTML response for a document")
            part.replace(final_path)
            return DownloadArtifact(url, "downloaded", final_path, digest.hexdigest(), total, content_type, attempts=attempt)
        except Exception as exc:
            try: part.unlink()
            except OSError: pass
            if isinstance(exc, _NonRetryable):
                return DownloadArtifact(url, "failed", error={"code": "download_failed", "message": str(exc), "retryable": False}, attempts=attempt)
            if attempt == max_retries:
                return DownloadArtifact(url, "failed", error={"code": "download_failed", "message": str(exc), "retryable": True}, attempts=attempt)
            time.sleep(1 if attempt == 1 else 2)
    return DownloadArtifact(url, "failed", error={"code": "download_failed", "message": "retry exhausted"}, attempts=max_retries)


def retain_captured(data: bytes, destination: str | Path, *, filename: str,
                    url: str, media_type: str | None = None) -> DownloadArtifact:
    """Persist adapter-captured public content with the same checks as a URL.

    Dynamic public portals sometimes expose a complete agenda only in a
    rendered dialog.  The adapter still supplies bounded bytes; this helper
    makes those bytes an ordinary verified ``DownloadArtifact`` so blob
    retention and document reading remain identical to URL downloads.
    """
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    final_path = destination / Path(filename).name
    try:
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise _NonRetryable("empty captured document")
        if len(data) > MAX_CAPTURE_BYTES:
            raise _NonRetryable("captured document exceeds 50 MiB")
        raw = bytes(data)
        digest = hashlib.sha256(raw).hexdigest()
        part = destination / f".{final_path.name}.part-{os.getpid()}-captured"
        with part.open("wb") as handle:
            handle.write(raw)
            handle.flush(); os.fsync(handle.fileno())
        if hashlib.sha256(part.read_bytes()).hexdigest() != digest:
            try: part.unlink()
            except OSError: pass
            raise _NonRetryable("captured document digest verification failed")
        part.replace(final_path)
        return DownloadArtifact(url, "downloaded", final_path, digest, len(raw), (media_type or "").split(";", 1)[0].lower(), attempts=1)
    except _NonRetryable as exc:
        return DownloadArtifact(url, "failed", error={"code": "captured_document_failed", "message": str(exc), "retryable": False}, attempts=1)
    except Exception as exc:
        try: part.unlink()
        except (OSError, UnboundLocalError): pass
        return DownloadArtifact(url, "failed", error={"code": "captured_document_failed", "message": str(exc), "retryable": False}, attempts=1)


def retain_blob(data_dir: str | Path, source_path: Path, digest: str) -> str:
    root = Path(data_dir); target = root / "blobs" / "sha256" / digest[:2] / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest: raise ValueError("existing blob digest verification failed")
    else:
        temp = target.with_name(f".{target.name}.part-{os.getpid()}")
        with source_path.open("rb") as src, temp.open("wb") as dst:
            while chunk := src.read(1024 * 128): dst.write(chunk)
            dst.flush(); os.fsync(dst.fileno())
        if hashlib.sha256(target.read_bytes() if target.exists() else temp.read_bytes()).hexdigest() != digest:
            try: temp.unlink()
            except OSError: pass
            raise ValueError("blob digest verification failed")
        temp.replace(target)
    return str(target.relative_to(root))
