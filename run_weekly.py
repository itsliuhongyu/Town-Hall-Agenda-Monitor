import pandas as pd
import os
import re
import requests
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright


# Calculate today and week_ago at script load
today = datetime.today()
week_ago = today - timedelta(days=7)

# Load list of urls from townlist.csv
townlist = pd.read_csv("./resources/townlist.csv")
parseable = townlist[townlist["parseable"] == "y"]
townweb = parseable[parseable["agenda-location"] == "TownWeb"]
townweb_urls = list(zip(townweb["Link to board agenda site"].tolist(), townweb["Gov body"].tolist()))

def _format_progress(current, total):
    percent = (current / total * 100) if total else 0
    return f"[{current}/{total} {percent:.1f}%]"

def download_file(url, dest_folder, new_filename=None):
    ext = os.path.splitext(url.split("/")[-1])[1] or ".pdf"
    local_filename = new_filename if new_filename else url.split("/")[-1]
    if new_filename and not os.path.splitext(new_filename)[1]:
        local_filename = new_filename + ext
    dest_path = os.path.join(dest_folder, local_filename)
    try:
        r = requests.get(url, stream=True)
        r.raise_for_status()
        with open(dest_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"Downloaded: {dest_path}")
    except Exception as e:
        print(f"Failed to download {url}: {e}")

def process_townweb_agendas(headless=False):
    os.makedirs("./temps", exist_ok=True)
    failed_urls = []
    total_urls = len(townweb_urls)
    print(f"Processing {total_urls} TownWeb agenda site(s).")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        for idx, (url, gov_body) in enumerate(townweb_urls, start=1):
            try:
                page = browser.new_page()
                print(f"{_format_progress(idx, total_urls)} Visiting {url}")
                page.goto(url)
                # Wait for at least one table to load
                page.wait_for_selector("table.tw-meeting-repo-table")
                tables = page.query_selector_all("table.tw-meeting-repo-table")
                for table in tables:
                    headers = [th.inner_text().strip() for th in table.query_selector_all("thead tr th")]
                    try:
                        date_idx = headers.index("Meeting Date")
                        agenda_idx = headers.index("Agenda")
                    except ValueError:
                        continue  # Skip tables without required columns
                    rows = table.query_selector_all("tbody tr")
                    for row in rows:
                        cells = row.query_selector_all("td")
                        if len(cells) <= max(date_idx, agenda_idx):
                            continue
                        date_str = cells[date_idx].inner_text().strip()
                        try:
                            meeting_date = datetime.strptime(date_str, "%B %d, %Y")
                        except Exception:
                            continue
                        if week_ago <= meeting_date <= today:
                            agenda_cell = cells[agenda_idx]
                            link = agenda_cell.query_selector("a")
                            if link:
                                href = link.get_attribute("href")
                                if href:
                                    # If href is relative, make it absolute
                                    if href.startswith("/"):
                                        from urllib.parse import urljoin
                                        href = urljoin(url, href)
                                    gov_body_safe = re.sub(r'[^\w\s-]', '', gov_body).strip().replace(' ', '_')
                                    meeting_date_str = meeting_date.strftime("%Y-%m-%d")
                                    ext = os.path.splitext(href.split("/")[-1])[1] or ".pdf"
                                    new_filename = f"{gov_body_safe}_{meeting_date_str}{ext}"
                                    download_file(href, "./temps", new_filename)
                page.close()
            except Exception as e:
                print(f"Error processing {url}: {e}")
                failed_urls.append(url)
        browser.close()
    if failed_urls:
        print("Failed to fetch update for website(s): " + ", ".join(failed_urls))

if __name__ == "__main__":
    process_townweb_agendas()



    