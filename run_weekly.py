import json
import hashlib
import os
import re
import threading
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from adapters import get_adapter, AgendaItem


DEFAULT_HEADLESS = True
DEFAULT_WORKERS = int(os.environ.get("AGENDA_WORKERS", "1"))
DEFAULT_DAYS_BACK = int(os.environ.get("AGENDA_DAYS_BACK", "0"))
DEFAULT_DAYS_FORWARD = int(os.environ.get("AGENDA_DAYS_FORWARD", "14"))
MAX_RETRIES = 3
RETRY_DELAY = 2
MANIFEST_FILENAME = "agenda_manifest.json"


@dataclass
class DownloadResult:
    url: str
    path: str
    status: str
    attempts: int = 0
    error: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def succeeded(self):
        return self.status in {"downloaded", "already_exists"}


@dataclass
class SiteResult:
    platform: str
    url: str
    gov_body: str
    status: str
    found_count: int = 0
    downloads: list[DownloadResult] = field(default_factory=list)
    error: str = ""

    @property
    def succeeded_downloads(self):
        return [download for download in self.downloads if download.succeeded]


@dataclass
class AgendaRunResult:
    status: str
    sites: list[SiteResult] = field(default_factory=list)
    failed_urls: list[str] = field(default_factory=list)

    @property
    def successful_downloads(self):
        return [download for site in self.sites for download in site.succeeded_downloads]

    @property
    def downloaded_file_count(self):
        return len({download.path for download in self.successful_downloads})


def _format_progress(current, total):
    percent = (current / total * 100) if total else 0
    return f"[{current}/{total} {percent:.1f}%]"


def is_meeting_date_in_window(meeting_date, base_date=None, days_back=DEFAULT_DAYS_BACK, days_forward=DEFAULT_DAYS_FORWARD):
    base_date = base_date or datetime.now()
    start = base_date - timedelta(days=days_back)
    end = base_date + timedelta(days=days_forward)
    meeting_day = meeting_date.date() if isinstance(meeting_date, datetime) else meeting_date
    start_day = start.date() if isinstance(start, datetime) else start
    end_day = end.date() if isinstance(end, datetime) else end
    return start_day <= meeting_day <= end_day


def _gov_body_safe(gov_body):
    return re.sub(r'[^\w\s-]', '', gov_body).strip().replace(' ', '_')


def _load_parseable_sites():
    townlist = pd.read_csv("./resources/townlist.csv")
    parseable = townlist[townlist["parseable"] == "y"]
    sites = []
    for _, row in parseable.iterrows():
        platform = str(row.get("agenda-location", "")).strip()
        url = str(row.get("Link to board agenda site", "")).strip()
        gov_body = str(row.get("Gov body", "")).strip()
        if platform and url and gov_body:
            sites.append((platform, url, gov_body))
    return sites


def download_file(url, dest_folder, new_filename=None, metadata=None):
    ext = os.path.splitext(url.split("?")[0].split("/")[-1])[1] or ".pdf"
    local_filename = new_filename if new_filename else url.split("/")[-1]
    if new_filename and not os.path.splitext(new_filename)[1]:
        local_filename = new_filename + ext
    dest_path = os.path.join(dest_folder, local_filename)

    if os.path.exists(dest_path):
        print(f"Already exists: {dest_path}")
        return DownloadResult(
            url=url,
            path=dest_path,
            status="already_exists",
            attempts=0,
            metadata=metadata or {},
        )

    for attempt in range(1, MAX_RETRIES + 1):
        temp_path = f"{dest_path}.part-{os.getpid()}-{threading.get_ident()}"
        try:
            r = requests.get(url, stream=True, timeout=30)
            r.raise_for_status()
            content_type = r.headers.get("Content-Type", "")
            if "text/html" in content_type and not dest_path.endswith((".html", ".htm")):
                print(f"Warning: {url} returned HTML instead of file, skipping")
                return DownloadResult(
                    url=url,
                    path=dest_path,
                    status="failed",
                    attempts=attempt,
                    error="HTML response for a non-HTML agenda",
                    metadata=metadata or {},
                )
            total_bytes = 0
            declared_length = r.headers.get("Content-Length")
            with open(temp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        total_bytes += len(chunk)
                        if total_bytes > 50 * 1024 * 1024:
                            raise RuntimeError("download exceeds 50 MiB")
                        f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            if total_bytes == 0:
                raise RuntimeError("empty response body")
            if declared_length and declared_length.isdigit() and int(declared_length) != total_bytes:
                raise RuntimeError("truncated response body")
            os.replace(temp_path, dest_path)
            print(f"Downloaded: {dest_path}")
            return DownloadResult(
                url=url,
                path=dest_path,
                status="downloaded",
                attempts=attempt,
                metadata=metadata or {},
            )
        except Exception as e:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            if attempt < MAX_RETRIES:
                print(f"Retry {attempt}/{MAX_RETRIES} for {url}: {e}")
                time.sleep(RETRY_DELAY * attempt)
            else:
                print(f"Failed to download {url} after {MAX_RETRIES} attempts: {e}")
                return DownloadResult(
                    url=url,
                    path=dest_path,
                    status="failed",
                    attempts=attempt,
                    error=str(e),
                    metadata=metadata or {},
                )

    return DownloadResult(
        url=url,
        path=dest_path,
        status="failed",
        attempts=MAX_RETRIES,
        error="retry loop exhausted",
        metadata=metadata or {},
    )


def write_captured_file(data, dest_folder, filename, media_type="", metadata=None):
    """Persist adapter-captured HTML/PDF without requesting a synthetic URL."""
    try:
        raw = bytes(data)
        if not raw or len(raw) > 50 * 1024 * 1024:
            raise ValueError("captured document is empty or exceeds 50 MiB")
        os.makedirs(dest_folder, exist_ok=True)
        dest_path = os.path.join(dest_folder, os.path.basename(filename))
        if os.path.exists(dest_path):
            return DownloadResult("captured://" + os.path.basename(filename), dest_path, "already_exists", metadata=metadata or {})
        temp_path = f"{dest_path}.part-{os.getpid()}-{threading.get_ident()}"
        with open(temp_path, "wb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        with open(temp_path, "rb") as handle:
            written_digest = hashlib.sha256(handle.read()).hexdigest()
        if written_digest != hashlib.sha256(raw).hexdigest():
            raise ValueError("captured document digest verification failed")
        os.replace(temp_path, dest_path)
        return DownloadResult("captured://" + os.path.basename(filename), dest_path, "downloaded", attempts=1, metadata=metadata or {})
    except Exception as exc:
        try:
            if os.path.exists(temp_path): os.remove(temp_path)
        except (OSError, UnboundLocalError):
            pass
        return DownloadResult("captured://" + os.path.basename(filename), os.path.join(dest_folder, os.path.basename(filename)), "failed", attempts=1, error=str(exc), metadata=metadata or {})


def _process_site(platform, url, gov_body, date_start, date_end, headless, progress_fn):
    idx, total = progress_fn()
    adapter = get_adapter(platform)
    if not adapter:
        print(f"{_format_progress(idx, total)} Skipping {gov_body}: no adapter for '{platform}'", flush=True)
        return SiteResult(platform, url, gov_body, "failed", error=f"no adapter for '{platform}'")

    print(f"{_format_progress(idx, total)} [{platform}] {gov_body} - {url}", flush=True)
    try:
        items = adapter.fetch_agendas(url, gov_body, date_start, date_end, headless=headless)
        print(f"  Found {len(items)} agenda(s) in date window", flush=True)
        downloads = []
        for item in items:
            metadata = {
                "filename": item.filename,
                "gov_body": item.gov_body,
                "meeting_date": item.meeting_date.date().isoformat()
                if isinstance(item.meeting_date, datetime)
                else str(item.meeting_date),
                "download_url": item.download_url,
                "source_url": item.source_url,
                "platform": platform,
            }
            result = write_captured_file(item.captured_bytes, "./temps", item.filename, item.captured_media_type, metadata=metadata) if item.captured_bytes is not None else download_file(item.download_url, "./temps", item.filename, metadata=metadata)
            result.metadata = metadata
            downloads.append(result)

        failed_downloads = [download for download in downloads if not download.succeeded]
        if failed_downloads:
            status = "partial_failure" if any(download.succeeded for download in downloads) else "failed"
            error = "; ".join(download.error for download in failed_downloads if download.error)
        else:
            status = "success"
            error = ""
        return SiteResult(
            platform,
            url,
            gov_body,
            status,
            found_count=len(items),
            downloads=downloads,
            error=error,
        )
    except Exception as e:
        print(f"  Error: {e}", flush=True)
        return SiteResult(platform, url, gov_body, "failed", error=str(e))


def _process_batch(batch, date_start, date_end, headless, progress_fn):
    results = []
    for platform, url, gov_body in batch:
        results.append(_process_site(platform, url, gov_body, date_start, date_end, headless, progress_fn))
    return results


def _write_manifest(results, dest_folder="./temps"):
    """Persist successful agenda metadata without putting it in extraction cache."""
    os.makedirs(dest_folder, exist_ok=True)
    entries = []
    for site in results:
        for download in site.succeeded_downloads:
            entry = dict(download.metadata)
            entry.setdefault("filename", os.path.basename(download.path))
            entry["path"] = download.path
            entries.append(entry)
    entries.sort(key=lambda entry: (str(entry.get("gov_body", "")).casefold(), str(entry.get("meeting_date", "")), str(entry.get("download_url", "")), str(entry.get("filename", ""))))

    path = os.path.join(dest_folder, MANIFEST_FILENAME)
    temp_path = f"{path}.tmp-{os.getpid()}"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, path)
    return path


def process_agendas(headless=DEFAULT_HEADLESS, workers=DEFAULT_WORKERS):
    os.makedirs("./temps", exist_ok=True)

    today = datetime.now()
    date_start = today - timedelta(days=DEFAULT_DAYS_BACK)
    date_end = today + timedelta(days=DEFAULT_DAYS_FORWARD)

    sites = _load_parseable_sites()
    total_sites = len(sites)
    workers = max(1, min(workers, total_sites or 1))

    platform_counts = {}
    for platform, _, _ in sites:
        platform_counts[platform] = platform_counts.get(platform, 0) + 1

    platform_summary = ", ".join(f"{count} {platform}" for platform, count in sorted(platform_counts.items()))
    print(
        f"Processing {total_sites} site(s) ({platform_summary}) with {workers} worker(s). "
        f"Date window: {date_start:%Y-%m-%d} to {date_end:%Y-%m-%d}.",
        flush=True,
    )

    if not sites:
        print("No enabled parseable agenda sources found.", flush=True)
        _write_manifest([])
        return AgendaRunResult(status="no_sources")

    progress = {"current": 0}
    progress_lock = threading.Lock()

    def next_progress():
        with progress_lock:
            progress["current"] += 1
            return progress["current"], total_sites

    chunks_count = min(workers, total_sites)
    chunk_size = max(1, (total_sites + chunks_count - 1) // chunks_count)
    batches = [sites[i:i + chunk_size] for i in range(0, total_sites, chunk_size)]

    site_results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(_process_batch, batch, date_start, date_end, headless, next_progress)
            for batch in batches
        ]
        for future in as_completed(futures):
            site_results.extend(future.result())

    _write_manifest(site_results)
    failed_urls = [site.url for site in site_results if site.status in {"failed", "partial_failure"}]
    if failed_urls:
        print("Failed to fetch update for website(s): " + ", ".join(failed_urls))

    successful_downloads = [download for site in site_results for download in site.succeeded_downloads]
    has_failures = any(site.status in {"failed", "partial_failure"} for site in site_results)
    if has_failures:
        status = "partial_failure" if successful_downloads else "failed"
    elif successful_downloads:
        status = "success"
    else:
        status = "empty_success"

    return AgendaRunResult(status=status, sites=site_results, failed_urls=failed_urls)


process_townweb_agendas = process_agendas

if __name__ == "__main__":
    process_agendas()
