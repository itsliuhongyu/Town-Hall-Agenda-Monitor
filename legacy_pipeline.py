"""Explicit stage-1 CSV compatibility helpers.

The durable application never calls this module.  It exists only for old
Python callers and regression tests that still exercise the original CSV
pipeline; the root CLI is implemented by :mod:`agenda_app.runs.service`.
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import tempfile
from datetime import datetime

from run_weekly import AgendaRunResult

ROOT_TOWNLIST = "./resources/townlist.csv"
ROOT_TEMPS = "./temps"
ROOT_HIGH = "./high.csv"
ROOT_STATUS = "./run_status.json"


def _gov_body_safe(gov_body):
    return re.sub(r"[^\w\s-]", "", gov_body).strip().replace(" ", "_")


def build_url_lookup(townlist_path=ROOT_TOWNLIST):
    lookup = {}
    try:
        with open(townlist_path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                key = _gov_body_safe(row.get("Gov body", ""))
                url = row.get("Link to board agenda site", "").strip()
                if key and url:
                    lookup[key] = url
    except Exception as exc:
        print(f"Warning: could not load townlist.csv: {exc}")
    return lookup


def add_url_column_to_high_csv(lookup, csv_path=ROOT_HIGH):
    if not os.path.exists(csv_path):
        return
    with open(csv_path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    url_col = "Link to board agenda site"
    if url_col not in fieldnames:
        fieldnames.insert(0, url_col)
    date_suffix = re.compile(r"_\d{4}-\d{2}-\d{2}(?:_[A-Za-z0-9-]+)?\.[^.]+$")
    for row in rows:
        filename = row.get("filename", "")
        key = _gov_body_safe(row.get("gov_body", "")) or date_suffix.sub("", filename)
        row[url_col] = row.get("source_url", "").strip() or lookup.get(key, "")
    temp_path = f"{csv_path}.tmp-{os.getpid()}"
    with open(temp_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader(); writer.writerows(rows); handle.flush(); os.fsync(handle.fileno())
    os.replace(temp_path, csv_path)
    print(f"Added '{url_col}' column to {csv_path}")


def clear_temps(temps_dir=ROOT_TEMPS):
    if os.path.isdir(temps_dir):
        for name in os.listdir(temps_dir):
            path = os.path.join(temps_dir, name)
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except Exception as exc:
                print(f"  Warning: could not delete {path}: {exc}")
        print(f"Cleared {temps_dir}/")
    os.makedirs(temps_dir, exist_ok=True)


def _write_run_status(status, publication, *, download_result=None, analysis_status="not_started", error="", status_path=ROOT_STATUS):
    sites = getattr(download_result, "sites", []) if download_result else []
    site_statuses = []
    for site in sites:
        site_statuses.append({
            "platform": site.platform, "gov_body": site.gov_body, "url": site.url,
            "status": site.status, "found_count": site.found_count, "error": site.error,
            "failed_downloads": [{"url": item.url, "attempts": item.attempts, "error": item.error} for item in site.downloads if not item.succeeded],
        })
    payload = {
        "run_status": status, "publication_status": publication, "analysis_status": analysis_status,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "failed_urls": getattr(download_result, "failed_urls", []) if download_result else [],
        "downloaded_file_count": getattr(download_result, "downloaded_file_count", 0) if download_result else 0,
        "site_statuses": site_statuses, "error": error,
    }
    temp_path = f"{status_path}.tmp-{os.getpid()}"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2); handle.flush(); os.fsync(handle.fileno())
    os.replace(temp_path, status_path)
    return payload


def _publish_analysis_outputs(staging_dir):
    for name in ("high.csv", "medium.csv", "low.csv"):
        temp_path = f"{name}.tmp-{os.getpid()}"
        shutil.copyfile(os.path.join(staging_dir, name), temp_path)
        os.replace(temp_path, name)


def run_legacy_pipeline(*, processor=None, analyzer=None):
    if processor is None:
        from run_weekly import process_agendas as processor
    if analyzer is None:
        from analyze_agendas import main as analyzer
    print("=" * 50); print("STEP 0: Clearing last week's temp files"); print("=" * 50)
    clear_temps()
    print(); print("=" * 50); print("STEP 1: Downloading agendas"); print("=" * 50)
    download_result = processor(headless=True)
    if download_result is None:
        download_result = AgendaRunResult(status="success")
    if download_result.status in {"failed", "partial_failure", "no_sources"}:
        _write_run_status(download_result.status, "preserved", download_result=download_result, error="Agenda download did not complete; previous published CSVs were preserved.")
        print(f"Pipeline stopped with status: {download_result.status}. Existing outputs preserved.")
        return download_result
    print(); print("=" * 50); print("STEP 2: Analyzing agendas"); print("=" * 50)
    with tempfile.TemporaryDirectory(prefix=".pipeline-staging-", dir=".") as staging_dir:
        try:
            analysis_result = analyzer(output_dir=staging_dir, temps_dir=ROOT_TEMPS)
        except Exception as exc:
            _write_run_status("failed", "preserved", download_result=download_result, analysis_status="failed", error=str(exc))
            print(f"Pipeline stopped during analysis: {exc}. Existing outputs preserved.")
            return AgendaRunResult(status="failed", sites=download_result.sites, failed_urls=download_result.failed_urls)
        if getattr(analysis_result, "status", "success") not in {"success", "empty_success"}:
            _write_run_status("failed", "preserved", download_result=download_result, analysis_status=getattr(analysis_result, "status", "failed"), error="Analysis did not produce a publishable result.")
            print("Pipeline stopped: analysis did not produce a publishable result. Existing outputs preserved.")
            return AgendaRunResult(status="failed", sites=download_result.sites, failed_urls=download_result.failed_urls)
        try:
            print(); print("=" * 50); print("STEP 3: Enriching high.csv with agenda URLs"); print("=" * 50)
            add_url_column_to_high_csv(build_url_lookup(), os.path.join(staging_dir, "high.csv"))
            _publish_analysis_outputs(staging_dir)
        except Exception as exc:
            _write_run_status("failed", "preserved", download_result=download_result, analysis_status="failed", error=str(exc))
            print(f"Pipeline stopped before publication: {exc}. Existing outputs preserved.")
            return AgendaRunResult(status="failed", sites=download_result.sites, failed_urls=download_result.failed_urls)
    publication = "empty_published" if download_result.status == "empty_success" else "published"
    _write_run_status(download_result.status, publication, download_result=download_result, analysis_status=getattr(analysis_result, "status", "success"))
    print(); print(f"Pipeline complete: run={download_result.status}, publication={publication}.")
    return download_result
