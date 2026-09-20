# Town Hall Agenda Monitor — local runbook

Current scope (2026-09-20): local deployment on desktop computers only. Browser
acceptance covers a 1440×900 full window, a reasonable 1024×768 split window,
keyboard operation, and persisted reviews/settings after reload and reopening
the server/browser. Mobile testing and optimization are outside this scope;
existing harmless responsive styles may remain. Historical review evidence is
preserved; its mobile acceptance requirements have been superseded.

Stage 2A uses SQLite as the durable system of record. Local development may use
`data/` (override with `--data-dir` or `AGENDA_DATA_DIR`); the weekly self-hosted
workflow must use an absolute persistent directory outside the checkout (for
example `/var/lib/town-hall-agenda-monitor`) with runner permissions and backups.
The legacy CSV files
at the repository root are read-only inputs and are never automatic outputs.

## Start the backend

```bash
.venv/bin/python run_local.py --app --data-dir /tmp/agenda-data --port 8000
```

`--review` is a compatibility alias for `--app`. Opening the app serves the
Overview/Runs, Review, and Settings pages. It does not fetch agendas, call
Ollama, serve/pull a model, or install dependencies.

On first `--app`/`--review` startup with an empty source registry, the local
app performs a source-only initialization from
`resources/townlist.csv`. It accepts supported rows marked `parseable=y`,
normalizes URLs/timezones, and records duplicate or skipped rows in SQLite;
it does not import agenda history, feedback, policy files, or create a run.
The maintained list currently yields 87 parseable rows and 80 unique sources.
The initialization is transactional and repeatable. Existing source rows,
including edited or disabled rows, are preserved on restart. If the list is
missing or invalid, Settings shows the diagnostic and a retry action after the
file is corrected. The diagnostic CSV is available at
`/api/v1/source-initialization/diagnostics.csv`.

The same operation can be invoked explicitly against an isolated data
directory:

```bash
.venv/bin/python -m agenda_app.cli --data-dir /tmp/agenda-data initialize-sources --from /Users/smallwest888/Documents/GitHub/Town-Hall-Agenda-Monitor
```

This command is source-only. Use `import-legacy` below only for the separately
gated historical CSV import workflow.

## Import existing CSVs explicitly

Production import is gated by [the final verification](docs/VERIFICATION_FINAL.md).
Do not run this command against production data until that gate passes.
Preserve existing production data and use temporary data directories for
verification. Production import is an explicit operation after approval;
dated verification counts do not describe the database's future contents.

```bash
.venv/bin/python -m agenda_app.cli --data-dir /tmp/agenda-data import-legacy --from /Users/smallwest888/Documents/GitHub/Town-Hall-Agenda-Monitor
```

The importer backs up the source bytes under `data/backups/imports/`, records
unresolved rows, and is repeatable. It never rewrites the source CSVs.

## Run the durable pipeline

Sources must be enabled in SQLite (the settings/API layer or a fixture setup can
create them). A synchronous CLI run is:

```bash
.venv/bin/python pipeline.py --data-dir /tmp/agenda-data --json
```

Exit codes are 0 for `success`/`no_results`, 2 for `partial`, 1 for `failed`,
3 for an active-run conflict, and 130 for an interrupted command. Successful runs publish one immutable export
generation under `data/exports/<export-id>/` and update one SQLite publication
pointer. Partial/failed runs retain the last complete publication and preserve
candidate history for explicit review.

A manual export creates a new immutable run-bound generation and does not move
the current publication pointer:

```bash
.venv/bin/python -m agenda_app.cli --data-dir /tmp/agenda-data export --run <run-id> --format raw
```

## Ollama settings

Model inventory is refreshed explicitly through `POST /api/v1/models/refresh` or
the Stage 2B Settings page. The app reads local `/api/tags` names and digests,
saves an exact name+digest selection, and never starts, pulls, copies, aliases,
or downloads a model. Cached extraction results can be reviewed offline. The
existing weekly high.csv mail is a workflow-only compatibility step: it is sent
only for the current complete run when the ready manifest is published, the
high.csv row count is positive, and its SHA-256 matches the manifest. The local
app and tests never call SMTP. Feature 3 (change monitoring, notifications, and
OCR) remains design-only; no new notification/OCR runtime is present.

Settings also exposes source enablement/timezones, window/workers/timeouts, rule
calibration, and the explicitly invoked legacy import operation.

## Compatibility entrypoints

```bash
.venv/bin/python run_local.py --app --data-dir /tmp/agenda-data --port 8000
.venv/bin/python run_local.py --review --data-dir /tmp/agenda-data --port 8000
```

`run_local.py --setup` is the only setup path; ordinary app startup does not
install packages or browsers. `pipeline.py --json` emits the run id, status,
publication state, export id, and manifest path.

## Tests and syntax checks

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
find agenda_app -name '*.py' -print0 | xargs -0 .venv/bin/python -m py_compile
.venv/bin/python -m py_compile pipeline.py run_local.py run_weekly.py analyze_agendas.py review_server.py adapters/*.py
```

Backend fixture tests use isolated `TemporaryDirectory` data roots. Platform
HTML fixtures live under `tests/fixtures/platforms/`; document fixtures live
under `tests/fixtures/documents/`. They do not contact production agenda sites,
real Ollama, mail, OCR, or notification services.

The complete local fixture suite is:

```bash
AGENDA_ENABLE_LOOPBACK_TESTS=1 AGENDA_ENABLE_BROWSER_FIXTURES=1 AGENDA_ENABLE_REAL_INTEGRATION=1 PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

Desktop browser QA should call `tests.browser_qa.exercise_desktop` from an
isolated wrapper with fake loopback servers, then check all three pages at the
two desktop sizes above. Do not execute `tests/browser_qa.py`'s main mobile loop.
Verify local retained PDF bytes and the optional preview; report headless native
PDF viewer limitations without claiming successful viewer rendering.

## Live adapter checks

The live adapter repair has a bounded derived fixture suite. Run it only when
browser and loopback checks are intended:

```bash
AGENDA_ENABLE_BROWSER_FIXTURES=1 PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m unittest tests.test_live_adapter_regressions
```

This suite uses local dynamic pages derived from the public TownWeb Suring,
BoardDocs, and CivicClerk Winnebago structures. It checks category selection,
duplicate DOM rows, delayed BoardDocs detailed-print capture for two same-day
meetings, event-scoped CivicClerk file menus, specific access/rate/date error
codes, visible-browser routing, and the captured HTML → verified blob → reader
path. It does not contact a live site or model.

BoardDocs may return an empty shell in the default Playwright headless browser.
For an approved attended run, enable **Use a visible browser window to read
BoardDocs agendas** in Settings. The persisted setting is
`allow_visible_browser` and defaults to `false`; the worker applies it only to
BoardDocs and keeps TownWeb/CivicClerk headless. A visible window may need to
remain available for the duration of the run. On 2026-09-20, Kimberly and
Kaukauna returned complete captured Detailed Agenda HTML, while Green Bay
returned a recognized explicit empty result for the requested window. The
default headless shell returned HTTP 200 with no usable Meetings content; the
full Chromium new-headless channel returned HTTP 403 from CloudFront, so the
repair does not claim unattended BoardDocs success for those modes.

The dated station-by-station results, bounded probe commands, and artifact paths
are recorded in [the live adapter verification record](docs/LIVE_ADAPTER_VERIFICATION.md).
