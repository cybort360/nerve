/* ============================================================
   NERVE — LIVE data adapter (SP1)
   Replaces the scripted data.jsx. Provides the static constants
   the components expect (AGENTS, AGENT_COLOR, MISSIONS presets,
   FLEET_GHOSTS) plus pure mapping helpers that turn the real
   backend (GET /missions/{id} + /ws/{id} events) into the exact
   shapes the presentational components consume.
   No backend changes — surfaces with no real data (memory,
   sparkline, blast radius) stay empty until SP2/SP3.
   ============================================================ */

const AGENTS = [
  { id: 'planner',   name: 'Planner',   role: 'plans',  color: '#9d8bff' },
  { id: 'execution', name: 'Execution', role: 'acts',   color: '#3fe0c5' },
  { id: 'risk',      name: 'Risk',      role: 'scores', color: '#f4c25a' },
  { id: 'auditor',   name: 'Auditor',   role: 'checks', color: '#46d39a' },
];
const ORCH = { id: 'orchestrator', name: 'Orchestrator', color: '#dcebe8' };
const AGENT_COLOR = {
  planner: '#9d8bff', execution: '#3fe0c5', risk: '#f4c25a',
  auditor: '#46d39a', orchestrator: '#dcebe8', system: '#5d6e6c',
};

/* Launch presets — the two mission types you can start. Each spawns a REAL
   backend mission; the dashboard then renders that mission's live state under
   the preset's identity (label + metric). nodes/edges are filled at runtime. */
const MISSIONS = {
  incident: {
    id: 'incident', label: 'Incident Autopilot', mission_type: 'INCIDENT_RESPONSE',
    launch: 'demo', // GET /demo/start
    goal: 'Keep <span class="hl">checkout</span> healthy — detect, diagnose & resolve production anomalies.',
    goalText: 'Keep checkout healthy — detect, diagnose and resolve production incidents automatically.',
    metric: { label: 'checkout · p99', unit: 'ms', baseline: 410, lowerBetter: true },
    nodes: [], edges: [], chaosEdges: [], approvalNode: null,
  },
  research: {
    id: 'research', label: 'Research Agent', mission_type: 'GENERAL',
    launch: 'create', // POST /missions {goal, mission_type:'GENERAL'}
    goal: 'Find the cheapest <span class="hl">2026 World Cup final</span> ticket + a cheap, well-reviewed hotel nearby.',
    goalText: 'Find the cheapest 2026 World Cup final ticket and a cheap, well-reviewed hotel nearby.',
    metric: { label: 'cheapest ticket', unit: '$', baseline: null, lowerBetter: true, prefix: '$' },
    nodes: [], edges: [], chaosEdges: [], approvalNode: null,
  },
};

const FLEET_GHOSTS = [
  { id: 'cost-audit', label: 'Nightly cost audit', status: 'queued', sub: 'starts 02:00' },
  { id: 'sec-sweep',  label: 'Dependency CVE sweep', status: 'running', sub: 'no action needed' },
];

/* ---------------- status / phase mapping ---------------- */
const PHASE_LABEL = {
  pending: 'Ready', planning: 'Planning', executing: 'Executing',
  replanning: 'Re-planning', resolved: 'Resolved', failed: 'Failed',
};
function mapPhase(status) { return PHASE_LABEL[status] || (status ? status[0].toUpperCase() + status.slice(1) : 'Ready'); }
function isTerminal(status) { return status === 'resolved' || status === 'failed'; }

function nodeStateFor(taskStatus) {
  if (taskStatus === 'completed') return 'done';
  if (taskStatus === 'in_progress' || taskStatus === 'retrying') return 'active';
  if (taskStatus === 'failed') return 'alert';
  return 'pending';
}

/* ---------------- graph auto-layout from tasks ---------------- */
const ICON_BY_AGENT = { planner: 'route', execution: 'bolt', risk: 'radius', auditor: 'check' };
function iconForTask(task) {
  const d = (task.description || '').toLowerCase();
  if (d.includes('search') || d.includes('find')) return 'target';
  if (d.includes('rollback') || d.includes('revert') || d.includes('deploy')) return 'git';
  if (d.includes('verify') || d.includes('check')) return 'check';
  if (d.includes('approv')) return 'shield';
  return ICON_BY_AGENT[task.agent_role] || 'target';
}
function shortLabel(desc) {
  if (!desc) return 'task';
  const s = desc.trim();
  return s.length > 34 ? s.slice(0, 32).trimEnd() + '…' : s;
}

/* Longest-path layering → left-to-right DAG, evenly spread vertically. */
function buildGraph(tasks) {
  if (!tasks || !tasks.length) return { nodes: [], edges: [] };
  const byId = {};
  tasks.forEach(t => { byId[t.task_id] = t; });
  const depth = {};
  const visiting = {};
  function depthOf(id) {
    if (depth[id] != null) return depth[id];
    if (visiting[id]) return 0; // guard against cycles
    visiting[id] = true;
    const deps = (byId[id] && byId[id].depends_on) || [];
    let d = 0;
    deps.forEach(dep => { if (byId[dep]) d = Math.max(d, depthOf(dep) + 1); });
    visiting[id] = false;
    return (depth[id] = d);
  }
  tasks.forEach(t => depthOf(t.task_id));
  const maxLayer = Math.max(1, ...Object.values(depth));
  const layers = {};
  tasks.forEach(t => { const L = depth[t.task_id]; (layers[L] = layers[L] || []).push(t); });

  const nodes = [];
  Object.keys(layers).forEach(Lk => {
    const L = +Lk;
    const col = layers[L];
    const x = maxLayer === 0 ? 50 : 9 + (L / maxLayer) * 82;
    col.forEach((t, i) => {
      const span = col.length;
      const y = span === 1 ? 50 : 18 + (i / (span - 1)) * 64;
      nodes.push({
        id: t.task_id, x, y, agent: t.agent_role || 'execution',
        icon: iconForTask(t), label: shortLabel(t.description), _status: t.status,
      });
    });
  });
  const ids = new Set(nodes.map(n => n.id));
  const edges = [];
  tasks.forEach(t => (t.depends_on || []).forEach(dep => { if (ids.has(dep)) edges.push([dep, t.task_id]); }));
  return { nodes, edges };
}

/* ---------------- event → feed item ---------------- */
const FEED_NOISE = new Set([
  'SNAPSHOT_TAKEN', 'MCP_TOOL_RESULT', 'MCP_TOOL_CALLED', 'AGENT_OBSERVATION',
  'RESOLUTION_CHECK', 'RISK_SCORE_UPDATED', 'METRIC_SAMPLE', 'BELIEF_UPDATED',
]);

/* beliefs (backend working memory) → MemoryPanel facts */
function beliefsToFacts(beliefs) {
  return (beliefs || []).map(b => ({
    k: b.key, label: b.label, v: b.value, conf: b.confidence, op: b.op, ver: b.version,
  }));
}

/* metric samples → sparkline {values, meta}; meta drives label/unit/baseline */
function seriesToSparkline(samples) {
  const s = samples || [];
  if (!s.length) return { values: [], meta: null };
  const latest = s[s.length - 1];
  const unit = latest.unit || '';
  return {
    values: s.map(x => x.value),
    meta: {
      label: latest.label || 'metric',
      unit,
      baseline: latest.baseline != null ? latest.baseline : null,
      lowerBetter: unit === '%' || unit === 'ms' || unit === '$',
      prefix: unit === '$' ? '$' : undefined,
    },
  };
}

/* Incident milestone graph — the demo emits milestone events but no task rows,
   so we light a fixed incident pipeline from the REAL events that arrive. */
const INCIDENT_MILESTONES = [
  { id: 'detect',    x: 9,  y: 50, agent: 'execution',    icon: 'pulse',  label: 'Detect anomaly',   on: ['INCIDENT_DETECTED'] },
  { id: 'correlate', x: 30, y: 32, agent: 'execution',    icon: 'git',    label: 'Assemble context', on: ['CONTEXT_ASSEMBLED'] },
  { id: 'diagnose',  x: 50, y: 50, agent: 'planner',      icon: 'target', label: 'Root-cause',       on: ['REASONING_COMPLETE'] },
  { id: 'approve',   x: 70, y: 32, agent: 'orchestrator', icon: 'shield', label: 'Human approval',   on: ['ACTION_CREATED', 'DEMO_APPROVAL_REQUESTED'] },
  { id: 'execute',   x: 86, y: 50, agent: 'execution',    icon: 'bolt',   label: 'Execute',          on: ['ACTION_EXECUTED', 'ACTION_APPROVED'] },
  { id: 'verify',    x: 96, y: 74, agent: 'auditor',      icon: 'check',  label: 'Verify recovery',  on: ['RESOLUTION_MONITORING_STARTED'] },
];
const INCIDENT_MS_EDGES = [
  ['detect', 'correlate'], ['correlate', 'diagnose'], ['diagnose', 'approve'],
  ['approve', 'execute'], ['execute', 'verify'],
];
function buildMilestoneGraph(seenTypes, resolved) {
  const nodes = INCIDENT_MILESTONES.map(m => ({ id: m.id, x: m.x, y: m.y, agent: m.agent, icon: m.icon, label: m.label }));
  const states = {};
  let lastDone = -1;
  INCIDENT_MILESTONES.forEach((m, i) => { if (m.on.some(t => seenTypes.has(t))) { states[m.id] = 'done'; lastDone = i; } });
  // the milestone just after the last completed one is "active"
  const nextIdx = lastDone + 1;
  INCIDENT_MILESTONES.forEach((m, i) => {
    if (states[m.id] === 'done') { if (i === lastDone && !resolved) states[m.id] = 'active'; }
    else states[m.id] = i === nextIdx ? 'active' : 'pending';
  });
  if (resolved) INCIDENT_MILESTONES.forEach(m => { if (m.on.some(t => seenTypes.has(t))) states[m.id] = 'done'; });
  return { nodes, edges: INCIDENT_MS_EDGES, nodeStates: states };
}

const KIND_BY_EVENT = {
  TASK_FAILED: 'alert', FAILURE_INJECTED: 'alert',
  RISK_SCORE_UPDATED: 'risk', REPLAN_TRIGGERED: 'risk', FAILURE_CLEARED: 'risk',
  ACTION_CREATED: 'approval', DEMO_APPROVAL_REQUESTED: 'approval',
  ACTION_APPROVED: 'approval', ACTION_REJECTED: 'approval',
  TASK_COMPLETED: 'success', ACTION_EXECUTED: 'success', RESEARCH_SYNTHESIZED: 'success',
  RESOLUTION_MONITORING_STARTED: 'success',
  CONTEXT_ASSEMBLED: 'memory', REASONING_COMPLETE: 'memory', MISSION_CREATED: 'memory',
};
const AGENT_BY_EVENT = {
  RISK_SCORE_UPDATED: 'risk', REPLAN_TRIGGERED: 'risk', FAILURE_INJECTED: 'risk', FAILURE_CLEARED: 'risk',
  REASONING_COMPLETE: 'planner', RESEARCH_SYNTHESIZED: 'planner',
  ACTION_CREATED: 'orchestrator', ACTION_APPROVED: 'orchestrator', ACTION_REJECTED: 'orchestrator',
  DEMO_APPROVAL_REQUESTED: 'orchestrator', MISSION_STATUS_CHANGED: 'orchestrator', MISSION_CREATED: 'orchestrator',
  RESOLUTION_MONITORING_STARTED: 'auditor', TASK_FAILED: 'auditor',
};
function agentForEvent(ev) {
  if (AGENT_BY_EVENT[ev.event_type]) return AGENT_BY_EVENT[ev.event_type];
  const p = ev.payload || {};
  if (p.agent_role && AGENT_COLOR[p.agent_role]) return p.agent_role;
  const src = ev.source;
  if (src === 'mcp') return 'execution';
  if (src === 'failure_engine') return 'risk';
  if (src === 'user') return 'orchestrator';
  if (src === 'orchestrator') return 'orchestrator';
  return 'execution';
}
function esc(s) { return String(s == null ? '' : s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])); }
/* light, XSS-safe markdown: escape first, then add only our own bold/link/break tags */
function mdLite(s) {
  let h = esc(s);
  // URL char classes exclude " ' < > so a captured URL can't break out of the
  // href="" attribute context (the recommendation is model/web-derived, untrusted).
  h = h.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s"'<>]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  h = h.replace(/(^|[\s(])(https?:\/\/[^\s)"'<>]+)/g, (m, pre, url) => `${pre}<a href="${url}" target="_blank" rel="noopener">${url}</a>`);
  h = h.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  h = h.replace(/(^|\n)[ \t]*[*\-•][ \t]+/g, '$1• ');  // list bullets → clean •
  h = h.replace(/\n/g, '<br>');
  return h;
}
function num(v) { return `<span class="num">${esc(v)}</span>`; }
function em(v) { return `<span class="em">${esc(v)}</span>`; }

/* Humanize an event into a readable feed line (light emphasis only). */
function eventText(ev) {
  const p = ev.payload || {};
  switch (ev.event_type) {
    case 'MISSION_CREATED': return `Mission opened — ${em(p.goal || 'new goal')}.`;
    case 'MISSION_STATUS_CHANGED': return `Status ${em(p.from)} → ${em(p.to)}.`;
    case 'INCIDENT_DETECTED': return `Anomaly detected — ${em(p.title || p.problem_id || 'incident')}.`;
    case 'CONTEXT_ASSEMBLED': return `Context assembled · ${num(p.deployments != null ? p.deployments : 0)} deploys for ${em(p.service_id || 'service')}.`;
    case 'REASONING_COMPLETE': return `Reasoning complete — recommendation ${em(p.recommendation || 'ready')}${p.confidence != null ? ` · confidence ${num(p.confidence)}` : ''}.`;
    case 'TASK_STARTED': return `Task started.`;
    case 'TASK_COMPLETED': return `Task complete${p.tool ? ` via ${em(p.tool)}` : ''}.`;
    case 'TASK_FAILED': return `Task failed${p.reason ? ` — ${esc(p.reason)}` : ''}.`;
    case 'TASK_RETRYING': return `Retrying task · attempt ${num(p.retry_count || 1)}.`;
    case 'RISK_SCORE_UPDATED': return `Risk index ${num(Math.round((p.overall != null ? p.overall : 0) * 100))}.`;
    case 'REPLAN_TRIGGERED': return `${em('Tripping re-plan')} — ${esc(p.reason || 'risk too high')} (risk ${num(p.risk != null ? p.risk : '—')}).`;
    case 'FAILURE_INJECTED': return `${em('Chaos injected')} — ${esc(p.type || 'failure')} on ${em(p.target || 'tool')} (severity ${num(p.severity != null ? p.severity : '—')}).`;
    case 'FAILURE_CLEARED': return `Failure cleared — ${esc(p.type || 'failure')} resolved.`;
    case 'ACTION_CREATED': return `${em('Approval required')} — ${esc(p.type || p.action_type || 'action')}. Mirrored to Telegram.`;
    case 'DEMO_APPROVAL_REQUESTED': return `${em('Approval required')} — ${esc(p.action || 'action')}. Mirrored to Telegram.`;
    case 'ACTION_APPROVED': return `Approved${p.approved_by ? ` by ${em(p.approved_by)}` : ''}. Executing.`;
    case 'ACTION_REJECTED': return `Held by human — standing down.`;
    case 'ACTION_EXECUTED': return `Action executed.`;
    case 'RESOLUTION_MONITORING_STARTED': return `Watching metrics for recovery on ${em(p.service_id || 'service')}…`;
    case 'RESEARCH_SYNTHESIZED': return `${em('Recommendation ready')} — handed off for approval.`;
    default: {
      const keys = Object.keys(p).slice(0, 3).map(k => `${k}=${esc(typeof p[k] === 'object' ? JSON.stringify(p[k]).slice(0, 24) : p[k])}`).join(' · ');
      return `${esc(ev.event_type)}${keys ? ' · ' + keys : ''}`;
    }
  }
}

function translateEvent(ev) {
  if (!ev || !ev.event_type || FEED_NOISE.has(ev.event_type)) return null;
  return {
    id: ev.event_id, ts: ev.created_at,
    kind: KIND_BY_EVENT[ev.event_type] || 'signal',
    agent: agentForEvent(ev),
    text: eventText(ev),
    tool: (ev.payload && ev.payload.tool) || null,
    event_type: ev.event_type,
  };
}

/* ---------------- pending action → approval card ---------------- */
function actionToApproval(action) {
  if (!action) return null;
  const p = action.payload || {};
  const type = action.action_type;
  if (type === 'gitlab_rollback') {
    return {
      action_id: action.action_id, title: 'Roll back deployment',
      detail: `NERVE wants to <span class="em">revert ${esc(p.ref || 'the latest deploy')}</span> on <span class="em">${esc(p.environment || 'production')}</span> to stop the regression. Runs a GitLab rollback pipeline.`,
      rows: [
        ['Target', esc((p.environment || 'prod'))],
        ['Action', `Rollback ${esc(p.ref || '')}`.trim()],
        p.sha ? ['Commit', esc(String(p.sha).slice(0, 8))] : null,
        p.confidence != null ? ['Confidence', `${Math.round(p.confidence * 100)}%`] : null,
      ].filter(Boolean),
      approve: 'Approve rollback', reject: 'Hold', integration: 'GitLab pipeline',
      impact: p.impact || null,
    };
  }
  if (type === 'human_approval_request') {
    const rec = p.recommendation || '';
    const firstLine = rec.split('\n').filter(Boolean)[0] || 'Review the recommendation NERVE prepared.';
    return {
      action_id: action.action_id, title: 'Review recommendation',
      detail: esc(firstLine),
      rows: rec.split('\n').filter(Boolean).slice(0, 4).map(line => ['', line.replace(/^[-•]\s*/, '')]),
      approve: 'Approve', reject: 'Hold', integration: 'Hand-off',
      impact: p.impact || null,
      kind: 'result',          // research hand-off → show as a result modal
      recommendation: rec,     // full text for the modal
    };
  }
  return {
    action_id: action.action_id, title: `Approve ${esc(type)}`,
    detail: esc(JSON.stringify(p).slice(0, 160)),
    rows: Object.entries(p).filter(([k]) => k !== 'impact').slice(0, 4).map(([k, v]) => [k, esc(typeof v === 'object' ? JSON.stringify(v).slice(0, 28) : v)]),
    approve: 'Approve', reject: 'Hold', integration: 'Action', impact: p.impact || null,
  };
}

/* ---------------- backend API client ---------------- */
const Api = {
  async startDemo() { const r = await fetch('/demo/start'); return r.json(); },
  async createMission(goal, mission_type) {
    const r = await fetch('/missions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ goal, mission_type }),
    });
    return r.json();
  },
  async getState(id) { const r = await fetch(`/missions/${id}`); if (!r.ok) throw new Error('state ' + r.status); return r.json(); },
  async listMissions() { try { const r = await fetch('/missions'); if (!r.ok) return []; const d = await r.json(); return (d && d.missions) || []; } catch (e) { return []; } },
  async approve(id) {
    return fetch(`/actions/${id}/approve`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approved_by: 'dashboard' }),
    });
  },
  async reject(id) {
    return fetch(`/actions/${id}/reject`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approved_by: 'dashboard', reason: 'held from dashboard' }),
    });
  },
  async injectChaos(missionId) {
    const body = { failure_type: 'CONTRADICTORY_METRICS', target: 'get_metrics', severity: 0.85, duration_seconds: 25 };
    return fetch(`/failure/inject?mission_id=${encodeURIComponent(missionId)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
  },
};

/* known-missions roster (shared with the classic dashboard) */
function getKnown() { try { return JSON.parse(localStorage.getItem('nerve_missions') || '[]'); } catch { return []; } }
function addKnown(id, type) {
  const list = getKnown().filter(m => m.id !== id);
  list.unshift({ id, type, at: Date.now() });
  try { localStorage.setItem('nerve_missions', JSON.stringify(list.slice(0, 12))); } catch {}
}

window.NERVE = { AGENTS, ORCH, AGENT_COLOR, MISSIONS, FLEET_GHOSTS };
window.NERVE_LIVE = {
  mapPhase, isTerminal, nodeStateFor, buildGraph, buildMilestoneGraph,
  translateEvent, actionToApproval, beliefsToFacts, seriesToSparkline,
  Api, getKnown, addKnown, FEED_NOISE, mdLite,
};
