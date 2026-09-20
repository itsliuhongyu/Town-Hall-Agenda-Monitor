from __future__ import annotations

import secrets
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..storage.repository import Repository
from ..runs.service import RunService
from ..storage.repository import ConflictError
from ..storage.source_initialization import SourceInitialization
from .api import API


class AgendaHandler(BaseHTTPRequestHandler):
    server_version = "AgendaApp/2"
    max_body = 64 * 1024

    def _send(self, status, headers, body):
        self.send_response(status)
        for key, value in headers.items(): self.send_header(key, value)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        if not self.path.startswith("/api/") and not self.path.startswith("/static/"):
            return self._send(*self.server.render_page(self.path))
        if self.path.startswith("/static/"):
            return self._send(*self.server.static_response(self.path))
        status, headers, body = self.server.api.handle("GET", self.path, headers=dict(self.headers)); self._send(status, headers, body)

    def do_POST(self):
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return self._send(*self.server.api.handle("POST", self.path, body=b"", headers=dict(self.headers)))
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            self.close_connection = True
            return self._send(*self.server.api_error("invalid_content_length", "Content-Length must be a non-negative integer.", 400))
        if length < 0:
            self.close_connection = True
            return self._send(*self.server.api_error("invalid_content_length", "Content-Length must be a non-negative integer.", 400))
        if length > self.max_body:
            self.close_connection = True
            return self._send(*self.server.api_error("body_too_large", "Request body is too large.", 413))
        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            self.close_connection = True
            return self._send(*self.server.api_error("incomplete_body", "Request body ended before Content-Length.", 400))
        status, headers, response = self.server.api.handle("POST", self.path, body=body, headers=dict(self.headers)); self._send(status, headers, response)

    def do_PUT(self): self._send(*self.server.api.handle("PUT", self.path, headers=dict(self.headers)))
    def do_PATCH(self): self._send(*self.server.api.handle("PATCH", self.path, headers=dict(self.headers)))
    def do_DELETE(self): self._send(*self.server.api.handle("DELETE", self.path, headers=dict(self.headers)))

    def log_message(self, fmt, *args): return


def create_server(data_dir: str | Path, host: str = "127.0.0.1", port: int = 8000, *, legacy_root: str | Path | None = None) -> ThreadingHTTPServer:
    repository = Repository(data_dir)
    # App startup owns only the source registry bootstrap.  The full legacy
    # importer remains an explicit Settings/CLI operation and is never
    # reached by constructing a Repository.
    source_initialization = None
    if legacy_root is not None:
        source_initialization = SourceInitialization(repository, legacy_root)
        source_initialization.initialize()
    try:
        RunService(repository).recover_interrupted()
    except ConflictError:
        # A live worker still owns the advisory lock; its run remains active.
        pass
    server = ThreadingHTTPServer((host, port), AgendaHandler)
    server.api = API(repository, legacy_root=legacy_root, source_initialization=source_initialization)
    server.csrf = secrets.token_urlsafe(24)
    server.api.csrf = server.csrf
    server.api.allowed_origin = f"http://{host}:{server.server_address[1]}"
    server.api.allowed_host = f"{host}:{server.server_address[1]}"

    template_root = Path(__file__).with_name("templates")
    static_root = Path(__file__).with_name("static")

    def render_page(path: str):
        requested = path.split("?", 1)[0]
        if requested == "/favicon.ico":
            return 204, {"Content-Type": "image/x-icon", "Cache-Control": "no-store"}, b""
        page = {"/": "overview", "/review": "review", "/settings": "settings"}.get(requested)
        if not page:
            return server.api_error("not_found", "Page not found.", 404)
        source = (template_root / f"{page}.html").read_text(encoding="utf-8")
        base = (template_root / "base.html").read_text(encoding="utf-8")
        content = source.replace("{{PAGE_TITLE}}", page.title()).replace("{{PAGE_CONTENT}}", source)
        # The page templates are complete fragments; base.html is a small shell
        # with a single content placeholder.  Escape the token because it is
        # the only server value embedded in HTML.
        if "{{PAGE_CONTENT}}" in base:
            content = base.replace("{{PAGE_TITLE}}", page.title()).replace("{{PAGE_CONTENT}}", source)
        content = content.replace("{{PAGE}}", page).replace("{{CSRF_TOKEN}}", html.escape(server.csrf, quote=True))
        return 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}, content.encode("utf-8")

    def static_response(path: str):
        relative = urlparse(path).path.removeprefix("/static/")
        target = (static_root / relative).resolve()
        if static_root.resolve() not in target.parents or not target.is_file():
            return server.api_error("not_found", "Static asset not found.", 404)
        content_types = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}
        return 200, {"Content-Type": content_types.get(target.suffix, "application/octet-stream"), "Cache-Control": "no-store"}, target.read_bytes()

    server.render_page = render_page
    server.static_response = static_response
    server.api_error = lambda code, message, status: __import__("agenda_app.web.responses", fromlist=["error"]).error(code, message, status)
    return server
