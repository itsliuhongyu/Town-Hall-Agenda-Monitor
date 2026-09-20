from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import hashlib
import threading
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

from ..config import ConfigSnapshot, DataPaths, local_date_window, utc_now
from ..storage.repository import ConflictError, Repository, dumps
from ..domain import new_id
from .supervision import register_child, supervise_worker


class _ActionGate:
    def __init__(self) -> None:
        # Recovery can canonicalize a pending receipt while the claimant is
        # inspecting it, so the condition must tolerate that re-entrant
        # release path.
        self.condition = threading.Condition(threading.RLock())
        self.owner = False
        self.body_hash: str | None = None


_ACTION_GATES_LOCK = threading.Lock()
_ACTION_GATES: dict[tuple[str, str, str], _ActionGate] = {}


@contextmanager
def pipeline_lock(paths: DataPaths):
    paths.ensure(); handle = open(paths.locks / "pipeline.lock", "a+")
    state = {"keep": False}
    try:
        try: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ConflictError("active_run", {})
        yield handle, state
    finally:
        if not state["keep"]:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class RunService:
    def __init__(self, repository: Repository): self.repo = repository

    def _action_gate_key(self, scope: str, key: str) -> tuple[str, str, str]:
        return (str(self.repo.paths.root), scope, key)

    def _action_row(self, scope: str, key: str):
        return self.repo.row("SELECT body_hash,result_json,http_status,response_state FROM action_requests WHERE scope=? AND idempotency_key=?", (scope, key))

    @staticmethod
    def _public_action_result(result: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in result.items() if not key.startswith("_")}

    def _pipeline_lock_held(self) -> bool:
        try:
            with pipeline_lock(self.repo.paths):
                return False
        except ConflictError as exc:
            if exc.code == "active_run":
                return True
            raise

    def _recover_pending_action(self, scope: str, key: str, body_hash: str, row) -> dict[str, Any] | None:
        """Resolve a receipt left pending by an owner crash without spawning twice."""
        if row[0] != body_hash:
            raise ConflictError("idempotency_conflict", {})
        if row[3] != "pending":
            return json.loads(row[1])
        pending = json.loads(row[1])
        run_id = pending.get("run_id")
        run = self.repo.get_run(run_id) if run_id else None
        if not run:
            failure = {"run_id": run_id, "status": "failed", "reason_code": "worker_start_failed", "publication_state": "preserved"}
            return self._store_action_result(scope, key, failure, 503) or failure
        if run["status"] != "running":
            if run.get("reason_code") == "worker_start_failed" or (
                run.get("reason_code") == "worker_interrupted" and run.get("worker_pid") is None
            ):
                failure = {"run_id": run_id, "status": "failed", "reason_code": "worker_start_failed", "publication_state": "preserved"}
                return self._store_action_result(scope, key, failure, 503) or failure
            accepted = {"run_id": run_id, "status": "running", "status_url": f"/api/v1/runs/{run_id}"}
            return self._store_action_result(scope, key, accepted, 202) or accepted

        # Once spawn has begun, a held pipeline lock belongs either to the
        # owner handoff or to the inherited child.  Treat it as accepted after
        # recovery; never start another worker for the same durable run.
        phase = pending.get("_receipt_state", "reserved")
        if phase == "spawning" and self._pipeline_lock_held():
            accepted = {"run_id": run_id, "status": "running", "status_url": f"/api/v1/runs/{run_id}"}
            return self._store_action_result(scope, key, accepted, 202) or accepted
        if not self._pipeline_lock_held():
            self._mark_worker_start_failed(scope, key, run_id, "action owner exited before worker handoff")
            return {"run_id": run_id, "status": "failed", "reason_code": "worker_start_failed", "publication_state": "preserved"}
        return None

    def _claim_action(self, scope: str | None, key: str | None, body_hash: str | None) -> dict[str, Any] | None:
        """Own only the short request/start handoff, not the worker run."""
        if not scope or not key or not body_hash:
            return None
        gate_key = self._action_gate_key(scope, key)
        wait_deadline = time.monotonic() + 10.0
        while True:
            with _ACTION_GATES_LOCK:
                gate = _ACTION_GATES.get(gate_key)
                if gate is None:
                    gate = _ActionGate()
                    _ACTION_GATES[gate_key] = gate
            with gate.condition:
                if gate.owner:
                    if gate.body_hash != body_hash:
                        raise ConflictError("idempotency_conflict", {})
                    remaining = wait_deadline - time.monotonic()
                    if remaining <= 0:
                        raise ConflictError("active_run", {})
                    gate.condition.wait(timeout=remaining)
                    continue
                row = self._action_row(scope, key)
                if row:
                    if row[0] != body_hash:
                        raise ConflictError("idempotency_conflict", {})
                    if row[3] != "pending":
                        return json.loads(row[1])
                    recovered = self._recover_pending_action(scope, key, body_hash, row)
                    if recovered is not None:
                        return recovered
                    if time.monotonic() >= wait_deadline:
                        raise ConflictError("active_run", {})
                    # A different process is still in the short handoff. Do
                    # not claim this token and risk a second worker.
                    gate.condition.wait(timeout=min(0.05, wait_deadline - time.monotonic()))
                    continue
                gate.owner = True
                gate.body_hash = body_hash
                return None

    def _release_action_gate(self, scope: str | None, key: str | None) -> None:
        if not scope or not key:
            return
        gate_key = self._action_gate_key(scope, key)
        with _ACTION_GATES_LOCK:
            gate = _ACTION_GATES.get(gate_key)
        if not gate:
            return
        with gate.condition:
            gate.owner = False
            gate.body_hash = None
            gate.condition.notify_all()
        with _ACTION_GATES_LOCK:
            if _ACTION_GATES.get(gate_key) is gate and not gate.owner:
                _ACTION_GATES.pop(gate_key, None)

    def _enter_action_lock(self, scope: str | None, key: str | None, body_hash: str | None):
        replay = self._claim_action(scope, key, body_hash)
        if replay is not None:
            return None, replay
        deadline = time.monotonic() + 5.0
        while True:
            lock = pipeline_lock(self.repo.paths)
            try:
                return (lock, lock.__enter__()), None
            except ConflictError as exc:
                if exc.code != "active_run" or not scope or not key or time.monotonic() >= deadline:
                    self._release_action_gate(scope, key)
                    raise
                time.sleep(0.01)

    def _store_action_result(self, scope: str | None, key: str | None, result: dict[str, Any], status: int) -> dict[str, Any] | None:
        if not scope or not key:
            return None
        from ..storage.db import transaction
        canonical = json.loads(dumps(self._public_action_result(result)))
        with transaction(self.repo.paths) as conn:
            conn.execute("UPDATE action_requests SET result_json=?,http_status=?,response_state='canonical' WHERE scope=? AND idempotency_key=?", (dumps(canonical), status, scope, key))
        self._release_action_gate(scope, key)
        return canonical

    def _mark_action_pending(self, scope: str | None, key: str | None, result: dict[str, Any], *, phase: str) -> None:
        if not scope or not key:
            return
        from ..storage.db import transaction
        pending = dict(result); pending["_receipt_state"] = phase
        with transaction(self.repo.paths) as conn:
            conn.execute("UPDATE action_requests SET result_json=?,response_state='pending' WHERE scope=? AND idempotency_key=?", (dumps(pending), scope, key))

    def _remove_action_reservation(self, scope: str | None, key: str | None) -> None:
        if not scope or not key:
            return
        from ..storage.db import transaction
        with transaction(self.repo.paths) as conn:
            conn.execute("DELETE FROM action_requests WHERE scope=? AND idempotency_key=?", (scope, key))
        self._release_action_gate(scope, key)

    def _mark_worker_start_failed(self, scope: str, key: str, run_id: str, message: str) -> None:
        from ..storage.db import transaction
        failure = {"run_id": run_id, "status": "failed", "reason_code": "worker_start_failed", "publication_state": "preserved"}
        error_json = json.dumps({"code": "worker_start_failed", "message": message})
        with transaction(self.repo.paths) as conn:
            conn.execute("UPDATE runs SET status='failed',phase='done',reason_code='worker_start_failed',error_json=?,finished_at=?,publication_state='preserved' WHERE id=? AND status='running'", (error_json, utc_now(), run_id))
            conn.execute("UPDATE run_sources SET state='failed',finished_at=?,error_json=? WHERE run_id=? AND state NOT IN ('success','no_results')", (utc_now(), error_json, run_id))
            conn.execute("UPDATE action_requests SET result_json=?,http_status=503,response_state='canonical' WHERE scope=? AND idempotency_key=?", (dumps(failure), scope, key))
        self._release_action_gate(scope, key)

    @staticmethod
    def _spawn_worker(cmd: list[str], lock_handle) -> int:
        environment = os.environ.copy()
        environment["AGENDA_WORKER_OWNER_PID"] = str(os.getpid())
        process = subprocess.Popen(cmd, pass_fds=(lock_handle.fileno(),), close_fds=True, env=environment)
        register_child(process)
        return process.pid

    def _run_sync_bounded(self, run_id: str, adapter_registry: dict[str, Any] | None) -> dict[str, Any]:
        """Run a synchronous action without allowing a blocked call to hold the lock."""
        from .worker import run_worker
        finished = threading.Event(); deadline_hit = threading.Event(); box: dict[str, Any] = {}

        def execute_sync():
            try:
                box["result"] = run_worker(self.repo, run_id, adapter_registry=adapter_registry, on_deadline=deadline_hit.set)
            except Exception as exc:
                box["exception"] = exc
            finally:
                finished.set()

        threading.Thread(target=execute_sync, name=f"agenda-sync-worker-{run_id}", daemon=True).start()
        while not finished.wait(0.02):
            if deadline_hit.is_set():
                current = self.repo.get_run(run_id) or {}
                return {"run_id": run_id, "status": current.get("status", "failed"), "reason_code": "run_deadline_exceeded", "publication_state": current.get("publication_state", "preserved")}
        if "exception" in box:
            current = self.repo.get_run(run_id) or {}
            return {"run_id": run_id, "status": current.get("status", "failed"), "reason_code": current.get("reason_code") or "worker_exception", "publication_state": current.get("publication_state", "preserved")}
        return box["result"]

    def _sources(self, source_ids: list[str] | None, snapshot: ConfigSnapshot) -> list[dict[str, Any]]:
        source_rows = self.repo.list_sources(True)
        if source_ids is not None:
            source_rows = [s for s in source_rows if s["id"] in set(source_ids)]
        result = []
        for source in source_rows:
            start, end = local_date_window(source["timezone"], snapshot.values["days_back"], snapshot.values["days_forward"])
            result.append({"id": source["id"], "platform": source["platform"], "name": source["name"], "collection_url": source["collection_url"], "timezone": source["timezone"], "enabled": bool(source["enabled"]), "config": json.loads(source["config_json"]), "window": {"start": start.isoformat(), "end": end.isoformat()}})
        return result

    def start(self, *, kind: str = "full", source_ids: list[str] | None = None, background: bool = True, adapter_registry: dict[str, Any] | None = None, snapshot: ConfigSnapshot | None = None,
              action_scope: str | None = None, action_key: str | None = None, action_body_hash: str | None = None) -> dict[str, Any]:
        if kind not in {"full", "analyze_only"}: raise ValueError("invalid run kind")
        if source_ids is not None:
            if not source_ids or len(source_ids) != len(set(source_ids)) or len(source_ids) > 1000: raise ValueError("source_ids must be a non-empty unique list of at most 1000 ids")
            known_ids = {row["id"] for row in self.repo.list_sources()}
            if any(source_id not in known_ids for source_id in source_ids): raise ValueError("unknown source id")
        snapshot = snapshot or self.repo.snapshot(); sources = self._sources(source_ids, snapshot)
        lock_result = self._enter_action_lock(action_scope, action_key, action_body_hash)
        if lock_result[0] is None:
            return lock_result[1]
        lock, (lock_handle, lock_state) = lock_result[0]
        try:
            if action_scope and action_key:
                existing = self.repo.row("SELECT body_hash,result_json FROM action_requests WHERE scope=? AND idempotency_key=?", (action_scope, action_key))
                if existing:
                    if existing[0] != action_body_hash:
                        raise ConflictError("idempotency_conflict", {})
                    self._release_action_gate(action_scope, action_key)
                    lock.__exit__(None, None, None)
                    return json.loads(existing[1])
            # Reserve the idempotency token before create_run. This closes the
            # narrow race where a duplicate arrives while create_run is still
            # assembling its transaction; create_run fills in the same row
            # with the durable run id.
            reserved_run_id = new_id()
            action_reserved = False
            if action_scope and action_key and action_body_hash:
                self._reserve_action(action_scope, action_key, action_body_hash, {"run_id": reserved_run_id, "status": "running", "status_url": f"/api/v1/runs/{reserved_run_id}"})
                action_reserved = True
            # The lock is held while the DB active-run row is created. A child
            # process receives the same advisory lock for its whole lifetime.
            from ..storage.db import transaction
            with transaction(self.repo.paths) as conn:
                stale = conn.execute("SELECT id FROM runs WHERE status='running'").fetchall()
                for row in stale:
                    conn.execute("UPDATE runs SET status='interrupted',phase='done',reason_code='worker_interrupted',finished_at=?,publication_state='preserved' WHERE id=?", (utc_now(), row[0]))
                    conn.execute("UPDATE run_sources SET state='interrupted',finished_at=? WHERE run_id=? AND state NOT IN ('success','no_results')", (utc_now(), row[0]))
            run_id = self.repo.create_run(kind=kind, snapshot=snapshot, sources=sources, status="running" if sources else "failed", reason_code=None if sources else "no_sources",
                                          action_scope=action_scope, action_key=action_key, action_body_hash=action_body_hash, run_id=reserved_run_id)
            self._mark_action_pending(action_scope, action_key, {"run_id": run_id, "status": "running", "status_url": f"/api/v1/runs/{run_id}"}, phase="reserved")
            if not sources:
                self.repo.update_run(run_id, phase="done", finished_at=utc_now(), publication_state="preserved")
                result = {"run_id": run_id, "status": "failed", "reason_code": "no_sources", "publication_state": "preserved"}
                # The request was accepted and durably recorded even though
                # there was no work to run; preserve the API's 202 action
                # contract for this terminal no-op.
                result = self._store_action_result(action_scope, action_key, result, 202) or result
                lock.__exit__(None, None, None)
                return result
            result = {"run_id": run_id, "status": "running", "status_url": f"/api/v1/runs/{run_id}"}
            if not background:
                result = self._run_sync_bounded(run_id, adapter_registry)
                result = self._store_action_result(action_scope, action_key, result, 200 if result.get("status") in {"success", "no_results"} else 500) or result
                lock.__exit__(None, None, None)
                return result
            cmd = [sys.executable, "-m", "agenda_app.runs.worker", str(self.repo.paths.root), run_id]
            self._mark_action_pending(action_scope, action_key, result, phase="spawning")
            try:
                worker_pid = self._spawn_worker(cmd, lock_handle)
            except Exception as exc:
                from ..storage.db import transaction
                failure = {"run_id": run_id, "status": "failed", "reason_code": "worker_start_failed", "publication_state": "preserved"}
                failure = json.loads(dumps(failure))
                with transaction(self.repo.paths) as conn:
                    conn.execute("UPDATE runs SET status='failed',phase='done',reason_code='worker_start_failed',error_json=?,finished_at=?,publication_state='preserved' WHERE id=?", (json.dumps({"code": "worker_start_failed", "message": str(exc)}), utc_now(), run_id))
                    conn.execute("UPDATE run_sources SET state='failed',finished_at=?,error_json=? WHERE run_id=? AND state NOT IN ('success','no_results')", (utc_now(), json.dumps({"code": "worker_start_failed", "message": str(exc)}), run_id))
                    if action_scope and action_key:
                        conn.execute("UPDATE action_requests SET result_json=?,http_status=503,response_state='canonical' WHERE scope=? AND idempotency_key=?", (dumps(failure), action_scope, action_key))
                self._release_action_gate(action_scope, action_key)
                lock.__exit__(None, None, None)
                return failure
            with __import__('agenda_app.storage.db', fromlist=['transaction']).transaction(self.repo.paths) as conn:
                conn.execute("UPDATE runs SET worker_pid=? WHERE id=?", (worker_pid, run_id))
            threading.Thread(target=supervise_worker, args=(self.repo, run_id, worker_pid), name=f"agenda-supervisor-{run_id}", daemon=True).start()
            result = self._store_action_result(action_scope, action_key, result, 202) or result
            lock_state["keep"] = True
            lock.__exit__(None, None, None)
            return result
        except sqlite3.IntegrityError as exc:
            if 'action_reserved' in locals() and action_reserved:
                self._remove_action_reservation(action_scope, action_key)
            try: lock.__exit__(type(exc), exc, exc.__traceback__)
            except Exception: pass
            raise ConflictError("active_run", {}) from exc
        except Exception:
            if 'action_reserved' in locals() and action_reserved:
                self._remove_action_reservation(action_scope, action_key)
            else:
                self._release_action_gate(action_scope, action_key)
            try: lock.__exit__(*sys.exc_info())
            except Exception: pass
            raise

    def _reserve_action(self, scope: str | None, key: str | None, body_hash: str | None, result: dict[str, Any]) -> None:
        if not scope or not key or not body_hash:
            return
        from ..storage.db import transaction
        from ..storage.repository import dumps
        with transaction(self.repo.paths) as conn:
            pending = dict(result); pending["_receipt_state"] = "reserved"
            conn.execute("INSERT INTO action_requests(id,scope,idempotency_key,body_hash,result_json,http_status,response_state,created_at) VALUES(?,?,?,?,?,?,?,?)",
                         (new_id(), scope, key, body_hash, dumps(pending), 202, "pending", utc_now()))

    def retry(self, parent_id: str, source_ids: list[str], *, background: bool = True, adapter_registry: dict[str, Any] | None = None,
              action_scope: str | None = None, action_key: str | None = None, action_body_hash: str | None = None) -> dict[str, Any]:
        parent = self.repo.get_run(parent_id)
        if not parent: raise KeyError("run not found")
        if parent["status"] == "running": raise ConflictError("active_run", {"active_run_id": parent_id})
        rows = self.repo.run_sources(parent_id); known = {r["source_id"]: r for r in rows}
        if not source_ids or any(s not in known for s in source_ids): raise ValueError("retry requires known non-empty source ids")
        if any(known[s]["state"] not in {"failed", "partial", "interrupted"} for s in source_ids): raise ValueError("only failed sources may be retried")
        current = self.repo.snapshot()
        parent_snapshot = json.loads(parent["settings_snapshot_json"])
        current_payload = json.loads(current.json())
        current_sources = {row["id"]: row for row in self.repo.list_sources()}
        parent_sources = {row["source_id"]: json.loads(row["source_snapshot_json"]) for row in rows}
        source_config_changed = False
        for source_id, saved in parent_sources.items():
            current_row = current_sources.get(source_id)
            current_value = {"id": source_id, "platform": current_row["platform"], "name": current_row["name"], "collection_url": current_row["collection_url"], "timezone": current_row["timezone"], "enabled": bool(current_row["enabled"]), "config": json.loads(current_row["config_json"])} if current_row else None
            saved_value = {"id": source_id, "platform": saved.get("platform"), "name": saved.get("name"), "collection_url": saved.get("collection_url"), "timezone": saved.get("timezone"), "enabled": bool(saved.get("enabled", True)), "config": saved.get("config", {})}
            if current_value != saved_value:
                source_config_changed = True
                break
        if current.policy_version_id != parent["policy_version_id"] or current_payload.get("values") != parent_snapshot.get("values") or current_payload.get("policy") != parent_snapshot.get("policy") or source_config_changed:
            raise ConflictError("retry_configuration_changed", {})
        # A retry has the full source scope. Successful sources are represented
        # as inherited rows; worker can reuse their persisted documents later.
        sources = [json.loads(r["source_snapshot_json"]) for r in rows]
        lock_result = self._enter_action_lock(action_scope, action_key, action_body_hash)
        if lock_result[0] is None:
            return lock_result[1]
        lock, (lock_handle, lock_state) = lock_result[0]
        try:
            if action_scope and action_key:
                existing = self.repo.row("SELECT body_hash,result_json FROM action_requests WHERE scope=? AND idempotency_key=?", (action_scope, action_key))
                if existing:
                    if existing[0] != action_body_hash:
                        raise ConflictError("idempotency_conflict", {})
                    self._release_action_gate(action_scope, action_key)
                    lock.__exit__(None, None, None)
                    return json.loads(existing[1])
            reserved_run_id = new_id(); action_reserved = False
            if action_scope and action_key and action_body_hash:
                self._reserve_action(action_scope, action_key, action_body_hash, {"run_id": reserved_run_id, "status": "running", "status_url": f"/api/v1/runs/{reserved_run_id}"})
                action_reserved = True
            run_id = self.repo.create_run(kind="retry", snapshot=current, sources=sources, parent_run_id=parent_id,
                                          action_scope=action_scope, action_key=action_key, action_body_hash=action_body_hash, run_id=reserved_run_id)
            from ..storage.db import transaction
            result = {"run_id": run_id, "status": "running", "status_url": f"/api/v1/runs/{run_id}"}
            self._mark_action_pending(action_scope, action_key, result, phase="reserved")
            with transaction(self.repo.paths) as conn:
                child_rows = conn.execute("SELECT * FROM run_sources WHERE run_id=?", (run_id,)).fetchall()
                for child in child_rows:
                    parent_source = known[child["source_id"]]
                    if child["source_id"] in source_ids:
                        continue
                    # A retry child has a full source manifest for honest
                    # coverage, but only explicitly selected failed/partial
                    # sources are eligible for discovery and download.
                    inherited_state = parent_source["state"]
                    conn.execute("UPDATE run_sources SET state=?,discovery_state=?,inherited_from_id=?,started_at=?,finished_at=?,observed_at=?,found_count=?,downloaded_count=?,analyzed_count=?,error_json=? WHERE id=?",
                                 (inherited_state, parent_source["discovery_state"], parent_source["id"], parent_source["started_at"], parent_source["finished_at"], parent_source["observed_at"], parent_source["found_count"], parent_source["downloaded_count"], parent_source["analyzed_count"], parent_source["error_json"], child["id"]))
                    if inherited_state in {"success", "no_results"}:
                        conn.execute("INSERT INTO run_items(run_id,item_version_id,analysis_id,policy_version_id,policy_priority,proposed_priority,decision_source,matched_rules_json,included,exclusion_reason,source_run_id) SELECT ?,ri.item_version_id,ri.analysis_id,ri.policy_version_id,ri.policy_priority,ri.proposed_priority,ri.decision_source,ri.matched_rules_json,ri.included,ri.exclusion_reason,? FROM run_items ri JOIN item_versions iv ON iv.id=ri.item_version_id JOIN items i ON i.id=iv.item_id JOIN documents d ON d.id=i.document_id JOIN meetings m ON m.id=d.meeting_id WHERE ri.run_id=? AND m.source_id=?",
                                         (run_id, parent_id, parent_id, child["source_id"]))
            if background:
                cmd = [sys.executable, "-m", "agenda_app.runs.worker", str(self.repo.paths.root), run_id]
                self._mark_action_pending(action_scope, action_key, result, phase="spawning")
                try:
                    worker_pid = self._spawn_worker(cmd, lock_handle)
                except Exception as exc:
                    failure = {"run_id": run_id, "status": "failed", "reason_code": "worker_start_failed", "publication_state": "preserved"}
                    failure = json.loads(dumps(failure))
                    with transaction(self.repo.paths) as conn:
                        conn.execute("UPDATE runs SET status='failed',phase='done',reason_code='worker_start_failed',error_json=?,finished_at=?,publication_state='preserved' WHERE id=?", (json.dumps({"code": "worker_start_failed", "message": str(exc)}), utc_now(), run_id))
                        conn.execute("UPDATE run_sources SET state='failed',finished_at=?,error_json=? WHERE run_id=? AND state NOT IN ('success','no_results')", (utc_now(), json.dumps({"code": "worker_start_failed", "message": str(exc)}), run_id))
                        if action_scope and action_key:
                            conn.execute("UPDATE action_requests SET result_json=?,http_status=503,response_state='canonical' WHERE scope=? AND idempotency_key=?", (dumps(failure), action_scope, action_key))
                    self._release_action_gate(action_scope, action_key)
                    lock.__exit__(None, None, None)
                    return failure
                with transaction(self.repo.paths) as conn:
                    conn.execute("UPDATE runs SET worker_pid=? WHERE id=?", (worker_pid, run_id))
                threading.Thread(target=supervise_worker, args=(self.repo, run_id, worker_pid), name=f"agenda-supervisor-{run_id}", daemon=True).start()
                result = self._store_action_result(action_scope, action_key, result, 202) or result
                lock_state["keep"] = True; lock.__exit__(None, None, None)
                return result
            result = self._run_sync_bounded(run_id, adapter_registry)
            result = self._store_action_result(action_scope, action_key, result, 200 if result.get("status") in {"success", "no_results"} else 500) or result
            lock.__exit__(None, None, None); return result
        except sqlite3.IntegrityError as exc:
            if 'action_reserved' in locals() and action_reserved:
                self._remove_action_reservation(action_scope, action_key)
            lock.__exit__(type(exc), exc, exc.__traceback__); raise ConflictError("active_run", {}) from exc
        except Exception:
            if 'action_reserved' in locals() and action_reserved:
                self._remove_action_reservation(action_scope, action_key)
            else:
                self._release_action_gate(action_scope, action_key)
            lock.__exit__(*__import__('sys').exc_info()); raise

    def recover_interrupted(self) -> int:
        with pipeline_lock(self.repo.paths):
            with __import__('agenda_app.storage.db', fromlist=['transaction']).transaction(self.repo.paths) as conn:
                rows = conn.execute("SELECT id,worker_pid FROM runs WHERE status='running'").fetchall()
                if rows:
                    run_ids = {row[0] for row in rows}
                    pending_failures: dict[str, tuple[str, str]] = {}
                    for receipt in conn.execute("SELECT scope,idempotency_key,result_json FROM action_requests WHERE response_state='pending'").fetchall():
                        try:
                            result = json.loads(receipt[2])
                        except (TypeError, ValueError):
                            continue
                        run_id = result.get("run_id")
                        worker_pid = next((row[1] for row in rows if row[0] == run_id), None)
                        if run_id in run_ids and worker_pid is None:
                            pending_failures[run_id] = (receipt[0], receipt[1])
                    now = utc_now()
                    start_failed = dumps({"code": "worker_start_failed", "message": "action owner exited before worker handoff"})
                    for row in rows:
                        if row[0] in pending_failures:
                            conn.execute("UPDATE runs SET status='failed',phase='done',reason_code='worker_start_failed',error_json=?,finished_at=?,publication_state='preserved' WHERE id=?", (start_failed, now, row[0]))
                            conn.execute("UPDATE run_sources SET state='failed',finished_at=?,error_json=? WHERE run_id=? AND state NOT IN ('success','no_results')", (now, start_failed, row[0]))
                            scope, key = pending_failures[row[0]]
                            conn.execute("UPDATE action_requests SET result_json=?,http_status=503,response_state='canonical' WHERE scope=? AND idempotency_key=?", (dumps({"run_id": row[0], "status": "failed", "reason_code": "worker_start_failed", "publication_state": "preserved"}), scope, key))
                        else:
                            conn.execute("UPDATE runs SET status='interrupted',phase='done',reason_code='worker_interrupted',finished_at=?,publication_state='preserved' WHERE id=?", (now, row[0]))
                            conn.execute("UPDATE run_sources SET state='interrupted',finished_at=? WHERE run_id=? AND state NOT IN ('success','no_results')", (now, row[0]))
                return len(rows)
