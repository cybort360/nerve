/* ============================================================
   NERVE — LIVE dashboard (SP1)
   Same component tree as the scripted showcase, but every surface
   is driven by the real backend: POST /missions · GET /demo/start ·
   GET /missions/{id} · WebSocket /ws/{id} · /actions/{id}/approve|reject ·
   /failure/inject. Memory / sparkline / blast stay empty (SP2/SP3).
   ============================================================ */
const { useState, useRef, useEffect, useCallback } = React;
const L = window.NERVE_LIVE;

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "autoChaos": false,
  "phoneDefault": false,
  "sound": false
}/*EDITMODE-END*/;

function bandColor(r) {
  if (r < 22) return '#46d39a';
  if (r < 50) return '#3fe0c5';
  if (r < 72) return '#f4c25a';
  return '#ff6a72';
}
function clock(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}
const STATE_EVENTS = new Set([
  'TASK_STARTED', 'TASK_COMPLETED', 'TASK_FAILED', 'TASK_RETRYING',
  'MISSION_STATUS_CHANGED', 'ACTION_CREATED', 'ACTION_APPROVED', 'ACTION_REJECTED',
  'ACTION_EXECUTED', 'DEMO_APPROVAL_REQUESTED', 'REPLAN_TRIGGERED',
  'RISK_SCORE_UPDATED', 'FAILURE_INJECTED', 'FAILURE_CLEARED', 'RESEARCH_SYNTHESIZED',
  'BELIEF_UPDATED', 'METRIC_SAMPLE',
]);
const POLL_MS = 4000;

/* Center-screen result modal shown when a research mission produces its hand-off. */
function ResultModal({ open, goal, recommendation, onApprove, onReject, onClose }) {
  if (!open) return null;
  return (
    <div className="result-overlay" onClick={onClose}>
      <div className="result-card" onClick={(e) => e.stopPropagation()}>
        <button className="result-close" onClick={onClose} aria-label="Close">✕</button>
        <div className="result-icon"><Icon name="check" /></div>
        <div className="result-badge">Research complete</div>
        {goal && <div className="result-goal">{goal}</div>}
        <div className="result-body" dangerouslySetInnerHTML={{ __html: L.mdLite(recommendation || '') }} />
        <div className="result-actions">
          <button className="ac-btn reject" onClick={onReject}>Hold</button>
          <button className="ac-btn approve" onClick={onApprove}>Approve</button>
        </div>
        <div className="result-hint"><CtrlIcon name="phone" />Also sent to your phone via Telegram</div>
      </div>
    </div>
  );
}

function App() {
  const [view, setView] = useState('incident');
  const preset = window.NERVE.MISSIONS[view];

  const [stage, setStage] = useState('idle');         // idle | launching | running
  const [graph, setGraph] = useState({ nodes: [], edges: [] });
  const [nodeStates, setNodeStates] = useState({});
  const [events, setEvents] = useState([]);
  const [phase, setPhase] = useState('Ready');
  const [status, setStatus] = useState('pending');
  const [targetRisk, setTargetRisk] = useState(6);
  const [dispRisk, setDispRisk] = useState(6);
  const [activeAgent, setActiveAgent] = useState(null);
  const [activeNode, setActiveNode] = useState(null);
  const [fireKey, setFireKey] = useState(0);
  const [actions, setActions] = useState(0);
  const [replans, setReplans] = useState(0);
  const [approval, setApproval] = useState(null);
  const [done, setDone] = useState(false);
  const [phoneOpen, setPhoneOpen] = useState(false);
  const [phoneStatus, setPhoneStatus] = useState('idle');
  const [connected, setConnected] = useState(false);
  const [memory, setMemory] = useState([]);
  const [metricSeries, setMetricSeries] = useState([]);
  const [metricMeta, setMetricMeta] = useState(null);
  const [fleetMissions, setFleetMissions] = useState([]);
  const [goalText, setGoalText] = useState('');  // the real goal of the running mission
  const [resultOpen, setResultOpen] = useState(false);  // research result modal
  const resultSeen = useRef(null);

  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);

  const ids = useRef({ incident: null, research: null });
  const sock = useRef(null);
  const pollRef = useRef(null);
  const reconnRef = useRef(null);
  const seen = useRef(new Set());
  const seenTypes = useRef(new Set());
  const seqRef = useRef(0);
  const startRef = useRef(0);
  const startedRef = useRef(false);
  const viewRef = useRef(view); viewRef.current = view;
  const riskRef = useRef(6);
  const counts = useRef({ actions: 0, replans: 0 });
  const fetchTimer = useRef(null);
  const autoPhone = useRef(false);

  /* ---- eased risk ---- */
  useEffect(() => {
    let raf, last;
    const loop = (ts) => {
      if (!last) last = ts;
      const dt = Math.min(0.05, (ts - last) / 1000); last = ts;
      riskRef.current += (targetRisk - riskRef.current) * Math.min(1, dt * 3.2);
      setDispRisk(p => (Math.abs(riskRef.current - p) > 0.4 ? riskRef.current : p));
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [targetRisk]);

  useEffect(() => {
    NerveAudio.enabled = !!t.sound;
    if (t.sound) { const c = NerveAudio.ensure(); if (c && c.state === 'suspended') c.resume(); }
  }, [t.sound]);

  /* ---- teardown helpers ---- */
  const closeSock = useCallback(() => {
    clearTimeout(reconnRef.current); clearInterval(pollRef.current); clearTimeout(fetchTimer.current);
    if (sock.current) { try { sock.current.onclose = null; sock.current.close(); } catch (e) {} sock.current = null; }
    setConnected(false);
  }, []);

  const resetRender = useCallback(() => {
    seen.current = new Set(); seenTypes.current = new Set(); seqRef.current = 0; counts.current = { actions: 0, replans: 0 };
    startRef.current = 0; startedRef.current = false;
    setGraph({ nodes: [], edges: [] }); setNodeStates({}); setEvents([]);
    setPhase('Ready'); setStatus('pending'); setTargetRisk(6); riskRef.current = 6; setDispRisk(6);
    setActiveAgent(null); setActiveNode(null); setFireKey(0);
    setActions(0); setReplans(0); setApproval(null); setDone(false); setPhoneStatus('idle'); autoPhone.current = false;
    setMemory([]); setMetricSeries([]); setMetricMeta(null); setGoalText('');
    setResultOpen(false); resultSeen.current = null;
  }, []);

  /* ---- apply a full backend state snapshot ---- */
  const applyState = useCallback((data) => {
    if (!data || !data.mission) return;
    /* seed feed once (also fills seenTypes) BEFORE deriving the graph */
    if (!startedRef.current && data.recent_events && data.recent_events.length) {
      [...data.recent_events].reverse().forEach(ingestRaw);
    }
    const st = data.mission.status;
    if (data.mission.goal) setGoalText(data.mission.goal);
    const resolved = L.isTerminal(st);
    const tasks = data.tasks || [];
    if (tasks.length) {
      setGraph(L.buildGraph(tasks));
      const ns = {}; tasks.forEach(tk => { ns[tk.task_id] = L.nodeStateFor(tk.status); }); setNodeStates(ns);
      const inProg = tasks.find(tk => tk.status === 'in_progress' || tk.status === 'retrying');
      setActiveNode(inProg ? inProg.task_id : null);
    } else {
      /* no task rows (e.g. the scripted incident demo) → light a milestone graph from real events */
      const mg = L.buildMilestoneGraph(seenTypes.current, resolved);
      setGraph({ nodes: mg.nodes, edges: mg.edges }); setNodeStates(mg.nodeStates);
      const act = mg.nodes.find(n => mg.nodeStates[n.id] === 'active');
      setActiveNode(act ? act.id : null);
    }
    if (typeof data.risk === 'number') setTargetRisk(Math.round(data.risk * 100));
    setMemory(L.beliefsToFacts(data.beliefs));
    const sp = L.seriesToSparkline(data.metric_series);
    setMetricSeries(sp.values); setMetricMeta(sp.meta);
    setStatus(st); setPhase(L.mapPhase(st)); setDone(resolved);
    const pa = (data.pending_actions || [])[0];
    const ap = pa ? L.actionToApproval(pa) : null;
    setApproval(ap);
    if (pa) {
      setPhoneStatus('pending');
      if (ap && ap.kind === 'result') {
        // research hand-off → pop the result modal once (don't hijack the phone)
        if (resultSeen.current !== pa.action_id) { resultSeen.current = pa.action_id; setResultOpen(true); }
      } else {
        setPhoneOpen(o => { if (!o) autoPhone.current = true; return true; });
      }
    } else {
      setPhoneStatus(s => (s === 'pending' ? 'idle' : s));
      if (autoPhone.current && !t.phoneDefault) { autoPhone.current = false; setPhoneOpen(false); }
    }
  }, [t.phoneDefault]); // ingestRaw referenced via closure (stable useCallback)

  /* ---- ingest one raw backend event ---- */
  const ingestRaw = useCallback((ev) => {
    if (!ev || !ev.event_id || seen.current.has(ev.event_id)) return;
    seen.current.add(ev.event_id);
    if (ev.event_type) seenTypes.current.add(ev.event_type);
    if (ev.event_type === 'REPLAN_TRIGGERED') { counts.current.replans += 1; setReplans(counts.current.replans); }
    if (ev.event_type === 'ACTION_EXECUTED' || ev.event_type === 'TASK_COMPLETED') { counts.current.actions += 1; setActions(counts.current.actions); }
    const item = L.translateEvent(ev);
    if (!item) return;
    if (!startRef.current) startRef.current = ev.created_at ? Date.parse(ev.created_at) : Date.now();
    seqRef.current += 1;
    const tms = ev.created_at ? Date.parse(ev.created_at) : Date.now();
    const row = { ...item, seq: seqRef.current, time: clock(tms - startRef.current) };
    setEvents(prev => [...prev, row]);
    setActiveAgent(item.agent); setFireKey(k => k + 1);
    if (item.kind === 'alert') NerveAudio.play('alert');
    if (item.kind === 'approval') NerveAudio.play('approve');
  }, []);

  const scheduleFetch = useCallback((id) => {
    clearTimeout(fetchTimer.current);
    fetchTimer.current = setTimeout(() => {
      L.Api.getState(id).then(applyState).catch(() => {});
    }, 260);
  }, [applyState]);

  /* ---- connect WS + poll for a mission id ---- */
  const connect = useCallback((id) => {
    closeSock();
    L.Api.getState(id).then(applyState).catch(() => {});
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    try { sock.current = new WebSocket(`${proto}://${location.host}/ws/${id}`); }
    catch (e) { return; }
    sock.current.onopen = () => setConnected(true);
    sock.current.onmessage = (m) => {
      let ev; try { ev = JSON.parse(m.data); } catch (e) { return; }
      ingestRaw(ev);
      if (STATE_EVENTS.has(ev.event_type)) scheduleFetch(id);
      startedRef.current = true;
    };
    sock.current.onclose = () => {
      setConnected(false);
      if (viewRef.current && ids.current[viewRef.current] === id) {
        reconnRef.current = setTimeout(() => connect(id), 1800);
      }
    };
    pollRef.current = setInterval(() => L.Api.getState(id).then(applyState).catch(() => {}), POLL_MS);
  }, [applyState, ingestRaw, scheduleFetch, closeSock]);

  /* ---- view change: show that preset's mission or the front door ---- */
  useEffect(() => {
    closeSock(); resetRender();
    const id = ids.current[view];
    if (id) { setStage('running'); startedRef.current = false; connect(id); }
    else { setStage('idle'); }
    setPhoneOpen(!!t.phoneDefault);
    return () => closeSock();
  }, [view]); // eslint-disable-line

  /* ---- launch a mission from the front door ---- */
  const launch = useCallback(async (presetId, text) => {
    const m = window.NERVE.MISSIONS[presetId];
    if (m.launch !== 'demo' && text) setGoalText(text);  // show the user's goal immediately
    setStage('launching'); setPhase('Planning'); setActiveAgent('planner');
    try {
      const res = m.launch === 'demo' ? await L.Api.startDemo() : await L.Api.createMission(text, m.mission_type);
      const id = res && res.mission_id;
      if (!id) { setStage('idle'); return; }
      ids.current[presetId] = id; L.addKnown(id, m.mission_type);
      if (viewRef.current !== presetId) { setView(presetId); return; }
      resetRender(); setStage('running'); connect(id);
    } catch (e) { setStage('idle'); }
  }, [connect, resetRender]);

  /* ---- approval handlers (real API) ---- */
  const curId = () => ids.current[viewRef.current];
  const onApprove = () => {
    const a = approval; if (!a) return;
    setApproval(null); setPhoneStatus('approved');
    L.Api.approve(a.action_id).finally(() => scheduleFetch(curId()));
  };
  const onReject = () => {
    const a = approval; if (!a) return;
    NerveAudio.play('reject');
    setApproval(null); setPhoneStatus('rejected');
    L.Api.reject(a.action_id).finally(() => scheduleFetch(curId()));
  };
  const chaos = () => {
    const id = curId(); if (!id) return;
    L.Api.injectChaos(id).finally(() => scheduleFetch(id));
  };

  const restart = () => { const id = curId(); if (id) { resetRender(); setStage('running'); startedRef.current = false; connect(id); } else resetRender(); };

  /* auto-chaos for an unattended demo */
  useEffect(() => {
    if (!t.autoChaos || stage !== 'running' || done || approval) return;
    const h = setTimeout(chaos, 8000);
    return () => clearTimeout(h);
  }, [t.autoChaos, stage, done, approval]); // eslint-disable-line

  /* real fleet roster from the backend */
  useEffect(() => {
    let stop = false;
    const tick = () => L.Api.listMissions().then(ms => { if (!stop) setFleetMissions(ms || []); });
    tick();
    const h = setInterval(tick, 6000);
    return () => { stop = true; clearInterval(h); };
  }, []);

  /* derived */
  const band = bandColor(dispRisk);
  const confidence = Math.max(2, Math.min(99, Math.round(100 - dispRisk)));
  const ticking = stage === 'running' && !done && connected;
  const coherence = status === 'replanning' ? 'reconciling' : 'coherent';
  const mission = { ...preset, nodes: graph.nodes, edges: graph.edges, chaosEdges: [], approvalNode: null };

  const statusOf = (mid) => {
    if (mid !== view) return ids.current[mid] ? { label: 'running', color: '#46d39a' } : { label: 'idle', color: '#3a4745' };
    if (approval) return { label: 'awaiting', color: '#f4c25a' };
    if (done) return phoneStatus === 'rejected' ? { label: 'held', color: '#ff6a72' } : { label: 'done', color: '#46d39a' };
    if (stage === 'idle') return { label: 'ready', color: '#5d6e6c' };
    return { label: 'running', color: '#46d39a' };
  };

  return (
    <div className="app">
      <header className="hdr">
        <div className="wordmark">
          <svg className="mark" viewBox="0 0 28 28" fill="none">
            <path d="M3 19 C3 19 7 7 11 7 C15 7 13 21 17 21 C21 21 25 9 25 9" stroke="#3fe0c5" strokeWidth="2" strokeLinecap="round" />
            <circle cx="3" cy="19" r="2.4" fill="#9d8bff" />
            <circle cx="25" cy="9" r="2.4" fill="#3fe0c5" />
            <circle cx="14" cy="14" r="1.5" fill="#dcebe8" opacity="0.6" />
          </svg>
          <span className="name">N<b>E</b>RVE</span>
          <span className="live-tag" title="Driven by the real backend">LIVE</span>
        </div>

        <div className="switcher">
          {Object.values(window.NERVE.MISSIONS).map(m => (
            <button key={m.id} data-on={view === m.id} onClick={() => setView(m.id)}>
              <span className="dot" />{m.label}
            </button>
          ))}
        </div>

        <Fleet missions={Object.values(window.NERVE.MISSIONS)} currentId={view}
          statusOf={statusOf} awaiting={!!approval} onSelect={(mid) => setView(mid)}
          recent={fleetMissions} />

        <div className="hdr-spacer" />

        <div className="hdr-status">
          <div className="statchip"><span className="k">Phase</span><span className="v">{phase}</span></div>
          <div className="statchip"><span className="k">Risk</span><span className="v tnum" style={{ color: band }}>{Math.round(dispRisk)}</span></div>
        </div>

        <div className="ctrls">
          <a className="btn demo-link" href="/showcase" target="_blank" rel="noopener"
            title="Open the scripted demo — always plays, no backend needed (great for presenting)">
            ▶ Demo
          </a>
          <button className="icon-btn" data-on={t.sound} title={t.sound ? 'Mute' : 'Sound on'} onClick={() => setTweak('sound', !t.sound)}>
            <CtrlIcon name={t.sound ? 'sound' : 'muted'} />
          </button>
          <button className="btn chaos" onClick={chaos} disabled={stage !== 'running' || done || !!approval}>
            <CtrlIcon name="chaos" />Inject chaos
          </button>
          <button className="btn" onClick={restart} disabled={stage === 'idle'}>
            <CtrlIcon name="restart" />Reconnect
          </button>
        </div>
      </header>

      <div className="body">
        <div className="col left">
          <Vitals
            risk={dispRisk} confidence={confidence} running={ticking}
            activeAgent={activeAgent} fireKey={fireKey} agents={window.NERVE.AGENTS}
            replans={replans} actions={actions}
            series={metricSeries} metric={metricMeta || preset.metric} baseline={(metricMeta || preset.metric).baseline}
            coherence={coherence}
          />
          <MemoryPanel facts={memory} />
        </div>

        <div className="col center">
          <div className="graph-head">
            <div className="goal-block">
              <div className="eyebrow">Mission · {preset.label}</div>
              {goalText
                ? <div className="goal">{goalText}</div>
                : <div className="goal" dangerouslySetInnerHTML={{ __html: preset.goal }} />}
            </div>
            <div className="phase-badge">
              <span className="pd" style={{ background: band, boxShadow: `0 0 8px ${band}` }} />{phase}
            </div>
          </div>

          <div className="graph-region">
            {stage === 'idle' && <GoalLauncher mission={preset} onLaunch={launch} />}

            {stage === 'launching' && <div className="launch-wait">Decomposing goal into a task graph…</div>}

            {done && (phoneStatus === 'rejected'
              ? <div className="done-toast" style={{ background: 'rgba(255,106,114,0.10)', borderColor: 'rgba(255,106,114,0.32)', color: '#ff6a72' }}>✕ Held — awaiting your review</div>
              : <div className="done-toast">✓ Mission {status === 'failed' ? 'ended' : 'complete'}</div>
            )}

            <MissionGraph mission={mission} nodeStates={nodeStates} fireKey={fireKey}
              activeNode={activeNode} tension={stage === 'running' && dispRisk > 58} />

            {stage !== 'idle' && (
              <div className="phone-fab">
                {phoneOpen
                  ? <TelegramPhone open={phoneOpen} approval={approval} status={phoneStatus}
                      onApprove={onApprove} onReject={onReject} onClose={() => setPhoneOpen(false)} />
                  : <button className="phone-pill" data-alert={phoneStatus === 'pending'} onClick={() => setPhoneOpen(true)}>
                      <span className="pp-dot" />Telegram{phoneStatus === 'pending' ? ' · 1 to approve' : ''}
                    </button>}
              </div>
            )}
          </div>
        </div>

        <div className="col right">
          <EventFeed events={events} />
          {approval && <ApprovalDock approval={approval} onApprove={onApprove} onReject={onReject} />}
        </div>
      </div>

      <TweaksPanel>
        <TweakSection label="Chaos engine" />
        <TweakToggle label="Auto-inject chaos" value={t.autoChaos} onChange={v => setTweak('autoChaos', v)} />
        <TweakSection label="Signals" />
        <TweakToggle label="Ambient sound" value={t.sound} onChange={v => setTweak('sound', v)} />
        <TweakToggle label="Show Telegram by default" value={t.phoneDefault} onChange={v => setTweak('phoneDefault', v)} />
      </TweaksPanel>

      <ResultModal
        open={resultOpen && !!approval && approval.kind === 'result'}
        goal={goalText}
        recommendation={approval && approval.recommendation}
        onApprove={() => { setResultOpen(false); onApprove(); }}
        onReject={() => { setResultOpen(false); onReject(); }}
        onClose={() => setResultOpen(false)}
      />
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
