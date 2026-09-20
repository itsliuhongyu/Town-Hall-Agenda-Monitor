(function () {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const page = document.body.dataset.page || "";
  let uiTimezone = localStorage.getItem("agenda.uiTimezone") || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  document.querySelectorAll("[data-nav]").forEach((node) => { if (node.dataset.nav === page) node.setAttribute("aria-current", "page"); });
  const headerStatus = document.getElementById("header-status");

  function idempotency(prefix) { return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`; }
  function escapeHtml(value) { const node = document.createElement("span"); node.textContent = value == null ? "" : String(value); return node.innerHTML; }
  function formatTime(value) { if (!value) return "Never"; const date = new Date(value); if (Number.isNaN(date.getTime())) return String(value); try { return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short", timeZone: uiTimezone }).format(date); } catch (_) { return date.toLocaleString(); } }
  function statusClass(value) { return String(value || "neutral").replace(/[^a-z_]/g, "_"); }
  function statusBadge(value, label) { return `<span class="status ${statusClass(value)}">${escapeHtml(label || String(value || "Unknown").replaceAll("_", " "))}</span>`; }
  function priorityBadge(value) { return value ? `<span class="priority-badge priority-${escapeHtml(value)}">${escapeHtml(value)}</span>` : `<span class="muted">Not set</span>`; }
  function showMessage(node, text, kind) { if (!node) return; node.hidden = !text; node.textContent = text || ""; node.className = `message${kind ? ` ${kind}` : ""}`; }

  async function request(path, options = {}) {
    const method = options.method || "GET";
    const headers = new Headers(options.headers || {});
    if (method !== "GET") { headers.set("Content-Type", "application/json"); headers.set("Idempotency-Key", options.idempotencyKey || idempotency("ui")); headers.set("X-CSRF-Token", csrf); }
    const response = await fetch(path, { ...options, method, headers, body: method === "GET" ? undefined : JSON.stringify(options.body || {}) });
    const type = response.headers.get("content-type") || "";
    const data = type.includes("json") ? await response.json() : await response.text();
    if (!response.ok) { const detail = data?.error || {}; const error = new Error(detail.message || `Request failed (${response.status})`); error.status = response.status; error.code = detail.code; error.details = detail.details || {}; throw error; }
    return data;
  }
  async function waitForJob(statusUrl, onUpdate) {
    for (let attempt = 0; attempt < 90; attempt += 1) {
      const job = await request(statusUrl); if (onUpdate) onUpdate(job);
      if (job.state && job.state !== "running") return job;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    throw new Error("The operation is still running; refresh to inspect its status.");
  }
  async function headerPoll() {
    if (!headerStatus) return;
    try {
      const overview = await request("/api/v1/overview"); const active = overview.active_run;
      headerStatus.textContent = active ? `Active run: ${active.phase || "working"}` : "No active run";
    } catch (_) { headerStatus.textContent = "Local app status unavailable"; }
  }
  window.AgendaUI = { request, waitForJob, idempotency, escapeHtml, formatTime, statusClass, statusBadge, priorityBadge, showMessage, setUiTimezone: (value) => { uiTimezone = value || uiTimezone; localStorage.setItem("agenda.uiTimezone", uiTimezone); } };
  fetch("/api/v1/settings").then((response) => response.ok ? response.json() : null).then((data) => { if (data?.values?.ui_timezone) { uiTimezone = data.values.ui_timezone; localStorage.setItem("agenda.uiTimezone", uiTimezone); window.dispatchEvent(new Event("agenda-timezone-changed")); } }).catch(() => {});
  headerPoll();
  window.setInterval(headerPoll, 5000);
})();
