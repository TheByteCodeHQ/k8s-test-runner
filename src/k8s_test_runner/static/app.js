"use strict";

const $ = (id) => document.getElementById(id);
let selectedRun = null;
let suites = [];

const store = {
  get() { try { return sessionStorage.getItem("ktr-token") || ""; } catch { return ""; } },
  set(v) { try { sessionStorage.setItem("ktr-token", v); } catch { /* storage unavailable */ } },
};

async function api(path, options = {}) {
  const token = store.get();
  const headers = { "Content-Type": "application/json", ...(token ? { Authorization: "Bearer " + token } : {}) };
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    const entered = prompt("API token");
    if (entered) { store.set(entered); return api(path, options); }
    throw new Error("A token is required");
  }
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch { /* not JSON */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("json") ? response.json() : response.text();
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v; else if (k === "text") node.textContent = v; else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c);
  return node;
}

const badge = (status, kind = "s") => el("span", { class: `badge ${kind}-${status}`, text: status.replace("_", " ") });

function duration(run) {
  if (!run.started_at) return "";
  const end = run.finished_at ? new Date(run.finished_at) : new Date();
  const seconds = Math.max(0, Math.round((end - new Date(run.started_at)) / 1000));
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function renderRuns(runs) {
  const body = $("runs");
  body.replaceChildren();
  $("empty").hidden = runs.length > 0;
  for (const run of runs) {
    const row = el("tr", { class: "clickable" + (run.id === selectedRun ? " selected" : "") },
      el("td", {}, el("code", { text: run.id })),
      el("td", { text: run.suite }),
      el("td", {}, badge(run.status)),
      el("td", { text: run.started_at ? new Date(run.started_at).toLocaleTimeString() : "" }),
      el("td", { text: duration(run) }),
      el("td", { class: "muted", text: run.selector }));
    row.addEventListener("click", () => selectRun(run.id));
    body.append(row);
  }
}

async function renderDetail(id) {
  const run = await api(`/api/runs/${encodeURIComponent(id)}`);
  $("detail").hidden = false;
  $("detail-title").textContent = `${run.suite} · ${run.id}`;
  $("detail-message").textContent = run.message || "";
  $("cancel-btn").hidden = run.status !== "running" && run.status !== "pending";

  const rows = $("results");
  const summary = $("summary");
  rows.replaceChildren();
  summary.replaceChildren();
  if (run.status === "succeeded" || run.status === "failed") {
    try {
      const results = await api(`/api/runs/${encodeURIComponent(id)}/results`);
      for (const [label, value] of [["Total", results.total], ["Passed", results.passed],
        ["Failed", results.failed + results.error], ["Skipped", results.skipped],
        ["Seconds", results.duration_seconds]]) {
        summary.append(el("div", {}, el("b", { text: String(value) }), label));
      }
      for (const r of results.results) {
        rows.append(el("tr", {}, el("td", {}, el("code", { text: r.test_id })),
          el("td", {}, badge(r.outcome, "o")), el("td", { text: r.duration.toFixed(2) + "s" })));
        if (r.message) rows.append(el("tr", { class: "fail-detail" }, el("td", { colspan: "3", text: r.message })));
      }
    } catch (err) {
      rows.append(el("tr", {}, el("td", { colspan: "3", class: "muted", text: err.message })));
    }
  }
  $("log").textContent = await api(`/api/runs/${encodeURIComponent(id)}/logs?tail=300`);
}

async function selectRun(id) {
  selectedRun = id;
  await refresh();
}

async function refresh() {
  try {
    renderRuns(await api("/api/runs?limit=30"));
    if (selectedRun) await renderDetail(selectedRun);
  } catch (err) {
    $("form-error").textContent = err.message;
  }
}

function updateHint() {
  const suite = suites.find((s) => s.name === $("suite").value);
  $("selector").disabled = !suite || !suite.accepts_selector;
  $("selector-hint").textContent = suite && suite.accepts_selector ? `(options: ${suite.allowed_options.join(" ")})` : "(not accepted)";
}

async function init() {
  try {
    suites = await api("/api/suites");
    $("suite").replaceChildren(...suites.map((s) => el("option", { value: s.name, text: s.name })));
    updateHint();
    const health = await (await fetch("/healthz")).json();
    $("executor").textContent = health.executor;
  } catch (err) {
    $("form-error").textContent = err.message;
  }
  await refresh();
  setInterval(refresh, 3000);
}

$("suite").addEventListener("change", updateHint);
$("token-btn").addEventListener("click", () => { const v = prompt("API token", store.get()); if (v !== null) { store.set(v); init(); } });
$("cancel-btn").addEventListener("click", async () => {
  try { await api(`/api/runs/${encodeURIComponent(selectedRun)}/cancel`, { method: "POST" }); await refresh(); }
  catch (err) { $("form-error").textContent = err.message; }
});
$("run-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("form-error").textContent = "";
  try {
    const run = await api("/api/runs", { method: "POST",
      body: JSON.stringify({ suite: $("suite").value, selector: $("selector").value || null }) });
    selectedRun = run.id;
    await refresh();
  } catch (err) {
    $("form-error").textContent = err.message;
  }
});

init();
