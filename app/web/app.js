"use strict";

const $ = (id) => document.getElementById(id);
const selectedFiles = new Set();
let currentView = "overview";
let currentIssue = "";
let refreshTimer = null;
let searchTimer = null;
let lastStatus = {};

const viewTitles = {overview: "概览", problems: "问题文件", files: "全部文件", tasks: "任务中心", settings: "运行设置"};
const statusClass = (status) => {
  const value = String(status || "").toLowerCase();
  if (["healthy", "repaired", "mediarefreshed", "succeeded"].includes(value)) return "good";
  if (["candidate", "uncertain", "queuedrepair", "queuedrecheck", "waitingstable", "queued", "running"].includes(value)) return "warn";
  if (["failed", "mediarefreshdeferred"].includes(value)) return "bad";
  return "neutral";
};

const formatSeconds = (seconds) => {
  if (seconds === null || seconds === undefined) return "—";
  const value = Math.max(0, Number(seconds));
  if (value >= 3600) return `${(value / 3600).toFixed(1)} 小时`;
  if (value >= 60) return `${Math.round(value / 60)} 分钟`;
  return `${Math.round(value)} 秒`;
};

const formatDate = (epoch) => {
  if (!epoch) return "—";
  return new Intl.DateTimeFormat("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit"}).format(new Date(Number(epoch) * 1000));
};

async function getJSON(url) {
  const response = await fetch(url, {cache: "no-store"});
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

async function postJSON(url, payload = {}) {
  const response = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `请求失败：${response.status}`);
  return data;
}

function makeBadge(code, label) {
  const badge = document.createElement("span");
  badge.className = `status-badge ${statusClass(code)}`;
  badge.textContent = label || code || "未知";
  return badge;
}

function renderStatus(data) {
  lastStatus = data;
  const summary = data.summary || {};
  $("count-total").textContent = summary.total ?? 0;
  $("count-candidate").textContent = summary.Candidate ?? 0;
  $("count-repaired").textContent = summary.Repaired ?? 0;
  $("count-failed").textContent = summary.Failed ?? 0;

  const heartbeat = data.heartbeat || {};
  const active = Boolean(heartbeat.current_action && heartbeat.current_action !== "idle");
  const percent = heartbeat.overall_progress;
  $("task-stage").textContent = heartbeat.current_stage || (active ? "正在处理" : "服务空闲");
  $("current-file").textContent = heartbeat.current_path || "—";
  $("progress-track").classList.toggle("indeterminate", active && (percent === null || percent === undefined));
  $("progress-bar").style.width = active && percent !== null && percent !== undefined ? `${Math.max(0, Math.min(100, Number(percent)))}%` : (active ? "35%" : "0%");
  $("progress-text").textContent = active ? (percent === null || percent === undefined ? "正在执行当前阶段" : `${Number(percent).toFixed(1)}%`) : "没有正在处理的任务";
  const speed = heartbeat.speed || "";
  $("speed-eta").textContent = active ? [speed, heartbeat.eta_seconds !== null && heartbeat.eta_seconds !== undefined ? `剩余约 ${formatSeconds(heartbeat.eta_seconds)}` : ""].filter(Boolean).join("；") || "计算中" : "—";
  const pending = Number(heartbeat.pending_count ?? summary.pending ?? 0);
  const refresh = Number(heartbeat.media_refresh_pending_count ?? summary.media_refresh_pending ?? 0);
  $("queue-text").textContent = `${pending} 个文件；${refresh} 个媒体库刷新`;

  const alive = Boolean(data.service_healthy);
  const state = $("service-state");
  state.className = `state-pill ${alive ? "good" : "bad"}`;
  state.replaceChildren();
  const dot = document.createElement("span"); dot.className = "state-dot";
  state.append(dot, document.createTextNode(alive ? (heartbeat.watcher_active ? "监听正常" : "降级运行") : "服务状态过期"));
  state.title = heartbeat.watcher_error || "";

  const config = data.config || {};
  const mode = $("mode-pill");
  mode.textContent = config.auto_repair ? "自动修复" : "仅检测";
  mode.className = `mode-pill ${config.auto_repair ? "repair" : "scan"}`;
  $("config-formats").textContent = (config.supported_formats || ["MP4", "MKV"]).join("、");
  $("config-auto-repair").textContent = config.auto_repair ? "已开启" : "已关闭";
  $("config-mkv").textContent = config.repair_mkv_timestamps ? "已开启" : "已关闭";
  $("config-reconcile").textContent = `每天 ${config.reconcile_local_time || "04:00"}`;
  $("config-settle").textContent = formatSeconds(config.file_settle_seconds);
  $("config-min-age").textContent = formatSeconds(config.min_file_age_seconds);
  $("config-media-refresh").textContent = config.media_refresh_enabled ? "已启用，失败自动重试" : "未配置";
  $("config-paths").textContent = config.show_full_paths ? "显示完整路径" : "仅显示文件名";
  renderIssueCards(summary.issues || {});
  return active;
}

function renderIssueCards(issues) {
  const labels = {timeline: "时间轴异常", chapter: "章节元数据异常", multiple: "多项异常", unsupported: "无法自动处理", failed: "处理失败"};
  const container = $("issue-cards");
  container.replaceChildren();
  for (const [code, label] of Object.entries(labels)) {
    const button = document.createElement("button");
    button.className = "issue-card";
    button.dataset.issue = code;
    const count = document.createElement("strong"); count.textContent = issues[code] || 0;
    const text = document.createElement("span"); text.textContent = label;
    button.append(count, text);
    button.addEventListener("click", () => {currentIssue = code; openView("problems");});
    container.append(button);
  }
}

function mediaList(data, selectable = false) {
  const wrap = document.createElement("div");
  wrap.className = "media-list";
  if (!data.items.length) {
    const empty = document.createElement("p"); empty.className = "empty-cell"; empty.textContent = "没有符合条件的文件"; wrap.append(empty); return wrap;
  }
  for (const item of data.items) {
    const row = document.createElement("article"); row.className = "media-row"; row.dataset.fileId = item.file_id;
    const select = document.createElement("div"); select.className = "media-select";
    if (selectable) {
      const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.checked = selectedFiles.has(item.file_id); checkbox.setAttribute("aria-label", `选择 ${item.path}`);
      checkbox.addEventListener("change", () => {checkbox.checked ? selectedFiles.add(item.file_id) : selectedFiles.delete(item.file_id); updateSelection();}); select.append(checkbox);
    }
    const main = document.createElement("div"); main.className = "media-main";
    const title = document.createElement("strong"); title.textContent = item.path;
    const meta = document.createElement("div"); meta.className = "media-meta"; meta.append(makeBadge(item.status_code || item.status, item.status_label), document.createTextNode(`${String(item.container || "").toUpperCase()} · ${item.issue_label || "无问题"}`));
    const reason = document.createElement("p"); reason.textContent = item.reason_label || item.reason;
    const sample = document.createElement("small");
    const diag = item.diagnostics || {};
    sample.textContent = diag.transitions !== undefined ? `逐帧样本 ${diag.transitions}，顺序异常 ${diag.non_increasing || 0}` : `检查时间 ${formatDate(item.checked_at)}`;
    main.append(title, meta, reason, sample);
    const actions = document.createElement("div"); actions.className = "row-actions";
    const recheck = document.createElement("button"); recheck.className = "button mini secondary"; recheck.textContent = "复检"; recheck.addEventListener("click", () => runFileAction("recheck", [item.file_id])); actions.append(recheck);
    if (item.status === "Candidate") {const repair = document.createElement("button"); repair.className = "button mini primary"; repair.textContent = "修复"; repair.addEventListener("click", () => runFileAction("repair", [item.file_id])); actions.append(repair);}
    if (item.status === "Failed") {const retry = document.createElement("button"); retry.className = "button mini secondary"; retry.textContent = "重试"; retry.addEventListener("click", () => runFileAction("retry", [item.file_id])); actions.append(retry);}
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
  $("file-result-count").textContent = `显示 ${data.items.length} / ${data.total}`;
  $("file-results").replaceChildren(mediaList(data, false));
}

async function loadHistory() {
  const data = await getJSON("/api/history?limit=50");
  const important = data.items.filter((item) => !["Healthy", "Skipped"].includes(item.status)).slice(0, 10);
  const list = $("history-list"); list.replaceChildren();
  if (!important.length) {const empty = document.createElement("li"); empty.className = "empty-cell"; empty.textContent = "暂无重要事件"; list.append(empty); return;}
  for (const event of important) {
    const item = document.createElement("li");
    const top = document.createElement("div"); const name = document.createElement("strong"); name.textContent = event.path || "系统任务"; top.append(name, makeBadge(event.status, event.status_label));
    const reason = document.createElement("p"); reason.textContent = event.reason;
    const time = document.createElement("time"); time.textContent = event.time || "—";
    item.append(top, reason, time); list.append(item);
  }
}

async function loadTasks() {
  const data = await getJSON("/api/tasks?limit=100"); const list = $("task-list"); list.replaceChildren();
  const labels = {reconcile: "扫描变化", recheck: "重新检测", repair: "无损修复", retry: "重试失败任务"};
  if (!data.items.length) {const empty = document.createElement("p"); empty.className = "empty-cell"; empty.textContent = "暂无手动任务"; list.append(empty); return;}
  for (const task of data.items) {
    const row = document.createElement("article"); row.className = "task-row";
    const title = document.createElement("strong"); title.textContent = labels[task.action] || task.action;
    const badge = makeBadge(task.state, {queued: "等待执行", running: "正在执行", succeeded: "已完成", failed: "失败"}[task.state] || task.state);
    const detail = document.createElement("p"); detail.textContent = task.result_detail || "已加入持久化任务队列";
    const time = document.createElement("small"); time.textContent = formatDate(task.requested_at);
    row.append(title, badge, detail, time); list.append(row);
  }
}

function updateSelection() {$("problem-selection").textContent = selectedFiles.size ? `已选择 ${selectedFiles.size} 个文件` : "尚未选择文件";}

async function runFileAction(action, ids) {
  if (!ids.length) {window.alert("请先选择文件"); return;}
  const labels = {recheck: "重新检测", repair: "无损修复并在校验通过后覆盖原文件", retry: "重试失败任务"};
  if (!window.confirm(`确认${labels[action]}所选的 ${ids.length} 个文件吗？`)) return;
  await postJSON(`/api/files/actions/${action}`, {file_ids: ids});
  selectedFiles.clear(); updateSelection();
  window.alert("操作已加入任务队列");
  await refreshAll();
}

function openView(name) {
  currentView = name;
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  document.querySelectorAll(".nav-button").forEach((button) => button.classList.toggle("active", button.dataset.view === name));
  $("page-title").textContent = viewTitles[name];
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
    $("last-updated").textContent = `更新于 ${new Intl.DateTimeFormat("zh-CN", {hour: "2-digit", minute: "2-digit", second: "2-digit"}).format(new Date())}`;
  } catch (error) {
    const state = $("service-state"); state.className = "state-pill bad"; state.textContent = `加载失败：${error.message}`;
  } finally {
    $("refresh-button").disabled = false;
    refreshTimer = window.setTimeout(refreshAll, active ? 1000 : 10000);
  }
}

document.querySelectorAll(".nav-button").forEach((button) => button.addEventListener("click", () => openView(button.dataset.view)));
document.querySelectorAll("[data-open-view]").forEach((button) => button.addEventListener("click", () => openView(button.dataset.openView)));
document.querySelectorAll("#issue-filter .chip").forEach((button) => button.addEventListener("click", () => {currentIssue = button.dataset.issue; loadProblems();}));
$("refresh-button").addEventListener("click", refreshAll);
$("scan-button").addEventListener("click", async () => {if (window.confirm("立即扫描媒体目录中的新增和变化文件吗？")) {await postJSON("/api/actions/reconcile"); await refreshAll();}});
$("recheck-selected").addEventListener("click", () => runFileAction("recheck", [...selectedFiles]));
$("repair-selected").addEventListener("click", () => runFileAction("repair", [...selectedFiles]));
$("retry-selected").addEventListener("click", () => runFileAction("retry", [...selectedFiles]));
$("status-filter").addEventListener("change", loadFiles);
$("format-filter").addEventListener("change", loadFiles);
$("search-input").addEventListener("input", () => {window.clearTimeout(searchTimer); searchTimer = window.setTimeout(loadFiles, 300);});

refreshAll();
