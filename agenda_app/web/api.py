from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import sqlite3
import threading
import csv
import io
from datetime import date
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from ..config import canonical_url, utc_now
from ..runs.service import RunService
from ..settings.service import SettingsService
from ..storage.exports import ExportService
from ..storage.legacy_import import LegacyImporter
from ..storage.source_initialization import SourceInitialization
from ..storage.repository import ConflictError, NotFoundError, Repository, RepositoryError, ValidationError, dumps, loads
from ..review.service import ReviewService
from .responses import error, success


class API:
    def __init__(self, repository: Repository, *, legacy_root: str | Path | None = None, source_initialization: SourceInitialization | None = None):
        self.repo = repository; self.runs = RunService(repository); self.settings = SettingsService(repository); self.review = ReviewService(repository); self.exports = ExportService(repository); self.legacy_root = legacy_root
        self.source_initialization = source_initialization or (SourceInitialization(repository, legacy_root) if legacy_root is not None else None)
        self.csrf = None
        self.allowed_origin = None
        self.allowed_host = None
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agenda-job")

    def _job(self, kind: str, payload: dict[str, Any], fn, *, idempotency_key: str | None = None, scope: str | None = None):
        job_id = __import__('agenda_app.domain', fromlist=['new_id']).new_id(); now = utc_now()
        scope = scope or f"local_user:job:{kind}"
        body_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        try:
            with __import__('agenda_app.storage.db', fromlist=['transaction']).transaction(self.repo.paths) as conn:
                if idempotency_key:
                    existing = conn.execute("SELECT body_hash,result_json FROM action_requests WHERE scope=? AND idempotency_key=?", (scope, idempotency_key)).fetchone()
                    if existing:
                        if existing[0] != body_hash: raise ConflictError("idempotency_conflict", {})
                        return json.loads(existing[1])
                conn.execute("INSERT INTO jobs(id,kind,state,owner_token,input_json,started_at) VALUES(?,?,?,?,?,?)", (job_id, kind, "running", job_id, dumps(payload), now))
                if idempotency_key:
                    conn.execute("INSERT INTO action_requests(id,scope,idempotency_key,body_hash,result_json,http_status,created_at) VALUES(?,?,?,?,?,?,?)", (job_id + ":request", scope, idempotency_key, body_hash, dumps({"job_id": job_id, "status": "running", "status_url": f"/api/v1/jobs/{job_id}"}), 202, now))
        except sqlite3.IntegrityError:
            # A concurrent identical job can win the unique idempotency key
            # between transaction attempts. Replay its durable receipt rather
            # than exposing a transient UNIQUE constraint failure.
            if not idempotency_key: raise
            existing = self.repo.row("SELECT body_hash,result_json FROM action_requests WHERE scope=? AND idempotency_key=?", (scope, idempotency_key))
            if not existing or existing[0] != body_hash: raise ConflictError("idempotency_conflict", {})
            return json.loads(existing[1])
        def execute():
            try:
                result = fn()
                with __import__('agenda_app.storage.db', fromlist=['transaction']).transaction(self.repo.paths) as conn:
                    conn.execute("UPDATE jobs SET state='success',result_json=?,finished_at=? WHERE id=?", (dumps(result), utc_now(), job_id))
            except Exception as exc:
                with __import__('agenda_app.storage.db', fromlist=['transaction']).transaction(self.repo.paths) as conn:
                    conn.execute("UPDATE jobs SET state='failed',error_json=?,finished_at=? WHERE id=?", (dumps({"code": "job_failed", "message": str(exc)}), utc_now(), job_id))
        self.executor.submit(execute)
        return {"job_id": job_id, "status": "running", "status_url": f"/api/v1/jobs/{job_id}"}

    @staticmethod
    def _query(query: str) -> dict[str, list[str]]: return parse_qs(query, keep_blank_values=True)

    def _short_action(self, scope: str, payload: dict[str, Any], key: str, fn):
        body_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        from ..storage.db import transaction
        from ..domain import new_id
        with transaction(self.repo.paths) as conn:
            existing = conn.execute("SELECT body_hash,result_json FROM action_requests WHERE scope=? AND idempotency_key=?", (scope, key)).fetchone()
            if existing:
                if existing[0] != body_hash: raise ConflictError("idempotency_conflict", {})
                result = json.loads(existing[1]); result["idempotent"] = True; return result
            result = fn(conn); result = dict(result) if isinstance(result, dict) else result
            if isinstance(result, dict): result.setdefault("idempotent", False)
            conn.execute("INSERT INTO action_requests(id,scope,idempotency_key,body_hash,result_json,http_status,created_at) VALUES(?,?,?,?,?,?,?)", (new_id(), scope, key, body_hash, dumps(result), 200, utc_now()))
            return result

    def _action_status(self, scope: str, key: str, default: int = 202) -> int:
        row = self.repo.row("SELECT http_status FROM action_requests WHERE scope=? AND idempotency_key=?", (scope, key))
        return int(row[0]) if row else default

    def handle(self, method: str, path: str, *, body: bytes = b"", headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        parsed = urlparse(path); clean = parsed.path; parts = [unquote(p) for p in clean.split("/") if p]
        if not parts or parts[0] != "api" or (len(parts) > 1 and parts[1] != "v1"):
            return success({"name": "Town Hall Agenda Monitor", "version": "2", "pages": ["/", "/review", "/settings"]})
        if method in {"POST", "PUT", "PATCH"}:
            if len(body) > 64 * 1024: return error("body_too_large", "Request body is too large.", 413)
            if headers.get("content-type", "").split(";", 1)[0].lower() != "application/json": return error("unsupported_media_type", "Mutations require application/json.", 415)
            origin = headers.get("origin")
            if self.allowed_host and headers.get("host") != self.allowed_host:
                return error("host_forbidden", "The request Host does not match this local server.", 403)
            if self.allowed_origin and origin != self.allowed_origin:
                return error("origin_forbidden", "Cross-origin mutations are not allowed.", 403)
            if self.csrf and headers.get("x-csrf-token") != self.csrf:
                return error("csrf_failed", "A valid CSRF token is required.", 403)
            if not headers.get("idempotency-key"):
                return error("idempotency_required", "Idempotency-Key is required.", 400)
            try: payload = json.loads(body.decode("utf-8")) if body else {}
            except (UnicodeDecodeError, json.JSONDecodeError): return error("malformed_json", "Request body is not valid JSON.", 400)
            if not isinstance(payload, dict): return error("invalid_json", "Request body must be an object.", 400)
        else: payload = {}
        if method not in {"GET", "POST"}:
            status, response_headers, response_body = error("method_not_allowed", "Method not allowed.", 405)
            response_headers["Allow"] = "GET, POST"
            return status, response_headers, response_body
        try:
            return self._dispatch(method, parts[2:], parsed.query, payload, headers)
        except ValidationError as exc: return error("invalid_value", str(exc), 422)
        except ConflictError as exc: return error(exc.code, str(exc), 409, details=exc.details)
        except NotFoundError as exc: return error("not_found", str(exc), 404)
        except KeyError as exc: return error("not_found", str(exc), 404)
        except ValueError as exc: return error("invalid_value", str(exc), 422)
        except FileNotFoundError: return error("original_missing", "The stored original is missing.", 410)
        except RuntimeError as exc:
            if str(exc) == "export_not_ready": return error("export_not_ready", "Export is still preparing.", 409)
            return error("internal_error", str(exc), 500)
        except Exception:
            return error("internal_error", "Internal server error.", 500)

    def _dispatch(self, method: str, parts: list[str], query: str, payload: dict[str, Any], headers: dict[str, str]):
        q = self._query(query)
        if method == "GET" and parts == ["overview"]: return success(self.repo.overview())
        if method == "GET" and parts == ["sources"]:
            return success({"sources": self.repo.list_sources(), "data_revision": self.repo.data_revision(), "source_initialization": self.source_initialization.latest() if self.source_initialization else None})
        if method == "GET" and parts == ["source-initialization"]:
            if not self.source_initialization:
                return success({"state": "unconfigured", "status": "unconfigured", "source_count": len(self.repo.list_sources()), "summary": {}, "error": {"code": "source_root_not_configured", "message": "No townlist source root was configured for this server."}})
            record = self.source_initialization.latest()
            result = record or {"state": "not_started", "status": "not_started", "source_count": len(self.repo.list_sources()), "summary": {}, "error": None}
            result = dict(result)
            result["source_count"] = len(self.repo.list_sources(True))
            return success(result)
        if method == "GET" and len(parts) == 2 and parts[0] == "source-initialization" and parts[1] == "diagnostics.csv":
            if not self.source_initialization:
                raise NotFoundError("source initialization")
            record = self.source_initialization.latest()
            if not record:
                raise NotFoundError("source initialization")
            rows = self.source_initialization.diagnostics(record["id"])
            output = io.StringIO(); writer = csv.writer(output, lineterminator="\n")
            writer.writerow(["row_no", "status", "reason", "source_id", "payload_json"])
            writer.writerows((row["row_no"], row["status"], row.get("reason") or "", row.get("source_id") or "", row["payload_json"]) for row in rows)
            return 200, {"Content-Type": "text/csv; charset=utf-8", "Content-Disposition": "attachment; filename=source-initialization-diagnostics.csv"}, output.getvalue().encode("utf-8")
        if method == "POST" and parts == ["sources", "initialize"]:
            if not self.source_initialization:
                raise ValidationError("source initialization is unavailable because no townlist root was configured")
            if "force" in payload and type(payload["force"]) is not bool:
                raise ValidationError("force must be a boolean")
            return success(self._short_action("local_user:sources:initialize", payload, headers["idempotency-key"], lambda conn: self.source_initialization.initialize(conn=conn, force=payload.get("force", False))))
        if method == "POST" and len(parts) == 2 and parts[0] == "sources": return success(self._short_action(f"local_user:source:{parts[1]}", payload, headers["idempotency-key"], lambda conn: self.repo.update_source(parts[1], payload.get("expected_revision"), {k: v for k, v in payload.items() if k != "expected_revision"}, conn=conn)))
        if method == "GET" and parts and parts[0] == "runs" and len(parts) == 1:
            limit = int((q.get("limit") or [20])[0]); limit = max(1, min(100, limit)); rows = self.repo.rows("SELECT * FROM runs ORDER BY COALESCE(created_seq,0) DESC,started_at DESC,id DESC LIMIT ?", (limit,)); return success({"runs": [dict(row) for row in rows], "next_cursor": rows[-1]["id"] if len(rows) == limit else None})
        if method == "POST" and parts == ["runs"]:
            key = headers["idempotency-key"]; scope = "local_user:runs"; body_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            result = self.runs.start(kind=payload.get("kind", "full"), source_ids=payload.get("source_ids"), background=True,
                                     action_scope=scope, action_key=key, action_body_hash=body_hash)
            return success(result, self._action_status(scope, key))
        if parts and parts[0] == "runs" and len(parts) >= 2:
            run_id = parts[1]
            if method == "GET" and len(parts) == 2:
                run = self.repo.get_run(run_id)
                if not run: raise NotFoundError("run")
                return success(self._run_response(run))
            if method == "GET" and len(parts) == 3 and parts[2] == "documents":
                return success({"documents": self.repo.run_documents(run_id)})
            if method == "POST" and len(parts) == 3 and parts[2] == "retry":
                key = headers["idempotency-key"]; scope = f"local_user:retry:{run_id}"; body_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                result = self.runs.retry(run_id, payload.get("source_ids", []), background=True,
                                         action_scope=scope, action_key=key, action_body_hash=body_hash)
                return success(result, self._action_status(scope, key))
        if method == "GET" and parts == ["items"]:
            search = (q.get("q") or [""])[0]
            if len(search) > 200: raise ValidationError("search must be <=200 characters")
            priorities = set((q.get("priority") or [""])[0].split(",")) - {""}; states = set((q.get("review_state") or [""])[0].split(",")) - {""}; limit = max(1, min(100, int((q.get("limit") or [50])[0]))); offset = max(0, int((q.get("offset") or [0])[0])); scope = (q.get("scope") or ["run"])[0]
            if scope not in {"run", "history"}: raise ValidationError("scope must be run or history")
            for key in ("from", "to"):
                if q.get(key) and q[key][0]:
                    try: date.fromisoformat(q[key][0])
                    except ValueError: raise ValidationError(f"{key} must be an ISO date")
            run_id = (q.get("run_id") or [self.repo.current_published_run()])[0]
            rows, total = self.repo.items(run_id=run_id, scope=scope, query=search, priorities=priorities, source_id=(q.get("source_id") or [None])[0], review_states=states, limit=limit, offset=offset, from_date=(q.get("from") or [None])[0], to_date=(q.get("to") or [None])[0])
            run_rows = self.repo.run_sources(run_id) if run_id else []
            return success({"items": rows, "total": total, "run_id": run_id, "data_revision": self.repo.data_revision(), "coverage": {"complete": sum(1 for row in run_rows if row["state"] in {"success", "no_results"}), "total": len(run_rows), "failed": sum(1 for row in run_rows if row["state"] in {"failed", "partial", "interrupted"})}, "next_offset": offset + limit if offset + limit < total else None})
        if parts and parts[0] == "items" and len(parts) in {2, 3}:
            if method == "GET":
                result = self.repo.item_detail(parts[1], (q.get("run_id") or [self.repo.current_published_run()])[0]);
                if not result: raise NotFoundError("item")
                return success(result)
            if method == "POST" and len(parts) == 3 and parts[-1] == "review": return success(self.review.apply(parts[1], payload, idempotency_key=headers["idempotency-key"]))
        if method == "POST" and len(parts) == 3 and parts[0] == "review-events" and parts[2] == "undo": return success(self.review.undo(parts[1], payload.get("expected_revision"), idempotency_key=headers["idempotency-key"]))
        if method == "GET" and parts == ["settings"]: return success(self.settings.get())
        if method == "POST" and parts == ["settings"]: return success(self._short_action("local_user:settings", payload, headers["idempotency-key"], lambda conn: self.settings.save(payload.get("expected_revision"), payload.get("values", {}), conn=conn)))
        if method == "GET" and parts == ["models"]: return success(self.repo.models_state())
        if method == "POST" and parts == ["models", "refresh"]: return success(self._job("model_refresh", payload, self.settings.refresh_models, idempotency_key=headers["idempotency-key"], scope="local_user:models:refresh"), 202)
        if method == "POST" and parts == ["models", "test-connection"]: return success(self._job("connection_test", payload, self.settings.test_connection, idempotency_key=headers["idempotency-key"], scope="local_user:models:test"), 202)
        if method == "GET" and parts == ["policy"]: return success(self.settings.policy())
        if method == "POST" and parts == ["policy"]: return success(self._short_action("local_user:policy", payload, headers["idempotency-key"], lambda conn: self.settings.update_policy(payload.get("expected_revision"), payload.get("strategy"), payload.get("rules", []), payload.get("removals", []), payload.get("reason", ""), conn=conn)))
        if method == "POST" and parts == ["policy", "preview"]:
            preview_run = payload.get("run_id") or self.repo.current_published_run()
            if not preview_run: raise ValidationError("a completed run is required for preview")
            return success(self.settings.preview(preview_run, payload.get("strategy"), payload.get("rules", []), payload.get("removals", [])))
        if method == "POST" and len(parts) == 3 and parts[0] == "policy" and parts[2] == "undo": return success(self._short_action(f"local_user:policy-undo:{parts[1]}", payload, headers["idempotency-key"], lambda conn: self.settings.undo_policy(parts[1], payload.get("expected_revision"), payload.get("reason", ""), conn=conn)))
        if method == "POST" and parts == ["exports"]:
            run_id = payload.get("run_id") or self.repo.current_published_run()
            if not run_id: raise ValidationError("run_id is required when there is no publication")
            return success(self._job("export", payload, lambda: self.exports.export_run(run_id, spreadsheet_safe=payload.get("format", "spreadsheet_safe") != "raw", scope=payload.get("scope", "run"), query=payload.get("filters")), idempotency_key=headers["idempotency-key"], scope="local_user:exports"), 202)
        if parts and parts[0] == "exports" and len(parts) == 2 and method == "GET":
            result = self.exports.get(parts[1]);
            if not result: raise NotFoundError("export")
            return success(result)
        if parts and parts[0] == "exports" and len(parts) == 4 and parts[2] == "files" and method == "GET":
            path, record = self.exports.file(parts[1], parts[3]); body = path.read_bytes(); return 200, {"Content-Type": "text/csv; charset=utf-8", "Content-Disposition": f"attachment; filename={parts[3]}", "ETag": record["manifest"]["files"][parts[3][:-4]]["sha256"]}, body
        if method == "GET" and parts and parts[0] == "documents" and len(parts) == 3:
            version_id, action = parts[1], parts[2]; row = self.repo.row("SELECT * FROM document_versions WHERE id=?", (version_id,));
            if not row: raise NotFoundError("document version")
            if action == "text":
                if row["retained_text"] is None: return error("text_unavailable", "Text has not been retained.", 404)
                return success({"text": row["retained_text"], "pages": [], "reader_version": row["reader_version"], "quality": row["read_status"]})
            rel = row["blob_relpath"]
            if not rel: return error("original_missing", "The stored original is missing.", 410)
            target = (self.repo.paths.root / rel).resolve(); blobs = self.repo.paths.blobs.resolve()
            if blobs not in target.parents: return error("internal_error", "Invalid stored path.", 500)
            if not target.is_file(): return error("original_missing", "The stored original is missing.", 410)
            ctype = row["media_type"] or "application/octet-stream"; disposition = "inline" if ctype == "application/pdf" else "attachment"; return 200, {"Content-Type": ctype, "Content-Disposition": disposition}, target.read_bytes()
        if method == "POST" and parts == ["imports", "legacy"]: return success(self._job("import", payload, lambda: LegacyImporter(self.repo).import_directory(self.legacy_root or Path.cwd()), idempotency_key=headers["idempotency-key"], scope="local_user:imports:legacy"), 202)
        if method == "GET" and len(parts) == 2 and parts[0] == "imports":
            row = self.repo.row("SELECT * FROM imports WHERE id=?", (parts[1],));
            if not row: raise NotFoundError("import")
            result = dict(row); result["summary"] = loads(result.pop("summary_json"), {}); return success(result)
        if method == "GET" and len(parts) == 3 and parts[0] == "imports" and parts[2] == "unresolved.csv":
            if not self.repo.row("SELECT 1 FROM imports WHERE id=?", (parts[1],)): raise NotFoundError("import")
            rows = self.repo.rows("SELECT file_kind,row_no,payload_json,status,reason FROM import_rows WHERE import_id=? AND status IN ('unresolved','invalid') ORDER BY file_kind,row_no", (parts[1],)); output = io.StringIO(); writer = csv.writer(output, lineterminator="\n"); writer.writerow(["file_kind", "row_no", "status", "reason", "payload_json"]); writer.writerows((row["file_kind"], row["row_no"], row["status"], row["reason"] or "", row["payload_json"]) for row in rows); return 200, {"Content-Type": "text/csv; charset=utf-8", "Content-Disposition": "attachment; filename=unresolved.csv"}, output.getvalue().encode("utf-8")
        if method == "GET" and parts and parts[0] == "jobs" and len(parts) == 2:
            row = self.repo.row("SELECT * FROM jobs WHERE id=?", (parts[1],));
            if not row: raise NotFoundError("job")
            result = dict(row); result["result"] = loads(result.pop("result_json"), None); result["error"] = loads(result.pop("error_json"), None); return success(result)
        return error("not_found", "Route not found.", 404)

    def _run_response(self, run: dict[str, Any]) -> dict[str, Any]:
        sources = []
        for row in self.repo.run_sources(run["id"]):
            error_value = loads(row["error_json"], None)
            if isinstance(error_value, list):
                retryable = any(bool(item.get("retryable")) for item in error_value if isinstance(item, dict))
            elif isinstance(error_value, dict):
                retryable = bool(error_value.get("retryable", False))
            else:
                retryable = False
            sources.append({"source_id": row["source_id"], "state": row["state"], "window": {"start": row["window_start"], "end": row["window_end"]}, "timezone": row["timezone"], "observed_at": row["observed_at"], "inherited_from": row["inherited_from_id"], "counts": {"found": row["found_count"], "downloaded": row["downloaded_count"], "analyzed": row["analyzed_count"]}, "error": error_value, "retryable": retryable})
        complete = sum(1 for row in sources if row["state"] in {"success", "no_results"})
        settings_snapshot = loads(run["settings_snapshot_json"], {})
        export_row = self.repo.row("SELECT id FROM exports WHERE run_id=? AND state='ready' ORDER BY created_at DESC,id DESC LIMIT 1", (run["id"],))
        return {"id": run["id"], "parent_run_id": run["parent_run_id"], "kind": run["kind"], "status": run["status"], "phase": run["phase"], "reason_code": run["reason_code"], "publication_state": run["publication_state"], "started_at": run["started_at"], "finished_at": run["finished_at"], "heartbeat_at": run["heartbeat_at"], "coverage": {"complete": complete, "total": len(sources), "inherited": sum(1 for row in sources if row["inherited_from"]), "failed": sum(1 for row in sources if row["state"] in {"failed", "partial", "interrupted"})}, "counts": {"found": sum(row["counts"]["found"] for row in sources), "downloaded": sum(row["counts"]["downloaded"] for row in sources), "analyzed": sum(row["counts"].get("analyzed", 0) for row in sources)}, "model": settings_snapshot.get("values", {}).get("selected_model"), "policy_version": run["policy_version_id"], "sources": sources, "candidate_review_url": f"/review?run_id={run['id']}", "export_id": export_row[0] if export_row else None}
