"""Durable agenda pipeline CLI.

The application path is intentionally a thin entrypoint around the SQLite
run service.  The old CSV-only implementation lives in ``legacy_pipeline``
solely for callers that explicitly import ``run_pipeline()`` without a data
directory; it is not reachable from this command-line entrypoint.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from agenda_app.runs.service import RunService
from agenda_app.storage.repository import ConflictError, Repository

# Compatibility callables only; importing the durable CLI does not load the
# retired analyzer or browser collector.
def process_agendas(*args, **kwargs):
    from run_weekly import process_agendas as collect
    return collect(*args, **kwargs)


def analyze_agendas(*args, **kwargs):
    from analyze_agendas import main as analyze
    return analyze(*args, **kwargs)


def add_url_column_to_high_csv(lookup, csv_path="./high.csv"):
    """Compatibility export for the retired stage-1 CSV helper."""
    from legacy_pipeline import add_url_column_to_high_csv as add_urls
    return add_urls(lookup, csv_path)


def run_pipeline(data_dir=None, *, json_output=False, background=False, source_ids=None, model=None):
    """Start the durable SQLite run.

    ``data_dir`` is required for the real application path.  A no-argument
    call remains a Python-only compatibility hook for the preserved stage-1
    regression suite and delegates to the explicitly named legacy module.
    """
    if data_dir is None and not os.environ.get("AGENDA_DATA_DIR"):
        from legacy_pipeline import run_legacy_pipeline
        return run_legacy_pipeline(processor=process_agendas, analyzer=analyze_agendas)

    root = Path(data_dir or os.environ["AGENDA_DATA_DIR"]).resolve()
    repo = Repository(root)
    snapshot = None
    selected_name = model or os.environ.get("AGENDA_MODEL")
    if selected_name:
        from agenda_app.config import ConfigSnapshot, utc_now
        settings_revision, values = repo.get_settings()
        inventory_state = repo.models_state()
        if inventory_state.get("connection") != "online":
            raise ValueError("a CLI model override requires a current online inventory")
        match = next((row for row in inventory_state.get("models", []) if row.get("name") == selected_name), None)
        if not match:
            raise ValueError(f"model '{selected_name}' is not present in the latest exact inventory")
        values["selected_model"] = {"name": match["name"], "digest": match["digest"], "verified_at": utc_now()}
        policy = repo.get_policy()
        snapshot = ConfigSnapshot(values, policy, settings_revision, policy["id"])
    result = RunService(repo).start(kind="full", source_ids=source_ids, background=background, snapshot=snapshot)
    if not background and result.get("status") in {"success", "no_results"} and not result.get("export_id"):
        result["export_id"] = repo.row("SELECT latest_export_id FROM app_state WHERE id=1")[0]
    if json_output:
        print(json.dumps(result, ensure_ascii=False))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the durable agenda pipeline")
    parser.add_argument("--data-dir", default=os.environ.get("AGENDA_DATA_DIR", str(Path(__file__).with_name("data"))))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--model")
    args = parser.parse_args(argv)
    try:
        result = run_pipeline(args.data_dir, json_output=args.json, model=args.model)
    except ConflictError as exc:
        result = {"status": "conflict", "code": exc.code, "details": exc.details}
        print(json.dumps(result, ensure_ascii=False))
        return 3
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted", "code": "interrupted"}, ensure_ascii=False))
        return 130
    status = result.get("status")
    return 0 if status in {"success", "no_results"} else 3 if status in {"running", "conflict"} else 2 if status == "partial" else 130 if status == "interrupted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
