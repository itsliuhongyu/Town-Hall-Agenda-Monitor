"""
Weekly Agenda Pipeline
======================
Combines agenda downloading (run_weekly) and AI analysis (analyze_agendas)
into a single script.

Run manually:
    python3 pipeline.py

For automated Monday runs, this script is triggered by the GitHub Actions
workflow at .github/workflows/weekly.yml using a self-hosted runner on
this machine. LM Studio must be running at http://localhost:1234 before
the workflow fires.
"""

import csv
import os
import re
import shutil

from run_weekly import process_townweb_agendas
from analyze_agendas import main as analyze_agendas

TOWNLIST_PATH = "./resources/townlist.csv"
TEMPS_DIR = "./temps"
HIGH_CSV = "./high.csv"


def clear_temps():
    """Remove all files from the temps folder so last week's downloads don't linger."""
    if os.path.isdir(TEMPS_DIR):
        for f in os.listdir(TEMPS_DIR):
            fp = os.path.join(TEMPS_DIR, f)
            try:
                if os.path.isfile(fp):
                    os.remove(fp)
            except Exception as e:
                print(f"  Warning: could not delete {fp}: {e}")
        print(f"Cleared {TEMPS_DIR}/")
    os.makedirs(TEMPS_DIR, exist_ok=True)


def _gov_body_safe(gov_body):
    """Mirror the sanitization used in run_weekly when building filenames."""
    return re.sub(r'[^\w\s-]', '', gov_body).strip().replace(' ', '_')


def build_url_lookup():
    """Return {gov_body_safe: Link to board agenda site} from townlist.csv."""
    lookup = {}
    try:
        with open(TOWNLIST_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = _gov_body_safe(row.get("Gov body", ""))
                url = row.get("Link to board agenda site", "").strip()
                if key and url:
                    lookup[key] = url
    except Exception as e:
        print(f"Warning: could not load townlist.csv: {e}")
    return lookup


def add_url_column_to_high_csv(lookup):
    """Read high.csv, add 'Link to board agenda site', write it back."""
    if not os.path.exists(HIGH_CSV):
        return

    rows = []
    with open(HIGH_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        for row in reader:
            rows.append(row)

    url_col = "Link to board agenda site"
    if url_col not in fieldnames:
        fieldnames = [url_col] + list(fieldnames)

    # filename pattern: {gov_body_safe}_{YYYY-MM-DD}.ext
    date_suffix = re.compile(r'_\d{4}-\d{2}-\d{2}\.\w+$')

    for row in rows:
        filename = row.get("filename", "")
        # Strip the date + extension to recover gov_body_safe
        gov_key = date_suffix.sub("", filename)
        row[url_col] = lookup.get(gov_key, "")

    with open(HIGH_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Added '{url_col}' column to {HIGH_CSV}")


def run_pipeline():
    print("=" * 50)
    print("STEP 0: Clearing last week's temp files")
    print("=" * 50)
    clear_temps()

    print()
    print("=" * 50)
    print("STEP 1: Downloading agendas")
    print("=" * 50)
    # headless=True so the browser runs silently when triggered automatically
    process_townweb_agendas(headless=True)

    print()
    print("=" * 50)
    print("STEP 2: Analyzing agendas")
    print("=" * 50)
    analyze_agendas()

    print()
    print("=" * 50)
    print("STEP 3: Enriching high.csv with agenda URLs")
    print("=" * 50)
    lookup = build_url_lookup()
    add_url_column_to_high_csv(lookup)

    print()
    print("Pipeline complete.")


if __name__ == "__main__":
    run_pipeline()
