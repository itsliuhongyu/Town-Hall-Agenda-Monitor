import csv
import html
import json
import mimetypes
import os
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
KEYWORD_SCORES_PATH = os.path.join(ROOT_DIR, "keyword_scores.csv")
FEEDBACK_PATH = os.path.join(ROOT_DIR, "priority_feedback.csv")
TOWNLIST_PATH = os.path.join(ROOT_DIR, "resources", "townlist.csv")
TEMPS_DIR = os.path.join(ROOT_DIR, "temps")
AGENDA_CSVS = [
    ("high", os.path.join(ROOT_DIR, "high.csv")),
    ("medium", os.path.join(ROOT_DIR, "medium.csv")),
    ("low", os.path.join(ROOT_DIR, "low.csv")),
]
SCORE_FIELDNAMES = ["keyword", "priority_score", "examples", "updated_at"]
FEEDBACK_FIELDNAMES = [
    "timestamp",
    "filename",
    "item",
    "current_priority",
    "feedback",
    "matched_keywords",
    "score_delta",
]


def read_keyword_scores(path=KEYWORD_SCORES_PATH):
    if not os.path.exists(path):
        return []

    with open(path, newline="", encoding="utf-8") as f:
        rows = []
        for row in csv.DictReader(f):
            keyword = row.get("keyword", "").strip().lower()
            if not keyword:
                continue

            try:
                score = int(float(row.get("priority_score", "0")))
            except ValueError:
                score = 0

            rows.append({
                "keyword": keyword,
                "priority_score": max(0, min(100, score)),
                "examples": row.get("examples", ""),
                "updated_at": row.get("updated_at", ""),
            })

    return rows


def write_keyword_scores(rows, path=KEYWORD_SCORES_PATH):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SCORE_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "keyword": row.get("keyword", ""),
                "priority_score": str(row.get("priority_score", 0)),
                "examples": row.get("examples", ""),
                "updated_at": row.get("updated_at", ""),
            })


def match_keywords(text, keyword_rows):
    lower = text.lower()
    matched = []

    for row in keyword_rows:
        keyword = row.get("keyword", "").strip().lower()
        if not keyword:
            continue

        pattern = r"(?<!\w)" + re.escape(keyword) + r"(?!\w)"
        if re.search(pattern, lower):
            matched.append(keyword)

    return matched


def _delta_for_feedback(current_priority, feedback):
    if feedback == "higher":
        return 8
    if feedback == "lower":
        return -8
    if feedback == "correct":
        if current_priority == "high":
            return 3
        if current_priority == "medium":
            return 1
        return -2
    return 0


def update_keyword_scores_for_feedback(text, current_priority, feedback, score_path=KEYWORD_SCORES_PATH):
    rows = read_keyword_scores(score_path)
    matched_keywords = match_keywords(text, rows)
    delta = _delta_for_feedback(current_priority, feedback)
    now = datetime.now().isoformat(timespec="seconds")

    for row in rows:
        if row["keyword"] in matched_keywords:
            row["priority_score"] = max(0, min(100, int(row["priority_score"]) + delta))
            row["updated_at"] = now

    write_keyword_scores(rows, score_path)

    return {
        "matched_keywords": matched_keywords,
        "delta": delta if matched_keywords else 0,
    }


def append_feedback(payload, result, feedback_path=FEEDBACK_PATH):
    exists = os.path.exists(feedback_path)

    with open(feedback_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FEEDBACK_FIELDNAMES)
        if not exists:
            writer.writeheader()

        writer.writerow({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "filename": payload.get("filename", ""),
            "item": payload.get("item", ""),
            "current_priority": payload.get("current_priority", ""),
            "feedback": payload.get("feedback", ""),
            "matched_keywords": "|".join(result.get("matched_keywords", [])),
            "score_delta": result.get("delta", 0),
        })


def _date_from_filename(filename):
    match = re.search(r"_(\d{4}-\d{2}-\d{2})\.", filename)
    return match.group(1) if match else ""


def _gov_body_safe(gov_body):
    return re.sub(r'[^\w\s-]', '', gov_body).strip().replace(' ', '_')


def build_source_lookup(path=TOWNLIST_PATH):
    lookup = {}

    if not os.path.exists(path):
        return lookup

    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = _gov_body_safe(row.get("Gov body", ""))
            url = row.get("Link to board agenda site", "").strip()
            if key and url:
                lookup[key] = url

    return lookup


def _gov_key_from_filename(filename):
    return re.sub(r"_\d{4}-\d{2}-\d{2}\.\w+$", "", filename)


def _source_label_from_filename(filename):
    return _gov_key_from_filename(filename).replace("_", " ").strip()


def _source_url_from_row(row, source_lookup):
    direct_url = row.get("Link to board agenda site", "").strip()
    if direct_url:
        return direct_url

    filename = row.get("filename", "")
    return source_lookup.get(_gov_key_from_filename(filename), "")


def _local_file_url(filename):
    return f"/files/{quote(filename)}" if filename else ""


def load_agenda_items(agenda_csvs=None, source_lookup=None):
    items = []
    agenda_csvs = agenda_csvs if agenda_csvs is not None else AGENDA_CSVS
    source_lookup = source_lookup if source_lookup is not None else build_source_lookup()

    for priority, path in agenda_csvs:
        if not os.path.exists(path):
            continue

        with open(path, newline="", encoding="utf-8") as f:
            for idx, row in enumerate(csv.DictReader(f)):
                filename = row.get("filename", "")
                title = row.get("item", "").strip()
                if not title:
                    continue

                items.append({
                    "id": f"{priority}-{idx}",
                    "date": _date_from_filename(filename),
                    "source": _source_label_from_filename(filename),
                    "source_url": _source_url_from_row(row, source_lookup),
                    "local_file_url": _local_file_url(filename),
                    "title": title,
                    "current_priority": row.get("importance", priority),
                    "filename": filename,
                    "original_text": row.get("original_text", ""),
                })

    priority_order = {"high": 0, "medium": 1, "low": 2}
    return sorted(items, key=lambda item: (priority_order.get(item["current_priority"], 9), item["date"]))


def render_review_page():
    items_json = html.escape(json.dumps(load_agenda_items()), quote=False)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agenda Priority Review</title>
  <style>
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f6f7f9;
      color: #1f2328;
    }}
    header {{
      padding: 24px 32px;
      background: #ffffff;
      border-bottom: 1px solid #d8dee4;
    }}
    main {{
      padding: 24px 32px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: #ffffff;
      border: 1px solid #d8dee4;
    }}
    th, td {{
      padding: 10px 12px;
      border-bottom: 1px solid #d8dee4;
      text-align: left;
      vertical-align: top;
      font-size: 14px;
    }}
    th {{
      background: #f0f2f4;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    button {{
      margin: 2px;
      padding: 6px 8px;
      border: 1px solid #8c959f;
      border-radius: 6px;
      background: #ffffff;
      cursor: pointer;
    }}
    button:hover {{
      background: #f0f2f4;
    }}
    .priority {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid #d8dee4;
      font-size: 12px;
      text-transform: uppercase;
    }}
    .status {{
      margin-top: 12px;
      color: #57606a;
    }}
    .title {{
      font-weight: 600;
    }}
    .original {{
      color: #57606a;
      margin-top: 4px;
      font-size: 12px;
    }}
    .links {{
      margin-top: 6px;
      font-size: 12px;
    }}
    .links a {{
      color: #0969da;
      margin-right: 10px;
    }}
  </style>
</head>
<body>
  <header>
    <h1>Agenda Priority Review</h1>
    <p>Review the current high, medium, and low priority agenda items. Your feedback updates keyword priority scores for future processing.</p>
    <p class="status" id="status">Ready.</p>
  </header>
  <main>
    <table>
      <thead>
        <tr>
          <th>Date</th>
          <th>Source</th>
          <th>Title</th>
          <th>Current Priority</th>
          <th>Feedback</th>
        </tr>
      </thead>
      <tbody id="agenda-body"></tbody>
    </table>
  </main>
  <script id="agenda-data" type="application/json">{items_json}</script>
  <script>
    const items = JSON.parse(document.getElementById("agenda-data").textContent);
    const tbody = document.getElementById("agenda-body");
    const status = document.getElementById("status");

    function textCell(value) {{
      const td = document.createElement("td");
      td.textContent = value || "";
      return td;
    }}

    function link(label, href) {{
      if (!href) return null;
      const a = document.createElement("a");
      a.textContent = label;
      a.href = href;
      a.target = "_blank";
      a.rel = "noreferrer";
      return a;
    }}

    function sendFeedback(item, feedback, row) {{
      fetch("/feedback", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ ...item, feedback }})
      }})
        .then(response => response.json())
        .then(data => {{
          status.textContent = `Saved feedback. Matched keywords: ${{data.matched_keywords.join(", ") || "none"}}. Delta: ${{data.delta}}.`;
          row.style.opacity = "0.55";
        }})
        .catch(error => {{
          status.textContent = `Could not save feedback: ${{error}}`;
        }});
    }}

    for (const item of items) {{
      const tr = document.createElement("tr");
      tr.appendChild(textCell(item.date));
      tr.appendChild(textCell(item.source));

      const titleTd = document.createElement("td");
      const title = document.createElement("div");
      title.className = "title";
      title.textContent = item.title;
      const original = document.createElement("div");
      original.className = "original";
      original.textContent = item.original_text || "";
      const links = document.createElement("div");
      links.className = "links";
      for (const itemLink of [
        link("Open local file", item.local_file_url),
        link("Open original source", item.source_url),
      ]) {{
        if (itemLink) links.appendChild(itemLink);
      }}
      titleTd.appendChild(title);
      titleTd.appendChild(original);
      titleTd.appendChild(links);
      tr.appendChild(titleTd);

      const priorityTd = document.createElement("td");
      const priority = document.createElement("span");
      priority.className = "priority";
      priority.textContent = item.current_priority;
      priorityTd.appendChild(priority);
      tr.appendChild(priorityTd);

      const feedbackTd = document.createElement("td");
      for (const [label, feedback] of [
        ["Correct Priority", "correct"],
        ["Wrong: Should Be Higher", "higher"],
        ["Wrong: Should Be Lower", "lower"],
      ]) {{
        const button = document.createElement("button");
        button.textContent = label;
        button.addEventListener("click", () => sendFeedback(item, feedback, tr));
        feedbackTd.appendChild(button);
      }}
      tr.appendChild(feedbackTd);
      tbody.appendChild(tr);
    }}
  </script>
</body>
</html>"""


class ReviewHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/files/"):
            self._serve_local_file()
            return

        if self.path != "/":
            self.send_error(404)
            return

        body = render_review_page().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_local_file(self):
        filename = unquote(self.path.removeprefix("/files/"))

        if not filename or filename != os.path.basename(filename):
            self.send_error(400)
            return

        path = os.path.join(TEMPS_DIR, filename)
        if not os.path.isfile(path):
            self.send_error(404)
            return

        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            body = f.read()

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/feedback":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        text = " ".join([
            payload.get("title", ""),
            payload.get("original_text", ""),
        ])
        result = update_keyword_scores_for_feedback(
            text=text,
            current_priority=payload.get("current_priority", "low"),
            feedback=payload.get("feedback", ""),
        )
        append_feedback(payload, result)

        body = json.dumps(result).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 8000), ReviewHandler)
    print("Agenda Priority Review running at http://127.0.0.1:8000")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


if __name__ == "__main__":
    main()
