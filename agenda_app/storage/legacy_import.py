from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..analysis.policy import evaluate
from ..config import LEGACY_ROOT, canonical_url, ensure_timezone, identity_key, utc_now
from ..domain import SourceSnapshot, new_id
from ..runs.downloads import retain_blob
from ..storage.db import transaction
from .repository import Repository, dumps, loads

LEGACY_FILES = {
    "townlist": ("resources/townlist.csv", "townlist.csv"),
    "high": ("high.csv",), "medium": ("medium.csv",), "low": ("low.csv",),
    "keyword_scores": ("keyword_scores.csv",), "removal_keywords": ("removal_keywords.csv",),
    "priority_feedback": ("priority_feedback.csv",), "review_state": ("review_state.json",),
}
KNOWN_PLATFORMS = {"townweb": "TownWeb", "boarddocs": "BoardDocs", "civicclerk": "CivicClerk"}
PRIORITIES = {"low", "medium", "high"}


def _csv_rows(raw: bytes) -> list[dict[str, str]]:
    """Parse without splitlines so quoted multiline fields stay byte-faithful."""
    stream = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8-sig", newline="")
    try:
        return [dict(row) for row in csv.DictReader(stream)]
    finally:
        stream.detach()


def _text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


class LegacyImporter:
    def __init__(self, repository: Repository): self.repo = repository

    def _find(self, root: Path, choices: tuple[str, ...]) -> Path | None:
        for choice in choices:
            path = root / choice
            if path.is_file(): return path
        return None

    @staticmethod
    def _platform(row: dict[str, str]) -> str:
        # The maintained legacy schema stores the operational adapter in
        # agenda-location; agenda-format is a presentation/file-format field
        # ("html" in the real townlist) and must never select an adapter.
        if "agenda-location" in row:
            value = _text(row.get("agenda-location")).strip()
        else:
            value = _text(row.get("platform") or row.get("agenda-format") or "").strip()
        return KNOWN_PLATFORMS.get(value.casefold(), value or "legacy")

    @staticmethod
    def _source_fields(row: dict[str, str]) -> tuple[str, str, str, str, bool, bool]:
        name = _text(row.get("Gov body") or row.get("gov_body") or row.get("name"))
        url = _text(row.get("Link to board agenda site") or row.get("source_url") or row.get("collection_url"))
        timezone = _text(row.get("timezone") or row.get("Time zone") or "America/Chicago").strip() or "America/Chicago"
        explicit_timezone = bool(row.get("timezone") or row.get("Time zone"))
        platform = LegacyImporter._platform(row)
        parseable_value = _text(row.get("parseable")).strip().casefold()
        # Older hand-built bundles did not have the real townlist column;
        # retain their known-platform behavior while requiring y in the
        # maintained schema.
        parseable = parseable_value == "y" or ("parseable" not in row and platform in KNOWN_PLATFORMS.values())
        return name, url, platform, timezone, explicit_timezone, parseable

    def import_directory(self, source_root: str | Path = LEGACY_ROOT) -> dict[str, Any]:
        root = Path(source_root).resolve()
        entries = []
        for kind, choices in LEGACY_FILES.items():
            path = self._find(root, choices); raw = path.read_bytes() if path else b""
            entries.append((kind, path, raw, hashlib.sha256(raw).hexdigest()))
        bundle_hash = hashlib.sha256("".join(f"{kind}:{digest}" for kind, _, _, digest in sorted(entries)).encode()).hexdigest()
        existing = self.repo.row("SELECT id,summary_json FROM imports WHERE bundle_hash=?", (bundle_hash,))
        if existing:
            return {"import_id": existing[0], "idempotent": True, **(loads(existing[1], {}) or {})}

        import_id = new_id(); backup = self.repo.paths.backups / "imports" / import_id; backup.mkdir(parents=True, exist_ok=True)
        for kind, path, raw, _ in entries:
            if path: (backup / f"{kind}-{path.name}").write_bytes(raw)
        by_kind = {kind: (path, raw, digest) for kind, path, raw, digest in entries}
        summary: dict[str, Any] = {"imported": 0, "duplicate": 0, "unresolved": 0, "invalid": 0, "warnings": [], "sources": 0, "source_duplicates": 0, "source_duplicate_reasons": {}, "policy": None}
        original_files: dict[str, tuple[str, int, str]] = {}
        temps = root / "temps"
        if temps.is_dir():
            for candidate in sorted(temps.iterdir(), key=lambda p: p.name.casefold()):
                if not candidate.is_file() or candidate.name == "agenda_manifest.json": continue
                try:
                    raw = candidate.read_bytes(); digest = hashlib.sha256(raw).hexdigest()
                    original_files[candidate.name] = (retain_blob(self.repo.paths.root, candidate, digest), len(raw), digest)
                except OSError as exc: summary["warnings"].append(f"{candidate.name}: {exc}")

        with transaction(self.repo.paths) as conn:
            conn.execute("INSERT INTO imports(id,bundle_hash,source_root,state,started_at,summary_json) VALUES(?,?,?,?,?,?)", (import_id, bundle_hash, str(root), "running", utc_now(), dumps(summary)))
            settings = conn.execute("SELECT values_json FROM settings WHERE id=1").fetchone()
            current_policy = conn.execute("SELECT p.* FROM policy_versions p JOIN app_state a ON a.current_policy_id=p.id WHERE a.id=1").fetchone()
            now = utc_now(); legacy_run_id = new_id(); seq = int(conn.execute("SELECT COALESCE(MAX(created_seq),0)+1 FROM runs").fetchone()[0])
            conn.execute("INSERT INTO runs(id,kind,status,phase,reason_code,requested_at,started_at,finished_at,heartbeat_at,owner_token,settings_snapshot_json,policy_version_id,source_snapshot_json,total_sources,publication_state,created_seq,deadline_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (legacy_run_id, "analyze_only", "success", "done", "legacy_import", now, now, now, now, legacy_run_id, settings[0], current_policy["id"], "[]", 0, "none", seq, now))
            summary["run_id"] = legacy_run_id
            source_cache: dict[str, tuple[str, SourceSnapshot]] = {}

            def diag(kind: str, digest: str, row_no: int, payload: dict[str, Any], status: str, reason: str | None = None, *, entity_type=None, entity_id=None, row_key=None):
                key = row_key or identity_key(kind, row_no, dumps(payload))
                conn.execute("INSERT OR IGNORE INTO import_rows(id,import_id,file_kind,file_hash,row_no,row_key,payload_json,status,entity_type,entity_id,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                             (new_id(), import_id, kind, digest, row_no, key, dumps(payload), status, entity_type, entity_id, reason))

            def source_for(name: str, url: str, platform: str, timezone: str, explicit: bool, enabled: bool | None = None):
                if not name or not url: return None
                try: normalized = canonical_url(url); ensure_timezone(timezone)
                except ValueError as exc:
                    summary["invalid"] += 1; summary["warnings"].append(f"{name}: {exc}"); return None
                key = identity_key(platform, normalized, name)
                if key in source_cache: return source_cache[key]
                old = conn.execute("SELECT * FROM sources WHERE identity_key=?", (key,)).fetchone()
                if old:
                    sid = old["id"]; snapshot = SourceSnapshot(sid, old["platform"], old["name"], old["collection_url"], old["timezone"], loads(old["config_json"], {}))
                else:
                    sid = new_id(); created = utc_now(); enabled_value = bool(enabled if enabled is not None else platform in KNOWN_PLATFORMS.values())
                    conn.execute("INSERT INTO sources(id,identity_key,platform,name,collection_url,timezone,timezone_origin,enabled,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                 (sid, key, platform, name, normalized, timezone, "explicit" if explicit else "legacy_assumed", int(enabled_value), dumps({"legacy": True, "import_id": import_id}), created, created))
                    snapshot = SourceSnapshot(sid, platform, name, normalized, timezone, {"legacy": True})
                source_cache[key] = (sid, snapshot)
                if not conn.execute("SELECT 1 FROM run_sources WHERE run_id=? AND source_id=?", (legacy_run_id, sid)).fetchone():
                    conn.execute("INSERT INTO run_sources(id,run_id,source_id,source_snapshot_json,window_start,window_end,timezone,state,discovery_state,started_at,finished_at,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (new_id(), legacy_run_id, sid, dumps({"id": sid, "platform": snapshot.platform, "name": snapshot.name, "collection_url": snapshot.collection_url, "timezone": snapshot.timezone, "config": snapshot.config}), "0001-01-01", "9999-12-31", snapshot.timezone, "success", "success", now, now, now))
                    conn.execute("UPDATE runs SET total_sources=total_sources+1 WHERE id=?", (legacy_run_id,))
                return source_cache[key]

            # Townlist is operational input, not just an archival backup.
            town_path, town_raw, town_hash = by_kind["townlist"]
            try: town_rows = _csv_rows(town_raw) if town_path and town_raw else []
            except Exception as exc: town_rows = []; summary["invalid"] += 1; summary["warnings"].append(f"townlist: {exc}")
            for row_no, row in enumerate(town_rows, 2):
                name, url, platform, timezone, explicit, parseable = self._source_fields(row)
                if not name or not url:
                    diag("townlist", town_hash, row_no, row, "unresolved", "source name or URL is blank"); summary["unresolved"] += 1; continue
                if platform not in KNOWN_PLATFORMS.values():
                    diag("townlist", town_hash, row_no, row, "unresolved", f"unsupported platform: {platform}"); summary["unresolved"] += 1; continue
                if not parseable:
                    diag("townlist", town_hash, row_no, row, "unresolved", "legacy row is not marked parseable=y"); summary["unresolved"] += 1; continue
                before_source_count = len(source_cache)
                if source_for(name, url, platform, timezone, explicit, enabled=True):
                    summary["sources"] += 1
                    if len(source_cache) == before_source_count:
                        summary["source_duplicates"] += 1
                        reasons = summary["source_duplicate_reasons"]
                        reasons["same platform + normalized URL + normalized name"] = reasons.get("same platform + normalized URL + normalized name", 0) + 1

            # One imported, inspectable policy. It is activated only when the
            # current policy is still the untouched builtin policy.
            rules: list[dict[str, Any]] = []; removals: list[dict[str, Any]] = []
            score_path, score_raw, score_hash = by_kind["keyword_scores"]
            if score_path and score_raw:
                try:
                    for row_no, row in enumerate(_csv_rows(score_raw), 2):
                        keyword = _text(row.get("keyword")); score = int(_text(row.get("priority_score")))
                        if not keyword or not 0 <= score <= 100: raise ValueError(f"row {row_no}: invalid keyword score")
                        rules.append({"id": identity_key("legacy-rule", keyword), "keyword": keyword, "score": score, "match": "word_boundary", "enabled": True, "reason": _text(row.get("examples"))})
                except Exception as exc: summary["invalid"] += 1; summary["warnings"].append(f"{score_path.name}: {exc}")
            removal_path, removal_raw, removal_hash = by_kind["removal_keywords"]
            if removal_path and removal_raw:
                try:
                    for row_no, row in enumerate(_csv_rows(removal_raw), 2):
                        keyword = _text(row.get("keyword"))
                        if not keyword: raise ValueError(f"row {row_no}: keyword is blank")
                        removals.append({"id": identity_key("legacy-removal", keyword), "text": keyword, "enabled": True, "reason": _text(row.get("reason"))})
                except Exception as exc: summary["invalid"] += 1; summary["warnings"].append(f"{removal_path.name}: {exc}")
            imported_policy = self._store_policy(conn, current_policy, rules, removals, import_id)
            builtin = current_policy["version_no"] == 1 and current_policy["change_reason"] == "builtin" and not loads(current_policy["rules_json"], []) and not loads(current_policy["removals_json"], [])
            if builtin:
                conn.execute("UPDATE app_state SET current_policy_id=?,data_revision=data_revision+1 WHERE id=1", (imported_policy,)); activated = True
            else:
                summary["warnings"].append("imported policy retained; active user policy was preserved"); activated = False
            summary["policy"] = {"id": imported_policy, "rules": rules, "removals": removals, "activated": activated}
            active_policy_id = imported_policy if activated else current_policy["id"]

            # Parse priority files first, then group by document. File name and
            # priority bucket are never part of item identity.
            groups: dict[tuple[str, str, str, str], list[tuple[str, int, dict[str, str], str]]] = defaultdict(list)
            for kind in ("high", "medium", "low"):
                path, raw, digest = by_kind[kind]
                if not path or not raw: continue
                try: rows = _csv_rows(raw)
                except Exception as exc: summary["invalid"] += 1; summary["warnings"].append(f"{path.name}: {exc}"); continue
                for row_no, row in enumerate(rows, 2):
                    filename = _text(row.get("filename")); title = _text(row.get("item") or row.get("title")); original = _text(row.get("original_text"))
                    source_url = _text(row.get("Link to board agenda site") or row.get("source_url")); body = _text(row.get("gov_body") or row.get("Gov body") or "Legacy source"); meeting_date = _text(row.get("meeting_date"))
                    try: normalized_source_url = canonical_url(source_url) if source_url else ""
                    except ValueError: normalized_source_url = ""
                    source_match = next((item for item in source_cache.values() if item[1].collection_url == normalized_source_url), None) if normalized_source_url else None
                    if not title.strip() or not filename:
                        diag(kind, digest, row_no, row, "unresolved", "blank title or filename"); summary["unresolved"] += 1; continue
                    if not source_match:
                        if town_path is None:
                            source_match = source_for(body, source_url or "https://legacy.invalid/", "legacy", "America/Chicago", False, enabled=False)
                        else:
                            diag(kind, digest, row_no, row, "unresolved", "source URL is not an imported parseable townlist row"); summary["unresolved"] += 1; continue
                    importance = _text(row.get("importance")).strip().lower() or kind
                    if importance not in PRIORITIES:
                        diag(kind, digest, row_no, row, "invalid", "invalid importance"); summary["invalid"] += 1; continue
                    groups[(source_match[0], filename, meeting_date, source_url)].append((kind, row_no, row, importance))

            item_lookup: dict[str, str] = {}
            for (source_id, filename, meeting_date, source_url), rows in sorted(groups.items()):
                source = next((item[1] for item in source_cache.values() if item[0] == source_id), SourceSnapshot(source_id, "legacy", "Legacy source", source_url or "https://legacy.invalid/", "America/Chicago", {"legacy": True}))
                by_item: dict[str, list[tuple[str, int, dict[str, str], str]]] = defaultdict(list)
                for row in rows:
                    by_item[identity_key(_text(row[2].get("original_text")), _text(row[2].get("item") or row[2].get("title")))].append(row)
                item_rows = sorted(by_item.items())
                evidence_payload = [{"title": _text(v[0][2].get("item") or v[0][2].get("title")), "original_text": _text(v[0][2].get("original_text")), "meeting_date": meeting_date} for _, v in item_rows]
                digest = original_files.get(filename, (None, 0, None))[2] or hashlib.sha256(dumps(evidence_payload).encode()).hexdigest()
                conn.execute("SAVEPOINT legacy_document")
                try:
                    heading = _text(rows[0][2].get("gov_body") or rows[0][2].get("Gov body") or "Legacy meeting")
                    meeting_key = identity_key("legacy", source_id, meeting_date, heading, filename)
                    meeting = conn.execute("SELECT id FROM meetings WHERE source_id=? AND identity_key=?", (source_id, meeting_key)).fetchone()
                    meeting_id = meeting[0] if meeting else new_id()
                    if not meeting:
                        conn.execute("INSERT INTO meetings(id,source_id,identity_key,identity_kind,title,local_date,timezone,raw_date,time_quality,status,created_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (meeting_id, source_id, meeting_key, "legacy", heading, meeting_date or None, source.timezone, meeting_date, "date_only" if meeting_date else "unknown", "unknown", utc_now(), utc_now()))
                    doc_identity = identity_key("legacy", source_id, filename); doc = conn.execute("SELECT id FROM documents WHERE meeting_id=? AND identity_key=?", (meeting_id, doc_identity)).fetchone(); document_id = doc[0] if doc else new_id()
                    if not doc:
                        conn.execute("INSERT INTO documents(id,meeting_id,identity_key,kind,original_url,source_url,display_filename,created_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?)", (document_id, meeting_id, doc_identity, "legacy", _text(rows[0][2].get("download_url")) or None, source_url or source.collection_url, filename, utc_now(), utc_now()))
                    retained = original_files.get(filename)
                    version_id, _ = self.repo.ensure_document_version(conn, document_id, sha256=digest, blob_relpath=retained[0] if retained else None, byte_size=retained[1] if retained else None, media_type="application/pdf" if filename.casefold().endswith(".pdf") else "text/plain", fetched_url=source_url or None, original_state="available" if retained else "missing", read_status="readable", reader_version="legacy")
                    analysis_id = self.repo.create_analysis(conn, version_id, extraction_key=f"legacy:{import_id}:{document_id}:{version_id}", model_name=None, model_digest=None, reader_version="legacy", extractor_version="legacy", prompt_hash="legacy", parameters={}, snapshot=loads(settings[0], {}), state="legacy_import", cache_hit=False)
                    policy_row = conn.execute("SELECT * FROM policy_versions WHERE id=?", (active_policy_id,)).fetchone(); policy = {"rules": loads(policy_row["rules_json"], []), "removals": loads(policy_row["removals_json"], []), "strategy": policy_row["strategy"], "thresholds": loads(policy_row["thresholds_json"], {})}
                    persisted_count = 0
                    for ordinal, (item_key, candidates) in enumerate(item_rows):
                        conn.execute("SAVEPOINT legacy_item")
                        try:
                            first = candidates[0]; row = first[2]; title = _text(row.get("item") or row.get("title")); original = _text(row.get("original_text")); suggestions = {candidate[3] for candidate in candidates}; priority = next(value for value in ("high", "medium", "low") if value in suggestions)
                            for duplicate in candidates[1:]:
                                diag(duplicate[0], by_kind[duplicate[0]][2], duplicate[1], duplicate[2], "duplicate", "same business identity as another priority row")
                                summary["duplicate"] += 1
                            if len(suggestions) > 1:
                                diag(first[0], by_kind[first[0]][2], first[1], row, "unresolved", "conflicting importance suggestions", row_key=identity_key("conflict", source_id, filename, item_key)); summary["unresolved"] += 1
                                conn.execute("RELEASE legacy_item")
                                continue
                            item_identity = identity_key("legacy-item", source_id, filename, original, title)
                            prior_item = conn.execute("SELECT 1 FROM items WHERE document_id=? AND identity_key=?", (document_id, item_identity)).fetchone()
                            item_version_id = self.repo.create_item_version(conn, document_id, version_id, original_text=original, title=title, priority=priority, reason=None, anchor={"status": "legacy"}, analysis_id=analysis_id, ordinal=ordinal, origin="legacy", model_priority=priority, identity=item_identity, reuse_existing=True)
                            legacy_note = row.get("note") if "note" in row else row.get("notes")
                            review = conn.execute("SELECT note,row_version FROM review_state WHERE item_version_id=?", (item_version_id,)).fetchone()
                            # A blank note is a meaningful human edit. The
                            # raw legacy value may be restored only while the
                            # imported review row is still untouched; once a
                            # review revision exists, a changed sibling bundle
                            # must not resurrect the CSV note.
                            untouched = bool(review and review[1] == 1 and not conn.execute("SELECT 1 FROM review_events WHERE item_version_id=?", (item_version_id,)).fetchone())
                            if isinstance(legacy_note, str) and legacy_note and untouched and not review[0]:
                                conn.execute("UPDATE review_state SET note=?,updated_at=? WHERE item_version_id=?", (legacy_note, utc_now(), item_version_id))
                            decision = evaluate(original, priority, policy)
                            self.repo.add_run_item(conn, run_id=legacy_run_id, item_version_id=item_version_id, analysis_id=analysis_id, policy_version_id=active_policy_id, policy_priority=decision.policy_priority, proposed_priority=decision.proposed_priority, decision_source="legacy", matched_rules=list(decision.matched_rules), included=decision.included, exclusion_reason=decision.exclusion_reason, source_run_id=legacy_run_id)
                            item_lookup[identity_key(filename, title, original)] = item_version_id
                            status = "duplicate" if prior_item else "imported"
                            diag(first[0], by_kind[first[0]][2], first[1], row, status, "business item already retained" if prior_item else None, entity_type="item_version", entity_id=item_version_id, row_key=identity_key("item", source_id, filename, original, title))
                            summary[status] += 1; persisted_count += 1
                            conn.execute("RELEASE legacy_item")
                        except Exception as exc:
                            conn.execute("ROLLBACK TO legacy_item"); conn.execute("RELEASE legacy_item")
                            summary["invalid"] += len(candidates); summary["warnings"].append(f"{filename} row {candidates[0][1]}: {exc}")
                            for candidate_kind, candidate_row_no, candidate_row, _ in candidates:
                                diag(candidate_kind, by_kind[candidate_kind][2], candidate_row_no, candidate_row, "invalid", str(exc))
                    run_source = conn.execute("SELECT id FROM run_sources WHERE run_id=? AND source_id=?", (legacy_run_id, source_id)).fetchone()
                    conn.execute("UPDATE run_sources SET found_count=found_count+?,downloaded_count=downloaded_count+?,analyzed_count=analyzed_count+? WHERE id=?", (persisted_count, persisted_count, persisted_count, run_source[0]))
                    conn.execute("RELEASE legacy_document")
                except Exception as exc:
                    conn.execute("ROLLBACK TO legacy_document"); conn.execute("RELEASE legacy_document"); summary["invalid"] += len(rows); summary["warnings"].append(f"{filename}: {exc}")
                    for kind, row_no, row, _ in rows: diag(kind, by_kind[kind][2], row_no, row, "invalid", str(exc))

            self._feedback(conn, by_kind["priority_feedback"], item_lookup, summary, diag, "priority_feedback")
            self._feedback(conn, by_kind["review_state"], item_lookup, summary, diag, "review_state", json_mode=True)
            conn.execute("UPDATE runs SET source_snapshot_json=? WHERE id=?", (dumps([dict(row) for row in conn.execute("SELECT source_id,state FROM run_sources WHERE run_id=?", (legacy_run_id,)).fetchall()]), legacy_run_id))
            summary["duplicate"] = int(conn.execute("SELECT COUNT(*) FROM import_rows WHERE import_id=? AND status='duplicate'", (import_id,)).fetchone()[0])
            self.repo.bump_revision(conn)
            conn.execute("UPDATE imports SET state=?,finished_at=?,summary_json=? WHERE id=?", ("success" if not summary["invalid"] else "partial", utc_now(), dumps(summary), import_id))
        return {"import_id": import_id, "idempotent": False, **summary}

    @staticmethod
    def _store_policy(conn, current, rules, removals, import_id: str) -> str:
        row = conn.execute("SELECT id FROM policy_versions WHERE rules_json=? AND removals_json=? AND strategy='model_first'", (dumps(rules), dumps(removals))).fetchone()
        if row: return row[0]
        version = int(conn.execute("SELECT COALESCE(MAX(version_no),0)+1 FROM policy_versions").fetchone()[0]); policy_id = new_id()
        conn.execute("INSERT INTO policy_versions(id,version_no,rules_json,removals_json,strategy,thresholds_json,change_reason,previous_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (policy_id, version, dumps(rules), dumps(removals), "model_first", dumps({"high": 80, "medium": 40}), f"legacy import {import_id}", current["id"], utc_now()))
        return policy_id

    @staticmethod
    def _feedback(conn, entry, item_lookup, summary, diag, kind: str, *, json_mode=False):
        path, raw, digest = entry
        if not path or not raw: return
        try:
            if json_mode:
                value = json.loads(raw.decode("utf-8")); rows = list(value.get("items", {}).values()) if isinstance(value, dict) else value
            else: rows = _csv_rows(raw)
        except Exception as exc: summary["invalid"] += 1; summary["warnings"].append(f"{path.name}: {exc}"); return
        for row_no, row in enumerate(rows or [], 1):
            payload = row if isinstance(row, dict) else {"value": row}; filename = _text(payload.get("filename")); title = _text(payload.get("item") or payload.get("title")); original = _text(payload.get("original_text")); keys = [key for key in item_lookup if (not filename or key.startswith(identity_key(filename))) and ((title and title.casefold() in key) or (original and original.casefold() in key))]
            if len(keys) != 1:
                diag(kind, digest, row_no, payload, "unresolved", "feedback did not uniquely identify one imported item"); summary["unresolved"] += 1; continue
            item_id = item_lookup[keys[0]]; feedback = _text(payload.get("feedback") or payload.get("state") or payload.get("review"))
            if feedback.casefold() not in {"correct", "confirmed", "confirm"}:
                diag(kind, digest, row_no, payload, "unresolved", "feedback is not an explicit confirmation"); summary["unresolved"] += 1; continue
            state = conn.execute("SELECT * FROM review_state WHERE item_version_id=?", (item_id,)).fetchone()
            note = payload.get("note") if "note" in payload else payload.get("notes")
            note = note if isinstance(note, str) else ""
            if state and state["human_priority"] is None and state["state"] != "confirmed" and not state["note"]:
                event_id = new_id(); before = {"state": state["state"], "human_priority": state["human_priority"], "note": state["note"]}; priority = _text(payload.get("current_priority") or payload.get("priority")) or None; after = {**before, "state": "confirmed", "human_priority": priority, "note": note}; revision = state["row_version"] + 1
                conn.execute("UPDATE review_state SET state='confirmed',human_priority=?,note=?,row_version=?,last_event_id=?,updated_at=? WHERE item_version_id=?", (priority, note, revision, event_id, utc_now(), item_id))
                conn.execute("INSERT INTO review_events(id,item_version_id,item_revision,action,before_json,after_json,actor,created_at) VALUES(?,?,?,?,?,?,?,?)", (event_id, item_id, revision, "import", dumps(before), dumps(after), "legacy_import", utc_now()))
            diag(kind, digest, row_no, payload, "imported", entity_type="item_version", entity_id=item_id); summary["imported"] += 1
