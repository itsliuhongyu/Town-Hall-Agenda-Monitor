(function () {
  "use strict";
  const { request, waitForJob, idempotency, formatTime, statusBadge, showMessage, escapeHtml } = window.AgendaUI;
  const $ = (id) => document.getElementById(id);
  let latestRun = null; let latestOverview = null; let sourceRows = [];

  function runLabel(run) { return run ? `${run.status.replaceAll("_", " ")} · ${formatTime(run.started_at)}` : "Never"; }
  function sourceErrorText(error) {
    if (!error) return "";
    const hints = {
      access_blocked: "The source website requires access verification. Repeating the run may not resolve it.",
      rate_limited: "The source website is limiting requests. Wait before retrying.",
      unsupported_structure: "The collector needs an update for this page layout.",
      date_parse_failed: "The collector could not read the meeting date.",
      category_not_found: "Check that the configured board matches a category on the source website.",
      document_unavailable: "Check whether the source has published the agenda file."
    };
    return (Array.isArray(error) ? error : [error]).map((item) => {
      if (!item || typeof item !== "object") return String(item || "");
      const description = [item.code, item.message].filter(Boolean).join(": ");
      const hint = hints[item.code] || (item.retryable && ["load_timeout", "network_error", "category_load_failed"].includes(item.code) ? "This failure may be temporary; retry after a short wait." : "");
      return [description || JSON.stringify(item), hint].filter(Boolean).join(" ");
    }).join("; ");
  }
  function renderSummary(data) {
    latestOverview = data;
    $("latest-attempt").textContent = runLabel(data.latest_attempt);
    $("latest-attempt-meta").textContent = data.latest_attempt?.reason_code || "";
    $("last-publication").textContent = data.last_complete_publication ? "Available" : "Never";
    $("last-publication-meta").textContent = data.last_complete_publication ? `Run ${data.last_complete_publication.slice(0, 8)}` : "No complete publication yet";
    $("last-full-success").textContent = data.last_full_success ? "Available" : "Never";
    $("last-full-success-meta").textContent = data.last_full_success ? `Run ${data.last_full_success.slice(0, 8)}` : "No full success yet";
    const counts = data.review_counts || {}; $("review-count").textContent = counts.unreviewed || 0;
    $("review-count-meta").textContent = `${counts.confirmed || 0} confirmed · ${counts.needs_review || 0} needs review`;
    const active = data.active_run; const button = $("start-run");
    button.disabled = Boolean(active) || data.source_count === 0; $("start-run-help").textContent = active ? "One run is already active. Inspect its progress below." : data.source_count === 0 ? "Configure or enable a source in Settings before starting." : "One active run at a time.";
    $("active-run-badge").outerHTML = active ? `<span id="active-run-badge" class="status running">Running</span>` : `<span id="active-run-badge" class="status neutral">No active run</span>`;
  }
  function renderActive(run) {
    const node = $("active-run-content");
    if (!run) { node.className = "empty-state compact"; node.textContent = latestOverview?.source_count === 0 ? "No enabled sources. Open Settings to initialize or enable the town list sources." : "No run is active. Start a run when sources are ready."; return; }
    node.className = "run-progress";
    node.innerHTML = `<div class="progress-line"><strong>${escapeHtml(run.phase || "working")}</strong><span>${statusBadge(run.status, run.status)}</span></div><p class="muted">Started ${escapeHtml(formatTime(run.started_at))}. Last heartbeat ${escapeHtml(formatTime(run.heartbeat_at))}.</p><div class="progress-stats"><span><strong>${run.coverage.complete}</strong>/${run.coverage.total} sources complete</span><span><strong>${run.counts.found}</strong> found</span><span><strong>${run.counts.downloaded}</strong> downloaded</span><span><strong>${run.counts.analyzed}</strong> analyzed</span></div><a class="button quiet" href="/review?run_id=${encodeURIComponent(run.id)}">View candidates</a>`;
  }
  function renderSources(detail, sources) {
    const list = $("source-list");
    const runRows = detail?.sources || [];
    const configured = (sources || []).filter((row) => row.enabled);
    sourceRows = runRows.length ? runRows : configured.map((source) => ({ source_id: source.id, timezone: source.timezone, state: "configured", counts: { found: 0, downloaded: 0, analyzed: 0 }, window: {} }));
    if (!sourceRows.length) { list.innerHTML = `<div class="empty-state compact">No enabled sources. Open Settings to initialize or enable the town list sources.</div>`; $("retry-selected").disabled = true; $("coverage-summary").textContent = "0 configured sources"; return; }
    const byId = new Map((sources || []).map((row) => [row.id, row]));
    const complete = sourceRows.filter((row) => ["success", "no_results"].includes(row.state)).length;
    $("coverage-summary").textContent = runRows.length ? `Latest run · ${complete}/${sourceRows.length} sources complete` : `${sourceRows.length} configured sources · no run coverage yet`;
    list.innerHTML = sourceRows.map((row) => {
      const source = byId.get(row.source_id) || {}; const selectable = ["failed", "partial", "interrupted"].includes(row.state);
      const errors = sourceErrorText(row.error);
      const meta = runRows.length ? `${escapeHtml(row.timezone || source.timezone || "")}: ${escapeHtml(row.window?.start || "")} to ${escapeHtml(row.window?.end || "")} · found ${row.counts.found}, downloaded ${row.counts.downloaded}, analyzed ${row.counts.analyzed}` : `${escapeHtml(source.platform || "") } · ${escapeHtml(source.timezone || "") } · configured and enabled`;
      return `<div class="source-row"><div class="source-main"><div class="source-title">${escapeHtml(source.name || row.source_id)}</div><div class="source-meta">${meta}</div></div><div class="source-actions">${statusBadge(runRows.length ? row.state : "neutral", runRows.length ? row.state : "Configured")}${selectable ? `<label><input class="retry-source" type="checkbox" value="${escapeHtml(row.source_id)}" aria-label="Retry ${escapeHtml(source.name || row.source_id)}"> Retry</label>` : ""}</div>${errors ? `<div class="source-error">${escapeHtml(errors)}</div>` : ""}</div>`;
    }).join("");
    document.querySelectorAll(".retry-source").forEach((input) => input.addEventListener("change", updateRetryButton)); updateRetryButton();
  }
  function updateRetryButton() { $("retry-selected").disabled = !document.querySelector(".retry-source:checked") || Boolean(latestOverview?.active_run); }
  async function load() {
    try {
      const [overview, sources, runs] = await Promise.all([request("/api/v1/overview"), request("/api/v1/sources"), request("/api/v1/runs?limit=20")]);
      renderSummary(overview); const detailRuns = await Promise.all((runs.runs || []).map((run) => request(`/api/v1/runs/${encodeURIComponent(run.id)}`).catch(() => ({ ...run, sources: [], coverage: { complete: 0, total: run.total_sources || 0, failed: 0 }, counts: { found: 0, downloaded: 0, analyzed: 0 } }))));
      latestRun = detailRuns[0] || null; renderActive(overview.active_run ? (await request(`/api/v1/runs/${encodeURIComponent(overview.active_run.id)}`)) : null); renderSources(latestRun, sources.sources || []);
      $("run-history").innerHTML = detailRuns.length ? detailRuns.map((run) => `<tr><td>${escapeHtml(formatTime(run.started_at))}</td><td>${escapeHtml(run.kind || "full")}</td><td>${statusBadge(run.status, run.status)}</td><td>${escapeHtml(run.phase || "done")}</td><td>${run.coverage?.complete || 0}/${run.coverage?.total || 0}</td><td>${escapeHtml(run.publication_state || "none")}</td><td><a class="button quiet" href="/review?run_id=${encodeURIComponent(run.id)}">Review</a></td></tr>`).join("") : `<tr><td colspan="7" class="empty-cell">No runs yet. Start a run to populate durable history.</td></tr>`;
      if (overview.active_run) window.setTimeout(load, 2000);
    } catch (error) { showMessage($("overview-message"), error.message, "error"); }
  }
  $("start-run").addEventListener("click", async () => {
    const button = $("start-run"); button.disabled = true;
    try { const result = await request("/api/v1/runs", { method: "POST", body: { kind: "full" }, idempotencyKey: idempotency("run") }); if (result.status !== "running") throw new Error(result.reason_code === "no_sources" ? "Run was not started: no enabled sources are configured. Open Settings to initialize or enable sources." : `Run was not started (${result.reason_code || result.status || "unknown reason"}).`); showMessage($("overview-message"), "Run started. Progress is persisted and will update here.", "success"); await load(); }
    catch (error) { showMessage($("overview-message"), error.message, "error"); button.disabled = false; }
  });
  $("retry-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const ids = [...document.querySelectorAll(".retry-source:checked")].map((input) => input.value); if (!ids.length || !latestRun) return;
    try { await request(`/api/v1/runs/${encodeURIComponent(latestRun.id)}/retry`, { method: "POST", body: { source_ids: ids }, idempotencyKey: idempotency("retry") }); showMessage($("overview-message"), "Selected sources queued for retry.", "success"); await load(); }
    catch (error) { showMessage($("overview-message"), error.message, "error"); }
  });
  $("refresh-overview").addEventListener("click", load); load();
})();
