"""Pure publication/mail eligibility checks used by the weekly workflow.

The function only reads a run result and its current manifest. It never sends
mail and never falls back to a root CSV.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def eligible_mail(result: dict[str, Any], manifest_path: str | Path) -> dict[str, Any]:
    path = Path(manifest_path)
    if result.get("status") != "success" or result.get("kind", "full") == "retry":
        return {"eligible": False, "reason": "run_not_full_success"}
    if not result.get("run_id") or not result.get("export_id") or not path.is_file():
        return {"eligible": False, "reason": "missing_run_export_manifest"}
    manifest = __import__("json").loads(path.read_text(encoding="utf-8"))
    if manifest.get("run_id") != result["run_id"] or manifest.get("export_id") != result["export_id"]:
        return {"eligible": False, "reason": "run_export_mismatch"}
    if manifest.get("publication_state") != "published":
        return {"eligible": False, "reason": "not_published"}
    high = manifest.get("files", {}).get("high", {})
    high_path = path.parent / str(high.get("path", ""))
    if int(high.get("row_count", 0)) <= 0 or not high_path.is_file():
        return {"eligible": False, "reason": "no_high_rows"}
    digest = hashlib.sha256(high_path.read_bytes()).hexdigest()
    if digest != high.get("sha256"):
        return {"eligible": False, "reason": "high_hash_mismatch"}
    return {"eligible": True, "run_id": result["run_id"], "export_id": result["export_id"], "high_csv": str(high_path), "sha256": digest}
