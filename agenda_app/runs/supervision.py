from __future__ import annotations

import os
import signal
import threading
import time
from typing import Any

from ..config import parse_utc, utc_now
from ..storage.db import transaction
from ..storage.repository import Repository, dumps


DEADLINE_ERROR = {
    "code": "run_deadline_exceeded",
    "message": "configured whole-run deadline exceeded",
    "retryable": True,
    "stage": "run",
}

_CHILDREN_LOCK = threading.Lock()
_CHILDREN: dict[int, Any] = {}


def register_child(process: Any) -> None:
    with _CHILDREN_LOCK:
        _CHILDREN[process.pid] = process


def _registered_child(pid: int) -> Any | None:
    with _CHILDREN_LOCK:
        return _CHILDREN.get(pid)


def _forget_child(pid: int) -> None:
    with _CHILDREN_LOCK:
        _CHILDREN.pop(pid, None)


def terminalize_deadline(repo: Repository, run_id: str) -> str:
    """Make an expired run terminal before its owner is allowed to continue."""
    with transaction(repo.paths) as conn:
        run = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            return "failed"
        if run[0] != "running":
            return run[0]
        useful = int(conn.execute("SELECT COUNT(*) FROM run_sources WHERE run_id=? AND state IN ('success','no_results')", (run_id,)).fetchone()[0])
        status = "partial" if useful else "failed"
        now = utc_now()
        error = dumps(DEADLINE_ERROR)
        conn.execute("UPDATE run_documents SET download_state=CASE WHEN download_state IN ('running','pending') THEN 'failed' ELSE download_state END, analyze_state=CASE WHEN analyze_state IN ('running','pending') THEN 'failed' ELSE analyze_state END, finished_at=COALESCE(finished_at,?), error_json=COALESCE(error_json,?) WHERE run_source_id IN (SELECT id FROM run_sources WHERE run_id=?)", (now, error, run_id))
        conn.execute("UPDATE run_sources SET state=CASE WHEN state IN ('success','no_results') THEN state ELSE 'interrupted' END, finished_at=COALESCE(finished_at,?), error_json=COALESCE(error_json,?) WHERE run_id=?", (now, error, run_id))
        conn.execute("UPDATE runs SET status=?,phase='done',reason_code='run_deadline_exceeded',error_json=?,finished_at=?,publication_state='preserved' WHERE id=? AND status='running'", (status, error, now, run_id))
        return status


def _child_state(pid: int, process: Any | None = None) -> str:
    """Return child ownership/liveness without trusting a reused PID."""
    if process is not None:
        return "exited" if process.poll() is not None else "alive"
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return "exited"
    except ChildProcessError:
        # This process does not own the child (or another owner already
        # reaped it). Never turn the integer PID into a kill target.
        return "unowned"
    except ProcessLookupError:
        return "exited"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "exited"
    except PermissionError:
        return "unowned"
    return "alive"


def _wait_for_child_exit(pid: int, timeout: float, process: Any | None = None) -> str:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        state = _child_state(pid, process)
        if state != "alive":
            return state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "alive"
        time.sleep(min(0.01, remaining))


def _stop_and_reap(pid: int, grace_seconds: float, process: Any | None = None) -> None:
    """Bound child lifetime even if its own watchdog already terminalized the run."""
    if _wait_for_child_exit(pid, grace_seconds, process) != "alive":
        _forget_child(pid)
        return
    try:
        process.terminate() if process is not None else os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    if _wait_for_child_exit(pid, grace_seconds, process) != "alive":
        _forget_child(pid)
        return
    try:
        process.kill() if process is not None else os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return
    _wait_for_child_exit(pid, grace_seconds, process)
    _forget_child(pid)


def _reap_naturally(pid: int, process: Any | None = None) -> None:
    while _child_state(pid, process) == "alive":
        time.sleep(0.01)
    _forget_child(pid)


def supervise_worker(repo: Repository, run_id: str, pid: int, *, poll_seconds: float = 0.05, grace_seconds: float = 0.25) -> None:
    """Enforce a child-process deadline without waiting on its Python threads."""
    process = _registered_child(pid)
    while True:
        try:
            row = repo.get_run(run_id)
        except Exception:
            # The owning test/process may remove a temporary data directory
            # immediately after the child has already finished.
            _stop_and_reap(pid, grace_seconds, process)
            return
        if not row:
            _stop_and_reap(pid, grace_seconds, process)
            return
        if row["status"] != "running":
            # A child watchdog can persist terminal deadline state while the
            # worker's main thread is blocked in discovery, download, or
            # inference. Own the process lifetime before releasing the lock.
            if row.get("reason_code") == "run_deadline_exceeded":
                _stop_and_reap(pid, grace_seconds, process)
            else:
                threading.Thread(target=_reap_naturally, args=(pid, process), name=f"agenda-reaper-{pid}", daemon=True).start()
            return
        deadline = parse_utc(row.get("deadline_at"))
        if deadline is not None and parse_utc(utc_now()) >= deadline:
            _stop_and_reap(pid, grace_seconds, process)
            terminalize_deadline(repo, run_id)
            return
        time.sleep(poll_seconds)
