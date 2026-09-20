from __future__ import annotations

import hashlib
import json
from typing import Any

from ..config import utc_now
from ..domain import PRIORITIES, new_id
from ..storage.db import transaction
from ..storage.repository import ConflictError, NotFoundError, Repository, ValidationError, dumps


class ReviewService:
    def __init__(self, repository: Repository): self.repo = repository

    @staticmethod
    def _hash(payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _replay(self, conn, scope: str, key: str, body_hash: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM action_requests WHERE scope=? AND idempotency_key=?", (scope, key)).fetchone()
        if not row: return None
        if row["body_hash"] != body_hash: raise ConflictError("idempotency_conflict", {})
        result = json.loads(row["result_json"]); result["idempotent"] = True; return result

    def apply(self, item_version_id: str, payload: dict[str, Any], *, idempotency_key: str, actor: str = "local_user") -> dict[str, Any]:
        if not isinstance(payload, dict): raise ValidationError("review body must be an object")
        action = payload.get("action"); run_id = payload.get("run_id"); expected = payload.get("expected_revision")
        if action not in {"set_priority", "confirm", "save_note"} or type(expected) is not int or not run_id: raise ValidationError("invalid review request")
        if action == "set_priority" and payload.get("priority") not in PRIORITIES: raise ValidationError("priority is required")
        if action == "confirm" and "priority" in payload: raise ValidationError("confirm does not accept priority")
        if action == "save_note" and (not isinstance(payload.get("note"), str) or len(payload["note"]) > 4000): raise ValidationError("note is required and must be <=4000 characters")
        scope = f"{actor}:review:{item_version_id}"; body_hash = self._hash(payload); request_id = new_id()
        with transaction(self.repo.paths) as conn:
            replay = self._replay(conn, scope, idempotency_key, body_hash)
            if replay is not None: return replay
            row = conn.execute("SELECT rv.*,ri.run_id,ri.proposed_priority,ri.decision_source,ai.title,iv.original_text FROM review_state rv JOIN item_versions iv ON iv.id=rv.item_version_id JOIN run_items ri ON ri.item_version_id=iv.id JOIN analysis_items ai ON ai.analysis_id=ri.analysis_id AND ai.item_version_id=iv.id WHERE rv.item_version_id=? AND ri.run_id=? AND ri.included=1 LIMIT 1", (item_version_id, run_id)).fetchone()
            run = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row or not run: raise NotFoundError("item or run")
            if run[0] not in {"success", "no_results", "partial", "interrupted"}: raise ConflictError("review_run_not_terminal", {})
            if row["row_version"] != expected: raise ConflictError("review_conflict", {"current_revision": row["row_version"]})
            before = {"state": row["state"], "human_priority": row["human_priority"], "note": row["note"]}
            after = dict(before)
            if action == "set_priority": after.update({"state": "confirmed", "human_priority": payload["priority"]})
            elif action == "confirm": after.update({"state": "confirmed", "human_priority": row["human_priority"] or row["proposed_priority"]})
            else: after["note"] = payload["note"]
            if after == before:
                result = {"item": self._item_response(row, after, undo_target_event_id=self._undo_target(conn, item_version_id)), "event_id": row["last_event_id"], "applied": False, "idempotent": False}
                conn.execute("INSERT INTO action_requests(id,scope,idempotency_key,body_hash,result_json,http_status,created_at) VALUES(?,?,?,?,?,?,?)", (request_id, scope, idempotency_key, body_hash, dumps(result), 200, utc_now()))
                return result
            revision = row["row_version"] + 1; event_id = new_id()
            conn.execute("UPDATE review_state SET state=?,human_priority=?,note=?,row_version=?,last_event_id=?,updated_at=? WHERE item_version_id=?", (after["state"], after["human_priority"], after["note"], revision, event_id, utc_now(), item_version_id))
            conn.execute("INSERT INTO action_requests(id,scope,idempotency_key,body_hash,result_json,http_status,created_at) VALUES(?,?,?,?,?,?,?)", (request_id, scope, idempotency_key, body_hash, "{}", 200, utc_now()))
            conn.execute("INSERT INTO review_events(id,item_version_id,item_revision,action,before_json,after_json,request_id,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (event_id, item_version_id, revision, action, dumps(before), dumps(after), request_id, actor, utc_now()))
            self.repo.bump_revision(conn)
            result = {"item": self._item_response(row, after, row_version=revision, event_id=event_id, undo_target_event_id=self._undo_target(conn, item_version_id)), "event_id": event_id, "applied": True, "idempotent": False}
            conn.execute("UPDATE action_requests SET result_json=? WHERE id=?", (dumps(result), request_id))
            return result

    @staticmethod
    def _item_response(row, state, *, row_version: int | None = None, event_id: str | None = None, undo_target_event_id: str | None = None) -> dict[str, Any]:
        final = state["human_priority"] or row["proposed_priority"]
        return {"id": row["item_version_id"], "run_id": row["run_id"] if "run_id" in row.keys() else None, "title": row["title"], "original_text": row["original_text"], "final_priority": final,
                "priority_source": "human" if state["human_priority"] else row["decision_source"], "review": {**state, "row_version": row_version or row["row_version"], "last_event_id": event_id or row["last_event_id"], "undo_target_event_id": undo_target_event_id}}

    @staticmethod
    def _undo_target(conn, item_version_id: str) -> str | None:
        row = conn.execute("""SELECT e.id FROM review_events e
            WHERE e.item_version_id=? AND e.actor='local_user' AND e.action NOT IN ('undo','import')
              AND NOT EXISTS (SELECT 1 FROM review_events reversal WHERE reversal.undo_of=e.id)
              AND NOT EXISTS (SELECT 1 FROM review_events newer
                              WHERE newer.item_version_id=e.item_version_id AND newer.item_revision>e.item_revision
                                AND newer.action NOT IN ('undo','import')
                                AND NOT EXISTS (SELECT 1 FROM review_events newer_reversal WHERE newer_reversal.undo_of=newer.id))
            ORDER BY e.item_revision DESC LIMIT 1""", (item_version_id,)).fetchone()
        return row[0] if row else None

    def undo(self, event_id: str, expected_revision: int, *, idempotency_key: str, actor: str = "local_user") -> dict[str, Any]:
        scope = f"{actor}:undo:{event_id}"; payload = {"event_id": event_id, "expected_revision": expected_revision}; body_hash = self._hash(payload); request_id = new_id()
        with transaction(self.repo.paths) as conn:
            replay = self._replay(conn, scope, idempotency_key, body_hash)
            if replay is not None: return replay
            event = conn.execute("SELECT e.*,rv.row_version,rv.last_event_id,rv.state,rv.human_priority,rv.note,ri.run_id,ri.proposed_priority,ri.decision_source,ai.title,iv.original_text FROM review_events e JOIN review_state rv ON rv.item_version_id=e.item_version_id JOIN run_items ri ON ri.item_version_id=e.item_version_id JOIN analysis_items ai ON ai.analysis_id=ri.analysis_id AND ai.item_version_id=e.item_version_id JOIN item_versions iv ON iv.id=e.item_version_id JOIN runs r ON r.id=ri.run_id WHERE e.id=? ORDER BY r.created_seq DESC,r.started_at DESC,r.id DESC LIMIT 1", (event_id,)).fetchone()
            if not event: raise NotFoundError("review event")
            if event["action"] in {"undo", "import"} or event["actor"] != "local_user": raise ConflictError("undo_not_allowed", {})
            if conn.execute("SELECT 1 FROM review_events WHERE undo_of=?", (event_id,)).fetchone(): raise ConflictError("undo_not_latest", {})
            later_active = conn.execute("""SELECT 1 FROM review_events newer
                WHERE newer.item_version_id=? AND newer.item_revision>?
                  AND newer.action NOT IN ('undo','import')
                  AND NOT EXISTS (SELECT 1 FROM review_events reversal WHERE reversal.undo_of=newer.id)
                LIMIT 1""", (event["item_version_id"], event["item_revision"])).fetchone()
            if later_active: raise ConflictError("undo_not_latest", {})
            if event["row_version"] != expected_revision: raise ConflictError("review_conflict", {"current_revision": event["row_version"]})
            before = {"state": event["state"], "human_priority": event["human_priority"], "note": event["note"]}; after = json.loads(event["before_json"]); revision = event["row_version"] + 1; new_event = new_id()
            conn.execute("UPDATE review_state SET state=?,human_priority=?,note=?,row_version=?,last_event_id=?,updated_at=? WHERE item_version_id=?", (after["state"], after["human_priority"], after["note"], revision, new_event, utc_now(), event["item_version_id"]))
            conn.execute("INSERT INTO action_requests(id,scope,idempotency_key,body_hash,result_json,http_status,created_at) VALUES(?,?,?,?,?,?,?)", (request_id, scope, idempotency_key, body_hash, "{}", 200, utc_now()))
            conn.execute("INSERT INTO review_events(id,item_version_id,item_revision,action,before_json,after_json,undo_of,request_id,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (new_event, event["item_version_id"], revision, "undo", dumps(before), dumps(after), event_id, request_id, actor, utc_now()))
            self.repo.bump_revision(conn)
            result = {"item": self._item_response(event, after, row_version=revision, event_id=new_event, undo_target_event_id=self._undo_target(conn, event["item_version_id"])), "event_id": new_event, "applied": True, "idempotent": False}
            conn.execute("UPDATE action_requests SET result_json=? WHERE id=?", (dumps(result), request_id)); return result
