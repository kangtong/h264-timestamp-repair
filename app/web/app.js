"use strict";

const $ = (id) => document.getElementById(id);
const selectedFiles = new Set();
const LANGUAGE_KEY = "video-integrity-repair.locale";
let currentView = "overview";
let currentIssue = "";
let currentLocale = "zh-CN";
let messages = {};
let labels = {statuses: {}, issues: {}, reasons: {}, stages: {}};
let refreshTimer = null;
let searchTimer = null;

const statusClass = (status) => {
  const value = String(status || "").toLowerCase();
  if (["healthy", "repaired", "mediarefreshed", "succeeded"].includes(value)) return "good";
  if (["candidate", "uncertain", "queuedrepair", "queuedrecheck", "waitingstable", "queued", "running"].includes(value)) return "warn";
  if (["failed", "mediarefreshdeferred"].includes(value)) return "bad";
  return "neutral";
};

function interpolate(template, values = {}) {
  return String(template || "").replace(/\{(\w+)\}/g, (_, key) => values[key] ?? `{${key}}`);
}

function t(key, values = {}) {return interpolate(messages[key] || key, values);}

function detectLocale() {
  try {
    const stored = localStorage.getItem(LANGUAGE_KEY);
    if (["zh-CN", "en"].includes(stored)) return stored;
  } catch (_) { /* Storage may be disabled. */ }
  const languages = navigator.languages?.length ? navigator.languages : [navigator.language || "en"];
  return languages.some((language) => String(language).toLowerCase().startsWith("zh")) ? "zh-CN" : "en";
}

function apiURL(path) {
  const url = new URL(path, window.location.origin);
  url.searchParams.set("lang", currentLocale);
  return `${url.pathname}${url.search}`;
}

const formatSeconds = (seconds) => {
  if (seconds === null || seconds === undefined) return "—";
  const value = Math.max(0, Number(seconds));
  if (value >= 3600) return t("common.hours", {value: (value / 3600).toFixed(1)});
  if (value >= 60) return t("common.minutes", {value: Math.round(value / 60)});
  return t("common.seconds", {value: Math.round(value)});
};

const formatDate = (epoch) => {
  if (!epoch) return "—";
  return new Intl.DateTimeFormat(currentLocale, {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit"}).format(new Date(Number(epoch) * 1000));
};

async function getJSON(url) {
  const response = await fetch(apiURL(url), {cache: "no-store"});
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

async function postJSON(url, payload = {}) {
  const response = await fetch(apiURL(url), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || t("error.request", {status: response.status}));
  return data;
}

function applyStaticTranslations() {
  document.documentElement.lang = currentLocale;
  document.querySelectorAll("[data-i18n]").forEach((element) => {element.textContent = t(element.dataset.i18n);});
  document.querySelectorAll("[data-i18n-placeholder]").forEach((element) => {element.placeholder = t(element.dataset.i18nPlaceholder);});
  document.querySelectorAll("[data-i18n-aria]").forEach((element) => {element.setAttribute("aria-label", t(element.dataset.i18nAria));});
  document.querySelectorAll("[data-status-label]").forEach((element) => {element.textContent = labels.statuses[element.dataset.statusLabel] || element.dataset.statusLabel;});
  $("language-select").value = currentLocale;
  $("page-title").textContent = t(`nav.${currentView}`);
  updateSelection();
}

async function setLocale(locale, persist = true) {
  currentLocale = locale === "zh-CN" ? "zh-CN" : "en";
  const response = await fetch(`/api/i18n?lang=${encodeURIComponent(currentLocale)}`, {cache: "no-store"});
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  const catalog = await response.json();
  currentLocale = catalog.locale;
  messages = catalog.ui;
  labels = catalog;
  if (persist) {
    try {localStorage.setItem(LANGUAGE_KEY, currentLocale);} catch (_) { /* Storage may be disabled. */ }
  }
  applyStaticTranslations();
}

function makeBadge(code, label) {
  const badge = document.createElement("span");
  badge.className = `status-badge ${statusClass(code)}`;
  badge.textContent = label || code || t("common.unknown");
  return badge;
}

function renderStatus(data) {
  const summary = data.summary || {};
  $("count-total").textContent = summary.total ?? 0;
  $("count-candidate").textContent = summary.Candidate ?? 0;
  $("count-repaired").textContent = summary.Repaired ?? 0;
  $("count-failed").textContent = summary.Failed ?? 0;

  const heartbeat = data.heartbeat || {};
  const active = Boolean(heartbeat.current_action && heartbeat.current_action !== "idle");
  const percent = heartbeat.overall_progress;
  $("task-stage").textContent = heartbeat.current_stage_label || labels.stages[active ? "processing" : "idle"];
  $("current-file").textContent = heartbeat.current_path || "—";
  $("progress-track").classList.toggle("indeterminate", active && (percent === null || percent === undefined));
  $("progress-bar").style.width = active && percent !== null && percent !== undefined ? `${Math.max(0, Math.min(100, Number(percent)))}%` : (active ? "35%" : "0%");
  $("progress-text").textContent = active ? (percent === null || percent === undefined ? t("work.runningStage") : `${Number(percent).toFixed(1)}%`) : t("work.noTask");
  const speed = heartbeat.speed || "";
  $("speed-eta").textContent = active ? [speed, heartbeat.eta_seconds !== null && heartbeat.eta_seconds !== undefined ? t("work.remaining", {value: formatSeconds(heartbeat.eta_seconds)}) : ""].filter(Boolean).join(currentLocale === "zh-CN" ? "；" : "; ") || t("work.calculating") : "—";
  const pending = Number(heartbeat.pending_count ?? summary.pending ?? 0);
  const refresh = Number(heartbeat.media_refresh_pending_count ?? summary.media_refresh_pending ?? 0);
  $("queue-text").textContent = t("work.queueText", {files: pending, refreshes: refresh});

  const alive = Boolean(data.service_healthy);
  const state = $("service-state");
  state.className = `state-pill ${alive ? "good" : "bad"}`;
  state.replaceChildren();
  const dot = document.createElement("span"); dot.className = "state-dot";
  state.append(dot, document.createTextNode(alive ? (heartbeat.watcher_active ? t("state.normal") : t("state.degraded")) : t("state.stale")));
  state.title = heartbeat.watcher_error || "";

  const config = data.config || {};
  const mode = $("mode-pill");
  mode.textContent = config.auto_repair ? t("mode.repair") : t("mode.scan");
  mode.className = `mode-pill ${config.auto_repair ? "repair" : "scan"}`;
  $("config-formats").textContent = (config.supported_formats || ["MP4", "MKV"]).join(currentLocale === "zh-CN" ? "、" : ", ");
  $("config-auto-repair").textContent = config.auto_repair ? t("common.on") : t("common.off");
  $("config-mkv").textContent = config.repair_mkv_timestamps ? t("common.on") : t("common.off");
  $("config-reconcile").textContent = t("common.daily", {value: config.reconcile_local_time || "04:00"});
  $("config-settle").textContent = formatSeconds(config.file_settle_seconds);
  $("config-min-age").textContent = formatSeconds(config.min_file_age_seconds);
  $("config-media-refresh").textContent = config.media_refresh_enabled ? t("common.enabledRetry") : t("common.notConfigured");
  $("config-paths").textContent = config.show_full_paths ? t("common.fullPaths") : t("common.fileNames");
  renderIssueCards(summary.issues || {});
  return active;
}

function renderIssueCards(issues) {
  const container = $("issue-cards");
  container.replaceChildren();
  for (const code of ["timeline", "chapter", "multiple", "unsupported", "failed"]) {
    const button = document.createElement("button");
    button.className = "issue-card";
    button.dataset.issue = code;
    const count = document.createElement("strong"); count.textContent = issues[code] || 0;
    const text = document.createElement("span"); text.textContent = labels.issues[code] || code;
    button.append(count, text);
    button.addEventListener("click", () => {currentIssue = code; openView("problems");});
    container.append(button);
  }
}

function mediaList(data, selectable = false) {
  const wrap = document.createElement("div");
  wrap.className = "media-list";
  if (!data.items.length) {
    const empty = document.createElement("p"); empty.className = "empty-cell"; empty.textContent = t("empty.files"); wrap.append(empty); return wrap;
  }
  for (const item of data.items) {
    const row = document.createElement("article"); row.className = "media-row"; row.dataset.fileId = item.file_id;
    const select = document.createElement("div"); select.className = "media-select";
    if (selectable) {
      const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.checked = selectedFiles.has(item.file_id); checkbox.setAttribute("aria-label", t("files.select", {path: item.path}));
      checkbox.addEventListener("change", () => {checkbox.checked ? selectedFiles.add(item.file_id) : selectedFiles.delete(item.file_id); updateSelection();}); select.append(checkbox);
    }
    const main = document.createElement("div"); main.className = "media-main";
    const title = document.createElement("strong"); title.textContent = item.path;
    const meta = document.createElement("div"); meta.className = "media-meta"; meta.append(makeBadge(item.status_code || item.status, item.status_label), document.createTextNode(`${String(item.container || "").toUpperCase()} · ${item.issue_label || labels.issues.none}`));
    const reason = document.createElement("p"); reason.textContent = item.reason_label || item.reason;
    const sample = document.createElement("small");
    const diag = item.diagnostics || {};
    sample.textContent = diag.transitions !== undefined ? t("files.samples", {transitions: diag.transitions, errors: diag.non_increasing || 0}) : t("files.checked", {value: formatDate(item.checked_at)});
    main.append(title, meta, reason, sample);
    const actions = document.createElement("div"); actions.className = "row-actions";
    const recheck = document.createElement("button"); recheck.className = "button mini secondary"; recheck.textContent = t("action.recheckShort"); recheck.addEventListener("click", () => runFileAction("recheck", [item.file_id])); actions.append(recheck);
    if (item.status === "Candidate") {const repair = document.createElement("button"); repair.className = "button mini primary"; repair.textContent = t("action.repairShort"); repair.addEventListener("click", () => runFileAction("repair", [item.file_id])); actions.append(repair);}
    if (item.status === "Failed") {const retry = document.createElement("button"); retry.className = "button mini secondary"; retry.textContent = t("action.retryShort"); retry.addEventListener("click", () => runFileAction("retry", [item.file_id])); actions.append(retry);}
    row.append(select, main, actions); wrap.append(row);
  }
  return wrap;
}

async function loadProblems() {
  const params = new URLSearchParams({limit: "100", problems: "1"});
  if (currentIssue) params.set("issue", currentIssue);
  const data = await getJSON(`/api/files?${params}`);
  $("problem-results").replaceChildren(mediaList(data, true));
  document.querySelectorAll("#issue-filter .chip").forEach((button) => button.classList.toggle("active", button.dataset.issue === currentIssue));
}

async function loadFiles() {
  const params = new URLSearchParams({limit: "50"});
  const query = $("search-input").value.trim();
  if (query) params.set("q", query);
  if ($("status-filter").value) params.set("status", $("status-filter").value);
  if ($("format-filter").value) params.set("container", $("format-filter").value);
  const data = await getJSON(`/api/files?${params}`);
  $("file-result-count").textContent = t("files.resultCount", {shown: data.items.length, total: data.total});
  $("file-results").replaceChildren(mediaList(data, false));
}

async function loadHistory() {
  const data = await getJSON("/api/history?limit=50");
  const important = data.items.filter((item) => !["Healthy", "Skipped"].includes(item.status)).slice(0, 10);
  const list = $("history-list"); list.replaceChildren();
  if (!important.length) {const empty = document.createElement("li"); empty.className = "empty-cell"; empty.textContent = t("empty.events"); list.append(empty); return;}
  for (const event of important) {
    const item = document.createElement("li");
    const top = document.createElement("div"); const name = document.createElement("strong"); name.textContent = event.path || t("common.systemTask"); top.append(name, makeBadge(event.status, event.status_label));
    const reason = document.createElement("p"); reason.textContent = event.reason_label || event.reason;
    const eventTime = document.createElement("time"); eventTime.textContent = event.time || "—";
    item.append(top, reason, eventTime); list.append(item);
  }
}

async function loadTasks() {
  const data = await getJSON("/api/tasks?limit=100"); const list = $("task-list"); list.replaceChildren();
  if (!data.items.length) {const empty = document.createElement("p"); empty.className = "empty-cell"; empty.textContent = t("empty.tasks"); list.append(empty); return;}
  for (const task of data.items) {
    const row = document.createElement("article"); row.className = "task-row";
    const title = document.createElement("strong"); title.textContent = task.action_label || t(`task.${task.action}`);
    const badge = makeBadge(task.state, task.state_label || t(`task.${task.state}`));
    const detail = document.createElement("p"); detail.textContent = task.result_detail_label || task.result_detail || t("task.queuedDetail");
    const taskTime = document.createElement("small"); taskTime.textContent = formatDate(task.requested_at);
    row.append(title, badge, detail, taskTime); list.append(row);
  }
}

function updateSelection() {$("problem-selection").textContent = selectedFiles.size ? t("selection.count", {count: selectedFiles.size}) : t("selection.none");}

async function runFileAction(action, ids) {
  if (!ids.length) {window.alert(t("dialog.selectFirst")); return;}
  const actionText = action === "repair" ? t("dialog.repair") : t(`task.${action}`);
  if (!window.confirm(t("dialog.confirm", {action: actionText, count: ids.length}))) return;
  await postJSON(`/api/files/actions/${action}`, {file_ids: ids});
  selectedFiles.clear(); updateSelection();
  window.alert(t("dialog.queued"));
  await refreshAll();
}

function openView(name) {
  currentView = name;
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  document.querySelectorAll(".nav-button").forEach((button) => button.classList.toggle("active", button.dataset.view === name));
  $("page-title").textContent = t(`nav.${name}`);
  refreshAll();
}

async function refreshAll() {
  window.clearTimeout(refreshTimer);
  $("refresh-button").disabled = true;
  let active = false;
  try {
    const status = await getJSON("/api/status"); active = renderStatus(status);
    if (currentView === "overview") await loadHistory();
    if (currentView === "problems") await loadProblems();
    if (currentView === "files") await loadFiles();
    if (currentView === "tasks") await loadTasks();
    const time = new Intl.DateTimeFormat(currentLocale, {hour: "2-digit", minute: "2-digit", second: "2-digit"}).format(new Date());
    $("last-updated").textContent = t("footer.updated", {time});
  } catch (error) {
    const state = $("service-state"); state.className = "state-pill bad"; state.textContent = t("error.load", {message: error.message});
  } finally {
    $("refresh-button").disabled = false;
    refreshTimer = window.setTimeout(refreshAll, active ? 1000 : 10000);
  }
}

document.querySelectorAll(".nav-button").forEach((button) => button.addEventListener("click", () => openView(button.dataset.view)));
document.querySelectorAll("[data-open-view]").forEach((button) => button.addEventListener("click", () => openView(button.dataset.openView)));
document.querySelectorAll("#issue-filter .chip").forEach((button) => button.addEventListener("click", () => {currentIssue = button.dataset.issue; loadProblems();}));
$("refresh-button").addEventListener("click", refreshAll);
$("scan-button").addEventListener("click", async () => {if (window.confirm(t("dialog.scan"))) {await postJSON("/api/actions/reconcile"); await refreshAll();}});
$("recheck-selected").addEventListener("click", () => runFileAction("recheck", [...selectedFiles]));
$("repair-selected").addEventListener("click", () => runFileAction("repair", [...selectedFiles]));
$("retry-selected").addEventListener("click", () => runFileAction("retry", [...selectedFiles]));
$("status-filter").addEventListener("change", loadFiles);
$("format-filter").addEventListener("change", loadFiles);
$("search-input").addEventListener("input", () => {window.clearTimeout(searchTimer); searchTimer = window.setTimeout(loadFiles, 300);});
$("language-select").addEventListener("change", async (event) => {await setLocale(event.target.value); await refreshAll();});

(async () => {await setLocale(detectLocale(), false); await refreshAll();})();
