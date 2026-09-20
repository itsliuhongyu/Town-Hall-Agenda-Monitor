"""Initialize the local source registry from the maintained town list.

This module deliberately has a narrower scope than :mod:`legacy_import`.
It reads only ``townlist.csv`` and creates source configuration rows.  It
does not create runs, meetings, documents, policies, feedback, or review
state, so app startup can safely make an empty local database usable without
pretending that historical agenda data was imported.
"""

from __future__ import annotations

import hashlib
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from ..config import LEGACY_ROOT, canonical_url, identity_key, normalize_text, utc_now
from ..domain import new_id
from .db import transaction
from .legacy_import import KNOWN_PLATFORMS, LegacyImporter, _csv_rows, _text
from .repository import Repository, dumps, loads


class SourceInitialization:
    """Perform an idempotent, source-only townlist initialization."""

    TOWNLIST_CHOICES = ("resources/townlist.csv", "townlist.csv")

    def __init__(self, repository: Repository, source_root: str | Path | None = None):
        self.repo = repository
        self.source_root = Path(source_root or LEGACY_ROOT).resolve()

    def _find_townlist(self) -> Path | None:
        for choice in self.TOWNLIST_CHOICES:
            path = self.source_root / choice
            if path.is_file():
                return path
        return None

    @staticmethod
    def _summary_counts(summary: dict[str, Any]) -> dict[str, int]:
        return {
            key: int(summary.get(key, 0))
            for key in ("rows", "parseable_rows", "sources", "imported", "source_duplicates", "unresolved", "invalid")
        }

    @staticmethod
    def _row_payload(row: dict[str, Any]) -> dict[str, Any]:
        """Make DictReader's ``None`` extra-column key inspectable/serializable."""
        payload: dict[str, Any] = {}
        for key, value in row.items():
            payload["__extra_columns__" if key is None else str(key)] = value
        return payload

    def latest(self) -> dict[str, Any] | None:
        row = self.repo.row("SELECT * FROM source_initializations ORDER BY rowid DESC LIMIT 1")
        if not row:
            return None
        result = dict(row)
        result["summary"] = loads(result.pop("summary_json"), {})
        result["error"] = loads(result.pop("error_json"), None)
        return result

    def diagnostics(self, initialization_id: str | None = None) -> list[dict[str, Any]]:
        record = initialization_id or (self.latest() or {}).get("id")
        if not record:
            return []
        return [
            {**dict(row), "payload": loads(row["payload_json"], {})}
            for row in self.repo.rows(
                "SELECT * FROM source_initialization_rows WHERE initialization_id=? ORDER BY row_no,id",
                (record,),
            )
        ]

    def initialize(self, *, conn=None, force: bool = False) -> dict[str, Any]:
        """Initialize once for a source database, safely across processes.

        ``conn`` is accepted so the API's idempotency transaction can own the
        same SQLite write lock.  A startup retry is performed only when the
        source bytes changed after a failed attempt.  Once any source exists,
        the registry is considered user-owned and is never reconciled from the
        CSV; this preserves edited and disabled sources.
        """

        owns_transaction = conn is None
        context = transaction(self.repo.paths) if owns_transaction else nullcontext(conn)
        with context as connection:
            existing_sources = int(connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0])
            latest = connection.execute(
                "SELECT * FROM source_initializations ORDER BY rowid DESC LIMIT 1"
            ).fetchone()

            path = self._find_townlist()
            source_path = str(path) if path else str(self.source_root / self.TOWNLIST_CHOICES[0])
            try:
                raw = path.read_bytes() if path else b""
                source_hash = hashlib.sha256(raw).hexdigest()
            except OSError as exc:
                raw = b""
                source_hash = hashlib.sha256(raw).hexdigest()
                path = None
                read_error = {"code": "townlist_unreadable", "message": str(exc), "retryable": True}
            else:
                read_error = None

            # Any existing registry is user-owned.  Even an explicit retry
            # cannot append/reconcile from the CSV and therefore cannot
            # accidentally restore a disabled source.
            if existing_sources:
                return self._existing_result(connection, latest, existing_sources)
            if latest and latest["state"] in {"success", "partial"} and latest["source_hash"] == source_hash and not force:
                return self._record_result(latest, idempotent=True, source_count=existing_sources)
            if latest and latest["state"] == "failed" and latest["source_hash"] == source_hash and not force:
                return self._record_result(latest, idempotent=True, source_count=existing_sources)

            initialization_id = new_id()
            started = utc_now()
            connection.execute(
                "INSERT INTO source_initializations(id,source_root,source_path,source_hash,state,started_at,summary_json) VALUES(?,?,?,?,?,?,?)",
                (initialization_id, str(self.source_root), source_path, source_hash, "running", started, dumps({})),
            )
            summary: dict[str, Any] = {
                "rows": 0,
                "parseable_rows": 0,
                "sources": 0,
                "imported": 0,
                "source_duplicates": 0,
                "source_duplicate_reasons": {},
                "unresolved": 0,
                "invalid": 0,
                "warnings": [],
                "source_path": source_path,
                "source_hash": source_hash,
            }

            def finish(state: str, *, error: dict[str, Any] | None = None) -> dict[str, Any]:
                summary["status"] = state
                summary["counts"] = self._summary_counts(summary)
                finished = utc_now()
                connection.execute(
                    "UPDATE source_initializations SET state=?,finished_at=?,summary_json=?,error_json=? WHERE id=?",
                    (state, finished, dumps(summary), dumps(error) if error else None, initialization_id),
                )
                return {
                    "id": initialization_id,
                    "status": state,
                    "state": state,
                    "idempotent": False,
                    "source_count": int(connection.execute("SELECT COUNT(*) FROM sources WHERE enabled=1").fetchone()[0]),
                    "summary": summary,
                    "error": error,
                }

            if read_error:
                summary["warnings"].append(read_error["message"])
                return finish("failed", error=read_error)
            if path is None:
                error = {"code": "townlist_missing", "message": f"No townlist.csv found under {self.source_root}.", "retryable": True}
                summary["warnings"].append(error["message"])
                return finish("failed", error=error)
            if not raw:
                error = {"code": "townlist_empty", "message": "townlist.csv is empty.", "retryable": True}
                summary["warnings"].append(error["message"])
                return finish("failed", error=error)

            try:
                rows = _csv_rows(raw)
            except Exception as exc:
                error = {"code": "townlist_invalid_csv", "message": str(exc), "retryable": True}
                summary["warnings"].append(error["message"])
                return finish("failed", error=error)
            summary["rows"] = len(rows)
            if not rows:
                error = {"code": "townlist_no_rows", "message": "townlist.csv has no data rows.", "retryable": True}
                summary["warnings"].append(error["message"])
                return finish("failed", error=error)

            # The maintained schema has these fields.  A missing URL/name
            # column is diagnosed as an invalid list instead of silently
            # creating zero sources.
            header = set(rows[0])
            if not ({"Gov body", "gov_body", "name"} & header) or not (
                {"Link to board agenda site", "source_url", "collection_url"} & header
            ):
                error = {
                    "code": "townlist_schema",
                    "message": "townlist.csv must contain a source name and agenda URL column.",
                    "retryable": True,
                }
                summary["warnings"].append(error["message"])
                return finish("failed", error=error)

            source_cache: dict[str, str] = {}
            for row_no, row in enumerate(rows, 2):
                payload = self._row_payload(row)
                row_key = identity_key("townlist", row_no, dumps(payload))
                name, url, platform, timezone, explicit_timezone, parseable = LegacyImporter._source_fields(row)
                if parseable:
                    summary["parseable_rows"] += 1
                reason = None
                status = "imported"
                source_id = None
                if row.get(None) is not None:
                    status, reason = "invalid", "CSV row has extra columns"
                elif not name.strip() or not url.strip():
                    status, reason = "unresolved", "source name or URL is blank"
                elif platform not in KNOWN_PLATFORMS.values():
                    status, reason = "unresolved", f"unsupported platform: {platform}"
                elif not parseable:
                    status, reason = "unresolved", "legacy row is not marked parseable=y"
                else:
                    try:
                        normalized = canonical_url(url)
                        from ..config import ensure_timezone

                        ensure_timezone(timezone)
                    except ValueError as exc:
                        status, reason = "invalid", str(exc)
                    else:
                        identity = identity_key(platform, normalized, normalize_text(name))
                        source_id = source_cache.get(identity)
                        if source_id:
                            status, reason = "duplicate", "same platform + normalized URL + normalized name"
                        else:
                            old = connection.execute("SELECT * FROM sources WHERE identity_key=?", (identity,)).fetchone()
                            if old:
                                source_id = old["id"]
                                source_cache[identity] = source_id
                                status, reason = "duplicate", "existing source configuration preserved"
                            else:
                                source_id = new_id()
                                now = utc_now()
                                connection.execute(
                                    "INSERT INTO sources(id,identity_key,platform,name,collection_url,timezone,timezone_origin,enabled,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                    (
                                        source_id,
                                        identity,
                                        platform,
                                        name.strip(),
                                        normalized,
                                        timezone,
                                        "explicit" if explicit_timezone else "legacy_assumed",
                                        1,
                                        dumps({"source": "townlist", "source_initialization_id": initialization_id}),
                                        now,
                                        now,
                                    ),
                                )
                                source_cache[identity] = source_id
                                summary["imported"] += 1
                                summary["sources"] += 1

                if status == "duplicate":
                    summary["source_duplicates"] += 1
                    bucket = summary["source_duplicate_reasons"]
                    bucket[reason or "duplicate"] = bucket.get(reason or "duplicate", 0) + 1
                elif status == "unresolved":
                    summary["unresolved"] += 1
                elif status == "invalid":
                    summary["invalid"] += 1
                connection.execute(
                    "INSERT INTO source_initialization_rows(id,initialization_id,row_no,row_key,payload_json,status,source_id,reason) VALUES(?,?,?,?,?,?,?,?)",
                    (new_id(), initialization_id, row_no, row_key, dumps(payload), status, source_id, reason),
                )

            if summary["imported"]:
                self.repo.bump_revision(connection)
            if not summary["sources"]:
                error = {
                    "code": "townlist_no_valid_sources",
                    "message": "townlist.csv contained no supported parseable sources.",
                    "retryable": True,
                }
                summary["warnings"].append(error["message"])
                return finish("failed", error=error)
            return finish("success")

    @staticmethod
    def _record_result(row, *, idempotent: bool, source_count: int | None = None) -> dict[str, Any]:
        summary = loads(row["summary_json"], {})
        return {
            "id": row["id"],
            "status": row["state"],
            "state": row["state"],
            "idempotent": idempotent,
            "source_count": source_count,
            "summary": summary,
            "error": loads(row["error_json"], None),
        }

    def _existing_result(self, conn, latest, count: int) -> dict[str, Any]:
        if latest:
            result = self._record_result(latest, idempotent=True, source_count=int(conn.execute("SELECT COUNT(*) FROM sources WHERE enabled=1").fetchone()[0]))
            result["status"] = "already_configured"
            result["state"] = "already_configured"
            result["source_count"] = int(conn.execute("SELECT COUNT(*) FROM sources WHERE enabled=1").fetchone()[0])
            result["summary"] = {**result.get("summary", {}), "existing_sources": count}
            return result
        return {
            "id": None,
            "status": "already_configured",
            "state": "already_configured",
            "idempotent": True,
            "source_count": int(conn.execute("SELECT COUNT(*) FROM sources WHERE enabled=1").fetchone()[0]),
            "summary": {"existing_sources": count},
            "error": None,
        }


def initialize_sources(repository: Repository, source_root: str | Path | None = None, *, force: bool = False) -> dict[str, Any]:
    """Reusable explicit entrypoint for source-only initialization."""

    return SourceInitialization(repository, source_root).initialize(force=force)


SourceInitializer = SourceInitialization
