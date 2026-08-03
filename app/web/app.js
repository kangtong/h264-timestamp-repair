"use strict";

const $ = (id) => document.getElementById(id);
let searchTimer = null;

const statusClass = (status) => {
  const value = String(status || "").toLowerCase();
  if (["healthy", "repaired"].includes(value)) return "good";
  if (["candidate", "waitingstable", "uncertain"].includes(value)) return "warn";
  if (value === "failed") return "bad";
  return "neutral";
};

const formatAge = (seconds) => {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${Math.round(seconds)} 秒前`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟前`;
  return `${(seconds / 3600).toFixed(1)} 小时前`;
};

const formatSeconds = (seconds) => {
  const value = Number(seconds || 0);
  if (value % 3600 === 0 && value >= 3600) return `${value / 3600} 小时`;
  if (value % 60 === 0 && value >= 60) return `${value / 60} 分钟`;
  return `${value} 秒`;
};

const formatDate = (epoch) => {
  if (!epoch) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit"
  }).format(new Date(Number(epoch) * 1000));
};

const makeBadge = (status) => {
  const span = document.createElement("span");
  span.className = `status-badge ${statusClass(status)}`;
  span.textContent = status || "Unknown";
  return span;
};

async function getJSON(url) {
  const response = await fetch(url, {cache: "no-store"});
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function renderStatus(data) {
  const summary = data.summary || {};
  $("count-total").textContent = summary.total ?? 0;
  $("count-candidate").textContent = summary.Candidate ?? 0;
  $("count-repaired").textContent = summary.Repaired ?? 0;
  $("count-failed").textContent = summary.Failed ?? 0;

  const heartbeat = data.heartbeat || {};
  const pending = Number(heartbeat.pending_count ?? summary.pending ?? 0);
  const active = Boolean(heartbeat.current_path);
  $("progress-bar").style.width = active ? "45%" : (pending ? "15%" : "100%");
  $("progress-text").textContent = pending ? `${pending} 个文件` : "队列为空";
  $("current-file").textContent = heartbeat.current_path || (heartbeat.current_action === "reconcile" ? "正在校准元数据" : "—");
  $("heartbeat-age").textContent = formatAge(data.heartbeat_age_seconds);

  const alive = Boolean(data.service_healthy);
  const state = $("service-state");
  state.className = `state-pill ${alive ? "good" : "bad"}`;
  state.innerHTML = "";
  const dot = document.createElement("span");
  dot.className = "state-dot";
  const watching = Boolean(heartbeat.watcher_active);
  state.append(dot, document.createTextNode(alive ? (watching ? "监听正常" : "降级运行") : "状态过期"));
  state.title = heartbeat.watcher_error || "";

  const autoRepair = Boolean(heartbeat.auto_repair ?? data.config?.auto_repair);
  const mode = $("mode-pill");
  mode.textContent = autoRepair ? "自动修复" : "仅扫描";
  mode.className = `mode-pill ${autoRepair ? "repair" : "scan"}`;

  const config = data.config || {};
  $("config-reconcile").textContent = `每天 ${config.reconcile_local_time || "04:00"}`;
  $("config-settle").textContent = formatSeconds(config.file_settle_seconds);
  $("config-min-age").textContent = formatSeconds(config.min_file_age_seconds);
  $("config-filter").textContent = config.name_filter_enabled ? "已启用" : "未启用（全部 MP4）";
  $("config-paths").textContent = config.show_full_paths ? "完整路径" : "仅文件名（脱敏）";
}

function renderFiles(data) {
  const body = $("files-body");
  body.replaceChildren();
  $("result-count").textContent = `显示 ${data.items.length} / ${data.total}`;
  if (!data.items.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.className = "empty-cell";
    cell.textContent = "没有符合条件的记录";
    row.append(cell);
    body.append(row);
    return;
  }
  for (const item of data.items) {
    const row = document.createElement("tr");
    const file = document.createElement("td");
    file.className = "file-cell";
    file.textContent = item.path;
    const status = document.createElement("td");
    status.append(makeBadge(item.status));
    const reason = document.createElement("td");
    reason.className = "reason-cell";
    reason.textContent = item.reason;
    const sample = document.createElement("td");
    sample.className = "numeric-cell";
    sample.textContent = `${item.different} / ${item.comparable}`;
    sample.title = "PTS 与 DTS 不同的数据包 / 可比较数据包";
    const checked = document.createElement("td");
    checked.className = "time-cell";
    checked.textContent = formatDate(item.checked_at);
    row.append(file, status, reason, sample, checked);
    body.append(row);
  }
}

function renderHistory(data) {
  const list = $("history-list");
  list.replaceChildren();
  if (!data.items.length) {
    const item = document.createElement("li");
    item.className = "empty-cell";
    item.textContent = "暂无事件";
    list.append(item);
    return;
  }
  for (const event of data.items.slice(0, 10)) {
    const item = document.createElement("li");
    const top = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = event.path || "未知文件";
    top.append(name, makeBadge(event.status));
    const reason = document.createElement("p");
    reason.textContent = event.reason;
    const time = document.createElement("time");
    time.textContent = event.time || "—";
    item.append(top, reason, time);
    list.append(item);
  }
}

async function loadStatus() {
  const data = await getJSON("/api/status");
  renderStatus(data);
}

async function loadFiles() {
  const params = new URLSearchParams({limit: "200"});
  const query = $("search-input").value.trim();
  const status = $("status-filter").value;
  if (query) params.set("q", query);
  if (status) params.set("status", status);
  renderFiles(await getJSON(`/api/files?${params.toString()}`));
}

async function loadHistory() {
  renderHistory(await getJSON("/api/history?limit=30"));
}

async function refreshAll() {
  $("refresh-button").disabled = true;
  try {
    await Promise.all([loadStatus(), loadFiles(), loadHistory()]);
    $("last-updated").textContent = `更新于 ${new Intl.DateTimeFormat("zh-CN", {hour: "2-digit", minute: "2-digit", second: "2-digit"}).format(new Date())}`;
  } catch (error) {
    const state = $("service-state");
    state.className = "state-pill bad";
    state.textContent = "加载失败";
    console.error(error);
  } finally {
    $("refresh-button").disabled = false;
  }
}

$("refresh-button").addEventListener("click", refreshAll);
$("status-filter").addEventListener("change", loadFiles);
$("search-input").addEventListener("input", () => {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(loadFiles, 300);
});

refreshAll();
window.setInterval(refreshAll, 10000);
