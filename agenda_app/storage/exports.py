from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from ..config import utc_now
from ..storage.db import transaction
from .repository import Repository, dumps, loads

HEADERS = ["item_id", "item_version_id", "run_id", "source_id", "document_id", "document_version_id", "filename", "item", "importance", "model_importance", "policy_importance", "human_importance", "priority_source", "review_state", "note", "original_text", "gov_body", "meeting_date", "source_timezone", "download_url", "source_url", "Link to board agenda site"]


def _safe(value: Any, enabled: bool) -> str:
    text = "" if value is None else str(value)
    if enabled and text and (text[0] in "=+-@\t\r\n" or text.lstrip(" \t\r\n")[:1] in "=+-@"):
        return "'" + text
    return text


class ExportService:
    def __init__(self, repository: Repository): self.repo = repository

    def _rows(self, run_id: str, *, include_excluded: bool = True, scope: str = "run", query: dict[str, Any] | None = None, conn=None) -> list[dict[str, Any]]:
        if conn is None:
            with self.repo.read() as read_conn:
                read_conn.execute("BEGIN")
                return self._rows(run_id, include_excluded=include_excluded, scope=scope, query=query, conn=read_conn)
        query = query or {}
        if scope not in {"run", "history"}:
            raise ValueError("scope must be run or history")
        params: list[Any] = []
        if scope == "history":
            run_source = "FROM (SELECT ri0.*,ROW_NUMBER() OVER (PARTITION BY ri0.item_version_id ORDER BY r0.created_seq DESC,r0.started_at DESC,r0.id DESC) rn FROM run_items ri0 JOIN runs r0 ON r0.id=ri0.run_id) ri"
            scope_clause = " AND ri.rn=1"
        else:
            run_source = "FROM run_items ri"
            scope_clause = " AND ri.run_id=?"
            params.append(run_id)
        sql = f"""SELECT iv.id item_version_id,i.id item_id,ri.run_id,s.id source_id,d.id document_id,dv.id document_version_id,d.display_filename filename,ai.title item,
                 ri.proposed_priority importance,ri.proposed_priority,ai.model_priority,ri.policy_priority,rv.human_priority,CASE WHEN rv.human_priority IS NOT NULL THEN 'human' ELSE ri.decision_source END priority_source,
                 rv.state review_state,rv.note,iv.original_text,s.name gov_body,m.local_date meeting_date,m.timezone source_timezone,d.original_url download_url,d.source_url
                 {run_source} JOIN item_versions iv ON iv.id=ri.item_version_id JOIN items i ON i.id=iv.item_id JOIN analysis_items ai ON ai.analysis_id=ri.analysis_id AND ai.item_version_id=ri.item_version_id
                 JOIN review_state rv ON rv.item_version_id=iv.id JOIN document_versions dv ON dv.id=iv.document_version_id JOIN documents d ON d.id=i.document_id JOIN meetings m ON m.id=d.meeting_id JOIN sources s ON s.id=m.source_id
                 WHERE 1=1""" + scope_clause
        if not include_excluded: sql += " AND ri.included=1"
        search = str(query.get("q", "") or "")
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += " AND (ai.title LIKE ? ESCAPE '\\' OR iv.original_text LIKE ? ESCAPE '\\' OR rv.note LIKE ? ESCAPE '\\')"
            params.extend([f"%{escaped}%"] * 3)
        priorities = {part for part in str(query.get("priority", "") or "").split(",") if part}
        if priorities:
            if not priorities.issubset({"low", "medium", "high"}):
                raise ValueError("invalid priority filter")
            sql += " AND COALESCE(rv.human_priority,ri.proposed_priority) IN (" + ",".join("?" for _ in priorities) + ")"
            params.extend(sorted(priorities))
        source_id = str(query.get("source_id", "") or "")
        if source_id:
            sql += " AND s.id=?"; params.append(source_id)
        review_states = {part for part in str(query.get("review_state", "") or "").split(",") if part}
        if review_states:
            if not review_states.issubset({"unreviewed", "confirmed", "needs_review"}):
                raise ValueError("invalid review state filter")
            sql += " AND rv.state IN (" + ",".join("?" for _ in review_states) + ")"
            params.extend(sorted(review_states))
        from_date = str(query.get("from", "") or "")
        to_date = str(query.get("to", "") or "")
        if from_date:
            sql += " AND m.local_date IS NOT NULL AND m.local_date>=?"; params.append(from_date)
        if to_date:
            sql += " AND m.local_date IS NOT NULL AND m.local_date<=?"; params.append(to_date)
        rows = [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
        for row in rows:
            row["importance"] = row["human_priority"] or row["proposed_priority"]
            row["model_importance"] = row.get("model_priority") or ""
            row["policy_importance"] = row.get("policy_priority") or ""
            row["human_importance"] = row.get("human_priority") or ""
            row["Link to board agenda site"] = row.get("source_url", "")
        return sorted(rows, key=lambda r: (r.get("gov_body", "").casefold(), r.get("meeting_date") is None, r.get("meeting_date") or "", r.get("document_id", ""), r.get("item_version_id", "")))

    def export_run(self, run_id: str, *, spreadsheet_safe: bool = True, scope: str = "run", query: dict[str, Any] | None = None,
                   publish: bool = False, owner_token: str | None = None) -> dict[str, Any]:
        from ..storage.db import connect
        from ..domain import new_id
        snapshot_at = utc_now()
        with connect(self.repo.paths) as read_conn:
            read_conn.execute("BEGIN")
            run_row = read_conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run_row: raise KeyError("run not found")
            if run_row["reason_code"] == "run_deadline_exceeded":
                raise RuntimeError("run_deadline_exceeded")
            run = dict(run_row)
            rows = self._rows(run_id, include_excluded=False, scope=scope, query=query, conn=read_conn)
            revision = int(read_conn.execute("SELECT data_revision FROM app_state WHERE id=1").fetchone()[0])
            source_rows = [dict(row) for row in read_conn.execute("SELECT * FROM run_sources WHERE run_id=? ORDER BY source_id,id", (run_id,)).fetchall()]
            read_conn.commit()
        export_id = new_id()
        review_at = utc_now()
        with transaction(self.repo.paths) as conn:
            conn.execute("INSERT INTO exports(id,run_id,query_json,data_revision,review_snapshot_at,state,created_at) VALUES(?,?,?,?,?,?,?)", (export_id, run_id, dumps(query or {"scope": scope}), revision, review_at, "preparing", review_at))
        staging = self.repo.paths.work / run_id / f"export-{export_id}"
        staging.mkdir(parents=True, exist_ok=True)
        files: dict[str, dict[str, Any]] = {}
        try:
            by_priority = {p: [r for r in rows if r["importance"] == p] for p in ("high", "medium", "low")}
            for name, values in [("high", by_priority["high"]), ("medium", by_priority["medium"]), ("low", by_priority["low"]), ("all", rows)]:
                path = staging / f"{name}.csv"
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=HEADERS, extrasaction="ignore", lineterminator="\n")
                    writer.writeheader()
                    for row in values:
                        writer.writerow({field: _safe(row.get(field, ""), spreadsheet_safe) for field in HEADERS})
                    handle.flush(); os.fsync(handle.fileno())
                content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                files[name] = {"path": f"{name}.csv", "sha256": content_hash, "row_count": len(values)}
            manifest = {"export_id": export_id, "run_id": run_id, "schema_version": "csv-v1", "format": "spreadsheet_safe" if spreadsheet_safe else "raw", "created_at": review_at, "data_revision": revision, "review_snapshot_at": review_at, "publication_state": "published" if publish else "none", "headers": HEADERS, "files": files,
                        "coverage": {"complete": sum(1 for row in source_rows if row["state"] in {"success", "no_results"}), "total": len(source_rows), "inherited": sum(1 for row in source_rows if row["inherited_from_id"]), "failed": sum(1 for row in source_rows if row["state"] in {"failed", "partial", "interrupted"})},
                        "sources": [{"source_id": row["source_id"], "observed_at": row["observed_at"], "state": row["state"]} for row in source_rows], "settings_snapshot": loads(run["settings_snapshot_json"], {}), "policy_version_id": run["policy_version_id"]}
            (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            final = self.repo.paths.exports / export_id
            if final.exists(): raise FileExistsError(final)
            staging.replace(final)
            with transaction(self.repo.paths) as conn:
                live = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                expected_owner = owner_token or run["owner_token"]
                publishable = (
                    live
                    and live["status"] in {"success", "no_results"}
                    and live["reason_code"] is None
                    and (not publish or live["publication_state"] == "pending")
                    and (not publish or live["owner_token"] == expected_owner)
                )
                if not publishable:
                    raise RuntimeError("run_deadline_exceeded")
                conn.execute("UPDATE exports SET state='ready',relpath=?,manifest_json=? WHERE id=?", (str(final.relative_to(self.repo.paths.root)), dumps(manifest), export_id))
                if publish:
                    # Publication is ordered by the durable run sequence as a
                    # second line of defense against an old publisher.  This
                    # check and the pointer update share one SQLite write
                    # transaction with the live run-state check above.
                    pointer = conn.execute("SELECT published_run_id FROM app_state WHERE id=1").fetchone()[0]
                    if pointer and pointer != run_id:
                        pointer_seq = conn.execute("SELECT created_seq FROM runs WHERE id=?", (pointer,)).fetchone()
                        if pointer_seq and pointer_seq[0] >= live["created_seq"]:
                            raise RuntimeError("stale_publication")
                    marked = conn.execute(
                        "UPDATE runs SET phase='done',finished_at=COALESCE(finished_at,?),publication_state='published' "
                        "WHERE id=? AND status IN ('success','no_results') AND reason_code IS NULL "
                        "AND publication_state='pending' AND owner_token=?",
                        (utc_now(), run_id, expected_owner),
                    )
                    if marked.rowcount != 1:
                        raise RuntimeError("publication_lost")
                    full_success = run["kind"] == "full"
                    pointer_update = conn.execute(
                        "UPDATE app_state SET published_run_id=?,last_complete_run_id=?,last_full_success_run_id=CASE WHEN ? THEN ? ELSE last_full_success_run_id END,latest_export_id=?,data_revision=data_revision+1 "
                        "WHERE id=1 AND (published_run_id IS NULL OR published_run_id=? OR published_run_id IN (SELECT id FROM runs WHERE created_seq<?))",
                        (run_id, run_id, int(full_success), run_id, export_id, run_id, live["created_seq"]),
                    )
                    if pointer_update.rowcount != 1:
                        raise RuntimeError("stale_publication")
            return {"export_id": export_id, "manifest": manifest, "path": str(final), "manifest_path": str(final / "manifest.json")}
        except Exception as exc:
            try: shutil.rmtree(staging)
            except OSError: pass
            with transaction(self.repo.paths) as conn:
                conn.execute("UPDATE exports SET state='failed',error_json=? WHERE id=?", (dumps({"code": "export_failed", "message": str(exc)}), export_id))
                live = conn.execute("SELECT status,reason_code,publication_state,owner_token FROM runs WHERE id=?", (run_id,)).fetchone()
                expected_owner = owner_token or run["owner_token"]
                if not publish:
                    # Preserve the established manual-generation contract:
                    # a failed manual attempt marks only that run's export
                    # state as preserved and never moves app_state.
                    conn.execute("UPDATE runs SET publication_state='preserved' WHERE id=?", (run_id,))
                elif live and live["reason_code"] is None and str(exc) not in {"stale_publication", "publication_lost"}:
                    # A publication failure may only terminalize the live
                    # publisher that still owns a pending publication.  In
                    # particular, a deadline winner cannot be overwritten by
                    # this failure path.
                    conn.execute(
                        "UPDATE runs SET status='failed',reason_code='publication_failed',publication_state='preserved',phase='done',finished_at=? "
                        "WHERE id=? AND status IN ('success','no_results') AND reason_code IS NULL AND publication_state='pending' AND owner_token=?",
                        (utc_now(), run_id, expected_owner),
                    )
                elif live and live["reason_code"] is None and live["publication_state"] == "pending" and live["owner_token"] == expected_owner:
                    # A failed manual generation did not alter the durable
                    # publication pointer. Preserve a pending worker attempt
                    # when publication was rejected as stale/lost.
                    conn.execute("UPDATE runs SET publication_state='preserved' WHERE id=? AND status IN ('success','no_results') AND reason_code IS NULL AND publication_state='pending' AND owner_token=?", (run_id, expected_owner))
            raise

    def get(self, export_id: str) -> dict[str, Any] | None:
        row = self.repo.row("SELECT * FROM exports WHERE id=?", (export_id,))
        if not row: return None
        result = dict(row); result["manifest"] = loads(result.pop("manifest_json"), None); return result

    def file(self, export_id: str, name: str) -> tuple[Path, dict[str, Any]]:
        if name not in {"high.csv", "medium.csv", "low.csv", "all.csv"}: raise KeyError("file")
        record = self.get(export_id)
        if not record: raise KeyError("export")
        if record["state"] != "ready": raise RuntimeError("export_not_ready")
        path = self.repo.paths.exports / export_id / name
        if not path.is_file(): raise FileNotFoundError(path)
        return path, record
