"""Stage-2 HTTP entrypoint and isolated stage-1 helper compatibility.

The live server is :mod:`agenda_app.web.server`; it serves review data through
JSON APIs and never embeds user JSON in an executable HTML script.  The CSV
helpers below are retained for old scripts only and are not used by the new
review service.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

ROOT_DIR = Path(__file__).resolve().parent
KEYWORD_SCORES_PATH = str(ROOT_DIR / "keyword_scores.csv")
FEEDBACK_PATH = str(ROOT_DIR / "priority_feedback.csv")
REVIEW_STATE_PATH = str(ROOT_DIR / "review_state.json")
TOWNLIST_PATH = str(ROOT_DIR / "resources" / "townlist.csv")
TEMPS_DIR = str(ROOT_DIR / "temps")
AGENDA_CSVS = [(p, str(ROOT_DIR / f"{p}.csv")) for p in ("high", "medium", "low")]
SCORE_FIELDNAMES = ["keyword", "priority_score", "examples", "updated_at"]
FEEDBACK_FIELDNAMES = ["timestamp", "filename", "item", "current_priority", "feedback", "matched_keywords", "score_delta"]
FEEDBACK_LOCK = threading.RLock(); VALID_PRIORITIES = {"low", "medium", "high"}; VALID_FEEDBACK = {"correct", "higher", "lower"}


def read_keyword_scores(path=KEYWORD_SCORES_PATH):
    if not os.path.exists(path): return []
    rows = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not row.get("keyword", "").strip(): continue
            try: score = int(float(row.get("priority_score", "0")))
            except ValueError: score = 0
            rows.append({"keyword": row["keyword"].strip().lower(), "priority_score": max(0, min(100, score)), "examples": row.get("examples", ""), "updated_at": row.get("updated_at", "")})
    return rows


def write_keyword_scores(rows, path=KEYWORD_SCORES_PATH):
    temp = f"{path}.tmp-{os.getpid()}"
    try:
        with open(temp, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=SCORE_FIELDNAMES); writer.writeheader()
            for row in rows: writer.writerow({field: row.get(field, "") for field in SCORE_FIELDNAMES})
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)
    except Exception:
        try: os.remove(temp)
        except OSError: pass
        raise


def match_keywords(text, keyword_rows):
    return [row["keyword"] for row in keyword_rows if re.search(r"(?<!\w)" + re.escape(row["keyword"]) + r"(?!\w)", text.lower())]


def _delta_for_feedback(current_priority, feedback):
    return {"higher": 8, "lower": -8, "correct": {"high": 3, "medium": 1, "low": -2}.get(current_priority, 0)}.get(feedback, 0)


def update_keyword_scores_for_feedback(text, current_priority, feedback, score_path=KEYWORD_SCORES_PATH):
    with FEEDBACK_LOCK:
        rows = read_keyword_scores(score_path); matched = match_keywords(text, rows); delta = _delta_for_feedback(current_priority, feedback); changes = []
        for row in rows:
            if row["keyword"] in matched:
                before = row["priority_score"]; row["priority_score"] = max(0, min(100, before + delta)); row["updated_at"] = datetime.now().isoformat(timespec="seconds"); changes.append({"keyword": row["keyword"], "before": before, "after": row["priority_score"]})
        write_keyword_scores(rows, score_path)
    return {"matched_keywords": matched, "delta": delta if matched else 0, "changes": changes}


def _review_item_id(filename, title, original_text=""):
    return hashlib.sha256("\0".join([filename.strip(), (original_text or title).strip()]).encode()).hexdigest()[:20]


def validate_feedback_payload(payload):
    if not isinstance(payload, dict): raise ValueError("Feedback body must be a JSON object")
    filename = str(payload.get("filename", "")).strip(); title = str(payload.get("title", "") or payload.get("item", "")).strip(); priority = str(payload.get("current_priority", "")).strip().lower(); feedback = str(payload.get("feedback", "")).strip().lower()
    if not filename or filename != os.path.basename(filename): raise ValueError("A safe agenda filename is required")
    if not title: raise ValueError("An agenda title is required")
    if priority not in VALID_PRIORITIES: raise ValueError("current_priority must be low, medium, or high")
    if feedback not in VALID_FEEDBACK: raise ValueError("feedback must be correct, higher, or lower")
    original = str(payload.get("original_text", "")).strip(); item_id = str(payload.get("item_id", "") or payload.get("id", "")).strip(); expected = _review_item_id(filename, title, original)
    if item_id and item_id != expected: raise ValueError("item_id does not match the agenda item")
    result = dict(payload); result.update({"item_id": expected, "filename": filename, "title": title, "item": title, "current_priority": priority, "feedback": feedback, "original_text": original}); return result


def _load_review_state(path=REVIEW_STATE_PATH):
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8")); return state if isinstance(state, dict) else {"items": {}, "submissions": {}}
    except (OSError, ValueError, TypeError): return {"items": {}, "submissions": {}}


def _write_review_state(state, path=REVIEW_STATE_PATH):
    Path(path).parent.mkdir(parents=True, exist_ok=True); temp = f"{path}.tmp-{os.getpid()}"; Path(temp).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"); os.replace(temp, path)


def record_feedback(payload, *, score_path=KEYWORD_SCORES_PATH, feedback_path=FEEDBACK_PATH, state_path=REVIEW_STATE_PATH):
    normalized = validate_feedback_payload(payload); key = hashlib.sha256(json.dumps({k: normalized.get(k, "") for k in ("item_id", "filename", "title", "original_text", "current_priority", "feedback")}, sort_keys=True).encode()).hexdigest()
    with FEEDBACK_LOCK:
        state = _load_review_state(state_path)
        if key in state.get("submissions", {}):
            result = dict(state["submissions"][key]["result"]); result.update({"idempotent": True, "item_id": normalized["item_id"]}); return result
        result = update_keyword_scores_for_feedback(" ".join([normalized["title"], normalized.get("original_text", "")]), normalized["current_priority"], normalized["feedback"], score_path)
        result.update({"idempotent": False, "item_id": normalized["item_id"]})
        exists = os.path.exists(feedback_path)
        with open(feedback_path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FEEDBACK_FIELDNAMES)
            if not exists: writer.writeheader()
            writer.writerow({"timestamp": datetime.now().isoformat(timespec="seconds"), "filename": normalized["filename"], "item": normalized["title"], "current_priority": normalized["current_priority"], "feedback": normalized["feedback"], "matched_keywords": "|".join(result["matched_keywords"]), "score_delta": result["delta"]})
        state.setdefault("items", {})[normalized["item_id"]] = {"reviewed": True, "filename": normalized["filename"], "title": normalized["title"], "current_priority": normalized["current_priority"], "feedback": normalized["feedback"], "timestamp": datetime.now().isoformat(timespec="seconds"), "last_submission": key, "score_changes": result["changes"]}
        state.setdefault("submissions", {})[key] = {"result": result, "timestamp": datetime.now().isoformat(timespec="seconds")}; _write_review_state(state, state_path); return result


def _date_from_filename(filename):
    match = re.search(r"_(\d{4}-\d{2}-\d{2})(?:_[A-Za-z0-9-]+)?\.", filename); return match.group(1) if match else ""


def _gov_key_from_filename(filename): return re.sub(r"_\d{4}-\d{2}-\d{2}(?:_[A-Za-z0-9-]+)?\.[^.]+$", "", filename)


def main(data_dir=None, port=8000):
    from agenda_app.web.server import create_server
    server = create_server(data_dir or (Path(ROOT_DIR) / "data"), port=port, legacy_root=ROOT_DIR)
    print(f"Agenda Monitor running at http://127.0.0.1:{port}"); server.serve_forever()


if __name__ == "__main__": main()
