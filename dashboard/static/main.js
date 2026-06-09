// NERVE dashboard client.
//
// NOTE: the backend serves only dashboard/templates/index.html (no /static mount),
// so the authoritative copy of this script is the inline <script> in index.html.
// This file is a JS-only mirror kept in sync for reference / future bundling.

"use strict";
const POLL_FALLBACK_MS = 10000;   // used only while the WebSocket is down
const RECONNECT_MAX_MS = 30000;
const FEED_NOISE = new Set(["MCP_TOOL_CALLED", "MCP_TOOL_RESULT", "RESOLUTION_CHECK"]);
const RISK_C = 326.73;            // 2π·52
const ROLE_ABBR = { planner: "PLAN", execution: "EXEC", risk: "RISK", auditor: "AUDT" };

let missionId = new URLSearchParams(location.search).get("mission");
let currentMission = null, failures = [], lastTasks = [];
let ws = null, wsAttempts = 0, wsReconnectTimer = null;
let pollFallbackTimer = null, elapsedTimer = null, derivedTimer = null, derivedNeeds = new Set();

/* ───────── known-mission registry (localStorage; no list API exists) ───────── */
function getKnown() { try { return JSON.parse(localStorage.getItem("nerve_missions") || "[]"); } catch { return []; } }
function addKnown(id, type) {
  const list = getKnown().filter((m) => m.id !== id);
  list.unshift({ id, type: type || "?" });
  localStorage.setItem("nerve_missions", JSON.stringify(list.slice(0, 25)));
  renderSelect();
}
function renderSelect() {
  const known = getKnown();
  const cur = known.find((m) => m.id === missionId);
  const label = document.getElementById("missionDdLabel");
  if (cur) label.innerHTML = `${escapeHtml(cur.type)} · <span class="dd-id">${escapeHtml(cur.id.slice(0, 8))}</span>`;
  else label.textContent = "— mission —";
  const items = [`<li role="option" class="dd-item placeholder ${missionId ? "" : "sel"}" onclick="ddPick('')">— mission —</li>`];
  known.forEach((m) => {
    const sel = m.id === missionId ? "sel" : "";
    items.push(`<li role="option" class="dd-item ${sel}" onclick="ddPick('${escapeHtml(m.id)}')">${escapeHtml(m.type)} · <span class="dd-id">${escapeHtml(m.id.slice(0, 8))}</span></li>`);
  });
  items.push(`<li role="option" class="dd-item action" onclick="ddPick('__launch__')">+ NEW / LINK…</li>`);
  document.getElementById("missionDdMenu").innerHTML = items.join("");
}
function toggleDd(e) { if (e) e.stopPropagation(); document.getElementById("missionDd").classList.contains("open") ? closeDd() : openDd(); }
function openDd() {
  document.getElementById("missionDd").classList.add("open");
  document.getElementById("missionDdMenu").classList.remove("hidden");
  document.getElementById("missionDdBtn").setAttribute("aria-expanded", "true");
}
function closeDd() {
  document.getElementById("missionDd").classList.remove("open");
  document.getElementById("missionDdMenu").classList.add("hidden");
  document.getElementById("missionDdBtn").setAttribute("aria-expanded", "false");
}
function ddPick(v) { closeDd(); if (v === "__launch__") { showLaunch(); return; } if (v) boot(v); }
document.addEventListener("click", (e) => { const dd = document.getElementById("missionDd"); if (dd && !dd.contains(e.target)) closeDd(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDd(); });

/* ───────── view switching + lifecycle ───────── */
function setQuery(id) { history.replaceState(null, "", id ? `?mission=${id}` : location.pathname); }
function showLaunch() {
  stopAll(); missionId = null; setQuery(null); renderSelect(); setConn(false, "offline");
  document.getElementById("missionView").classList.add("hidden");
  document.getElementById("launchView").classList.remove("hidden");
}
function showMission() {
  document.getElementById("launchView").classList.add("hidden");
  document.getElementById("missionView").classList.remove("hidden");
}
function stopAll() {
  if (elapsedTimer) clearInterval(elapsedTimer);
  if (wsReconnectTimer) clearTimeout(wsReconnectTimer);
  if (derivedTimer) clearTimeout(derivedTimer);
  elapsedTimer = wsReconnectTimer = derivedTimer = null;
  stopPollFallback(); closeWS();
}
function boot(id) {
  stopAll();
  missionId = id; setQuery(id); renderSelect(); showMission();
  wsAttempts = 0; failures = [];
  fetchState();                                  // one full render to seed all panels
  connectWS();                                   // then live updates over WebSocket
  elapsedTimer = setInterval(updateElapsed, 1000);
}

/* ───────── WebSocket transport (logic unchanged) ───────── */
function wsUrl() { const proto = location.protocol === "https:" ? "wss" : "ws"; return `${proto}://${location.host}/ws/${missionId}`; }
function connectWS() {
  closeWS();
  let socket;
  try { socket = new WebSocket(wsUrl()); } catch { scheduleReconnect(); return; }
  ws = socket;
  socket.onopen = () => { setConn(true); wsAttempts = 0; stopPollFallback(); };
  socket.onmessage = (m) => { try { handleEvent(JSON.parse(m.data)); } catch { /* ignore bad frame */ } };
  socket.onerror = () => { try { socket.close(); } catch {} };
  socket.onclose = () => { if (ws !== socket) return; ws = null; setConn(false); startPollFallback(); scheduleReconnect(); };
}
function closeWS() { if (!ws) return; ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null; try { ws.close(); } catch {} ws = null; }
function scheduleReconnect() {
  if (wsReconnectTimer || !missionId) return;
  const delay = Math.min(RECONNECT_MAX_MS, 1000 * Math.pow(2, wsAttempts)); wsAttempts++;
  setConn(false, `reconnect ${Math.round(delay / 1000)}s`);
  wsReconnectTimer = setTimeout(() => { wsReconnectTimer = null; connectWS(); }, delay);
}
function startPollFallback() { if (pollFallbackTimer) return; fetchState(); pollFallbackTimer = setInterval(fetchState, POLL_FALLBACK_MS); }
function stopPollFallback() { if (pollFallbackTimer) clearInterval(pollFallbackTimer); pollFallbackTimer = null; }
function setConn(connected, text) {
  const dot = document.getElementById("connDot"), label = document.getElementById("connText");
  if (connected) { dot.className = "glow-dot on"; label.textContent = "LINKED"; label.className = "conn-text linked"; }
  else { dot.className = "glow-dot off"; label.textContent = (text ? text : "reconnecting").toUpperCase(); label.className = "conn-text down"; }
}

/* ───────── incremental updates from a single event (dispatch unchanged) ───────── */
function handleEvent(ev) {
  prependFeed(ev);
  switch (ev.event_type) {
    case "MISSION_STATUS_CHANGED":
      if (currentMission && ev.payload && ev.payload.to) {
        currentMission.status = ev.payload.to;
        if (["resolved", "failed"].includes(ev.payload.to)) currentMission.updated_at = ev.created_at;
        renderStatusBar(currentMission);
      }
      break;
    case "RISK_SCORE_UPDATED":
      if (ev.payload) renderRisk(ev.payload.overall, ev.payload.breakdown);
      break;
    case "FAILURE_INJECTED":
      if (ev.payload && ev.payload.type) { failures.push({ type: ev.payload.type, target: ev.payload.target, severity: ev.payload.severity }); renderFailures(failures); }
      break;
    case "FAILURE_CLEARED":
      if (ev.payload && ev.payload.type) { failures = failures.filter((f) => f.type !== ev.payload.type); renderFailures(failures); }
      break;
    case "ACTION_CREATED": case "ACTION_APPROVED": case "ACTION_REJECTED": case "ACTION_EXECUTED":
      scheduleDerivedRefresh("actions"); break;
    case "TASK_STARTED": case "TASK_COMPLETED": case "TASK_FAILED": case "TASK_RETRYING":
      if (ev.task_id) fireNeuron(ev.task_id);
      scheduleDerivedRefresh("tasks"); break;
  }
}
function scheduleDerivedRefresh(which) {
  derivedNeeds.add(which);
  if (derivedTimer) return;
  derivedTimer = setTimeout(async () => {
    derivedTimer = null;
    const needs = derivedNeeds; derivedNeeds = new Set();
    if (!missionId) return;
    try {
      const res = await fetch(`/missions/${missionId}`);
      if (!res.ok) return;
      const data = await res.json();
      if (needs.has("actions")) renderActions(data.pending_actions || []);
      if (needs.has("tasks")) renderTasks(data.tasks || []);
    } catch { /* transient */ }
  }, 300);
}

/* ───────── full state fetch (initial seed + disconnected fallback) ───────── */
async function fetchState() {
  if (!missionId) return;
  let data;
  try {
    const res = await fetch(`/missions/${missionId}`);
    if (!res.ok) { setStatusBadge(res.status === 404 ? "not found" : "error " + res.status); return; }
    data = await res.json();
  } catch { return; }
  currentMission = data.mission;
  failures = (data.active_failures || []).slice();
  renderStatusBar(data.mission);
  renderRisk(data.risk, data.risk_breakdown);
  renderActions(data.pending_actions || []);
  renderFailures(failures);
  renderFeed(data.recent_events || []);
  renderTasks(data.tasks || []);
}

/* ───────── launchers ───────── */
function setLoading(btn, txt) { if (!btn) return; btn.dataset.orig = btn.dataset.orig || btn.textContent; btn.disabled = true; btn.textContent = txt; }
function resetButton(btn) { if (!btn) return; btn.disabled = false; if (btn.dataset.orig) btn.textContent = btn.dataset.orig; }
function launchMsg(t, err) { const el = document.getElementById("launchMsg"); el.textContent = t; el.style.color = err ? "var(--alert)" : "var(--vital)"; }

async function launchMission() {
  const goal = document.getElementById("goalInput").value.trim();
  if (!goal) return launchMsg("Enter a mission goal first.", true);
  const btn = document.getElementById("launchBtn"); setLoading(btn, "INITIALIZING…");
  try {
    const res = await fetch("/missions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ goal, mission_type: "GENERAL" }) });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json(); addKnown(data.mission_id, "GENERAL"); boot(data.mission_id);
  } catch (e) { launchMsg("Launch failed: " + e.message, true); resetButton(btn); }
}
async function startDemo() {
  const btn = document.getElementById("demoBtn") || document.getElementById("demoBtnLaunch"); setLoading(btn, "INITIALIZING…");
  try {
    const res = await fetch("/demo/start");
    if (!res.ok) throw new Error(res.status === 403 ? "DEMO_MODE is not enabled on the server." : "HTTP " + res.status);
    const data = await res.json();
    if (data.mission_id) { addKnown(data.mission_id, "INCIDENT_RESPONSE"); resetButton(btn); boot(data.mission_id); return; }
    resetButton(btn); launchMsg("Demo started — link by mission_id from the server log.", false); showLaunch();
  } catch (e) { resetButton(btn); showLaunch(); launchMsg("Could not start demo: " + e.message, true); }
}
function loadById() {
  const id = document.getElementById("missionIdInput").value.trim();
  if (!id) return launchMsg("Enter a mission_id.", true);
  addKnown(id, "?"); boot(id);
}

/* ───────── approvals ───────── */
async function approve(id) {
  const who = prompt("Approve as:", "demo-operator"); if (!who) return;
  await fetch(`/actions/${id}/approve`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ approved_by: who }) });
  scheduleDerivedRefresh("actions");
}
async function reject(id) {
  const who = prompt("Reject as:", "demo-operator"); if (!who) return;
  const reason = prompt("Reason:", "not the cause"); if (!reason) return;
  await fetch(`/actions/${id}/reject`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ approved_by: who, reason }) });
  scheduleDerivedRefresh("actions");
}

/* ───────── status + vitals ───────── */
function setStatusBadge(text) { const b = document.getElementById("sbStatus"); b.textContent = text.toUpperCase(); b.className = "status-badge"; }
function setVCard(id, cls) { const el = document.getElementById(id); if (el) el.className = "vcard " + cls; }
function renderStatusBar(m) {
  document.getElementById("sbId").textContent = m.mission_id.slice(0, 8);
  document.getElementById("sbType").textContent = m.mission_type;
  const b = document.getElementById("sbStatus"); b.textContent = m.status.toUpperCase(); b.className = "status-badge " + m.status;
  const sv = document.getElementById("vStatusVal"); sv.textContent = m.status.toUpperCase(); sv.className = "s-" + m.status;
  const active = ["planning", "executing", "replanning"].includes(m.status);
  setVCard("vStatus", active ? "is-nerve rise-1" : m.status === "resolved" ? "is-vital rise-1" : m.status === "failed" ? "is-alert rise-1" : "rise-1");
  updatePulse();
  updateElapsed();
}
function updateElapsed() {
  if (!currentMission) return;
  const start = parseUTC(currentMission.created_at);
  const terminal = ["resolved", "failed"].includes(currentMission.status);
  const end = terminal ? parseUTC(currentMission.updated_at) : Date.now();
  document.getElementById("vElapsed").textContent = "elapsed " + fmtElapsed(end - start);
}
function updatePulse() {
  const card = document.getElementById("ecgCard"); if (!card) return;
  const st = currentMission ? currentMission.status : "";
  const risk = currentMission ? currentMission._risk : null;
  let cls = "pulse-idle", label = "IDLE";
  if (st === "failed" || (risk != null && risk >= 0.7)) { cls = "pulse-critical"; label = "CRITICAL"; }
  else if (["planning", "executing", "replanning"].includes(st)) { cls = "pulse-active"; label = "ACTIVE"; }
  else if (st === "resolved") { cls = "pulse-ok"; label = "STABLE"; }
  card.className = "vcard ecg rise-4 " + cls;
  document.getElementById("ecgState").textContent = label;
}

/* ───────── risk instrument ───────── */
function riskColor(r) { return r >= 0.7 ? "#ff5d72" : r >= 0.4 ? "#ffb454" : "#56e39f"; }
function renderRisk(risk, breakdown) {
  const arc = document.getElementById("riskArc"), num = document.getElementById("riskNum"),
        lab = document.getElementById("riskLabel"), dot = document.getElementById("riskDot");
  if (currentMission) currentMission._risk = (risk == null ? null : risk);
  if (risk === null || risk === undefined) {
    arc.style.strokeDashoffset = RISK_C; arc.style.stroke = "rgba(120,160,200,.12)";
    dot.setAttribute("cx", 112); dot.setAttribute("cy", 60); dot.setAttribute("fill", "var(--faint)");
    num.textContent = "—"; num.style.color = "var(--mute)"; lab.textContent = "NO SIGNAL";
    document.getElementById("vRiskVal").textContent = "—"; document.getElementById("vRiskLabel").textContent = "no signal";
    setVCard("vRisk", "rise-2"); renderBreakdown(breakdown); updatePulse(); return;
  }
  const clamped = Math.max(0, Math.min(1, risk)), c = riskColor(risk);
  arc.style.strokeDashoffset = RISK_C * (1 - clamped); arc.style.stroke = c;
  const ang = (360 * clamped) * Math.PI / 180;
  dot.setAttribute("cx", (60 + 52 * Math.cos(ang)).toFixed(2));
  dot.setAttribute("cy", (60 + 52 * Math.sin(ang)).toFixed(2));
  dot.setAttribute("fill", c);
  const level = risk >= 0.7 ? "CRITICAL" : risk >= 0.4 ? "ELEVATED" : "NOMINAL";
  num.textContent = risk.toFixed(2); num.style.color = c; lab.textContent = level;
  document.getElementById("vRiskVal").textContent = risk.toFixed(2);
  document.getElementById("vRiskVal").style.color = c;
  document.getElementById("vRiskLabel").textContent = level.toLowerCase();
  setVCard("vRisk", (risk >= 0.7 ? "is-alert" : risk >= 0.4 ? "is-caution" : "is-vital") + " rise-2");
  renderBreakdown(breakdown); updatePulse();
}
function renderBreakdown(bk) {
  const el = document.getElementById("riskBreakdown");
  const entries = bk && typeof bk === "object" ? Object.entries(bk).filter(([, v]) => typeof v === "number") : [];
  if (!entries.length) { el.innerHTML = ""; return; }
  el.innerHTML = entries.slice(0, 5).map(([k, v]) => {
    const val = Math.max(0, Math.min(1, v));
    return `<div class="bk"><span class="bk-k">${escapeHtml(k)}</span>
      <span class="bk-bar"><span class="bk-fill" style="width:${Math.round(val * 100)}%;background:${riskColor(val)}"></span></span>
      <span class="bk-v">${val.toFixed(2)}</span></div>`;
  }).join("");
}

/* ───────── approvals + failures ───────── */
function actionDescription(a) {
  const p = a.payload || {};
  switch (a.action_type) {
    case "gitlab_rollback": return `Roll back deployment ${p.deployment_id ?? "?"} (${p.sha || p.ref || "unknown"}) on ${p.environment || "production"} via rollback pipeline.`;
    case "gitlab_issue": return "File a GitLab incident issue documenting the anomaly.";
    case "gitlab_mr": return "Open a merge request to revert the change.";
    case "dynatrace_query": return "Run a Dynatrace query for additional signal.";
    case "human_approval_request": return "Manual approval requested before proceeding.";
    default: return `Execute action: ${a.action_type}.`;
  }
}
function renderActions(actions) {
  document.getElementById("approvalCount").textContent = actions.length;
  document.getElementById("vApprovals").textContent = actions.length;
  document.getElementById("approvalsCard").classList.toggle("has-items", actions.length > 0);
  setVCard("vAttn", (actions.length || failures.length) ? "is-caution rise-4" : "rise-4");
  const el = document.getElementById("actions");
  if (!actions.length) { el.innerHTML = '<div class="empty">// no actions awaiting approval</div>'; return; }
  el.innerHTML = "";
  actions.forEach((a) => {
    const card = document.createElement("div"); card.className = "approval";
    card.innerHTML =
      `<div><span class="atype">${escapeHtml(a.action_type)}</span><span class="aid">${escapeHtml(a.action_id.slice(0, 8))}</span></div>
       <div class="adesc">${escapeHtml(actionDescription(a))}</div>
       <div class="btnrow"><button class="mini-btn approve">✓ APPROVE</button><button class="mini-btn reject">✕ REJECT</button></div>`;
    card.querySelector(".approve").onclick = () => approve(a.action_id);
    card.querySelector(".reject").onclick = () => reject(a.action_id);
    el.appendChild(card);
  });
}
function renderFailures(list) {
  document.getElementById("failuresCard").classList.toggle("has-items", list.length > 0);
  document.getElementById("vFailures").textContent = list.length;
  const el = document.getElementById("failures");
  if (!list.length) { el.innerHTML = '<div class="empty">// all systems nominal</div>'; return; }
  el.innerHTML = list.map((f) => {
    const sev = Math.max(0, Math.min(1, Number(f.severity) || 0));
    return `<div class="failure">
      <div class="frow"><span class="ftype">${escapeHtml(f.type)}</span><span class="ftarget">${escapeHtml(f.target)} · ${sev.toFixed(2)}</span></div>
      <div class="sev-bar"><div class="sev-fill" style="width:${Math.round(sev * 100)}%"></div></div></div>`;
  }).join("");
}

/* ───────── feed ───────── */
function feedClass(ev) {
  const t = ev.event_type || "";
  if (/FAIL/.test(t)) return "ev-alert";
  if (/RESOLVED|COMPLETED|ISSUE_CLOSED|APPROVED|EXECUTED/.test(t)) return "ev-vital";
  if (/RISK|REPLAN|FAILURE_|UNCERTAINTY/.test(t)) return "ev-caution";
  if (ev.source === "orchestrator") return "ev-nerve";
  if (ev.source === "user") return "ev-vital";
  if (ev.source === "agent") return "ev-synapse";
  if (ev.source === "failure_engine" || ev.source === "dynatrace_webhook") return "ev-caution";
  return "ev-dim";
}
function srcColor(src) {
  return { orchestrator: "var(--nerve)", agent: "var(--synapse)", user: "var(--vital)", failure_engine: "var(--alert)", mcp: "var(--mute)" }[src] || "var(--mute)";
}
function payloadPreview(ev) {
  const p = ev.payload || {};
  const keys = Object.keys(p).filter((k) => p[k] !== null && p[k] !== undefined && typeof p[k] !== "object");
  return keys.slice(0, 3).map((k) => `${k}=${String(p[k]).slice(0, 26)}`).join("  ");
}
function feedRow(ev) {
  const li = document.createElement("li"); li.className = "feed-row";
  const t = (ev.created_at || "").slice(11, 19);
  li.innerHTML =
    `<span class="ts">${escapeHtml(t)}</span>
     <span class="src"><span class="src-pill" style="color:${srcColor(ev.source)}">${escapeHtml(ev.source || "sys")}</span></span>
     <span class="evt ${feedClass(ev)}">${escapeHtml(ev.event_type || "")}</span>
     <span class="pl">${escapeHtml(payloadPreview(ev))}</span>`;
  return li;
}
function renderFeed(events) {
  const feed = document.getElementById("feed"); feed.innerHTML = "";
  events.filter((e) => !FEED_NOISE.has(e.event_type)).slice(0, 90).forEach((e) => feed.appendChild(feedRow(e)));
}
function prependFeed(ev) {
  if (FEED_NOISE.has(ev.event_type)) return;
  const feed = document.getElementById("feed");
  const row = feedRow(ev); row.classList.add("feed-new");
  feed.insertBefore(row, feed.firstChild);
  while (feed.children.length > 90) feed.removeChild(feed.lastChild);
}

/* ───────── synaptic mission map ───────── */
function fireNeuron(taskId) {
  const g = document.querySelector(`#synapse g.neuron[data-task="${cssEsc(taskId)}"]`);
  if (g) { g.classList.remove("fire"); void g.getBoundingClientRect(); g.classList.add("fire"); }
}
function cssEsc(s) { return String(s).replace(/["\\]/g, "\\$&"); }
function renderTasks(tasks) {
  lastTasks = tasks || [];
  document.getElementById("taskCount").textContent = lastTasks.length;
  const total = lastTasks.length, done = lastTasks.filter((t) => t.status === "completed").length;
  document.getElementById("vTasksVal").textContent = total ? `${done}/${total}` : "0";
  document.getElementById("vTasksBar").style.width = total ? Math.round(done / total * 100) + "%" : "0%";
  setVCard("vTasks", total ? "is-nerve rise-3" : "rise-3");
  const empty = document.getElementById("graphEmpty"), svg = document.getElementById("synapse");
  if (!total) { empty.classList.remove("hidden"); svg.innerHTML = ""; return; }
  empty.classList.add("hidden");
  drawSynapse(lastTasks);
}
function statusColor(s) {
  return { pending: "#62718a", in_progress: "#3fe0c5", completed: "#56e39f", failed: "#ff5d72", retrying: "#ffb454" }[s] || "#62718a";
}
function drawSynapse(tasks) {
  const byId = {}; tasks.forEach((t, i) => { t._n = i + 1; byId[t.task_id] = t; });
  const depthCache = {};
  const depth = (t, seen) => {
    if (depthCache[t.task_id] != null) return depthCache[t.task_id];
    seen = seen || new Set(); if (seen.has(t.task_id)) return 0; seen.add(t.task_id);
    const deps = (t.depends_on || []).map((d) => byId[d]).filter(Boolean);
    const d = deps.length ? 1 + Math.max(...deps.map((x) => depth(x, seen))) : 0;
    return depthCache[t.task_id] = d;
  };
  tasks.forEach((t) => depth(t));
  const layers = {}; let maxD = 0;
  tasks.forEach((t) => { const d = depthCache[t.task_id]; (layers[d] = layers[d] || []).push(t); maxD = Math.max(maxD, d); });
  const cols = maxD + 2;                                   // +1 core column, +1 padding
  const maxRows = Math.max(1, ...Object.values(layers).map((l) => l.length));
  const W = 1000, H = Math.max(300, maxRows * 120 + 60);
  const colX = (c) => 70 + c * ((W - 140) / Math.max(1, cols - 1));
  const rowY = (i, n) => (H / (n + 1)) * (i + 1);
  const pos = {}; // task_id -> {x,y}
  Object.keys(layers).forEach((d) => { layers[d].forEach((t, i) => { pos[t.task_id] = { x: colX(Number(d) + 1), y: rowY(i, layers[d].length) }; }); });
  const core = { x: colX(0), y: H / 2 };

  const edges = [], pulses = [], roots = [];
  tasks.forEach((t) => {
    const p = pos[t.task_id];
    const deps = (t.depends_on || []).map((d) => byId[d]).filter(Boolean);
    if (!deps.length) roots.push(t);
    deps.forEach((dep) => {
      const a = pos[dep.task_id]; if (!a) return;
      edges.push(edgePath(a, p, /(in_progress|completed)/.test(t.status)));
      if (t.status === "in_progress" || t.status === "completed") pulses.push(pulsePath(a, p, statusColor(t.status)));
    });
  });
  roots.forEach((t) => { const p = pos[t.task_id]; edges.push(edgePath(core, p, true)); pulses.push(pulsePath(core, p, "#3fe0c5")); });

  const neurons = tasks.map((t) => neuronSvg(pos[t.task_id], t)).join("");
  const coreSvg = `<g class="neuron breathe"><circle class="halo" cx="${core.x}" cy="${core.y}" r="26" fill="rgba(63,224,197,.12)"/>
    <circle cx="${core.x}" cy="${core.y}" r="15" fill="rgba(63,224,197,.18)" stroke="#3fe0c5" stroke-width="1.4"/>
    <text class="nrole" x="${core.x}" y="${core.y + 2}" style="fill:#bfffaf;font-size:7px">CORE</text></g>`;

  const svg = document.getElementById("synapse");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = `<g>${edges.join("")}</g><g>${pulses.join("")}</g>${coreSvg}${neurons}`;
  attachNeuronTips();
}
function edgePath(a, b, hot) {
  const mx = (a.x + b.x) / 2;
  return `<path class="edge${hot ? " hot" : ""}" d="M${a.x} ${a.y} C ${mx} ${a.y}, ${mx} ${b.y}, ${b.x} ${b.y}"/>`;
}
function pulsePath(a, b, color) {
  const mx = (a.x + b.x) / 2;
  return `<path class="pulse" stroke="${color}" d="M${a.x} ${a.y} C ${mx} ${a.y}, ${mx} ${b.y}, ${b.x} ${b.y}"/>`;
}
function neuronSvg(p, t) {
  const c = statusColor(t.status), active = t.status === "in_progress";
  const cls = "neuron breathe" + (active ? " active" : "");
  const role = ROLE_ABBR[t.agent_role] || (t.agent_role || "").slice(0, 4).toUpperCase();
  const tip = `${t.description || ""}|||${t.status}|||${(t.depends_on || []).length}`;
  return `<g class="${cls}" data-task="${escapeAttr(t.task_id)}" data-tip="${escapeAttr(tip)}">
    <circle class="halo" cx="${p.x}" cy="${p.y}" r="22" fill="${c}" opacity="${active ? .22 : .12}"/>
    <circle class="body" cx="${p.x}" cy="${p.y}" r="16" fill="${c}" stroke="rgba(255,255,255,.25)" stroke-width="1"/>
    <text class="nid" x="${p.x}" y="${p.y - 1}">${t._n}</text>
    <text class="nrole" x="${p.x}" y="${p.y + 9}">${escapeHtml(role)}</text></g>`;
}
function attachNeuronTips() {
  const tip = document.getElementById("graphTip"), wrap = document.getElementById("graphWrap");
  document.querySelectorAll("#synapse g.neuron[data-tip]").forEach((g) => {
    g.addEventListener("mousemove", (e) => {
      const parts = (g.getAttribute("data-tip") || "").split("|||");
      tip.innerHTML = `<div class="tt">TASK · ${parts[1] ? parts[1].toUpperCase() : ""}</div>${escapeHtml(parts[0] || "")}${Number(parts[2]) ? `<div style="color:var(--mute);margin-top:4px">${parts[2]} dependency(ies)</div>` : ""}`;
      const r = wrap.getBoundingClientRect();
      let x = e.clientX - r.left + 14, y = e.clientY - r.top + 14;
      x = Math.min(x, r.width - 290); tip.style.left = Math.max(6, x) + "px"; tip.style.top = y + "px";
      tip.classList.add("show");
    });
    g.addEventListener("mouseleave", () => tip.classList.remove("show"));
  });
}

/* ───────── ambient neural field (canvas) ───────── */
function initNeuralField() {
  const cv = document.getElementById("neuralField"); if (!cv) return;
  const ctx = cv.getContext("2d");
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let w, h, nodes = [], pulses = [], raf = null;
  function resize() {
    w = cv.width = Math.floor(innerWidth * devicePixelRatio);
    h = cv.height = Math.floor(innerHeight * devicePixelRatio);
    cv.style.width = innerWidth + "px"; cv.style.height = innerHeight + "px";
    const count = Math.min(70, Math.floor(innerWidth * innerHeight / 26000));
    nodes = Array.from({ length: count }, () => ({
      x: Math.random() * w, y: Math.random() * h,
      vx: (Math.random() - .5) * .12 * devicePixelRatio, vy: (Math.random() - .5) * .12 * devicePixelRatio
    }));
  }
  const LINK = 150 * devicePixelRatio;
  function step() {
    ctx.clearRect(0, 0, w, h);
    for (const n of nodes) {
      n.x += n.vx; n.y += n.vy;
      if (n.x < 0 || n.x > w) n.vx *= -1; if (n.y < 0 || n.y > h) n.vy *= -1;
    }
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j], dx = a.x - b.x, dy = a.y - b.y, d = Math.hypot(dx, dy);
        if (d < LINK) {
          const o = (1 - d / LINK) * .22;
          ctx.strokeStyle = `rgba(63,224,197,${o.toFixed(3)})`; ctx.lineWidth = devicePixelRatio * .6;
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        }
      }
    }
    for (const n of nodes) {
      ctx.fillStyle = "rgba(63,224,197,.5)";
      ctx.beginPath(); ctx.arc(n.x, n.y, 1.4 * devicePixelRatio, 0, 6.283); ctx.fill();
    }
    if (Math.random() < .03 && nodes.length > 1) {
      const a = nodes[(Math.random() * nodes.length) | 0], b = nodes[(Math.random() * nodes.length) | 0];
      if (a !== b && Math.hypot(a.x - b.x, a.y - b.y) < LINK * 1.6) pulses.push({ a, b, t: 0 });
    }
    for (let i = pulses.length - 1; i >= 0; i--) {
      const p = pulses[i]; p.t += .025; if (p.t >= 1) { pulses.splice(i, 1); continue; }
      const x = p.a.x + (p.b.x - p.a.x) * p.t, y = p.a.y + (p.b.y - p.a.y) * p.t;
      ctx.fillStyle = `rgba(157,139,255,${(1 - p.t) * .9})`;
      ctx.beginPath(); ctx.arc(x, y, 2.4 * devicePixelRatio, 0, 6.283); ctx.fill();
    }
    raf = requestAnimationFrame(step);
  }
  resize(); addEventListener("resize", resize);
  if (reduce) { step(); cancelAnimationFrame(raf); } else step();
}

/* ───────── helpers ───────── */
function parseUTC(ts) { if (!ts) return Date.now(); return new Date(/[Z+]/.test(ts) ? ts : ts + "Z").getTime(); }
function fmtElapsed(ms) { const s = Math.max(0, Math.floor(ms / 1000)); const m = Math.floor(s / 60); return (m ? m + "m " : "") + (s % 60) + "s"; }
function escapeHtml(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function escapeAttr(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

/* ───────── init ───────── */
initNeuralField();
renderSelect();
if (missionId) boot(missionId); else showLaunch();
