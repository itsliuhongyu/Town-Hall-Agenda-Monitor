from __future__ import annotations

import json
import os
import queue
import sys
import time
import threading
from datetime import date
from pathlib import Path
from typing import Any

from ..analysis.service import AnalysisService
from ..analysis.policy import evaluate
from ..config import ConfigSnapshot, utc_now
from ..domain import DateWindow, SourceSnapshot, ErrorInfo
from ..storage.repository import Repository, dumps
from ..storage.db import transaction
from .discovery import discover_source
from .downloads import download, retain_blob, retain_captured
from .supervision import terminalize_deadline


class RunDeadlineExceeded(RuntimeError):
    pass


def _error_dict(exc: Exception) -> dict[str, Any]:
    return {"code": "run_deadline_exceeded" if isinstance(exc, RunDeadlineExceeded) else "worker_exception", "message": str(exc), "retryable": True, "stage": "run"}


def _source_snapshot(row: dict[str, Any], window: dict[str, Any] | None = None) -> SourceSnapshot:
    return SourceSnapshot(row["id"], row["platform"], row["name"], row["collection_url"], row["timezone"], json.loads(row.get("config_json", "{}")) if isinstance(row.get("config_json"), str) else row.get("config", {}))


def discovery_headless(source: SourceSnapshot, requested_headless: bool, settings: dict[str, Any]) -> bool:
    """Choose browser visibility without changing other platform behavior."""
    if source.platform.casefold() == "boarddocs" and bool(settings.get("allow_visible_browser", False)):
        return False
    return requested_headless


def _candidate_for_source(source: SourceSnapshot, candidate: Any) -> tuple[dict[str, Any], str]:
    return {"id": source.source_id, "platform": source.platform, "name": source.name, "collection_url": source.collection_url,
            "timezone": source.timezone, "config": source.config}, candidate.suggested_filename


def _parallel_map(fn, values: list[Any], workers: int, *, cancel=None) -> list[Any]:
    """Run stage calls with a bounded daemon worker set.

    A semaphore around one thread per input limits calls but not resources:
    every input still creates a live thread waiting for the semaphore.  A
    small queue and a fixed helper set bound both the calls and the number of
    threads.  ``cancel`` is checked before dequeuing and immediately before a
    stage call, so a deadline/exception does not start more work.  Helpers are
    daemon threads because a stage call may be inside an uninterruptible
    library call; the coordinator returns without joining those helpers.
    """
    if not values:
        return []

    worker_count = min(max(1, int(workers)), len(values))
    jobs: queue.Queue[tuple[int, Any]] = queue.Queue()
    for index, value in enumerate(values):
        jobs.put((index, value))
    results: list[Any] = [None] * len(values)
    stop = threading.Event()
    changed = threading.Event()
    errors: list[BaseException] = []
    completed = 0
    completed_lock = threading.Lock()

    def one() -> None:
        nonlocal completed
        while not stop.is_set():
            if cancel and cancel():
                stop.set()
                changed.set()
                return
            try:
                index, value = jobs.get_nowait()
            except queue.Empty:
                return
            try:
                # Do not turn a queued input into a stage call after the
                # deadline/exception became visible between dequeue and call.
                if cancel and cancel():
                    stop.set()
                    return
                results[index] = fn(value)
            except BaseException as exc:  # propagate the original stage error
                with completed_lock:
                    if not errors:
                        errors.append(exc)
                stop.set()
                changed.set()
                return
            finally:
                jobs.task_done()
            with completed_lock:
                completed += 1
            changed.set()

    threads = [threading.Thread(target=one, daemon=True, name="agenda-stage") for _ in range(worker_count)]
    for thread in threads:
        thread.start()

    cancelled = False
    while True:
        if cancel and cancel():
            cancelled = True
            stop.set()
            break
        with completed_lock:
            done = completed == len(values)
            failed = bool(errors)
        if done or failed:
            break
        changed.wait(0.01)
        changed.clear()

    stop.set()
    if cancelled:
        raise RunDeadlineExceeded("stage map cancelled")
    if errors:
        raise errors[0]
    return results


def run_worker(repo: Repository, run_id: str, *, headless: bool = True, adapter_registry: dict[str, Any] | None = None, on_deadline=None, owner_pid: int | None = None) -> dict[str, Any]:
    run = repo.get_run(run_id)
    if not run: raise ValueError("run not found")
    snapshot_data = json.loads(run["settings_snapshot_json"])
    snapshot = ConfigSnapshot(snapshot_data["values"], snapshot_data["policy"], snapshot_data["revision"], snapshot_data["policy_version_id"], snapshot_data.get("captured_at", ""))
    paths = repo.paths; work = paths.work / run_id; work.mkdir(parents=True, exist_ok=True)
    if adapter_registry is None:
        from adapters import REGISTRY
        adapter_registry = REGISTRY
    analysis = AnalysisService(repo)
    owner_token = run["owner_token"]
    healthy = 0; failures = 0; included = 0; successful_observations = 0
    deadline = time.monotonic() + max(1, int(snapshot.values.get("run_timeout_minutes", 60))) * 60
    stop_heartbeat = threading.Event(); deadline_signal = threading.Event()

    def heartbeat() -> None:
        while not stop_heartbeat.wait(1.0):
            try:
                with transaction(paths) as conn:
                    conn.execute("UPDATE runs SET heartbeat_at=? WHERE id=? AND status='running'", (utc_now(), run_id))
            except Exception: return

    beat = threading.Thread(target=heartbeat, name=f"agenda-heartbeat-{run_id}", daemon=True); beat.start()

    def check_deadline() -> None:
        current = repo.get_run(run_id)
        if deadline_signal.is_set() or not current or current["status"] != "running" or time.monotonic() >= deadline:
            raise RunDeadlineExceeded("configured whole-run deadline exceeded")

    def commit_final(status: str, phase: str, *, reason_code: str | None = None,
                     publication_state: str = "preserved", error_json: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any] | None]:
        return repo.finalize_run(
            run_id,
            owner_token=owner_token,
            status=status,
            phase=phase,
            reason_code=reason_code,
            finished_at=utc_now(),
            publication_state=publication_state,
            error_json=error_json,
        )

    def result_from_live(live: dict[str, Any] | None, *, default_status: str, default_reason: str | None = None) -> dict[str, Any]:
        current = live or repo.get_run(run_id) or {}
        return {
            "run_id": run_id,
            "status": current.get("status", default_status),
            "included": included,
            "failures": failures,
            "reason_code": current.get("reason_code") or default_reason,
            "publication_state": current.get("publication_state", "preserved"),
        }

    def watch_deadline() -> None:
        while not stop_heartbeat.wait(0.02):
            if time.monotonic() >= deadline:
                deadline_signal.set(); stop_heartbeat.set(); terminalize_deadline(repo, run_id)
                terminal = repo.get_run(run_id) or {}
                # Finalization and deadline terminalization are competing
                # SQLite transitions.  Only the worker whose deadline write
                # actually won may notify a synchronous caller or terminate
                # a background child.  A healthy child in slow export
                # remains publishable while its export transaction completes.
                deadline_failed = terminal.get("reason_code") == "run_deadline_exceeded"
                if deadline_failed and on_deadline: on_deadline()
                # Synchronous callers deliberately use a daemon worker thread
                # and return a bounded result without os._exit. A real
                # background child must enforce the deadline independently of
                # its original HTTP parent, including while its main thread is
                # blocked in a stage call.
                if owner_pid is not None and deadline_failed:
                    os._exit(1)
                return

    deadline_thread = threading.Thread(target=watch_deadline, name=f"agenda-deadline-{run_id}", daemon=True); deadline_thread.start()

    try:
        if run["kind"] == "analyze_only":
            published_id = repo.current_published_run()
            if not published_id:
                committed, live = commit_final("failed", "done", reason_code="no_published_documents", publication_state="preserved")
                if not committed:
                    return result_from_live(live, default_status="failed", default_reason="no_published_documents")
                return {"run_id": run_id, "status": "failed", "included": 0, "failures": 1, "reason_code": "no_published_documents", "publication_state": "preserved"}
            with transaction(paths) as conn:
                parent_sources = conn.execute("SELECT * FROM run_sources WHERE run_id=?", (published_id,)).fetchall()
                for child in conn.execute("SELECT * FROM run_sources WHERE run_id=?", (run_id,)).fetchall():
                    parent = next((row for row in parent_sources if row["source_id"] == child["source_id"]), None)
                    if not parent: continue
                    conn.execute("UPDATE run_sources SET state=?,discovery_state=?,inherited_from_id=?,started_at=?,finished_at=?,observed_at=?,found_count=?,downloaded_count=?,analyzed_count=? WHERE id=?", (parent["state"], parent["discovery_state"], parent["id"], parent["started_at"], parent["finished_at"], parent["observed_at"], parent["found_count"], parent["downloaded_count"], parent["analyzed_count"], child["id"]))
                conn.execute("INSERT INTO run_items(run_id,item_version_id,analysis_id,policy_version_id,policy_priority,proposed_priority,decision_source,matched_rules_json,included,exclusion_reason,source_run_id) SELECT ?,item_version_id,analysis_id,policy_version_id,policy_priority,proposed_priority,decision_source,matched_rules_json,included,exclusion_reason,? FROM run_items WHERE run_id=?", (run_id, published_id, published_id))
                for item in conn.execute("SELECT ri.item_version_id,iv.original_text,ai.model_priority FROM run_items ri JOIN item_versions iv ON iv.id=ri.item_version_id JOIN analysis_items ai ON ai.analysis_id=ri.analysis_id AND ai.item_version_id=ri.item_version_id WHERE ri.run_id=?", (run_id,)).fetchall():
                    decision = evaluate(item["original_text"], item["model_priority"], snapshot.policy)
                    conn.execute("UPDATE run_items SET policy_version_id=?,policy_priority=?,proposed_priority=?,decision_source=?,matched_rules_json=?,included=?,exclusion_reason=? WHERE run_id=? AND item_version_id=?", (snapshot.policy_version_id, decision.policy_priority, decision.proposed_priority, decision.decision_source, dumps(list(decision.matched_rules)), int(decision.included), decision.exclusion_reason, run_id, item["item_version_id"]))
                included = int(conn.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND included=1", (run_id,)).fetchone()[0])
                healthy = int(conn.execute("SELECT COUNT(*) FROM run_sources WHERE run_id=? AND state IN ('success','no_results')", (run_id,)).fetchone()[0])
            final_status = "success" if included else "no_results"
            check_deadline()
            committed, live = commit_final(final_status, "publish", publication_state="pending")
            if not committed:
                return result_from_live(live, default_status=final_status)
            from ..storage.exports import ExportService
            export = ExportService(repo).export_run(run_id, publish=True, owner_token=owner_token)
            return {"run_id": run_id, "status": final_status, "included": included, "failures": 0, "export_id": export["export_id"], "manifest_path": export.get("manifest_path")}
        repo.update_run(run_id, phase="discovery")
        for run_source in repo.run_sources(run_id):
            check_deadline()
            if run_source["inherited_from_id"]:
                if run_source["state"] in {"success", "no_results"}:
                    healthy += 1
                    inherited_count = int(repo.row("SELECT COUNT(*) FROM run_items ri JOIN item_versions iv ON iv.id=ri.item_version_id JOIN items i ON i.id=iv.item_id JOIN documents d ON d.id=i.document_id JOIN meetings m ON m.id=d.meeting_id WHERE ri.run_id=? AND ri.included=1 AND m.source_id=?", (run_id, run_source["source_id"]))[0])
                    included += inherited_count
                else:
                    failures += 1
                continue
            source_data = json.loads(run_source["source_snapshot_json"])
            source = _source_snapshot(source_data)
            window = DateWindow(date.fromisoformat(run_source["window_start"]), date.fromisoformat(run_source["window_end"]))
            with transaction(paths) as conn:
                conn.execute("UPDATE run_sources SET state='discovering',started_at=?,observed_at=? WHERE id=?", (__import__('agenda_app.config', fromlist=['utc_now']).utc_now(), __import__('agenda_app.config', fromlist=['utc_now']).utc_now(), run_source["id"]))
            repo.update_run(run_id, heartbeat_at=__import__('agenda_app.config', fromlist=['utc_now']).utc_now())
            adapter = adapter_registry.get(source.platform)
            if not adapter:
                result = type("R", (), {"recognized": False, "explicit_empty": False, "candidates": (), "warnings": (ErrorInfo("adapter_unavailable", "No adapter is installed", False, "discovery"),)})()
            else:
                result = discover_source(adapter, source, window, headless=discovery_headless(source, headless, snapshot.values))
            check_deadline()
            if not result.recognized:
                failures += 1
                with transaction(paths) as conn:
                    conn.execute("UPDATE run_sources SET state='failed',discovery_state='failed',finished_at=?,error_json=? WHERE id=?", (__import__('agenda_app.config', fromlist=['utc_now']).utc_now(), dumps([e.as_dict() for e in result.warnings]), run_source["id"]))
                continue
            if result.explicit_empty and not result.candidates:
                healthy += 1
                with transaction(paths) as conn:
                    conn.execute("UPDATE run_sources SET state='no_results',discovery_state='empty',finished_at=?,observed_at=? WHERE id=?", (__import__('agenda_app.config', fromlist=['utc_now']).utc_now(), __import__('agenda_app.config', fromlist=['utc_now']).utc_now(), run_source["id"]))
                continue
            if not result.candidates:
                failures += 1
                with transaction(paths) as conn:
                    conn.execute("UPDATE run_sources SET state='failed',discovery_state='failed',finished_at=?,error_json=? WHERE id=?", (__import__('agenda_app.config', fromlist=['utc_now']).utc_now(), dumps({"code": "unrecognized_empty", "message": "The source was recognized but did not provide an explicit empty result.", "retryable": True, "stage": "discovery"}), run_source["id"]))
                continue
            source_included = 0; source_failed = bool(result.warnings); source_successful_docs = 0
            repo.update_run(run_id, phase="download")
            records = []
            for candidate in result.candidates:
                check_deadline()
                with transaction(paths) as conn:
                    _, document_id = repo.ensure_meeting_document(conn, source, candidate)
                    run_doc_id = __import__('agenda_app.domain', fromlist=['new_id']).new_id()
                    conn.execute("INSERT INTO run_documents(id,run_source_id,document_id,locator_key,original_url,download_state,read_state,analyze_state) VALUES(?,?,?,?,?,?,?,?)", (run_doc_id, run_source["id"], document_id, candidate.locator_key, candidate.original_url or "", "running", "pending", "pending"))
                records.append((candidate, document_id, run_doc_id))

            def fetch(record):
                check_deadline(); candidate, document_id, run_doc_id = record
                if candidate.captured_bytes is not None:
                    artifact = retain_captured(candidate.captured_bytes, work / "downloads", filename=candidate.suggested_filename,
                                                url=candidate.original_url or candidate.source_url,
                                                media_type=candidate.captured_media_type)
                else:
                    artifact = download(candidate.original_url or "", work / "downloads", filename=candidate.suggested_filename)
                return record, artifact

            fetched = _parallel_map(fetch, records, int(snapshot.values.get("download_workers", 1)), cancel=deadline_signal.is_set)
            check_deadline()

            def prepare(record_artifact):
                record, artifact = record_artifact; candidate, document_id, run_doc_id = record
                check_deadline()
                if artifact.status != "downloaded" or not artifact.path or not artifact.sha256:
                    return record, artifact, None, None
                blob_rel = retain_blob(paths.root, artifact.path, artifact.sha256); data = artifact.path.read_bytes()
                prepared = analysis.prepare_bytes(data=data, media_type=artifact.media_type, filename=candidate.suggested_filename, snapshot=snapshot)
                return record, artifact, data, (prepared, blob_rel)

            prepared_records = _parallel_map(prepare, fetched, int(snapshot.values.get("analysis_workers", 1)), cancel=deadline_signal.is_set)
            check_deadline()

            for (candidate, document_id, run_doc_id), artifact, data, prepared_value in prepared_records:
                check_deadline()
                if artifact.status != "downloaded" or not artifact.path or not artifact.sha256:
                    source_failed = True
                    with transaction(paths) as conn:
                        conn.execute("UPDATE run_documents SET download_state='failed',attempts=?,finished_at=?,error_json=? WHERE id=?", (artifact.attempts, __import__('agenda_app.config', fromlist=['utc_now']).utc_now(), dumps(artifact.error or {}), run_doc_id))
                    continue
                prepared, blob_rel = prepared_value; repo.update_run(run_id, phase="analyze")
                with transaction(paths) as conn:
                    conn.execute("UPDATE run_documents SET download_state='downloaded',bytes_received=? WHERE id=?", (artifact.size, run_doc_id))
                    result_data = analysis.persist_prepared(conn, run_id=run_id, source=source, document_id=document_id, data=data, media_type=artifact.media_type, filename=candidate.suggested_filename, snapshot=snapshot, prepared=prepared, fetched_url=artifact.url, blob_relpath=blob_rel)
                    read_status = result_data["read"].status; state = result_data.get("state")
                    analyze_state = "success" if state in {"success", "empty"} else "failed"
                    conn.execute("UPDATE run_documents SET document_version_id=?,analysis_id=?,read_state=?,analyze_state=?,finished_at=?,error_json=? WHERE id=?", (result_data["document_version_id"], result_data.get("analysis_id"), read_status, analyze_state, __import__('agenda_app.config', fromlist=['utc_now']).utc_now(), dumps(result_data["error"].as_dict()) if result_data.get("error") else None, run_doc_id))
                    if state in {"success", "empty"}: source_successful_docs += 1; successful_observations += 1
                    if state == "success": source_included += sum(1 for i in result_data.get("items", []) if i["decision"].included)
                    if state not in {"success", "empty"}: source_failed = True
                repo.update_run(run_id, heartbeat_at=__import__('agenda_app.config', fromlist=['utc_now']).utc_now())
            included += source_included
            state = "partial" if source_failed and source_successful_docs else ("failed" if source_failed else ("success" if source_included else "no_results"))
            if source_failed: failures += 1
            else: healthy += 1
            with transaction(paths) as conn:
                warning_json = dumps([e.as_dict() for e in result.warnings]) if result.warnings else None
                conn.execute("UPDATE run_sources SET state=?,discovery_state=?,found_count=?,downloaded_count=(SELECT COUNT(*) FROM run_documents WHERE run_source_id=? AND download_state IN ('downloaded','reused')),analyzed_count=(SELECT COUNT(*) FROM run_documents WHERE run_source_id=? AND analyze_state IN ('success','cached','empty')),finished_at=?,observed_at=?,error_json=? WHERE id=?", (state, "partial" if result.warnings else "success", len(result.candidates), run_source["id"], run_source["id"], __import__('agenda_app.config', fromlist=['utc_now']).utc_now(), __import__('agenda_app.config', fromlist=['utc_now']).utc_now(), warning_json, run_source["id"]))
        # A source with at least one persisted candidate is useful coverage
        # even when another document/source failed. Do not turn that into an
        # all-failed run merely because no source reached a terminal healthy
        # state.
        final_status = "failed" if successful_observations == 0 and healthy == 0 else ("partial" if failures else ("success" if included else "no_results"))
        reason = "all_sources_failed" if final_status == "failed" else "partial_source_failure" if final_status == "partial" else None
        check_deadline()
        final_phase = "publish" if final_status in {"success", "no_results"} else "done"
        final_publication_state = "pending" if final_status in {"success", "no_results"} else "preserved"
        committed, live = commit_final(final_status, final_phase, reason_code=reason, publication_state=final_publication_state)
        if not committed:
            return result_from_live(live, default_status=final_status, default_reason=reason)
        export = None
        if final_status in {"success", "no_results"}:
            from ..storage.exports import ExportService
            export = ExportService(repo).export_run(run_id, publish=True, owner_token=owner_token)
        return {"run_id": run_id, "status": final_status, "included": included, "failures": failures, "export_id": export["export_id"] if export else None, "manifest_path": export.get("manifest_path") if export else None}
    except RunDeadlineExceeded as exc:
        error = _error_dict(exc); current = repo.get_run(run_id); status = current["status"] if current else "failed"
        if status == "running": status = terminalize_deadline(repo, run_id)
        return {"run_id": run_id, "status": status, "included": included, "failures": max(1, failures), "reason_code": "run_deadline_exceeded"}
    except Exception as exc:
        current = repo.get_run(run_id)
        if not current or current.get("reason_code") != "run_deadline_exceeded":
            repo.finalize_run(run_id, owner_token=owner_token, status="failed", phase="done", reason_code="worker_exception", error_json={"code": "worker_exception", "message": str(exc)}, finished_at=utc_now(), publication_state="preserved")
        raise
    finally:
        stop_heartbeat.set(); beat.join(timeout=2); deadline_thread.join(timeout=2)


if __name__ == "__main__":
    from ..storage.repository import Repository
    data_dir, run_id = sys.argv[1], sys.argv[2]
    raw_owner_pid = os.environ.get("AGENDA_WORKER_OWNER_PID")
    owner_pid = int(raw_owner_pid) if raw_owner_pid and raw_owner_pid.isdigit() else None
    run_worker(Repository(data_dir), run_id, owner_pid=owner_pid)
