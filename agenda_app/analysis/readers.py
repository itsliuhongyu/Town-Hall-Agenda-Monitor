from __future__ import annotations

import hashlib
import io
import mimetypes
import zipfile
from pathlib import Path
from typing import Any

from ..domain import ErrorInfo, ReadResult

READER_VERSION = "reader-v1"


def _error(code: str, message: str, *, retryable: bool = False, details: dict[str, Any] | None = None) -> ErrorInfo:
    return ErrorInfo(code, message, retryable, "read", details or {})


def _read_pdf(data: bytes) -> ReadResult:
    if not data.startswith(b"%PDF"):
        return ReadResult("failed", "", error=_error("invalid_pdf", "The file is not a valid PDF signature."), reader_version=READER_VERSION)
    try:
        import pdfplumber
        pages: list[dict[str, Any]] = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for number, page in enumerate(pdf.pages, 1):
                text = page.extract_text() or ""
                pages.append({"page": number, "text": text})
        text = "\n\n".join(p["text"] for p in pages if p["text"])
        if not text.strip():
            return ReadResult("needs_ocr", "", tuple(pages), "scanned", _error("ocr_required", "PDF has no extractable text; OCR is not enabled in stage 2."), READER_VERSION)
        return ReadResult("readable", text, tuple(pages), "text", reader_version=READER_VERSION)
    except Exception as exc:
        return ReadResult("failed", "", error=_error("pdf_read_failed", str(exc), details={"type": type(exc).__name__}), reader_version=READER_VERSION)


def _read_docx(data: bytes) -> ReadResult:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml")
        from xml.etree import ElementTree
        root = ElementTree.fromstring(xml)
        text = " ".join(node.text or "" for node in root.iter() if node.tag.endswith("}t"))
        if not text.strip():
            return ReadResult("empty", "", quality="empty", reader_version=READER_VERSION)
        return ReadResult("readable", text, quality="text", reader_version=READER_VERSION)
    except Exception as exc:
        return ReadResult("failed", "", error=_error("docx_read_failed", str(exc), details={"type": type(exc).__name__}), reader_version=READER_VERSION)


def _read_html(data: bytes) -> ReadResult:
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(data, "html.parser")
        for element in soup(["script", "style", "noscript"]): element.decompose()
        text = soup.get_text("\n", strip=True)
    except ImportError:
        import re
        text = re.sub(r"<[^>]+>", " ", data.decode("utf-8", "replace"))
    except Exception as exc:
        return ReadResult("failed", "", error=_error("html_read_failed", str(exc)), reader_version=READER_VERSION)
    if not text.strip(): return ReadResult("empty", "", quality="empty", reader_version=READER_VERSION)
    return ReadResult("readable", text, quality="text", reader_version=READER_VERSION)


def read_bytes(data: bytes, media_type: str | None = None, filename: str = "") -> ReadResult:
    if not data:
        return ReadResult("empty", "", quality="empty", error=_error("empty_document", "The downloaded document was empty."), reader_version=READER_VERSION)
    path_suffix = Path(filename).suffix.lower()
    media_type = (media_type or "").split(";", 1)[0].lower()
    if data.startswith(b"%PDF") or media_type == "application/pdf" or path_suffix == ".pdf":
        return _read_pdf(data)
    if data.startswith(b"PK\x03\x04") and (path_suffix == ".docx" or media_type in {"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/zip"}):
        return _read_docx(data)
    if media_type in {"text/html", "application/xhtml+xml"} or path_suffix in {".html", ".htm"} or data.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return _read_html(data)
    if path_suffix in {".txt", ".csv"} or media_type.startswith("text/"):
        text = data.decode("utf-8", "replace")
        return ReadResult("readable" if text.strip() else "empty", text, quality="text" if text.strip() else "empty", reader_version=READER_VERSION)
    return ReadResult("unsupported", "", error=_error("unsupported_document", "The document type is not supported."), reader_version=READER_VERSION)


def read_file(path: str | Path, media_type: str | None = None) -> ReadResult:
    path = Path(path)
    try:
        return read_bytes(path.read_bytes(), media_type, path.name)
    except OSError as exc:
        return ReadResult("failed", "", error=_error("document_unavailable", str(exc), retryable=True), reader_version=READER_VERSION)


def reader_cache_key(data: bytes, media_type: str | None = None, options: dict[str, Any] | None = None) -> str:
    import json
    value = data + b"\0" + (media_type or "").encode() + b"\0" + READER_VERSION.encode() + b"\0" + json.dumps(options or {}, sort_keys=True).encode()
    return hashlib.sha256(value).hexdigest()
