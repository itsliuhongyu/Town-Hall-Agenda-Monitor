from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager, nullcontext
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from ..config import ConfigSnapshot, DataPaths, default_settings, identity_key, normalize_text, parse_utc, utc_now, validate_settings
from ..domain import AgendaCandidate, DateWindow, ErrorInfo, SourceSnapshot, new_id
from .db import apply_migrations, connect, transaction


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(value: str | None, fallback: Any = None) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


class Repository:
    def __init__(self, data_dir: str | Path | DataPaths):
        self.paths = data_dir if isinstance(data_dir, DataPaths) else DataPaths(Path(data_dir))
        apply_migrations(self.paths)

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.paths)
        try:
            yield conn
        finally:
            conn.close()

    def row(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self.read() as conn:
            return conn.execute(sql, params).fetchone()

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.read() as conn:
            return conn.execute(sql, params).fetchall()

    def data_revision(self, conn: sqlite3.Connection | None = None) -> int:
        if conn is not None:
            return int(conn.execute("SELECT data_revision FROM app_state WHERE id=1").fetchone()[0])
        row = self.row("SELECT data_revision FROM app_state WHERE id=1")
        return int(row[0])

    @staticmethod
    def bump_revision(conn: sqlite3.Connection) -> int:
        conn.execute("UPDATE app_state SET data_revision=data_revision+1 WHERE id=1")
        return int(conn.execute("SELECT data_revision FROM app_state WHERE id=1").fetchone()[0])

    def get_settings(self) -> tuple[int, dict[str, Any]]:
        row = self.row("SELECT revision,values_json FROM settings WHERE id=1")
        return int(row[0]), loads(row[1], default_settings())

    def get_policy(self, policy_id: str | None = None) -> dict[str, Any]:
        with self.read() as conn:
            if policy_id is None:
                row = conn.execute("SELECT p.* FROM policy_versions p JOIN app_state a ON a.current_policy_id=p.id WHERE a.id=1").fetchone()
            else:
                row = conn.execute("SELECT * FROM policy_versions WHERE id=?", (policy_id,)).fetchone()
            if not row:
                raise KeyError("policy not found")
            return self._policy_row(row)

    @staticmethod
    def _policy_row(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "version_no": row["version_no"], "rules": loads(row["rules_json"], []),
                "removals": loads(row["removals_json"], []), "strategy": row["strategy"],
                "thresholds": loads(row["thresholds_json"], {"high": 80, "medium": 40}),
                "change_reason": row["change_reason"], "created_at": row["created_at"],
                "previous_id": row["previous_id"], "undo_of": row["undo_of"]}

    def snapshot(self) -> ConfigSnapshot:
        revision, values = self.get_settings()
        policy = self.get_policy()
        return ConfigSnapshot(values=values, policy=policy, revision=revision, policy_version_id=policy["id"])

    def save_settings(self, expected_revision: int, values: dict[str, Any], *, inventory: list[dict[str, Any]] | None = None,
                      conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        if type(expected_revision) is not int: raise ValidationError("expected_revision must be an integer")
        values = validate_settings(values)
        owns_transaction = conn is None
        context = transaction(self.paths) if owns_transaction else nullcontext(conn)
        with context as conn:
            row = conn.execute("SELECT revision,values_json FROM settings WHERE id=1").fetchone()
            if row[0] != expected_revision:
                raise ConflictError("settings_conflict", {"current_revision": row[0]})
            selected = values.get("selected_model")
            inventory_state = conn.execute("SELECT state,models_json,endpoint_url FROM model_inventory WHERE id=1").fetchone()
            inventory_endpoint = inventory_state[2] if inventory_state else None
            current_url = loads(row[1], {}).get("ollama_base_url")
            if selected and inventory is not None:
                old_values = loads(row[1], {})
                old_selected = old_values.get("selected_model") or {}
                # Offline reuse is allowed only for the exact prior selection.
                # A changed verification timestamp, endpoint, or metadata is
                # a new selection and must be rejected against stale state.
                unchanged_selection = selected == old_selected
                verified_selection = any(m.get("name") == selected.get("name") and m.get("digest") == selected.get("digest") for m in inventory)
                can_retain_offline = unchanged_selection and current_url == values["ollama_base_url"] and inventory_state and inventory_state[0] in {"offline", "unknown"}
                # Validate against the endpoint being saved, not merely the
                # endpoint that was current before this CAS write.
                inventory_is_current = bool(inventory_state and inventory_state[0] == "online" and inventory_endpoint == values["ollama_base_url"])
                if not can_retain_offline and (not inventory_is_current or not verified_selection):
                    raise ValidationError("selected model is not present in the latest inventory")
            if values["ollama_base_url"] != current_url:
                conn.execute("UPDATE model_inventory SET state='unknown',models_json='[]',endpoint_url=?,error_json=NULL WHERE id=1", (values["ollama_base_url"],))
            new_revision = expected_revision + 1
            conn.execute("UPDATE settings SET revision=?,values_json=?,updated_at=? WHERE id=1", (new_revision, dumps(values), utc_now()))
            self.bump_revision(conn)
        return {"revision": new_revision, "values": values}

    def update_inventory(self, *, state: str, models: list[dict[str, Any]], error: dict[str, Any] | None = None) -> dict[str, Any]:
        if state not in {"unknown", "online", "offline"}:
            raise ValueError("invalid inventory state")
        now = utc_now()
        with transaction(self.paths) as conn:
            settings_url = loads(conn.execute("SELECT values_json FROM settings WHERE id=1").fetchone()[0], {}).get("ollama_base_url")
            conn.execute("UPDATE model_inventory SET state=?,models_json=?,observed_at=?,last_attempt_at=?,error_json=? WHERE id=1",
                         (state, dumps(models), now if state == "online" else conn.execute("SELECT observed_at FROM model_inventory WHERE id=1").fetchone()[0], now, dumps(error) if error else None))
            conn.execute("UPDATE model_inventory SET endpoint_url=? WHERE id=1", (settings_url,))
            self.bump_revision(conn)
        return self.models_state()

    def models_state(self) -> dict[str, Any]:
        row = self.row("SELECT * FROM model_inventory WHERE id=1")
        settings = self.get_settings()[1]
        selected = settings.get("selected_model")
        models = loads(row["models_json"], [])
        selection = "none" if not selected else "unverified"
        if selected and row["state"] == "online":
            matches = [m for m in models if m.get("name") == selected.get("name")]
            if not matches: selection = "removed"
            elif any(m.get("digest") == selected.get("digest") for m in matches): selection = "available"
            else: selection = "digest_changed"
        return {"connection": row["state"], "selection": selection, "models": models, "selected_model": selected,
                "observed_at": row["observed_at"], "last_attempt_at": row["last_attempt_at"],
                "stale": row["state"] != "online", "endpoint_url": row["endpoint_url"], "error": loads(row["error_json"], None)}

    def create_source(self, *, platform: str, name: str, collection_url: str, timezone: str,
                      timezone_origin: str = "explicit", config: dict[str, Any] | None = None,
                      source_id: str | None = None, enabled: bool = True) -> dict[str, Any]:
        from ..config import canonical_url, ensure_timezone
        url = canonical_url(collection_url)
        ensure_timezone(timezone)
        key = identity_key(platform, url, normalize_text(name))
        now = utc_now()
        with transaction(self.paths) as conn:
            old = conn.execute("SELECT * FROM sources WHERE identity_key=?", (key,)).fetchone()
            if old:
                return dict(old)
            source_id = source_id or new_id()
            conn.execute("INSERT INTO sources(id,identity_key,platform,name,collection_url,timezone,timezone_origin,enabled,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (source_id, key, platform, name.strip(), url, timezone, timezone_origin, int(enabled), dumps(config or {}), now, now))
            self.bump_revision(conn)
            return dict(conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone())

    def list_sources(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sources" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY name COLLATE NOCASE,id"
        return [dict(row) for row in self.rows(sql)]

    def update_source(self, source_id: str, expected_revision: int, changes: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        if type(expected_revision) is not int: raise ValidationError("expected_revision must be an integer")
        allowed = {"name", "collection_url", "timezone", "enabled"}
        if set(changes) - allowed:
            raise ValidationError("unknown source field")
        if "enabled" in changes and type(changes["enabled"]) is not bool:
            raise ValidationError("enabled must be a boolean")
        owns_transaction = conn is None
        context = transaction(self.paths) if owns_transaction else nullcontext(conn)
        with context as conn:
            row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if not row: raise NotFoundError("source")
            if row["revision"] != expected_revision: raise ConflictError("source_conflict", {"current_revision": row["revision"]})
            values = dict(row)
            values.update(changes)
            if "collection_url" in changes:
                from ..config import canonical_url
                values["collection_url"] = canonical_url(values["collection_url"])
            if "timezone" in changes:
                from ..config import ensure_timezone
                ensure_timezone(values["timezone"])
            values["revision"] += 1; values["updated_at"] = utc_now()
            conn.execute("UPDATE sources SET name=?,collection_url=?,timezone=?,enabled=?,revision=?,updated_at=? WHERE id=?",
                         (values["name"], values["collection_url"], values["timezone"], int(values["enabled"]), values["revision"], values["updated_at"], source_id))
            self.bump_revision(conn)
            return dict(conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone())

    def ensure_meeting_document(self, conn: sqlite3.Connection, source: SourceSnapshot, candidate: AgendaCandidate) -> tuple[str, str]:
        meeting_key = candidate.native_meeting_key or identity_key(source.source_id, candidate.local_date.isoformat() if candidate.local_date else "", candidate.meeting_heading, candidate.local_datetime or "", candidate.original_url or candidate.source_url)
        meeting_kind = "native" if candidate.native_meeting_key else "fallback"
        meeting = conn.execute("SELECT id FROM meetings WHERE source_id=? AND identity_key=?", (source.source_id, meeting_key)).fetchone()
        now = utc_now()
        if meeting:
            meeting_id = meeting[0]
            conn.execute("UPDATE meetings SET last_seen_at=? WHERE id=?", (now, meeting_id))
        else:
            meeting_id = new_id()
            conn.execute("INSERT INTO meetings(id,source_id,identity_key,identity_kind,native_key,title,local_date,local_datetime,timezone,raw_date,time_quality,status,created_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (meeting_id, source.source_id, meeting_key, meeting_kind, candidate.native_meeting_key, candidate.meeting_heading,
                          candidate.local_date.isoformat() if candidate.local_date else None, candidate.local_datetime, source.timezone,
                          candidate.raw_date, "exact" if candidate.local_datetime else ("date_only" if candidate.local_date else "unknown"),
                          "scheduled" if candidate.local_date else "unknown", now, now))
        doc_key = candidate.native_document_key or identity_key(candidate.original_url or "", candidate.document_kind)
        doc = conn.execute("SELECT id FROM documents WHERE meeting_id=? AND identity_key=?", (meeting_id, doc_key)).fetchone()
        if doc:
            document_id = doc[0]
            conn.execute("UPDATE documents SET last_seen_at=? WHERE id=?", (now, document_id))
        else:
            document_id = new_id()
            conn.execute("INSERT INTO documents(id,meeting_id,identity_key,native_key,kind,original_url,source_url,display_filename,created_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (document_id, meeting_id, doc_key, candidate.native_document_key, candidate.document_kind,
                          candidate.original_url, candidate.source_url, candidate.suggested_filename, now, now))
        return meeting_id, document_id

    def ensure_document_version(self, conn: sqlite3.Connection, document_id: str, *, sha256: str | None, blob_relpath: str | None,
                                byte_size: int | None, media_type: str | None, fetched_url: str | None,
                                original_state: str = "available", retained_text: str | None = None,
                                read_status: str = "pending", read_error: dict[str, Any] | None = None,
                                reader_version: str | None = None) -> tuple[str, bool]:
        if sha256:
            row = conn.execute("SELECT id FROM document_versions WHERE document_id=? AND sha256=?", (document_id, sha256)).fetchone()
            if row: return row[0], False
        version_no = int(conn.execute("SELECT COALESCE(MAX(version_no),0)+1 FROM document_versions WHERE document_id=?", (document_id,)).fetchone()[0])
        version_id = new_id()
        text_sha = hashlib.sha256(retained_text.encode("utf-8")).hexdigest() if retained_text is not None else None
        conn.execute("INSERT INTO document_versions(id,document_id,version_no,sha256,blob_relpath,byte_size,media_type,fetched_url,fetched_at,original_state,retained_text,reader_version,text_sha256,read_status,read_error_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (version_id, document_id, version_no, sha256, blob_relpath, byte_size, media_type, fetched_url, utc_now(), original_state,
                      retained_text, reader_version, text_sha, read_status, dumps(read_error) if read_error else None, utc_now()))
        return version_id, True

    def create_analysis(self, conn: sqlite3.Connection, version_id: str, *, extraction_key: str, model_name: str | None,
                        model_digest: str | None, reader_version: str, extractor_version: str, prompt_hash: str,
                        parameters: dict[str, Any], snapshot: dict[str, Any], state: str, cache_hit: bool,
                        error: dict[str, Any] | None = None) -> str:
        existing = conn.execute("SELECT id FROM analyses WHERE document_version_id=? AND extraction_key=?", (version_id, extraction_key)).fetchone()
        if existing: return existing[0]
        analysis_id = new_id(); now = utc_now()
        conn.execute("INSERT INTO analyses(id,document_version_id,extraction_key,model_name,model_digest,reader_version,extractor_version,prompt_hash,parameters_json,settings_snapshot_json,state,cache_hit,started_at,finished_at,error_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (analysis_id, version_id, extraction_key, model_name, model_digest, reader_version, extractor_version, prompt_hash, dumps(parameters), dumps(snapshot), state, int(cache_hit), now, now, dumps(error) if error else None))
        return analysis_id

    def create_item_version(self, conn: sqlite3.Connection, document_id: str, version_id: str, *, original_text: str,
                             title: str, priority: str, reason: str | None, anchor: dict[str, Any],
                             analysis_id: str, ordinal: int, origin: str = "llm", model_priority: str | None = None,
                             identity: str | None = None, reuse_existing: bool = False) -> str:
        evidence_hash = hashlib.sha256(normalize_text(original_text).encode("utf-8")).hexdigest()
        if identity is None:
            normalized = normalize_text(original_text)
            # A unique exact evidence quote is stable across extraction
            # reordering and inserted earlier items. Ambiguous duplicate
            # quotes retain an occurrence discriminator so two identical
            # agenda lines do not collapse into one business item.
            if anchor.get("status") == "exact":
                # A verified span is the business identity. Including the
                # span keeps two identical sentences in one document
                # separate, while leaving a unique quote stable when an
                # earlier item is inserted or the model reorders output.
                if isinstance(anchor.get("start"), int) and isinstance(anchor.get("end"), int):
                    identity = identity_key("evidence", normalized, "span", anchor["start"], anchor["end"])
                else:
                    identity = identity_key("evidence", normalized)
            else:
                identity = identity_key("analysis", analysis_id, "evidence", normalized, "occurrence", str(ordinal))
            if anchor.get("status") == "exact":
                prior = conn.execute("""SELECT i.id,i.identity_key,iv.anchor_json,iv.document_version_id
                    FROM items i JOIN item_versions iv ON iv.item_id=i.id
                    WHERE i.document_id=? AND iv.evidence_hash=? ORDER BY iv.created_at""", (document_id, evidence_hash)).fetchall()
                same_span = next((row for row in prior if loads(row["anchor_json"], {}).get("status") == "exact" and loads(row["anchor_json"], {}).get("start") == anchor.get("start") and loads(row["anchor_json"], {}).get("end") == anchor.get("end")), None)
                if same_span:
                    identity = same_span["identity_key"]
                elif len({row["id"] for row in prior}) == 1 and all(loads(row["anchor_json"], {}).get("status") == "exact" for row in prior):
                    prior_anchor = loads(prior[0]["anchor_json"], {})
                    text_row = conn.execute("SELECT retained_text FROM document_versions WHERE id=?", (version_id,)).fetchone()
                    retained = text_row[0] if text_row else None
                    occurrences = retained.count(normalized) if isinstance(retained, str) and normalized else 0
                    # A changed span is a new occurrence unless the retained
                    # source proves that the quote is unique in this version.
                    if prior[0]["document_version_id"] != version_id or occurrences == 1:
                        identity = prior[0]["identity_key"]
        row = conn.execute("SELECT id FROM items WHERE document_id=? AND identity_key=?", (document_id, identity)).fetchone()
        item_id = row[0] if row else new_id()
        if not row:
            identity_kind = "exact_anchor" if anchor.get("status") == "exact" else "analysis_scoped"
            conn.execute("INSERT INTO items(id,document_id,identity_key,identity_kind,created_at) VALUES(?,?,?,?,?)", (item_id, document_id, identity, identity_kind, utc_now()))
        iv = conn.execute("SELECT id FROM item_versions WHERE item_id=? AND document_version_id=? AND evidence_hash=?", (item_id, version_id, evidence_hash)).fetchone()
        if not iv and reuse_existing:
            # Legacy bundles often have no retained original bytes for the
            # unchanged rows. Reusing the exact prior observation keeps a
            # human note/priority attached while a sibling append is imported;
            # normal live ingestion still creates a new item version.
            iv = conn.execute("SELECT iv.id FROM item_versions iv WHERE iv.item_id=? AND iv.evidence_hash=? ORDER BY iv.created_at DESC LIMIT 1", (item_id, evidence_hash)).fetchone()
            if iv:
                item_version_id = iv[0]
                conn.execute("INSERT OR IGNORE INTO analysis_items(analysis_id,item_version_id,ordinal,title,model_priority,model_reason,extraction_origin,suggested_priority,suggestion_source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (analysis_id, item_version_id, ordinal, title, model_priority, reason, origin, priority, "legacy" if origin == "legacy" else ("model" if model_priority else "builtin"), utc_now()))
                return item_version_id
        item_version_id = iv[0] if iv else new_id()
        if not iv:
            conn.execute("INSERT INTO item_versions(id,item_id,document_version_id,evidence_hash,original_text,anchor_json,anchor_status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                         (item_version_id, item_id, version_id, evidence_hash, original_text, dumps(anchor), anchor.get("status", "exact"), utc_now()))
            previous = conn.execute("SELECT 1 FROM item_versions WHERE item_id=? AND id<>? LIMIT 1", (item_id, item_version_id)).fetchone()
            conn.execute("INSERT INTO review_state(item_version_id,state,human_priority,note,row_version,updated_at) VALUES(?,?,?,?,?,?)", (item_version_id, "needs_review" if previous else "unreviewed", None, "", 1, utc_now()))
        conn.execute("INSERT OR IGNORE INTO analysis_items(analysis_id,item_version_id,ordinal,title,model_priority,model_reason,extraction_origin,suggested_priority,suggestion_source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (analysis_id, item_version_id, ordinal, title, model_priority, reason, origin, priority, "legacy" if origin == "legacy" else ("model" if model_priority else "builtin"), utc_now()))
        return item_version_id

    def add_run_item(self, conn: sqlite3.Connection, *, run_id: str, item_version_id: str, analysis_id: str, policy_version_id: str,
                     policy_priority: str, proposed_priority: str, decision_source: str, matched_rules: list[dict[str, Any]],
                     included: bool, exclusion_reason: str | None = None, source_run_id: str | None = None) -> None:
        conn.execute("INSERT OR REPLACE INTO run_items(run_id,item_version_id,analysis_id,policy_version_id,policy_priority,proposed_priority,decision_source,matched_rules_json,included,exclusion_reason,source_run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (run_id, item_version_id, analysis_id, policy_version_id, policy_priority, proposed_priority, decision_source, dumps(matched_rules), int(included), exclusion_reason, source_run_id or run_id))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.row("SELECT * FROM runs WHERE id=?", (run_id,))
        return dict(row) if row else None

    def create_run(self, *, kind: str, snapshot: ConfigSnapshot, sources: list[dict[str, Any]], parent_run_id: str | None = None,
                   owner_token: str | None = None, status: str = "running", reason_code: str | None = None,
                   action_scope: str | None = None, action_key: str | None = None, action_body_hash: str | None = None,
                   run_id: str | None = None) -> str:
        now = utc_now(); run_id = run_id or new_id(); owner_token = owner_token or new_id()
        with transaction(self.paths) as conn:
            active = conn.execute("SELECT id FROM runs WHERE status='running'").fetchone()
            if active and status == "running": raise ConflictError("active_run", {"active_run_id": active[0]})
            sequence = int(conn.execute("SELECT COALESCE(MAX(created_seq),0)+1 FROM runs").fetchone()[0])
            timeout = int(snapshot.values.get("run_timeout_minutes", 60))
            deadline = (parse_utc(now) + __import__("datetime").timedelta(minutes=timeout)).isoformat().replace("+00:00", "Z")
            conn.execute("INSERT INTO runs(id,parent_run_id,kind,status,phase,reason_code,requested_at,started_at,heartbeat_at,owner_token,worker_pid,settings_snapshot_json,policy_version_id,source_snapshot_json,total_sources,publication_state,revision,created_seq,deadline_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (run_id, parent_run_id, kind, status, "discovery", reason_code, now, now, now, owner_token, None, snapshot.json(), snapshot.policy_version_id, dumps(sources), len(sources), "none", 1, sequence, deadline))
            for source in sources:
                window = source.get("window") or {}
                conn.execute("INSERT INTO run_sources(id,run_id,source_id,source_snapshot_json,window_start,window_end,timezone,state,discovery_state) VALUES(?,?,?,?,?,?,?,?,?)",
                             (new_id(), run_id, source["id"], dumps(source), window["start"], window["end"], source["timezone"], "pending", "pending"))
            if action_scope and action_key and action_body_hash:
                result = {"run_id": run_id, "status": "running" if status == "running" else status, "status_url": f"/api/v1/runs/{run_id}"}
                existing_action = conn.execute("SELECT body_hash FROM action_requests WHERE scope=? AND idempotency_key=?", (action_scope, action_key)).fetchone()
                if existing_action:
                    if existing_action[0] != action_body_hash:
                        raise sqlite3.IntegrityError("idempotency body hash mismatch")
                    conn.execute("UPDATE action_requests SET result_json=?,http_status=202 WHERE scope=? AND idempotency_key=?", (dumps(result), action_scope, action_key))
                else:
                    conn.execute("INSERT INTO action_requests(id,scope,idempotency_key,body_hash,result_json,http_status,created_at) VALUES(?,?,?,?,?,?,?)",
                                 (new_id(), action_scope, action_key, action_body_hash, dumps(result), 202, now))
        return run_id

    def update_run(self, run_id: str, **changes: Any) -> None:
        allowed = {"status", "phase", "reason_code", "finished_at", "heartbeat_at", "error_json", "publication_state", "revision"}
        if set(changes) - allowed: raise ValueError("invalid run update")
        if not changes: return
        if "error_json" in changes and changes["error_json"] is not None and not isinstance(changes["error_json"], str): changes["error_json"] = dumps(changes["error_json"])
        if "heartbeat_at" not in changes: changes["heartbeat_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in changes)
        with transaction(self.paths) as conn:
            conn.execute(f"UPDATE runs SET {assignments} WHERE id=?", tuple(changes.values()) + (run_id,))

    def finalize_run(self, run_id: str, *, owner_token: str, status: str, phase: str,
                     reason_code: str | None = None, finished_at: str | None = None,
                     publication_state: str = "preserved",
                     error_json: dict[str, Any] | str | None = None) -> tuple[bool, dict[str, Any] | None]:
        """Compare-and-set the worker's final transition.

        Deadline terminalization and worker finalization must have one durable
        winner.  The owner token prevents a stale worker from committing a
        decision after ownership has changed; the deadline predicate prevents
        a worker that reaches this boundary after the persisted wall-clock
        deadline from becoming publishable.  The returned live row lets the
        caller preserve the winner's durable result without a second decision
        write.
        """
        if status not in {"success", "no_results", "partial", "failed", "interrupted"}:
            raise ValueError("invalid final run status")
        if phase not in {"publish", "done"}:
            raise ValueError("invalid final run phase")
        if publication_state not in {"pending", "preserved", "none", "failed", "published"}:
            raise ValueError("invalid publication state")
        if error_json is not None and not isinstance(error_json, str):
            error_json = dumps(error_json)
        finished_at = finished_at or utc_now()
        with transaction(self.paths) as conn:
            live = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not live:
                return False, None
            updated = conn.execute(
                "UPDATE runs SET status=?,phase=?,reason_code=?,error_json=?,finished_at=?,heartbeat_at=?,publication_state=? "
                "WHERE id=? AND status='running' AND owner_token=? AND (deadline_at IS NULL OR deadline_at>?)",
                (status, phase, reason_code, error_json, finished_at, finished_at, publication_state,
                 run_id, owner_token, utc_now()),
            )
            live = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return updated.rowcount == 1, (dict(live) if live else None)

    def run_sources(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows("SELECT * FROM run_sources WHERE run_id=? ORDER BY id", (run_id,))]

    def run_documents(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows("SELECT d.*,rs.run_id,rs.source_id FROM run_documents d JOIN run_sources rs ON rs.id=d.run_source_id WHERE rs.run_id=? ORDER BY d.id", (run_id,))]

    def current_published_run(self) -> str | None:
        row = self.row("SELECT published_run_id FROM app_state WHERE id=1")
        return row[0] if row else None

    def overview(self) -> dict[str, Any]:
        state = self.row("SELECT * FROM app_state WHERE id=1")
        active = self.row("SELECT * FROM runs WHERE status='running'")
        latest = self.row("SELECT * FROM runs ORDER BY COALESCE(created_seq,0) DESC,started_at DESC,id DESC LIMIT 1")
        revision, settings = self.get_settings()
        counts = {row["state"]: row["count"] for row in self.rows("SELECT state,COUNT(*) count FROM review_state GROUP BY state")}
        source_initialization = self.row("SELECT * FROM source_initializations ORDER BY rowid DESC LIMIT 1")
        source_init = None
        if source_initialization:
            source_init = dict(source_initialization)
            source_init["summary"] = loads(source_init.pop("summary_json"), {})
            source_init["error"] = loads(source_init.pop("error_json"), None)
        configured = self.row("SELECT COUNT(*) FROM sources")[0]
        enabled = self.row("SELECT COUNT(*) FROM sources WHERE enabled=1")[0]
        return {"active_run": dict(active) if active else None, "latest_attempt": dict(latest) if latest else None,
                "last_complete_publication": state["published_run_id"], "last_full_success": state["last_full_success_run_id"],
                "source_count": enabled, "configured_source_count": configured, "source_initialization": source_init, "review_counts": counts,
                "settings_summary": {"revision": revision, "days_back": settings["days_back"], "days_forward": settings["days_forward"]},
                "data_revision": state["data_revision"]}

    def set_publication(self, conn: sqlite3.Connection, run_id: str, export_id: str | None, *, full_success: bool) -> None:
        conn.execute("UPDATE runs SET status=CASE WHEN status='running' THEN 'success' ELSE status END,phase='done',finished_at=?,heartbeat_at=?,publication_state='published' WHERE id=?", (utc_now(), utc_now(), run_id))
        conn.execute("UPDATE app_state SET published_run_id=?,last_complete_run_id=?,last_full_success_run_id=CASE WHEN ? THEN ? ELSE last_full_success_run_id END,latest_export_id=?,data_revision=data_revision+1 WHERE id=1",
                     (run_id, run_id, int(full_success), run_id, export_id))

    def item_detail(self, item_version_id: str, run_id: str | None = None) -> dict[str, Any] | None:
        params: list[Any] = []
        run_clause = ""
        if run_id:
            run_clause = " AND ri.run_id=?"; params.append(run_id)
        params.append(item_version_id)
        sql = """SELECT iv.id item_version_id,i.id item_id,iv.original_text,iv.anchor_json,iv.anchor_status,
                 ai.title,ai.model_priority,ai.model_reason,ai.suggested_priority,ai.suggestion_source,ai.analysis_id,
                 ri.run_id,ri.policy_priority,ri.proposed_priority,ri.decision_source,ri.matched_rules_json,ri.included,ri.exclusion_reason,
                 rs.state review_state,rs.human_priority,rs.note,rs.row_version,rs.last_event_id,
                 m.title meeting_title,m.local_date,m.local_datetime,m.timezone meeting_timezone,
                 s.id source_id,s.name source_name,s.collection_url, d.id document_id,d.display_filename,d.original_url,d.source_url,dv.id document_version_id,dv.blob_relpath,dv.original_state,dv.media_type
                 FROM item_versions iv JOIN items i ON i.id=iv.item_id JOIN analysis_items ai ON ai.item_version_id=iv.id
                 JOIN review_state rs ON rs.item_version_id=iv.id JOIN analyses a ON a.id=ai.analysis_id
                 JOIN document_versions dv ON dv.id=iv.document_version_id JOIN documents d ON d.id=i.document_id JOIN meetings m ON m.id=d.meeting_id JOIN sources s ON s.id=m.source_id
                 LEFT JOIN run_items ri ON ri.analysis_id=ai.analysis_id AND ri.item_version_id=iv.id
                 LEFT JOIN runs r ON r.id=ri.run_id""" + (" WHERE ri.run_id=? AND ri.item_version_id=?" if run_id else " WHERE ri.item_version_id=?") + " ORDER BY r.created_seq DESC,r.started_at DESC,r.id DESC,ai.created_at DESC LIMIT 1"
        # The run constraint belongs in the run_items membership predicate. A
        # LEFT JOIN plus a filter on runs can otherwise return an unrelated
        # historical observation for a requested run.
        if run_id:
            params = [run_id, item_version_id]
        else:
            params = [item_version_id]
        row = self.row(sql, tuple(params))
        if not row: return None
        result = dict(row)
        result["anchor"] = loads(result.pop("anchor_json"), {})
        result["matched_rules"] = loads(result.pop("matched_rules_json"), [])
        result["final_priority"] = result["human_priority"] or result["proposed_priority"] or result["suggested_priority"]
        result["priority_source"] = "human" if result["human_priority"] else result["decision_source"]
        result["model_suggestion"] = {"priority": result["model_priority"], "reason": result["model_reason"], "source": result["suggestion_source"]}
        result["policy_decision"] = {"priority": result["policy_priority"], "proposed_priority": result["proposed_priority"], "source": result["decision_source"], "matched_rules": result["matched_rules"]}
        result["review"] = {"state": result.pop("review_state"), "human_priority": result["human_priority"], "note": result["note"],
                             "row_version": result["row_version"], "last_event_id": result["last_event_id"]}
        blob_path = self.paths.root / result["blob_relpath"] if result.get("blob_relpath") else None
        original_available = bool(blob_path and blob_path.is_file())
        result["links"] = {"original": f"/api/v1/documents/{result['document_version_id']}/content" if original_available else None,
                           "text": f"/api/v1/documents/{result['document_version_id']}/text", "download_url": result.get("original_url"), "source_url": result.get("source_url")}
        result["original_available"] = original_available
        result["review_history"] = [dict(event) for event in self.rows("SELECT id,action,item_revision,actor,created_at,before_json,after_json,undo_of FROM review_events WHERE item_version_id=? ORDER BY item_revision DESC", (item_version_id,))]
        for event in result["review_history"]:
            event["before"] = loads(event.pop("before_json"), {})
            event["after"] = loads(event.pop("after_json"), {})
        result["previous_reviews"] = [dict(previous) for previous in self.rows("""SELECT rs.item_version_id,rs.state,rs.human_priority,rs.note,iv.document_version_id,dv.version_no
            FROM item_versions iv JOIN review_state rs ON rs.item_version_id=iv.id
            JOIN document_versions dv ON dv.id=iv.document_version_id
            WHERE iv.item_id=? AND iv.id<>? ORDER BY dv.version_no DESC,iv.created_at DESC""", (result["item_id"], item_version_id))]
        result["undo_target_event_id"] = self._undo_target(item_version_id)
        result["review"]["undo_target_event_id"] = result["undo_target_event_id"]
        return result

    def _undo_target(self, item_version_id: str) -> str | None:
        row = self.row("""SELECT e.id FROM review_events e
            WHERE e.item_version_id=? AND e.actor='local_user' AND e.action NOT IN ('undo','import')
              AND NOT EXISTS (SELECT 1 FROM review_events reversal WHERE reversal.undo_of=e.id)
              AND NOT EXISTS (SELECT 1 FROM review_events newer
                              WHERE newer.item_version_id=e.item_version_id AND newer.item_revision>e.item_revision
                                AND newer.action NOT IN ('undo','import')
                                AND NOT EXISTS (SELECT 1 FROM review_events newer_reversal WHERE newer_reversal.undo_of=newer.id))
            ORDER BY e.item_revision DESC LIMIT 1""", (item_version_id,))
        return row[0] if row else None

    def items(self, *, run_id: str | None, scope: str = "run", query: str = "", priorities: set[str] | None = None,
              source_id: str | None = None, review_states: set[str] | None = None, limit: int = 50, offset: int = 0,
              from_date: str | None = None, to_date: str | None = None) -> tuple[list[dict[str, Any]], int]:
        if scope == "run" and not run_id:
            return [], 0
        params: list[Any] = []
        if scope == "history":
            run_source = "FROM (SELECT ri0.*,ROW_NUMBER() OVER (PARTITION BY ri0.item_version_id ORDER BY r0.created_seq DESC,r0.started_at DESC,r0.id DESC) rn FROM run_items ri0 JOIN runs r0 ON r0.id=ri0.run_id) ri"
            history_clause = " AND ri.rn=1"
        else:
            run_source = "FROM run_items ri"; history_clause = ""
        sql = run_source + " JOIN item_versions iv ON iv.id=ri.item_version_id JOIN analysis_items ai ON ai.analysis_id=ri.analysis_id AND ai.item_version_id=ri.item_version_id " \
              + "JOIN review_state rv ON rv.item_version_id=iv.id JOIN items i ON i.id=iv.item_id JOIN documents d ON d.id=i.document_id " \
              + "JOIN meetings m ON m.id=d.meeting_id JOIN sources s ON s.id=m.source_id WHERE ri.included=1" + history_clause
        if run_id and scope == "run": sql += " AND ri.run_id=?"; params.append(run_id)
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += " AND (ai.title LIKE ? ESCAPE '\\' OR iv.original_text LIKE ? ESCAPE '\\' OR rv.note LIKE ? ESCAPE '\\')"; params += [f"%{escaped}%"] * 3
        if priorities:
            if not priorities.issubset({"low", "medium", "high"}):
                raise ValidationError("invalid priority filter")
            sql += " AND COALESCE(rv.human_priority,ri.proposed_priority) IN (" + ",".join("?" for _ in priorities) + ")"; params += sorted(priorities)
        if source_id: sql += " AND s.id=?"; params.append(source_id)
        if review_states:
            if not review_states.issubset({"unreviewed", "confirmed", "needs_review"}):
                raise ValidationError("invalid review state filter")
            sql += " AND rv.state IN (" + ",".join("?" for _ in review_states) + ")"; params += sorted(review_states)
        if from_date: sql += " AND m.local_date IS NOT NULL AND m.local_date>=?"; params.append(from_date)
        if to_date: sql += " AND m.local_date IS NOT NULL AND m.local_date<=?"; params.append(to_date)
        count = int(self.row("SELECT COUNT(*) " + sql, tuple(params))[0])
        rows = self.rows("SELECT iv.id,ri.run_id,ai.title,iv.original_text,COALESCE(rv.human_priority,ri.proposed_priority) final_priority,CASE WHEN rv.human_priority IS NULL THEN ri.decision_source ELSE 'human' END priority_source,rv.state review_state,rv.note,s.id source_id,s.name source_name,m.local_date,m.timezone,d.display_filename " + sql + " ORDER BY CASE COALESCE(rv.human_priority,ri.proposed_priority) WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,m.local_date IS NULL,m.local_date,s.name COLLATE NOCASE,iv.id,ri.run_id LIMIT ? OFFSET ?", tuple(params + [limit, offset]))
        return [dict(row) for row in rows], count


class RepositoryError(RuntimeError): pass
class NotFoundError(RepositoryError): pass
class ValidationError(RepositoryError): pass
class ConflictError(RepositoryError):
    def __init__(self, code: str, details: dict[str, Any] | None = None):
        self.code = code; self.details = details or {}; super().__init__(code)
