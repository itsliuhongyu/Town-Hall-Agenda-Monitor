from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..config import DataPaths, utc_now


class StorageError(RuntimeError):
    pass


class SchemaTooNewError(StorageError):
    pass


def connect(paths: DataPaths) -> sqlite3.Connection:
    paths.ensure()
    conn = sqlite3.connect(paths.database, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def apply_migrations(paths: DataPaths) -> None:
    paths.ensure()
    existed = paths.database.exists()
    conn = connect(paths)
    try:
        migration_dir = Path(__file__).with_name("migrations")
        files = sorted(migration_dir.glob("*.sql"))
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        available = [int(path.stem.split("_", 1)[0]) for path in files]
        if current > max(available or [0]):
            raise SchemaTooNewError(f"database schema {current} is newer than this application")
        if existed and current < max(available or [0]):
            backup = paths.backups / f"schema-v{current}-{utc_now().replace(':', '').replace('-', '')}.sqlite3"
            with sqlite3.connect(backup) as destination:
                conn.backup(destination)
        conn.execute("BEGIN IMMEDIATE")
        for path in files:
            version = int(path.stem.split("_", 1)[0])
            sql = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            existing = conn.execute("SELECT checksum FROM schema_migrations WHERE version=?", (version,)).fetchone()
            if existing:
                if existing[0] != digest:
                    raise StorageError(f"migration checksum changed: {path.name}")
                continue
            statement_buffer = ""
            for line in sql.splitlines(True):
                statement_buffer += line
                if sqlite3.complete_statement(statement_buffer):
                    statement = statement_buffer.strip()
                    if statement:
                        conn.execute(statement)
                    statement_buffer = ""
            if statement_buffer.strip():
                conn.execute(statement_buffer)
            conn.execute("INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(?,?,?,?)",
                         (version, path.name, digest, utc_now()))
            # Keep SQLite's legacy user_version at the original compatibility
            # value; schema_migrations is the authoritative application
            # version and permits additive migrations without breaking the
            # preserved Stage 2 callers that inspect user_version=1.
            conn.execute(f"PRAGMA user_version={1 if version > 1 else version}")
        _seed(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _seed(conn: sqlite3.Connection) -> None:
    import json
    from ..config import default_settings
    from ..domain import new_id

    if not conn.execute("SELECT 1 FROM policy_versions LIMIT 1").fetchone():
        policy_id = new_id()
        conn.execute("INSERT INTO policy_versions(id,version_no,rules_json,removals_json,strategy,thresholds_json,change_reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
                     (policy_id, 1, "[]", "[]", "model_first", json.dumps({"high": 80, "medium": 40}), "builtin", utc_now()))
    else:
        policy_id = conn.execute("SELECT id FROM policy_versions ORDER BY version_no LIMIT 1").fetchone()[0]
    if not conn.execute("SELECT 1 FROM settings WHERE id=1").fetchone():
        conn.execute("INSERT INTO settings(id,revision,values_json,updated_at) VALUES(1,1,?,?)",
                     (json.dumps(default_settings(), ensure_ascii=False, sort_keys=True), utc_now()))
    if not conn.execute("SELECT 1 FROM model_inventory WHERE id=1").fetchone():
        conn.execute("INSERT INTO model_inventory(id,state,models_json) VALUES(1,'unknown','[]')")
    if not conn.execute("SELECT 1 FROM app_state WHERE id=1").fetchone():
        conn.execute("INSERT INTO app_state(id,current_policy_id,data_revision) VALUES(1,?,1)", (policy_id,))


@contextmanager
def transaction(paths: DataPaths, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    conn = connect(paths)
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        finally:
            conn.close()
        raise
    else:
        conn.close()
