// 剪辑工作台: the platform's web client. Plain JS, no build step, no outside requests (works on an offline
// company network); talks only to /api/v1 on the same server. The layout follows Yuxi's web client
// (github.com/xerrors/Yuxi, MIT): a sidebar with recent tasks, and each task shown as a conversation with
// the agent, its questions to you as approval cards.
"use strict";

const $app = document.getElementById("app");
const STATUS = { pending: "排队中", running: "运行中", waiting: "待审核", succeeded: "已完成", failed: "失败",
  cancelled: "已取消" };
const OUTCOME = { delivered: "已交付", rejected_by_reviewer: "已驳回", cancelled_by_reviewer: "已取消",
  stopped: "已停止（有必须修改的问题）", cancelled: "已取消" };
const WORKFLOW = {};  // type → the name people see; filled from /workflows (each workflow's `label`)
let APPS = [];  // the platform's apps (registry.APPS): each groups the workflows and pages of one business job
let PLATFORM = { name: "智能工作平台", tagline: "" };
async function loadLabels() {
  try { (await api("GET", "/workflows")).forEach(w => { WORKFLOW[w.type] = w.label || w.type; }); }
  catch { /* the list still shows the type */ }
  try { const r = await api("GET", "/apps"); APPS = r.apps; PLATFORM = r.platform; }
  catch { /* no apps: the home page shows the workflows' tasks only */ }
}
const KEY = { needs_input: "找不到商品：请给时段", confirm_plan: "确认分镜", review_video: "审核成片",
  approve_text: "审核文案", approve_draft: "审核文档", confirm_segments: "确认入库片段" };
const SHOT = { look_full: "全身", look_half: "半身", detail_fit: "上身细节", detail_fabric: "面料细节",
  talking_head: "主播近景", hanging: "挂拍", flat_lay: "平铺", other: "其他" };
const QUALITY = { good: "清楚", ok: "一般", bad: "差" };
const PURPOSE = { hook: "开头钩子", selling_point: "卖点", proof: "证明", price: "价格", cta: "引导下单",
  ending: "结尾" };
const STEP = { intake: "接收任务", route_mode: "判断模式", transcribe: "语音转写", locate_product: "定位商品讲解",
  refine_words: "逐字对齐", look: "理解画面", select_quotes: "挑选主播原话", prepare_script: "整理口播脚本",
  storyboard: "生成分镜", match_assets: "匹配素材", auto_review: "自动审核", confirm_plan: "确认分镜",
  tts: "配音", build_timeline: "排时间线", render: "渲染", qc: "质检", review_video: "审核成片", deliver: "交付",
  slice_front: "切片前处理" };
function step(s) {
  if (!s) return "";
  if (s.startsWith("waiting: ")) return "等待审核：" + (KEY[s.slice(9)] || s.slice(9));
  return STEP[s] || s;
}
const VERDICT = { approve: "批准", edit: "修改", revise: "按意见重写", reject: "驳回", provide_range: "给出时段",
  cancel: "取消" };
const ERROR_KIND = { step_error: "步骤出错", timeout: "超时", worker_lost: "执行进程失联", bad_version: "版本不符",
  bad_output: "交付文件缺失" };
const FLOW_ICON = { clip: "video", report: "doc", ingest: "library", echo: "spark" };

let timer = null;          // the page's refresh loop
let shown = "";            // what the detail page last drew, so a refresh doesn't wipe a review in progress

// ---- icons (drawn in the style of Lucide, which Yuxi uses: 24-unit grid, 2-unit round strokes) -----

const ICONS = {
  pen: '<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.4 2.6a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4Z"/>',
  list: '<rect width="8" height="4" x="8" y="2" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M12 11h4M12 16h4M8 11h.01M8 16h.01"/>',
  library: '<rect width="8" height="18" x="3" y="3" rx="1"/><path d="M7 3v18"/><path d="M20.4 18.9c.2.5-.1 1.1-.6 1.3l-1.9.7c-.5.2-1.1-.1-1.3-.6L11.1 5.1c-.2-.5.1-1.1.6-1.3l1.9-.7c.5-.2 1.1.1 1.3.6Z"/>',
  activity: '<path d="M22 12h-2.5a2 2 0 0 0-1.9 1.5l-2.4 8.3a.3.3 0 0 1-.5 0L9.2 2.2a.3.3 0 0 0-.5 0L6.4 10.5A2 2 0 0 1 4.5 12H2"/>',
  panel: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  loader: '<path d="M21 12a9 9 0 1 1-6.2-8.6"/>',
  chev: '<path d="m6 9 6 6 6-6"/>',
  spark: '<path d="M9.9 15.5A2 2 0 0 0 8.5 14l-6.1-1.6a.5.5 0 0 1 0-1L8.5 10A2 2 0 0 0 9.9 8.5l1.6-6.1a.5.5 0 0 1 1 0L14 8.5a2 2 0 0 0 1.5 1.4l6.1 1.6a.5.5 0 0 1 0 1L15.5 14a2 2 0 0 0-1.5 1.5l-1.6 6.1a.5.5 0 0 1-1 0z"/>',
  video: '<path d="m16 13 5.2 3.5a.5.5 0 0 0 .8-.4V7.9a.5.5 0 0 0-.8-.4L16 10.5"/><rect x="2" y="6" width="14" height="12" rx="2"/>',
  doc: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4M10 9H8M16 13H8M16 17H8"/>',
  file: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5M12 15V3"/>',
  alert: '<circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/>',
  hand: '<path d="M18 11V6a2 2 0 0 0-4 0v5M14 10V4a2 2 0 0 0-4 0v2M10 10.5V6a2 2 0 0 0-4 0v8"/><path d="M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.9-5.9-2.3l-3.6-3.6a2 2 0 0 1 2.8-2.8L7 15"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  retry: '<path d="M3 12a9 9 0 1 0 9-9 9.8 9.8 0 0 0-6.7 2.7L3 8"/><path d="M3 3v5h5"/>',
  stop: '<circle cx="12" cy="12" r="10"/><rect x="9" y="9" width="6" height="6" rx="1"/>',
  up: '<path d="m5 12 7-7 7 7M12 19V5"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  home: '<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .7-1.5l7-6a2 2 0 0 1 2.6 0l7 6a2 2 0 0 1 .7 1.5v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  arrow: '<path d="M5 12h14M12 5l7 7-7 7"/>',
  grid: '<rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/>',
};
function icon(name, cls = "") {
  return `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ""}</svg>`;
}
function paintIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach(el => {
    if (el.tagName === "I") el.outerHTML = icon(el.dataset.icon); else el.innerHTML = icon(el.dataset.icon);
  });
}

// ---- helpers ------------------------------------------------------------------------------------

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
    "'": "&#39;" }[c]));
}
function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    second: "2-digit" });
}
function secs(ms) { return (ms / 1000).toFixed(1) + " 秒"; }
function clock(ms) {  // 1:02:03 (what the time fields accept back)
  const s = Math.round(ms / 1000);
  return `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}
function badge(status) { return `<span class="badge ${esc(status)}">${esc(STATUS[status] || status)}</span>`; }
function me() { return document.getElementById("me").value.trim(); }
function hash(text) {  // short and stable: the same answer sent twice is one decision
  let h = 5381;
  for (let i = 0; i < text.length; i++) h = (h * 33 + text.charCodeAt(i)) >>> 0;
  return h.toString(36) + text.length.toString(36);
}
function uid() { return (crypto.randomUUID && crypto.randomUUID()) || String(Date.now()) + Math.random(); }

function toast(msg, bad = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = bad ? "bad" : "";
  t.hidden = false;
  clearTimeout(toast.h);
  toast.h = setTimeout(() => { t.hidden = true; }, bad ? 9000 : 3500);
}

async function api(method, path, body) {
  const r = await fetch("/api/v1" + path, { method, headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined });
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
  if (!r.ok) throw { status: r.status, detail: data?.detail ?? data ?? r.statusText };
  return data;
}

function problem(e) {
  const d = e.detail;
  if (Array.isArray(d)) return d.map(x => `${(x.loc || []).join(".")}: ${x.msg}`).join("；");
  if (d && typeof d === "object") return d.message || JSON.stringify(d);
  return String(d ?? e);
}

function title(t) {
  if (t.title) return t.title;  // the workflow's own short name (from the API)
  const i = t.input || {};
  const first = i.product_id || i.product?.name || i.topic || i.title || i.text
    || (i.path ? i.path.split(/[\\/]/).pop() : "") || "";
  return first ? String(first).slice(0, 24) : t.id;
}

// ---- router and the sidebar -------------------------------------------------------------------------

function route() {
  clearInterval(timer);
  shown = "";
  const h = location.hash || "#/";
  const inApp = APPS.find(a => h === "#/app/" + a.id || (a.pages || []).some(pg => h.startsWith("#/" + pg.route)));
  document.querySelectorAll(".nav-item").forEach(a => a.classList.toggle("active",
    (a.dataset.nav === "new" && h.startsWith("#/new")) || (a.dataset.nav === "home" && (h === "#/" || h === ""))
    || (a.dataset.nav === "tasks" && h === "#/tasks") || (a.dataset.nav === "status" && h === "#/status")
    || (!!a.dataset.app && a.dataset.app === inApp?.id)));
  $app.parentElement.scrollTop = 0;
  drawSessions();
  if (h.startsWith("#/new")) return newTask(decodeURIComponent(h.slice("#/new/".length)) || null);
  if (h === "#/tasks") return list();
  if (h === "#/status") return statusPage();
  if (h.startsWith("#/app/")) return appPage(decodeURIComponent(h.slice("#/app/".length)));
  if (h.startsWith("#/library")) return libraryPage(decodeURIComponent(h.slice("#/library/".length)));
  const m = h.match(/^#\/task\/([\w-]+)/);
  if (m) return detail(m[1]);
  return home();
}

// The apps in the sidebar (the ones meant for people; hidden ones are tools for whoever runs the platform).
function drawApps() {
  document.getElementById("app-nav").innerHTML = APPS.filter(a => !a.hidden).map(a =>
    `<a class="nav-item" href="#/app/${esc(a.id)}" data-app="${esc(a.id)}" title="${esc(a.name)}">${icon(a.icon || "spark")}
      <span class="nav-text">${esc(a.name)}</span></a>`).join("") || `<div class="session-empty">还没有应用</div>`;
}

// Recent tasks in the sidebar, like Yuxi's conversation list: what waits for you first.
let sessions = [];
async function refreshSessions() {
  try {  // every task waiting for a person, however old, plus the most recent ones
    const [waiting, recent] = await Promise.all([api("GET", "/tasks?status=waiting&limit=50"), api("GET", "/tasks?limit=30")]);
    sessions = [...waiting, ...recent.filter(r => !waiting.some(w => w.id === r.id))];
  } catch { return; }
  drawSessions();
}
function drawSessions() {
  const box = document.getElementById("sessions");
  if (!box) return;
  const current = (location.hash.match(/^#\/task\/([\w-]+)/) || [])[1];
  const order = { waiting: 0, running: 1, pending: 2 };
  const rows = [...sessions].sort((a, b) => (order[a.status] ?? 3) - (order[b.status] ?? 3)
    || (b.created_at > a.created_at ? 1 : -1)).slice(0, 30);
  box.innerHTML = rows.map(r => `<a class="session-item ${r.id === current ? "active" : ""}" href="#/task/${esc(r.id)}"
      title="${esc(STATUS[r.status] || r.status)} · ${esc(r.id)}">
      <span class="dot ${esc(r.status)}"></span><span class="session-title">${esc(title(r))}</span>
      <span class="session-kind">${r.status === "waiting" ? "待审核" : esc(WORKFLOW[r.workflow_type] || r.workflow_type)}</span></a>`).join("")
    || `<div class="session-empty">还没有任务</div>`;
}

function pageHeader(titleText, subtitle = "", actions = "") {
  return `<header class="page-header"><div><h1>${titleText}</h1>${subtitle ? `<div class="subtitle">${subtitle}</div>` : ""}</div>
    <span class="spacer"></span>${actions}</header>`;
}

// ---- task list ----------------------------------------------------------------------------------

let filter = "";
async function list() {
  return taskPage({ heading: "所有任务", subtitle: "平台上所有应用的任务；需要你处理的会显示“待审核”",
    actions: `<a href="#/new"><button class="primary">${icon("pen")}新建任务</button></a>` });
}

// A page with a header, optional content above, and the tasks (of `types`, or all) with a status filter.
async function taskPage({ heading, subtitle, actions, types = null, before = "" }) {
  async function draw() {
    let rows;
    const q = "/tasks?limit=100" + (filter ? "&status=" + filter : "") + (types ? "&workflow_type=" + types.join(",") : "");
    try { rows = await api("GET", q); }
    catch (e) { $app.innerHTML = pageHeader(heading) + `<div class="page-body"><div class="box bad">读取任务失败：${esc(problem(e))}</div></div>`; return; }
    const waiting = (health?.queues || []).filter(x => !types || types.includes(x.type)).reduce((n, x) => n + x.waiting, 0);
    $app.innerHTML = `<div class="page">
      ${pageHeader(heading, subtitle, actions)}
      <div class="page-body">
        ${problems().map(w => `<div class="box bad">${icon("alert")} ${esc(w.message)} · <a href="#/status">运行状态</a></div>`).join("")}
        ${before}
        <div class="row" style="margin-bottom:12px"><div class="segmented">
          ${[["", "全部"], ["waiting", "待审核"], ["running", "运行中"], ["pending", "排队中"],
             ["succeeded", "已完成"], ["failed", "失败"], ["cancelled", "已取消"]]
            .map(([v, l]) => `<button data-f="${v}" class="${v === filter ? "on" : ""}">${l}${v === "waiting" && waiting
              ? `<span class="count">${waiting}</span>` : ""}</button>`).join("")}
        </div></div>
        ${rows.length ? `<div class="table-wrap"><table><thead><tr><th>任务</th><th>类型</th><th>状态</th><th>当前步骤 / 结果</th>
            <th class="wide-only">提交人</th><th class="wide-only">提交时间</th></tr></thead><tbody>
          ${rows.map(r => `<tr class="click" data-id="${esc(r.id)}">
              <td><div class="cell-title">${esc(title(r))}</div><div class="small muted">${esc(r.id)}</div></td>
              <td class="nowrap">${icon(FLOW_ICON[r.workflow_type] || "spark")} ${esc(WORKFLOW[r.workflow_type] || r.workflow_type)}</td>
              <td>${badge(r.status)}</td>
              <td>${esc(r.status === "succeeded" ? (OUTCOME[r.outcome] || r.outcome)
                   : r.status === "failed" ? (ERROR_KIND[r.error_kind] || r.error_kind) : step(r.current_step))}</td>
              <td class="wide-only">${esc(r.created_by)}</td><td class="small muted nowrap wide-only">${when(r.created_at)}</td></tr>`).join("")}
          </tbody></table></div>` : `<div class="empty">没有${filter ? "这个状态的" : ""}任务。</div>`}
      </div></div>`;
    $app.querySelectorAll("[data-f]").forEach(b => b.onclick = () => { filter = b.dataset.f; draw(); });
    $app.querySelectorAll("tr[data-id]").forEach(tr => tr.onclick = () => { location.hash = "#/task/" + tr.dataset.id; });
  }
  await draw();
  timer = setInterval(draw, 4000);
}

// ---- the platform's home: what waits for you, the apps, how the platform is doing ------------------

function appStats(types) {
  const qs = (health?.queues || []).filter(x => types.includes(x.type));
  return { busy: qs.reduce((n, x) => n + x.running + x.pending, 0), waiting: qs.reduce((n, x) => n + x.waiting, 0) };
}

async function home() {
  async function draw() {
    if (!health) await refreshHealth();
    let waiting = [];
    try { waiting = await api("GET", "/tasks?status=waiting&limit=8"); } catch { /* shown as nothing waiting */ }
    const hour = new Date().getHours();
    const hello = hour < 11 ? "上午好" : hour < 18 ? "下午好" : "晚上好";
    const alive = (health?.workers || []).filter(w => w.alive).length;
    const failing = (health?.checks || []).filter(c => !c.ok).length;
    $app.innerHTML = `<div class="home-page">
      <div class="hero"><h1>${hello}${me() ? "，" + esc(me()) : ""}</h1>
        <p>${esc(PLATFORM.tagline || "")}</p></div>
      ${problems().map(w => `<div class="box bad">${icon("alert")} ${esc(w.message)} · <a href="#/status">运行状态</a></div>`).join("")}
      ${waiting.length ? `<section><h2 class="section-title">${icon("hand")}等你处理<span class="count-pill">${waiting.length}</span></h2>
        <div class="todo-list">${waiting.map(r => `<a class="todo-row" href="#/task/${esc(r.id)}">
          <span class="app-icon sm">${icon(FLOW_ICON[r.workflow_type] || "spark")}</span>
          <span class="todo-title">${esc(title(r))}</span><span class="todo-step">${esc(step(r.current_step))}</span>
          <span class="spacer"></span><span class="muted small nowrap">${when(r.updated_at)}</span>${icon("arrow")}</a>`).join("")}
        </div></section>` : ""}
      <section><h2 class="section-title">${icon("grid")}应用</h2><div class="app-grid">
        ${APPS.filter(a => !a.hidden).map(a => {
          const st = appStats(a.workflows.map(w => w.type));
          return `<div class="app-card" data-app="${esc(a.id)}">
            <div class="app-card-head"><span class="app-icon">${icon(a.icon || "spark")}</span>
              <div><div class="app-name">${esc(a.name)}</div><div class="app-meta">
                ${st.waiting ? `<span class="badge waiting">${st.waiting} 待审核</span>` : ""}
                ${st.busy ? `<span class="badge running">${st.busy} 进行中</span>` : ""}
                ${!st.waiting && !st.busy ? `<span class="muted small">${a.workflows.map(w => esc(w.label)).join(" · ")}</span>` : ""}</div></div></div>
            <p class="app-desc">${esc(a.description || "")}</p>
            <div class="app-actions"><a href="#/new/${esc(a.workflows[0].type)}"><button class="primary">${icon("pen")}新建</button></a>
              <a href="#/app/${esc(a.id)}"><button>进入${icon("arrow")}</button></a></div></div>`;
        }).join("") || `<div class="empty">还没有应用。在 registry.APPS 里登记。</div>`}
      </div></section>
      <a class="platform-line" href="#/status">${icon("activity")}<span>${alive} 个执行进程在线</span><span>·</span>
        <span>${failing ? `${failing} 项检查有问题` : "各项检查正常"}</span><span class="spacer"></span><span>运行状态</span>${icon("arrow")}</a>
    </div>`;
    $app.querySelectorAll(".app-card").forEach(c => c.onclick = e => {
      if (!e.target.closest("a")) location.hash = "#/app/" + c.dataset.app;
    });
  }
  await draw();
  timer = setInterval(draw, 8000);
}

// An app's own page: start its work, open its pages, and its tasks.
async function appPage(id) {
  const app = APPS.find(a => a.id === id);
  if (!app) { $app.innerHTML = `<div class="page-pad"><div class="box bad">没有这个应用：${esc(id)}</div></div>`; return; }
  const types = app.workflows.map(w => w.type);
  const quick = `<div class="quick-grid">
    ${app.workflows.map(w => `<a class="quick-card" href="#/new/${esc(w.type)}"><span class="app-icon sm">${icon(FLOW_ICON[w.type] || "spark")}</span>
      <div><div class="quick-title">新建：${esc(w.label)}</div><div class="small muted">${esc(FLOW_TIP[w.type] || "")}</div></div></a>`).join("")}
    ${(app.pages || []).map(pg => `<a class="quick-card" href="#/${esc(pg.route)}"><span class="app-icon sm">${icon(pg.icon || "file")}</span>
      <div><div class="quick-title">${esc(pg.label)}</div><div class="small muted">${esc(pg.description || "")}</div></div></a>`).join("")}
  </div><h2 class="section-title">${icon("list")}任务</h2>`;
  return taskPage({ heading: `<span class="crumb-icon">${icon(app.icon || "spark")}</span>${esc(app.name)}`, subtitle: esc(app.description || ""),
    actions: `<a href="#/new/${esc(types[0])}"><button class="primary">${icon("pen")}新建</button></a>`, types, before: quick });
}

// ---- new task -----------------------------------------------------------------------------------

function lines(text) { return text.split("\n").map(s => s.trim()).filter(Boolean); }
function words(text) { return text.split(/[,，、\s]+/).map(s => s.trim()).filter(Boolean); }

function clipInput(f) {
  const mode = f.mode.value;
  const input = { mode, product_id: f.product_id.value.trim(), options: {
    target_ms: [Math.round(+f.min_s.value * 1000), Math.round(+f.max_s.value * 1000)],
    confirm_plan: f.confirm_plan.checked, focus: words(f.focus.value), hook_keywords: words(f.hook_keywords.value),
  } };
  if (mode === "slice") {
    input.source_videos = lines(f.replays.value).map(l => {
      // file names may contain spaces: only trailing times (1:02:30, 236.5s, ms) are split off
      const m = l.match(/^(.*?)((?:\s+\d+(?::\d+){0,2}(?:\.\d+)?s?){1,2})$/);
      const [start_ms, end_ms] = m ? m[2].trim().split(/\s+/) : [];
      return Object.assign({ asset_id: m ? m[1] : l }, start_ms ? { start_ms } : {}, end_ms ? { end_ms } : {});
    });
  } else {
    if (f.script.value.trim()) input.script = { text: f.script.value.trim(), status: f.script_status.value };
    input.product_shots = lines(f.shots.value);
  }
  return input;
}

// A form drawn from a workflow's input schema (its pydantic model), so a new workflow needs no page
// code: text, numbers, yes/no, a choice from a list, and lists of text (one per line). A field of any
// other kind means no form; the JSON box below is used instead.
// Send a file in chunks; after a dropped connection or a 409, carry on from where the server got to.
let uploading = 0;  // files still on their way: the form waits for them
async function uploadFile(file, progress) {
  const up = await api("POST", "/uploads", { name: file.name, bytes: file.size, mime: file.type || null,
    created_by: me() });
  let offset = up.received, failures = 0;
  while (offset < file.size) {
    try {
      const r = await fetch(`/api/v1/uploads/${up.id}?offset=${offset}`,
        { method: "PUT", body: file.slice(offset, offset + up.chunk_bytes) });
      const data = await r.json();
      if (r.status === 409) offset = data.detail.received;
      else if (!r.ok) throw new Error(problem({ status: r.status, detail: data.detail }));
      else { offset = data.received; failures = 0; }
    } catch (e) {
      if (++failures > 5) throw e;
      await new Promise(res => setTimeout(res, 1500 * failures));
      offset = (await api("GET", "/uploads/" + up.id)).received;  // resume where the server got to
    }
    progress(offset / Math.max(1, file.size));
  }
  return up.id;
}

function schemaField(name, f, required) {
  if (f.anyOf && f.anyOf.length === 2 && f.anyOf.some(a => a.type === "null")) {  // Optional[...]: draw the inner type
    const inner = f.anyOf.find(a => a.type !== "null");
    f = { ...inner, title: f.title, description: f.description, default: f.default };
  }
  const label = esc(f.title || name) + (f.description ? ` <span class="muted small">（${esc(f.description)}）</span>` : "");
  const def = f.default, n = esc(name);
  if (f["x-upload"])
    return `<label class="field"><span>${label}</span><input type="file" data-upload-for="${n}">
      <input type="hidden" name="${n}" data-kind="upload"><span class="small muted" data-progress="${n}"></span></label>`;
  if (f.enum) {
    return `<label class="field"><span>${label}</span><select name="${n}" data-kind="enum">
      ${f.enum.map(o => `<option ${o === def ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></label>`;
  }
  if (f.type === "boolean")
    return `<label class="field"><input type="checkbox" name="${n}" data-kind="bool" ${def ? "checked" : ""}> ${label}</label>`;
  if (f.type === "integer" || f.type === "number")
    return `<label class="field"><span>${label}</span><input type="number" name="${n}" data-kind="${f.type}"
      ${f.type === "number" ? 'step="any"' : ""} value="${def ?? ""}" style="width:120px"></label>`;
  if (f.type === "array" && f.items?.type === "string")
    return `<label class="field"><span>${label}（每行一个）</span><textarea name="${n}" data-kind="lines" rows="3">${esc((def || []).join("\n"))}</textarea></label>`;
  if (f.type === "string") {
    const req = required ? "required" : "";
    return !f.maxLength || f.maxLength > 300
      ? `<label class="field"><span>${label}</span><textarea name="${n}" data-kind="text" rows="5" ${req}>${esc(def ?? "")}</textarea></label>`
      : `<label class="field"><span>${label}</span><input name="${n}" data-kind="text" value="${esc(def ?? "")}" ${req}></label>`;
  }
  return null;
}

function schemaForm(schema) {
  const req = new Set(schema.required || []);
  const parts = Object.entries(schema.properties || {}).map(([k, f]) => schemaField(k, f, req.has(k)));
  return parts.includes(null) || !parts.length ? null : parts.join("");
}

function schemaInput(f, schema) {
  const out = {}, req = schema.required || [];
  for (const name of Object.keys(schema.properties || {})) {
    const el = f.elements.namedItem(name);  // not f.elements[name]: a field may be called "length"
    if (!el) continue;
    const kind = el.dataset.kind;
    if (kind === "upload") { if (el.value) out[name] = el.value; }
    else if (kind === "bool") out[name] = el.checked;
    else if (kind === "integer" || kind === "number") { if (el.value !== "") out[name] = +el.value; }
    else if (kind === "lines") out[name] = el.value.split("\n").map(x => x.trim()).filter(Boolean);
    else if (el.value !== "" || req.includes(name)) out[name] = el.value;
  }
  return out;
}

const FLOW_TIP = {
  clip: "剪一条带货短视频：出片前请你确认分镜，出片后请你审核成片。",
  ingest: "把直播回放或商品实拍登记进素材库；回放里找到的讲解段会请你确认。",
  report: "模型按要点起草一份文档，你可以批准、直接修改，或写意见让它重写。",
  echo: "平台自测用的小任务。",
};
const FLOW_ORDER = ["clip", "ingest", "report", "echo"];

// The start screen, like Yuxi's new conversation: what to do, then the request in one composer box.
async function newTask(pre = null) {
  let flows;
  try { flows = await api("GET", "/workflows"); }
  catch (e) { $app.innerHTML = `<div class="page-pad"><div class="box bad">${esc(problem(e))}</div></div>`; return; }
  const tools = APPS.filter(a => a.hidden).flatMap(a => a.workflows.map(w => w.type));
  flows = flows.filter(w => !tools.includes(w.type) || w.type === pre);  // platform tools only when asked for
  flows.sort((a, b) => ((FLOW_ORDER.indexOf(a.type) + 1) || 99) - ((FLOW_ORDER.indexOf(b.type) + 1) || 99));
  const first = flows.some(w => w.type === pre) ? pre : flows[0]?.type || "";
  const greeting = me() ? `${esc(me())}，今天要做什么？` : "今天要做什么？";
  $app.innerHTML = `<div class="home">
    <h1>${greeting}</h1>
    <p class="lead">选一种任务，写下要求，交给智能体去做；需要你拍板的地方，它会停下来问你。</p>
    <div class="flow-pills">${flows.map(w => `<button type="button" class="flow-pill" data-flow="${esc(w.type)}">
      ${icon(FLOW_ICON[w.type] || "spark")}${esc(WORKFLOW[w.type] || w.type)}</button>`).join("")}</div>
    <form id="f" class="composer form" autocomplete="off">
      <input type="hidden" name="wf" value="${esc(first)}">
      <div class="fields" id="fields"></div>
      <details class="json-panel"><summary class="small">高级：直接编辑提交的 JSON</summary>
        <p class="small muted">修改这里会覆盖上面的表单。</p><textarea name="json" rows="10"></textarea></details>
      <div class="composer-foot"><span class="tip" id="tip"></span><span class="spacer"></span>
        <button class="primary round-btn" type="submit" title="提交" aria-label="提交">${icon("up")}</button></div>
    </form></div>`;
  const f = document.getElementById("f");
  let jsonEdited = false;
  f.json.oninput = () => { jsonEdited = true; };
  const sync = () => { if (!jsonEdited) f.json.value = JSON.stringify(current(), null, 1); };
  const current = () => f.wf.value === "clip" ? clipInput(f)
    : f.wf.value === "echo" ? { text: f.text.value, steps: +f.steps.value, step_s: +f.step_s.value,
        review: f.review.checked }
    : schemaInput(f, flows.find(w => w.type === f.wf.value).input_schema);
  function fields() {
    jsonEdited = false;
    document.querySelectorAll(".flow-pill").forEach(p => p.classList.toggle("on", p.dataset.flow === f.wf.value));
    document.getElementById("tip").textContent = FLOW_TIP[f.wf.value] || "";
    const box = document.getElementById("fields");
    if (f.wf.value === "clip") {
      box.innerHTML = `
        <label class="field half"><span>款名（商品 ID）</span><input name="product_id" required placeholder="如 示例裤A"></label>
        <label class="field half"><span>模式</span><select name="mode">
          <option value="slice">切片：从直播回放里剪主播原话</option>
          <option value="material">素材带货：商品素材 + 口播脚本配音</option></select></label>
        <div data-mode="slice"><label class="field"><span>直播回放（每行一个文件；可在后面加大致起止时间，如
          <code>material/回放.mp4 1:02:00 1:15:00</code>；留空则用素材库里关联的回放）</span>
          <textarea name="replays" rows="2"></textarea></label></div>
        <div data-mode="material" hidden>
          <label class="field"><span>口播脚本（留空则按商品资料写草稿）</span><textarea name="script" rows="4"></textarea></label>
          <label class="field half"><span>脚本状态</span><select name="script_status">
            <option value="final">定稿：只拆句，不改字</option><option value="draft">草稿：可以改写</option></select></label>
          <label class="field"><span>商品素材（每行一个文件或文件夹；留空则用素材库的）</span>
            <textarea name="shots" rows="2"></textarea></label></div>
        <label class="field half"><span>最短（秒）</span><input name="min_s" type="number" value="30" min="5"></label>
        <label class="field half"><span>最长（秒）</span><input name="max_s" type="number" value="45" min="5"></label>
        <label class="field half"><span>主推卖点（逗号分隔，如 显瘦，不贴腿）</span><input name="focus"></label>
        <label class="field half"><span>开头必须说到的词（可留空）</span><input name="hook_keywords"></label>
        <label class="field"><input type="checkbox" name="confirm_plan" checked> 出片前先确认分镜</label>`;
      f.mode.onchange = () => {
        box.querySelectorAll("[data-mode]").forEach(d => { d.hidden = d.dataset.mode !== f.mode.value; });
        sync();
      };
    } else if (f.wf.value === "echo") {
      box.innerHTML = `
        <label class="field"><span>文字</span><input name="text" value="测试"></label>
        <label class="field half"><span>步骤数</span><input name="steps" type="number" value="3"></label>
        <label class="field half"><span>每步秒数</span><input name="step_s" type="number" value="1"></label>
        <label class="field"><input type="checkbox" name="review" checked> 交付前要人审核</label>`;
    } else {
      const wf = flows.find(w => w.type === f.wf.value);
      const form = schemaForm(wf.input_schema);
      box.innerHTML = form ?? `<p class="muted">这个类型的输入表单画不出来，请在下面的 JSON 里填写。</p>
        <details><summary>输入格式</summary><pre>${esc(JSON.stringify(wf.input_schema, null, 1))}</pre></details>`;
      f.json.closest("details").open = form === null;
    }
    box.querySelectorAll("input, select, textarea").forEach(el => { el.oninput = sync; el.onchange = el.onchange || sync; });
    sync();
  }
  document.querySelectorAll(".flow-pill").forEach(p => p.onclick = () => { f.wf.value = p.dataset.flow; fields(); });
  fields();
  const request = uid();  // the same submit twice (a double click) creates one task
  f.addEventListener("change", async ev => {  // a file picked for an upload field: send it now
    const pick = ev.target;
    if (!pick.dataset?.uploadFor || !pick.files.length) return;
    const name = pick.dataset.uploadFor, hidden = f.elements.namedItem(name);
    const show = f.querySelector(`[data-progress="${name}"]`), file = pick.files[0];
    const mb = (file.size / 1048576).toFixed(1);
    hidden.value = "";
    uploading++;
    try {
      const id = await uploadFile(file, x => { show.textContent = `上传中 ${Math.floor(x * 100)}%（${mb} MB）`; });
      hidden.value = id;
      show.textContent = `已上传（${mb} MB）`;
      hidden.dispatchEvent(new Event("change"));
    } catch (e) {
      show.textContent = "上传失败：" + (e.message || problem(e));
    } finally { uploading--; }
  });
  f.onsubmit = async ev => {
    ev.preventDefault();
    if (uploading) return toast("文件还在上传，传完再提交", true);
    let input;
    try { input = jsonEdited ? JSON.parse(f.json.value) : current(); }
    catch { return toast("JSON 格式不对", true); }
    try {
      const r = await api("POST", "/tasks", { workflow_type: f.wf.value, input, created_by: me(),
        client_request_id: request });
      location.hash = "#/task/" + r.task_id;
      refreshSessions();
    } catch (e) { toast("提交失败：" + problem(e), true); }
  };
}

// ---- a task, shown as a conversation with the agent ------------------------------------------------

function fileName(p) { return String(p || "").split(/[\\/]/).pop(); }

// What was asked, in a sentence, with the files and options as chips (the person's message).
function request(t) {
  const i = t.input || {}, chips = [];
  let text;
  if (t.workflow_type === "clip") {
    const [lo, hi] = i.options?.target_ms || [];
    text = `剪一条「${i.product_id}」的带货视频（${i.mode === "material" ? "素材带货" : "直播切片"}${lo ? `，${lo / 1000}–${hi / 1000} 秒` : ""}）`;
    (i.source_videos || []).forEach(v => chips.push(["video", fileName(v.asset_id) + (v.start_ms ? ` ${v.start_ms}–${v.end_ms || ""}` : "")]));
    (i.product_shots || []).forEach(s => chips.push(["file", fileName(s)]));
    if (i.options?.focus?.length) chips.push(["spark", "卖点：" + i.options.focus.join("、")]);
    if (i.options?.hook_keywords?.length) chips.push(["spark", "开头：" + i.options.hook_keywords.join("、")]);
    if (i.options && !i.options.confirm_plan) chips.push(["check", "不用确认分镜"]);
    if (i.script?.text) text += `\n口播脚本（${i.script.status === "final" ? "定稿" : "草稿"}）：${i.script.text.slice(0, 120)}`;
  } else if (t.workflow_type === "ingest") {
    text = `把这个${i.kind === "footage" ? "商品实拍素材" : "直播回放"}登记进素材库`
      + (i.products?.length ? `，${i.kind === "footage" ? "拍的是" : "找这些款"}：${i.products.join("、")}${i.color ? `（${i.color}）` : ""}` : "，在所有商品里找");
    chips.push(["video", i.path ? fileName(i.path) : "上传的文件"]);
    if (!i.confirm) chips.push(["check", "不用人工确认"]);
  } else if (t.workflow_type === "report") {
    text = `起草一份文档：「${i.topic}」` + (i.notes ? `\n要点：${i.notes.slice(0, 300)}` : "");
    if (i.audience) chips.push(["doc", "读者：" + i.audience]);
    if (i.length) chips.push(["doc", { short: "约 300 字", medium: "约 800 字", long: "约 1500 字" }[i.length] || i.length]);
  } else if (t.workflow_type === "echo") {
    text = `测试任务：${i.text}`;
    if (i.review) chips.push(["check", "交付前要人审核"]);
  } else {
    text = JSON.stringify(i).slice(0, 300);
  }
  return `<div class="msg-human"><div class="meta">${esc(t.created_by || "未署名")} · ${when(t.created_at)}</div>
    <div class="bubble">${esc(text)}</div>
    ${chips.length ? `<div class="chips">${chips.map(([ic, c]) => `<span class="chip">${icon(ic)}<span>${esc(c)}</span></span>`).join("")}</div>` : ""}</div>`;
}

// The person's answer to a question, said as a sentence.
function answerText(w) {
  const b = w.decision_body || {};
  if (b.action === "cancel") return "这场没讲这个商品，取消";
  if (b.start !== undefined) return `在 ${b.start}–${b.end} 之间找`;
  let s = VERDICT[b.verdict] || b.verdict || "回答";
  if (b.verdict === "edit") {
    if (b.edits) s = `修改了 ${b.edits.length} 处，再看一次`;
    else if (b.segments) s = `改了片段（${b.segments.length} 段），再看一次`;
    else if (b.text !== undefined) s = "按我改的文字再审一次";
  }
  if (b.verdict === "approve" && w.key === "confirm_segments") s = "确认入库";
  return s + (b.reason ? `\n${b.reason}` : "");
}

function errorHint(kind, error, full) {
  const first = String(error || "").split("\n")[0];
  const gateway = /Timeout|timed out|Connection|RateLimit|InternalServerError/.test(error || "");
  return `<div class="error-hint"><b>${esc(ERROR_KIND[kind] || kind || "出错")}</b>：${esc(first)}
    ${gateway ? `<div class="hint">模型服务暂时不可用或太慢。做完的步骤都保存着，稍后点“重试”会从出错的步骤继续。</div>` : ""}
    ${full ? `<details><summary class="small">技术细节</summary><pre>${esc(full)}</pre></details>` : ""}</div>`;
}

const expandedGroups = new Set();  // process groups the person opened (kept across refreshes)

function timeline(t, events, history) {
  const segs = [];  // {who: "human"|"ai"|"line", html}
  const ai = html => { const last = segs.at(-1); if (last?.who === "ai") last.parts.push(html); else segs.push({ who: "ai", parts: [html] }); };
  let group = [], groupNo = 0;
  const live = ["running", "pending"].includes(t.status);
  const flush = (isLast = false) => {
    if (!group.length && !(isLast && live)) return;
    const n = groupNo++, open = expandedGroups.has(n) || (isLast && live);
    const rows = group.map(g => `<div class="step-row">${icon("check")}<span>${esc(g.name)}</span><span class="when">${esc(g.at)}</span></div>`).join("");
    const liveRow = isLast && live ? `<div class="step-row live shimmer">${icon("loader")}<span>${t.status === "pending"
      ? "排队中，等待执行进程领取" : "正在进行下一步…"}</span></div>` : "";
    const label = isLast && live ? `正在执行 · 已完成 ${group.length} 步` : `执行了 ${group.length} 个步骤`;
    ai(`<div class="process ${open ? "" : "collapsed"}" data-group="${n}">
      <button type="button" class="process-summary">${icon("chev", "chev")}<span>${label}</span></button>
      <div class="steps">${rows}${liveRow}</div></div>`);
    group = [];
  };
  for (const e of events) {
    const d = e.data || {};
    if (e.kind === "step") {
      if (!String(d.step).startsWith("waiting: ")) group.push({ name: step(d.step), at: when(e.at).slice(-8) });
    } else if (e.kind === "waiting") {
      flush();
      const w = history.find(x => x.id === d.waitpoint);
      ai(`<div class="ai-text">需要你来确认：<b>${esc(KEY[d.key] || d.key)}</b>${d.version ? `（v${d.version}）` : ""}</div>`);
      if (w && w.decision_body) {
        segs.push({ who: "human", html: `<div class="msg-human"><div class="meta">${esc(w.decided_by || "未署名")} · ${when(w.decided_at)}</div>
          <div class="bubble">${esc(answerText(w))}</div></div>` });
      } else if (w && w.status === "cancelled") {
        segs.push({ who: "line", html: `<div class="system-line">问题已关闭：${esc(w.close_reason || "任务取消")}</div>` });
      }
    } else if (e.kind === "decision_not_accepted") {
      ai(`<div class="box warn">上一个回答没有被接受：${esc(d.reason)}</div>`);
    } else if (e.kind === "failed" || e.kind === "worker_lost") {
      flush();
      const latest = !events.slice(events.indexOf(e) + 1).some(x => ["failed", "worker_lost", "succeeded"].includes(x.kind));
      ai(errorHint(d.kind || "worker_lost", d.error || "执行进程没有在租约到期前报到", latest && t.status === "failed" ? t.error : ""));
    } else if (e.kind === "retry") {
      segs.push({ who: "line", html: `<div class="system-line">${esc(d.by || "有人")} 点了重试 · ${when(e.at)}：从出错的步骤继续</div>` });
    } else if (e.kind === "cancelled" || e.kind === "cancel_requested") {
      flush();
      segs.push({ who: "line", html: `<div class="system-line">${e.kind === "cancelled" ? "任务已取消" : `${esc(d.by || "有人")} 请求取消`} · ${when(e.at)}</div>` });
    } else if (e.kind === "succeeded") {
      flush();
      ai(`<div class="ai-text">${t.outcome === "delivered" ? icon("check") + " " : ""}<b>${esc(OUTCOME[t.outcome] || t.outcome)}</b></div>
        ${summary(t.summary)}${t.artifacts.length ? artifactsCard(t.artifacts) : ""}`);
    } else if (e.kind.startsWith("workflow.")) {  // a workflow's own note: in words where we know it
      const said = { product_skipped: `跳过「${d.product}」：${d.reason || ""}`,
        command_already_applied: "上一次的回答已经生效，接着往下做" }[e.kind.slice(9)];
      ai(`<div class="small muted">${esc(said || `${e.kind.slice(9)}：${JSON.stringify(d)}`)}</div>`);
    }
  }
  flush(true);
  const name = esc(WORKFLOW[t.workflow_type] || t.workflow_type);
  return request(t) + segs.map(s => s.who === "human" || s.who === "line" ? s.html
    : `<div class="msg-ai"><div class="ai-avatar">${icon(FLOW_ICON[t.workflow_type] || "spark")}</div>
        <div class="ai-body"><div class="ai-name">${name}</div>${s.parts.join("")}</div></div>`).join("");
}

function artifactsCard(list) {
  return `<div class="artifacts"><div class="artifacts-head">${icon("file")}交付文件 · ${list.length} 个</div>
    <div class="artifacts-body">${files(list)}</div></div>`;
}

function statusBar(t) {
  const can = ["pending", "running", "waiting"].includes(t.status);
  const cancel = can ? `<button class="ghost small" id="cancel">${icon("stop")}取消任务</button>` : "";
  if (t.status === "pending") return `${icon("loader", "spin")}<span>排队中，等待执行进程领取</span><span class="spacer"></span>${cancel}`;
  if (t.status === "running") {
    const pct = t.progress != null ? ` · ${Math.round(t.progress * 100)}%` : "";
    return `${icon("loader", "spin")}<span class="shimmer">${t.current_step ? `刚完成：${esc(step(t.current_step))}，正在进行下一步` : "开始执行"}${pct}</span>
      ${t.cancel_requested ? `<span class="badge waiting">已请求取消</span>` : ""}<span class="spacer"></span>${cancel}`;
  }
  if (t.status === "waiting") return `${icon("hand")}<span>等你确认：<b>${esc(KEY[t.waiting_for?.key] || t.waiting_for?.key)}</b></span>
    <span class="spacer"></span><button class="small" id="goto">去处理</button>${cancel}`;
  if (t.status === "failed") return `${icon("alert")}<span>${esc(ERROR_KIND[t.error_kind] || t.error_kind)}：任务停下了</span>
    <span class="spacer"></span><button class="primary" id="retry">${icon("retry")}重试（从出错的步骤继续）</button>`;
  return `${icon(t.status === "succeeded" ? "check" : "x")}<span>${t.status === "succeeded" ? esc(OUTCOME[t.outcome] || t.outcome) : "已取消"}</span>
    <span class="spacer"></span><a href="#/new"><button class="small">${icon("pen")}新建任务</button></a>`;
}

async function detail(id) {
  let reviewFor = null;  // the question the review card was drawn for: a refresh never redraws a half-filled answer
  $app.innerHTML = `<div class="chat">
    <div class="chat-header" id="chat-header"></div>
    <div class="chat-main"><div id="timeline"></div><div id="review"></div></div>
    <div class="bottom"><div class="composer"><div class="composer-status" id="status-bar"></div></div>
      <div class="hint-bottom" id="hint"></div></div></div>`;
  const scroller = $app.parentElement;
  async function draw(force) {
    let t;
    try { t = await api("GET", "/tasks/" + id); }
    catch (e) { $app.innerHTML = `<div class="page-pad"><div class="box bad">${esc(problem(e))}</div></div>`; clearInterval(timer); return; }
    const [history, events] = await Promise.all([api("GET", `/tasks/${id}/waitpoints`), api("GET", `/tasks/${id}/events`)]);
    const key = [t.status, t.current_step, t.progress, t.waiting_for?.id, events.length, t.artifacts.length].join("|");
    if (key === shown && !force) return;  // nothing new
    const first = !shown;
    shown = key;
    const nearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 240;
    document.getElementById("chat-header").innerHTML = `
      <span class="chat-title">${esc(title(t))}</span><span class="kind">· ${esc(WORKFLOW[t.workflow_type] || t.workflow_type)}</span>
      ${badge(t.status)}<span class="spacer"></span>`;
    document.getElementById("timeline").innerHTML = timeline(t, events, history);
    document.querySelectorAll(".process-summary").forEach(b => b.onclick = () => {
      const g = b.closest(".process"), n = +g.dataset.group;
      g.classList.toggle("collapsed");
      if (g.classList.contains("collapsed")) expandedGroups.delete(n); else expandedGroups.add(n);
    });
    document.getElementById("status-bar").innerHTML = statusBar(t);
    document.getElementById("hint").textContent = `${t.id} · 第 ${t.attempt} 次执行${t.finished_at ? " · 结束于 " + when(t.finished_at) : ""}`;
    const review = document.getElementById("review");
    if (t.status === "waiting" && (force || reviewFor !== t.waiting_for.id)) {
      reviewFor = t.waiting_for.id;
      review.innerHTML = `<div class="approval" id="approval"><div class="approval-head"><span class="q-icon">${icon("hand")}</span>
        <span>需要你来确认 · 问题于 ${when(t.waiting_for.opened_at)} 提出</span></div><div class="approval-content" id="review-body"></div></div>`;
      renderReview(document.getElementById("review-body"), t, () => draw(true));
    } else if (t.status !== "waiting") {
      reviewFor = null;
      review.innerHTML = "";
    }
    document.getElementById("cancel")?.addEventListener("click", async () => {
      if (!confirm("确定取消这个任务？")) return;
      try { await api("POST", `/tasks/${id}/cancel`, { by: me() }); draw(true); refreshSessions(); }
      catch (e) { toast(problem(e), true); }
    });
    document.getElementById("retry")?.addEventListener("click", async () => {
      try { await api("POST", `/tasks/${id}/retry`, { by: me() }); draw(true); refreshSessions(); }
      catch (e) { toast(problem(e), true); }
    });
    document.getElementById("goto")?.addEventListener("click", () =>
      document.getElementById("approval")?.scrollIntoView({ behavior: "smooth", block: "start" }));
    if (first && t.status === "waiting") document.getElementById("approval")?.scrollIntoView({ block: "start" });
    else if (first || nearBottom) scroller.scrollTop = scroller.scrollHeight;
    if (!first) refreshSessions();
  }
  await draw(true);
  timer = setInterval(() => draw(false), 3000);
}

function files(list) {
  const first = ["video", "preview_video", "cover", "captions"];
  const rank = a => (first.indexOf(a.kind) + 1) || 99;  // the video first, data files last
  return [...list].sort((a, b) => rank(a) - rank(b)).map(a => {
    const name = { video: "成片", plan: "分镜（JSON）", cover: "封面", captions: "字幕文件", text: "文本", report: "文档",
      segments: "入库结果（JSON）", preview_video: "成片预览" }[a.kind] || a.kind;
    const player = a.mime.startsWith("video/") ? `<video controls preload="metadata" src="${esc(a.url)}"></video>` : "";
    return `${player}<div class="file-row"><span class="file-icon">${icon(a.mime.startsWith("video/") ? "video" : "doc")}</span>
      <div><div class="name">${esc(name)}</div><div class="size">${(a.bytes / 1048576).toFixed(1)} MB · ${esc(a.mime)}</div></div>
      <a class="dl" href="${esc(a.url)}" download>${icon("download")}下载</a></div>`;
  }).join("");
}

function summary(s) {
  if (!s) return "";
  const parts = [];
  if (s.qc_passed !== undefined) parts.push(`质检：${s.qc_passed ? "通过" : "未通过"}`);
  if (s.plan_version) parts.push(`分镜 v${s.plan_version}`);
  if (s.timeline_version) parts.push(`时间线 v${s.timeline_version}`);
  if (s.segments !== undefined) parts.push(`入库 ${s.segments} 段`);
  if (s.not_found?.length) parts.push(`没找到：${s.not_found.join("、")}`);
  if (s.approved_by) parts.push(`${s.approved_by} 批准`);
  const waived = (s.waived || []).map(w => `<li>${esc(w)}</li>`).join("");
  return `${parts.length ? `<div class="small muted">${esc(parts.join(" · "))}</div>` : ""}
    ${waived ? `<div class="box warn">审核时接受的提示项：<ul>${waived}</ul></div>` : ""}`;
}

// ---- review pages -------------------------------------------------------------------------------

async function decide(t, body, done) {
  const wp = t.waiting_for;
  try {
    await api("POST", `/tasks/${t.id}/decisions`, { waitpoint_id: wp.id, body, by: me(), client_request_id: `${wp.id}:${hash(JSON.stringify(body))}` });
    toast("已提交，任务继续执行");
    done();
  } catch (e) {
    if (e.status === 409) { toast("这个问题已经不是当前的问题了（可能别人已经处理过），已刷新。", true); done(); }
    else toast("不能提交：" + problem(e), true);
  }
}

function renderReview(el, t, done) {
  const wp = t.waiting_for;
  const view = wp.view;
  if (view === "clip.needs_input") return needsInput(el, t, done);
  if (view === "ingest.confirm_segments") return segmentReview(el, t, done);
  if (view === "clip.confirm_plan" || view === "clip.review_video") return clipReview(el, t, done);
  if (view === "generic.text_review") return textReview(el, t, done);
  return genericReview(el, t, done);
}

function needsInput(el, t, done) {
  const p = t.waiting_for.payload;
  el.innerHTML = `<h2>${esc(KEY.needs_input)}</h2><div class="box warn">${esc(p.question)}</div>
    <div class="row"><label>开始 <input id="start" placeholder="如 1:02:30" style="width:110px"></label>
      <label>结束 <input id="end" placeholder="如 1:10:00" style="width:110px"></label>
      <button class="primary" id="go">按这个时段继续</button><span class="spacer"></span>
      <button class="danger" id="stop">这场没讲这个商品，取消</button></div>`;
  el.querySelector("#go").onclick = () => decide(t, { start: el.querySelector("#start").value.trim(),
    end: el.querySelector("#end").value.trim() }, done);
  el.querySelector("#stop").onclick = () => decide(t, { action: "cancel" }, done);
}

function list_(items, cls, head) {
  return items && items.length ? `<div class="box ${cls}">${head}<ul>${items.map(i => `<li>${esc(i)}</li>`).join("")}</ul></div>` : "";
}

function clipReview(el, t, done) {
  const wp = t.waiting_for, p = wp.payload, isVideo = wp.key === "review_video";
  const shots = p.plan?.shots || [];
  const notes = (p.review?.findings || []).filter(f => f.severity === "note").map(f => (f.seq ? `镜头 ${f.seq}：` : "") + f.detail);
  const qc = p.qc?.checks || [];
  const can = a => wp.actions.includes(a);
  el.innerHTML = `
    <div class="row"><h2 style="margin:0">${esc(KEY[wp.key])} · v${wp.subject_version}</h2>
      ${isVideo && p.rounds_left !== undefined ? `<span class="muted small">还可以修改 ${p.rounds_left} 次</span>` : ""}</div>
    ${p.error ? `<div class="box bad">上次的提交没有被接受：${esc(p.error)}</div>` : ""}
    ${list_(p.must_fix, "bad", "<b>必须先修改</b>（修改或删掉这些镜头后才能批准）：")}
    ${list_(p.may_waive, "warn", "<b>提示项</b>（可以接受，但批准时要写理由）：")}
    ${list_(notes, "note", "供参考：")}
    <div class="${isVideo ? "two" : ""}">
      ${isVideo ? `<div>${files(wp.previews || [])}
        ${qc.length ? `<div class="small"><b>质检</b>${qc.map(c => `<div style="color:${c.severity === "fail" ? "var(--bad)" : "var(--warn)"}">${esc(c.id)}：${esc(c.detail)}</div>`).join("")}</div>`
          : `<div class="box ok small">质检全部通过</div>`}
        ${p.qc?.measured ? `<div class="small muted">${secs(p.qc.measured.duration_ms)} · ${p.qc.measured.width}×${p.qc.measured.height} · ${p.qc.measured.lufs?.toFixed?.(1)} LUFS</div>` : ""}</div>` : ""}
      <div><table><thead><tr><th>#</th><th>用途</th><th>台词 / 字幕${can("edit") ? "（可直接改字幕）" : ""}</th><th>时长</th>
        ${can("edit") ? "<th>素材</th><th>删除</th>" : ""}</tr></thead><tbody>
        ${shots.map(s => `<tr data-seq="${s.seq}"><td>${s.seq}</td>
          <td class="small">${esc(PURPOSE[s.purpose] || s.purpose)}<div class="muted">${esc(s.shot_type)}</div></td>
          <td>${s.line !== s.caption ? `<div class="small muted">${esc(s.line)}</div>` : ""}
            ${can("edit") ? `<input class="caption" value="${esc(s.caption)}" data-orig="${esc(s.caption)}">` : esc(s.caption)}</td>
          <td class="small">${secs(s.duration_ms)}</td>
          ${can("edit") ? `<td>${s.alternates?.length ? `<select class="alt"><option value="">当前</option>
              ${s.alternates.map((a, i) => `<option value="${i}">备选 ${i + 1}</option>`).join("")}</select>` : `<span class="muted small">—</span>`}</td>
            <td><input type="checkbox" class="del"></td>` : ""}</tr>`).join("")}
        </tbody></table></div></div>
    <label class="field" style="margin-top:12px"><span>理由 / 备注${p.may_waive?.length ? "（有提示项时批准必须填写）" : ""}</span>
      <textarea id="reason" rows="2"></textarea></label>
    <div class="row">
      ${can("approve") ? `<button class="primary" id="approve">批准</button>` : `<button class="primary" disabled title="有必须修改的问题">批准</button>`}
      ${can("edit") ? `<button id="edit" disabled>提交修改（0 处）</button>` : ""}
      <span class="spacer"></span>${can("reject") ? `<button class="danger" id="reject">驳回，不出这条</button>` : ""}</div>`;
  const edits = () => {
    const out = [];
    el.querySelectorAll("tr[data-seq]").forEach(tr => {
      const seq = +tr.dataset.seq, cap = tr.querySelector(".caption"), del = tr.querySelector(".del"), alt = tr.querySelector(".alt");
      tr.classList.toggle("deleted", !!del?.checked);
      const changed = cap && cap.value.trim() !== cap.dataset.orig;
      tr.classList.toggle("changed", !del?.checked && (changed || !!alt?.value));
      if (del?.checked) { out.push({ kind: "delete", seq }); return; }
      if (changed && cap.value.trim()) out.push({ kind: "caption", seq, value: cap.value.trim() });
      if (alt?.value) out.push({ kind: "asset", seq, value: +alt.value });
    });
    return out;
  };
  const refresh = () => {
    const n = edits().length, b = el.querySelector("#edit");
    if (b) { b.disabled = !n; b.textContent = `提交修改（${n} 处）`; }
  };
  el.querySelectorAll("input, select").forEach(i => { i.oninput = refresh; i.onchange = refresh; });
  const reason = () => el.querySelector("#reason").value.trim();
  el.querySelector("#approve")?.addEventListener("click", () => decide(t, { verdict: "approve", version: wp.subject_version, reason: reason() }, done));
  el.querySelector("#edit")?.addEventListener("click", () => decide(t, { verdict: "edit", version: wp.subject_version, edits: edits(), reason: reason() }, done));
  el.querySelector("#reject")?.addEventListener("click", () => {
    if (!reason()) return toast("驳回请写理由", true);
    decide(t, { verdict: "reject", version: wp.subject_version, reason: reason() }, done);
  });
}

// Any workflow's text review. Payload {text, heading?, error?}; the buttons are the actions offered:
// approve, edit (submit the changed text), revise (have it rewritten from the comments), reject.
function textReview(el, t, done) {
  const wp = t.waiting_for, p = wp.payload, can = a => wp.actions.includes(a);
  const rows = Math.min(24, Math.max(5, (p.text || "").split(/\n/).length + 1));  // the text, up to a screen
  el.innerHTML = `<h2>${esc(p.heading || KEY[wp.key] || wp.key)} · v${wp.subject_version}</h2>
    ${p.error ? `<div class="box bad">上次的提交没有被接受：${esc(p.error)}</div>` : ""}
    <textarea id="text" rows="${rows}" ${can("edit") ? "" : "readonly"}>${esc(p.text)}</textarea>
    <label class="field" style="margin-top:8px"><span>${can("revise") ? "意见（要求重写时必填：哪里要改、怎么改）" : "理由 / 备注"}</span>
      <textarea id="reason" rows="2"></textarea></label>
    <div class="row">${can("approve") ? '<button class="primary" id="approve">批准</button>' : ""}
      ${can("edit") ? '<button id="edit" disabled>按我改的文字再审一次</button>' : ""}
      ${can("revise") ? '<button id="revise">按意见重写</button>' : ""}
      <span class="spacer"></span>${can("reject") ? '<button class="danger" id="reject">驳回</button>' : ""}</div>`;
  const v = wp.subject_version, r = () => el.querySelector("#reason").value.trim(), text = el.querySelector("#text");
  const on = (id, fn) => { const b = el.querySelector("#" + id); if (b) b.onclick = fn; };
  text.oninput = () => {  // changed text is submitted as an edit, never approved by mistake
    const changed = text.value !== p.text, b = el.querySelector("#edit"), a = el.querySelector("#approve");
    if (b) b.disabled = !changed;
    if (a) { a.disabled = changed; a.title = changed ? "文字改过了：请点“按我改的文字再审一次”" : ""; }
  };
  on("approve", () => decide(t, { verdict: "approve", version: v, reason: r() }, done));
  on("edit", () => decide(t, { verdict: "edit", version: v, text: text.value, reason: r() }, done));
  on("revise", () => r() ? decide(t, { verdict: "revise", version: v, reason: r() }, done) : toast("请先写意见", true));
  on("reject", () => decide(t, { verdict: "reject", version: v, reason: r() }, done));
}

function genericReview(el, t, done) {
  const wp = t.waiting_for;
  el.innerHTML = `<h2>${esc(KEY[wp.key] || wp.key)}</h2><pre>${esc(JSON.stringify(wp.payload, null, 1))}</pre>
    <label class="field"><span>回答（JSON）</span><textarea id="body" rows="4">${esc(JSON.stringify({ version: wp.subject_version }))}</textarea></label>
    <div class="row">${wp.actions.map(a => `<button data-a="${esc(a)}">${esc(VERDICT[a] || a)}</button>`).join("")}</div>`;
  el.querySelectorAll("[data-a]").forEach(b => b.onclick = () => {
    let extra;
    try { extra = JSON.parse(el.querySelector("#body").value || "{}"); } catch { return toast("JSON 格式不对", true); }
    decide(t, { verdict: b.dataset.a, ...extra }, done);
  });
}

// ---- material intake (phase 4) -----------------------------------------------------------------

// Confirm what ingestion found in one file: change a time, change the product, drop a segment or
// add one, then send the corrected list back (it is asked again as the next version), or approve.
function segmentReview(el, t, done) {
  const wp = t.waiting_for, p = wp.payload, replay = p.kind === "replay";
  const name = p.asset.split(/[\\/]/).pop();
  const opts = sel => p.products.map(x => `<option ${x === sel ? "selected" : ""}>${esc(x)}</option>`).join("");
  const row = (g, ref) => `<tr data-ref="${ref ?? ""}">
      <td>${ref === null ? "新" : ref + 1}</td>
      <td><select class="product">${opts(g.product)}</select></td>
      <td><input class="start" value="${esc(g.start_ms === undefined ? "" : clock(g.start_ms))}" style="width:84px"></td>
      <td><input class="end" value="${esc(g.end_ms === undefined ? "" : clock(g.end_ms))}" style="width:84px"></td>
      <td class="small">${g.end_ms ? secs(g.end_ms - g.start_ms) : ""}</td>
      <td class="small">${replay
        ? `${(g.evidence || []).map(q => `<div>「${esc(q)}」</div>`).join("")}
           ${g.text ? `<details><summary class="muted">这段说了什么</summary>${esc(g.text)}</details>` : ""}`
        : `${esc(SHOT[g.shot_type] || g.shot_type || "")} · ${esc(g.color || "")} · ${esc(QUALITY[g.quality] || "")}`}</td>
      <td><input type="checkbox" class="drop" title="删掉这一段"></td>
      ${p.media_url ? `<td><button class="small play" title="从这一段的开始播放">▶</button></td>` : ""}</tr>`;
  el.innerHTML = `
    <div class="row"><h2 style="margin:0">${esc(KEY.confirm_segments)} · v${wp.subject_version}</h2>
      <span class="muted small">${replay ? "直播回放" : "实拍素材"} · ${esc(name)} · ${clock(p.duration_ms)}</span></div>
    ${p.error ? `<div class="box bad">上次的提交没有被接受：${esc(p.error)}</div>` : ""}
    ${p.not_found?.length ? `<div class="box warn">这个文件里没找到：${p.not_found.map(esc).join("、")}</div>` : ""}
    <p class="small muted">${replay ? "每一段是主播在讲某一款的时段。核对时间和商品，" : ""}确认后，剪辑时选这款就能直接用这些片段。
      时间写成 1:02:30。${p.media_url ? "点 ▶ 从那一段的开始播放。" : ""}</p>
    ${p.media_url ? `<video id="src" controls preload="metadata" src="${esc(p.media_url)}" style="max-height:300px"></video>` : ""}
    <table><thead><tr><th>#</th><th>商品</th><th>开始</th><th>结束</th><th>时长</th>
      <th>${replay ? "依据（主播原话）" : "镜头 · 颜色 · 清晰度"}</th><th>删除</th>${p.media_url ? "<th></th>" : ""}</tr></thead>
      <tbody>${p.segments.map((g, i) => row(g, i)).join("")}</tbody></table>
    <div class="row"><button id="add" class="small">+ 添加一段</button></div>
    <label class="field" style="margin-top:8px"><span>理由 / 备注</span><input id="reason"></label>
    <div class="row"><button class="primary" id="approve">确认入库</button>
      <button id="edit" disabled>按修改后的再看一次</button>
      <span class="spacer"></span><button class="danger" id="reject">整份不要</button></div>`;
  const body = el.querySelector("tbody"), v = wp.subject_version, r = () => el.querySelector("#reason").value.trim();
  const current = () => [...body.querySelectorAll("tr")].filter(tr => !tr.querySelector(".drop").checked).map(tr => ({
    ...(tr.dataset.ref === "" ? {} : { ref: +tr.dataset.ref }), product: tr.querySelector(".product").value,
    start: tr.querySelector(".start").value.trim(), end: tr.querySelector(".end").value.trim() }));
  const original = JSON.stringify(current());
  const changed = () => {
    const c = JSON.stringify(current()) !== original;
    el.querySelector("#edit").disabled = !c;
    el.querySelector("#approve").disabled = c;
    el.querySelector("#approve").title = c ? "改过了：请点“按修改后的再看一次”" : "";
  };
  body.oninput = body.onchange = changed;
  body.onclick = e => {  // ▶ plays from the start time in that row (as edited)
    if (!e.target.classList.contains("play")) return;
    const v = el.querySelector("#src"), at = e.target.closest("tr").querySelector(".start").value.trim();
    const parts = at.split(":").map(Number);
    if (!v || parts.some(isNaN)) return;
    v.currentTime = parts.reduce((acc, x) => acc * 60 + x, 0);
    v.play();
  };
  el.querySelector("#add").onclick = () => { body.insertAdjacentHTML("beforeend", row({ product: p.products[0] }, null)); changed(); };
  el.querySelector("#approve").onclick = () => decide(t, { verdict: "approve", version: v, reason: r() }, done);
  el.querySelector("#edit").onclick = () => decide(t, { verdict: "edit", version: v, segments: current(), reason: r() }, done);
  el.querySelector("#reject").onclick = () => decide(t, { verdict: "reject", version: v, reason: r() }, done);
}

// 素材库: pick a product, get every processed segment of it with its file and time range.
async function libraryPage(product) {
  let products;
  try { products = await api("GET", "/library/products"); }
  catch (e) {
    $app.innerHTML = pageHeader("素材库") + `<div class="page-body"><div class="box bad">素材库没有启动：${esc(problem(e))}<br>
      先运行 <code>python -m clipflow.library migrate</code>，再重启工作台。</div></div>`;
    return;
  }
  product = product || products.find(x => x.sell_blocks + x.shots)?.product || products[0]?.product || "";
  const owner = APPS.find(a => (a.pages || []).some(pg => pg.route === "library"));
  const crumb = owner ? `<a class="crumb" href="#/app/${esc(owner.id)}">${esc(owner.name)}</a><span class="crumb-sep">/</span>` : "";
  $app.innerHTML = `<div class="page">${pageHeader(crumb + "素材库", "选一款商品，召回已经处理过的直播讲解段和实拍镜头",
      `<a href="#/new"><button>${icon("pen")}登记新素材</button></a>`)}
    <div class="page-body">
      <div class="row" style="margin-bottom:12px"><select id="product" style="min-width:320px">
        ${products.map(x => `<option value="${esc(x.product)}" ${x.product === product ? "selected" : ""}>${esc(x.product)}
          （讲解 ${x.sell_blocks} 段 · 实拍 ${x.shots} 段 · ${x.files} 个文件）</option>`).join("")}</select></div>
      <div id="player"></div><div id="rows"><p class="muted">加载中…</p></div></div></div>`;
  document.getElementById("product").onchange = e => { location.hash = "#/library/" + encodeURIComponent(e.target.value); };
  if (!product) { document.getElementById("rows").innerHTML = `<div class="empty">还没有商品资料。</div>`; return; }
  let rows;
  try { rows = await api("GET", "/library/segments?product=" + encodeURIComponent(product)); }
  catch (e) { document.getElementById("rows").innerHTML = `<div class="box bad">${esc(problem(e))}</div>`; return; }
  document.getElementById("rows").innerHTML = rows.length ? `<div class="table-wrap"><table><thead><tr><th>类型</th><th>文件</th>
      <th>时段</th><th>内容</th><th>确认</th><th></th></tr></thead><tbody>
    ${rows.map((g, i) => `<tr><td>${g.kind === "sell_block" ? icon("video") + " 直播讲解" : icon("file") + " 实拍镜头"}</td>
      <td class="small" title="${esc(g.path)}">${esc(fileName(g.path))}</td>
      <td class="small">${clock(g.start_ms)}–${clock(g.end_ms)}<br><span class="muted">${secs(g.end_ms - g.start_ms)}</span></td>
      <td class="small">${g.kind === "sell_block" ? (g.evidence || []).slice(0, 2).map(q => `「${esc(q)}」`).join("<br>")
        : `${esc(SHOT[g.shot_type] || g.shot_type || "")} · ${esc(g.color || "")} · ${esc(QUALITY[g.quality] || "")}`}</td>
      <td>${g.confirmed ? `<span class="badge succeeded">${esc(g.confirmed_by || "已确认")}</span>` : `<span class="badge pending">未确认</span>`}</td>
      <td><button class="small" data-i="${i}">看</button></td></tr>`).join("")}</tbody></table></div>`
    : `<div class="empty">素材库里还没有「${esc(product)}」的片段。<br>到“新建任务”选“素材入库”登记回放或实拍素材，
       或运行 <code>python -m workflows.ingest scan material</code>。</div>`;
  document.querySelectorAll("[data-i]").forEach(b => b.onclick = () => {
    const g = rows[+b.dataset.i], start = g.start_ms / 1000, end = g.end_ms / 1000;
    document.getElementById("player").innerHTML = `<div class="card"><h2>${icon("video")}${esc(fileName(g.path))}
      <span class="muted small">${clock(g.start_ms)}–${clock(g.end_ms)}</span></h2>
      <div class="two">${g.frame_url ? `<img src="${esc(g.frame_url)}" style="max-width:100%;max-height:320px;border-radius:10px">` : "<div></div>"}
      <video controls preload="metadata" src="${esc(g.media_url)}#t=${start},${end}" style="max-height:320px;width:auto"></video></div>
      <div class="small muted" style="margin-top:8px">视频从这一段的开头播放。手机拍的 HEVC（.mov）素材有的浏览器放不了，看截图即可。</div></div>`;
    $app.parentElement.scrollTo({ top: 0, behavior: "smooth" });
  });
}

// ---- 运行状态: are the workers there, what is queued, what needs attention ----------------------

let health = null;  // the last /health answer, refreshed in the background
function problems() {  // what stops work (not "waiting for review", which is normal)
  return (health?.warnings || []).filter(w => w.kind !== "waiting_review");
}
async function refreshHealth() {
  try {
    const r = await fetch("/api/v1/health");
    health = await r.json();
  } catch { health = { warnings: [{ kind: "database", message: "工作台服务连不上" }] }; }
  const waiting = (health.queues || []).reduce((n, q) => n + q.waiting, 0);
  document.title = (waiting ? `（${waiting} 待审核）` : "") + PLATFORM.name;
  document.querySelector('.nav-item[data-nav="status"]')?.classList.toggle("alert", problems().length > 0);
}

function ago(seconds) {
  if (seconds == null) return "";
  const s = Math.round(seconds);
  return s < 60 ? `${s} 秒前` : s < 3600 ? `${Math.floor(s / 60)} 分钟前` : `${Math.floor(s / 3600)} 小时前`;
}

async function statusPage() {
  async function draw() {
    await refreshHealth();
    const h = health;
    $app.innerHTML = `<div class="page">${pageHeader("运行状态", h.checked_at ? "检查于 " + when(h.checked_at) : "")}
      <div class="page-body">
      ${h.database === "down" ? `<div class="box bad">数据库连不上：${esc(h.error || "")}</div>` : ""}
      ${problems().map(w => `<div class="box bad">${icon("alert")} ${esc(w.message)}</div>`).join("")
        || (h.database === "ok" ? `<div class="box ok">${icon("check")} 一切正常：每种任务都有执行进程在运行。</div>` : "")}
      <div class="card"><h2>${icon("list")}任务队列</h2><div class="table-wrap"><table><thead><tr><th>类型</th><th>排队</th><th>运行中</th>
        <th>待审核</th><th>最久排队</th><th>24 小时失败</th><th>执行进程</th></tr></thead><tbody>
        ${(h.queues || []).map(q => `<tr><td>${icon(FLOW_ICON[q.type] || "spark")} ${esc(WORKFLOW[q.type] || q.type)} <span class="muted small">${esc(q.type)}</span></td>
          <td>${q.pending}</td><td>${q.running}</td><td>${q.waiting ? `<a href="#/">${q.waiting}</a>` : 0}</td>
          <td>${q.oldest_pending_s == null ? "" : ago(q.oldest_pending_s).replace("前", "")}</td>
          <td>${q.failed_24h ? `<span style="color:var(--error-700)">${q.failed_24h}</span>` : 0}</td>
          <td>${q.workers.length ? q.workers.length + " 个" : `<span class="badge failed">没有</span>`}</td></tr>`).join("")}
        </tbody></table></div></div>
      ${(h.checks || []).length ? `<div class="card"><h2>${icon("activity")}检查 <span class="muted small">${h.checks_at ? when(h.checks_at) : ""}（每 5 分钟）</span></h2>
        <div class="table-wrap"><table><tbody>${h.checks.map(c => `<tr><td style="width:120px">${esc(c.name)}</td>
          <td style="width:90px">${c.ok ? `<span class="badge succeeded">正常</span>` : `<span class="badge failed">有问题</span>`}</td>
          <td class="small">${esc(c.message)}</td></tr>`).join("")}</tbody></table></div></div>` : ""}
      <div class="card"><h2>${icon("spark")}执行进程</h2>${(h.workers || []).length ? `<div class="table-wrap"><table><thead><tr><th>进程</th>
        <th>处理的类型</th><th>状态</th><th>最后报到</th><th>正在跑</th></tr></thead><tbody>
        ${h.workers.map(w => `<tr><td class="small">${esc(w.name)}</td><td>${w.types.map(t => esc(WORKFLOW[t] || t)).join("、")}</td>
          <td>${w.alive ? `<span class="badge running">在线</span>` : w.stopped_at ? `<span class="badge cancelled">已停止</span>`
            : `<span class="badge failed">失联</span>`}</td>
          <td class="small">${ago(w.silent_s)}</td>
          <td class="small">${w.current_task ? `<a href="#/task/${esc(w.current_task)}">${esc(w.current_task)}</a>` : ""}</td></tr>`).join("")}
        </tbody></table></div>` : `<p class="muted">最近 24 小时没有执行进程报到。用 <code>python -m foundation.dev</code> 启动。</p>`}</div>
      ${(h.failures_24h || []).length ? `<div class="card"><h2>${icon("alert")}24 小时内的失败</h2>
        ${h.failures_24h.map(f => `<div>${esc(ERROR_KIND[f.error_kind] || f.error_kind)}：${f.n} 个</div>`).join("")}
        <p class="small muted">在任务列表里点“失败”查看，多数可以点重试继续。</p></div>` : ""}
      </div></div>`;
  }
  await draw();
  timer = setInterval(draw, 5000);
}

// ---- start --------------------------------------------------------------------------------------

const meInput = document.getElementById("me");
const avatar = () => { document.getElementById("avatar").textContent = (me() || "?").slice(0, 1).toUpperCase(); };
try { meInput.value = localStorage.getItem("me") || ""; } catch { /* private mode */ }
meInput.oninput = () => { try { localStorage.setItem("me", meInput.value.trim()); } catch { /* ignore */ } avatar(); };
avatar();
const layout = document.getElementById("layout");
try { layout.classList.toggle("collapsed", localStorage.getItem("collapsed") === "1"); } catch { /* ignore */ }
document.getElementById("collapse").onclick = () => {
  layout.classList.toggle("collapsed");
  try { localStorage.setItem("collapsed", layout.classList.contains("collapsed") ? "1" : "0"); } catch { /* ignore */ }
};
paintIcons();
window.addEventListener("hashchange", route);
loadLabels().then(() => {
  document.getElementById("brand-name").textContent = PLATFORM.name;
  document.getElementById("brand-logo").textContent = PLATFORM.name.slice(0, 1);
  document.title = PLATFORM.name;
  drawApps();
  refreshSessions();
  route();
});
refreshHealth();
setInterval(refreshHealth, 15000);
setInterval(refreshSessions, 8000);
