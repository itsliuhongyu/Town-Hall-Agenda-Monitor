from __future__ import annotations

import json
import time
from typing import Any

from ..analysis.ollama import OllamaClient, OllamaError
from ..analysis.policy import evaluate, validate_policy
from ..config import utc_now
from ..storage.db import transaction
from ..storage.repository import ConflictError, Repository, ValidationError, dumps, loads
from ..domain import new_id


class SettingsService:
    def __init__(self, repository: Repository): self.repo = repository

    def get(self) -> dict[str, Any]:
        revision, values = self.repo.get_settings()
        policy = self.repo.get_policy()
        last_import = self.repo.row("SELECT * FROM imports ORDER BY started_at DESC LIMIT 1")
        source_initialization = self.repo.row("SELECT * FROM source_initializations ORDER BY rowid DESC LIMIT 1")
        source_init = None
        if source_initialization:
            source_init = dict(source_initialization)
            source_init["summary"] = loads(source_init.pop("summary_json"), {})
            source_init["error"] = loads(source_init.pop("error_json"), None)
        return {"revision": revision, "values": values, "current_policy": policy, "model_state": self.repo.models_state(), "last_import": dict(last_import) if last_import else None, "source_initialization": source_init}

    def save(self, expected_revision: int, values: dict[str, Any], *, conn=None) -> dict[str, Any]:
        inventory = self.repo.models_state().get("models", [])
        return self.repo.save_settings(expected_revision, values, inventory=inventory, conn=conn)

    def refresh_models(self) -> dict[str, Any]:
        values = self.repo.get_settings()[1]; client = OllamaClient(values["ollama_base_url"], timeout=3)
        try:
            models = client.tags(); return self.repo.update_inventory(state="online", models=models)
        except OllamaError as exc:
            return self.repo.update_inventory(state="offline", models=self.repo.models_state().get("models", []), error={"code": exc.code, "message": str(exc), "retryable": exc.retryable})

    def test_connection(self) -> dict[str, Any]:
        values = self.repo.get_settings()[1]; selected = values.get("selected_model")
        from ..config import ModelSelection
        selection = ModelSelection(selected["name"], selected["digest"], values["ollama_base_url"]) if selected else None
        started = time.monotonic()
        try:
            state = OllamaClient(values["ollama_base_url"], timeout=3).check_selection(selection)
            self.repo.update_inventory(state="online", models=state["models"])
            return {**state, "latency_ms": round((time.monotonic() - started) * 1000)}
        except OllamaError as exc:
            self.repo.update_inventory(state="offline", models=self.repo.models_state().get("models", []), error={"code": exc.code, "message": str(exc)})
            return {"connection": "offline", "selection": "unverified" if selected else "none", "models": [], "error": {"code": exc.code, "message": str(exc)}}

    def policy(self) -> dict[str, Any]:
        return self.repo.get_policy()

    def update_policy(self, expected_revision: int, strategy: str, rules: list[dict[str, Any]], removals: list[dict[str, Any]], reason: str, *, conn=None) -> dict[str, Any]:
        if type(expected_revision) is not int: raise ValidationError("expected_revision must be an integer")
        validate_policy(strategy, rules, removals)
        owns_transaction = conn is None
        from contextlib import nullcontext
        context = transaction(self.repo.paths) if owns_transaction else nullcontext(conn)
        with context as conn:
            current = conn.execute("SELECT p.* FROM policy_versions p JOIN app_state a ON a.current_policy_id=p.id WHERE a.id=1").fetchone()
            if current["version_no"] != expected_revision: raise ConflictError("policy_conflict", {"current_revision": current["version_no"]})
            # Imports may retain a non-active policy version. The active CAS
            # still guards this edit, but the sequence must span every
            # retained version, not just the active chain.
            row_id = new_id(); version = int(conn.execute("SELECT COALESCE(MAX(version_no),0)+1 FROM policy_versions").fetchone()[0])
            conn.execute("INSERT INTO policy_versions(id,version_no,rules_json,removals_json,strategy,thresholds_json,change_reason,previous_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (row_id, version, dumps(rules), dumps(removals), strategy, dumps({"high": 80, "medium": 40}), reason[:500], current["id"], utc_now()))
            conn.execute("UPDATE app_state SET current_policy_id=?,data_revision=data_revision+1 WHERE id=1", (row_id,))
            return self.repo._policy_row(conn.execute("SELECT * FROM policy_versions WHERE id=?", (row_id,)).fetchone())

    def preview(self, run_id: str, strategy: str, rules: list[dict[str, Any]], removals: list[dict[str, Any]]) -> dict[str, Any]:
        validate_policy(strategy, rules, removals); policy = {"strategy": strategy, "rules": rules, "removals": removals, "thresholds": {"high": 80, "medium": 40}}
        rows = self.repo.rows("SELECT ri.item_version_id,iv.original_text,ai.model_priority,ri.proposed_priority FROM run_items ri JOIN item_versions iv ON iv.id=ri.item_version_id JOIN analysis_items ai ON ai.analysis_id=ri.analysis_id AND ai.item_version_id=ri.item_version_id WHERE ri.run_id=?", (run_id,))
        changed = excluded = 0; sample = []
        for row in rows:
            decision = evaluate(row["original_text"], row["model_priority"], policy)
            if decision.proposed_priority != row["proposed_priority"]: changed += 1
            if not decision.included: excluded += 1
            if len(sample) < 20: sample.append({"item_version_id": row["item_version_id"], "proposed_priority": decision.proposed_priority, "included": decision.included, "matched_rules": list(decision.matched_rules)})
        return {"examined": len(rows), "changed": changed, "excluded": excluded, "sample": sample}

    def undo_policy(self, version_id: str, expected_revision: int, reason: str, *, conn=None) -> dict[str, Any]:
        if type(expected_revision) is not int: raise ValidationError("expected_revision must be an integer")
        owns_transaction = conn is None
        from contextlib import nullcontext
        context = transaction(self.repo.paths) if owns_transaction else nullcontext(conn)
        with context as conn:
            current = conn.execute("SELECT p.* FROM policy_versions p JOIN app_state a ON a.current_policy_id=p.id WHERE a.id=1").fetchone()
            target = conn.execute("SELECT * FROM policy_versions WHERE id=?", (version_id,)).fetchone()
            if not target or target["id"] != current["id"]: raise ConflictError("policy_undo_not_latest", {})
            if current["version_no"] != expected_revision: raise ConflictError("policy_conflict", {"current_revision": current["version_no"]})
            if not current["previous_id"]: raise ConflictError("policy_undo_not_allowed", {})
            previous = conn.execute("SELECT * FROM policy_versions WHERE id=?", (current["previous_id"],)).fetchone(); new_id_value = new_id()
            version = int(conn.execute("SELECT COALESCE(MAX(version_no),0)+1 FROM policy_versions").fetchone()[0])
            conn.execute("INSERT INTO policy_versions(id,version_no,rules_json,removals_json,strategy,thresholds_json,change_reason,previous_id,undo_of,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (new_id_value, version, previous["rules_json"], previous["removals_json"], previous["strategy"], previous["thresholds_json"], reason[:500], current["id"], current["id"], utc_now()))
            conn.execute("UPDATE app_state SET current_policy_id=?,data_revision=data_revision+1 WHERE id=1", (new_id_value,))
            return self.repo._policy_row(conn.execute("SELECT * FROM policy_versions WHERE id=?", (new_id_value,)).fetchone())
