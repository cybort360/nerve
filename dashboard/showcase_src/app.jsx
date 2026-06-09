/* ============================================================
   NERVE — orchestrator engine + dashboard layout (v2)
   ============================================================ */
const { useState, useRef, useEffect, useCallback } = React;

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "speed": 1,
  "autoChaos": false,
  "loop": false,
  "phoneDefault": false,
  "sound": false
}/*EDITMODE-END*/;

function fmtClock(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}
function bandColor(r) {
  if (r < 22) return '#46d39a';
  if (r < 50) return '#3fe0c5';
  if (r < 72) return '#f4c25a';
  return '#ff6a72';
}
function cloneBeats(arr) { return arr.map(b => ({ ...b, node: b.node ? [...b.node] : null })); }

function App() {
  const [missionId, setMissionId] = useState('incident');
  const mission = window.NERVE.MISSIONS[missionId];

  const [stage, setStage] = useState('idle');        // idle | launching | running
  const [running, setRunning] = useState(true);       // play/pause
  const [nodeStates, setNodeStates] = useState({});
  const [events, setEvents] = useState([]);
  const [memory, setMemory] = useState([]);
  const [metricSeries, setMetricSeries] = useState([]);
  const [phase, setPhase] = useState('Ready');
  const [activeAgent, setActiveAgent] = useState(null);
  const [activeNode, setActiveNode] = useState(null);
  const [fireKey, setFireKey] = useState(0);
  const [targetRisk, setTargetRisk] = useState(6);
  const [dispRisk, setDispRisk] = useState(6);
  const [actions, setActions] = useState(0);
  const [replans, setReplans] = useState(0);
  const [done, setDone] = useState(false);
  const [approval, setApproval] = useState(null);
  const [phoneOpen, setPhoneOpen] = useState(false);
  const [phoneStatus, setPhoneStatus] = useState('idle');
  const [chaosUsed, setChaosUsed] = useState(false);
  const [rejectedOnce, setRejectedOnce] = useState(false);

  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);

  const eng = useRef({ beats: [], i: 0, timer: null, start: 0 });
  const seqRef = useRef(0);
  const memVerRef = useRef(0);
  const runningRef = useRef(running);
  const speedRef = useRef(t.speed);
  const missionIdRef = useRef(missionId);
  const launchAfterSetupRef = useRef(false);
  const rejectedRef = useRef(false);
  runningRef.current = running;
  speedRef.current = t.speed || 1;
  missionIdRef.current = missionId;
  rejectedRef.current = rejectedOnce;

  /* ---- risk easing (bounded re-renders) ---- */
  const riskRef = useRef(6);
  useEffect(() => {
    let raf, last;
    const loop = (ts) => {
      if (!last) last = ts;
      const dt = Math.min(0.05, (ts - last) / 1000); last = ts;
      riskRef.current += (targetRisk - riskRef.current) * Math.min(1, dt * 3.2);
      setDispRisk(prev => (Math.abs(riskRef.current - prev) > 0.4 ? riskRef.current : prev));
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [targetRisk]);

  /* ---- audio enable sync ---- */
  useEffect(() => {
    NerveAudio.enabled = !!t.sound;
    if (t.sound) { const c = NerveAudio.ensure(); if (c && c.state === 'suspended') c.resume(); }
  }, [t.sound]);

  /* ---- event helper ---- */
  const appendEvent = useCallback((e) => {
    seqRef.current += 1;
    setEvents(prev => [...prev, { ...e, seq: seqRef.current, time: fmtClock(Date.now() - eng.current.start) }]);
  }, []);

  /* ---- reset to front door (idle) ---- */
  const setup = useCallback((mid) => {
    clearTimeout(eng.current.timer);
    const m = window.NERVE.MISSIONS[mid];
    eng.current.beats = cloneBeats(m.beats);
    eng.current.i = 0;
    eng.current.start = Date.now();
    seqRef.current = 0;
    const ns = {}; m.nodes.forEach(n => ns[n.id] = 'hidden'); setNodeStates(ns);
    setEvents([]); setMemory([]); setMetricSeries([]);
    setStage('idle'); setPhase('Ready'); setActiveAgent(null); setActiveNode(null); setFireKey(0);
    setTargetRisk(6); riskRef.current = 6; setDispRisk(6);
    setActions(0); setReplans(0); setDone(false); setApproval(null); setPhoneStatus('idle');
    setChaosUsed(false); setRejectedOnce(false);
  }, []);

  /* ---- launch: self-build the graph, then run ---- */
  const doLaunch = useCallback(() => {
    const m = window.NERVE.MISSIONS[missionIdRef.current];
    eng.current.beats = cloneBeats(m.beats);
    eng.current.i = 0;
    eng.current.start = Date.now();
    seqRef.current = 0; memVerRef.current = 0;
    setEvents([]); setMemory([]); setMetricSeries([]);
    setDone(false); setApproval(null); setPhoneStatus('idle');
    setChaosUsed(false); setRejectedOnce(false);
    setStage('launching'); setPhase('Planning'); setActiveAgent('planner');
    setTargetRisk(m.beats[0] ? m.beats[0].risk : 14);
    const ns = {}; m.nodes.forEach(n => ns[n.id] = 'hidden'); setNodeStates(ns);
    appendEvent({ kind: 'signal', agent: 'planner', text: 'Goal accepted. Decomposing into a task graph…' });
    const ids = m.nodes.filter(n => !n.chaos).map(n => n.id);
    ids.forEach((id, k) => setTimeout(() => setNodeStates(prev => ({ ...prev, [id]: 'pending' })), 140 * (k + 1)));
    const total = 140 * (ids.length + 1) + 240;
    setTimeout(() => {
      appendEvent({ kind: 'memory', agent: 'planner', text: `Plan ready — <span class="num">${ids.length}</span> tasks, <span class="num">${m.edges.length}</span> dependencies. Executing.` });
      setStage('running');
    }, total);
  }, [appendEvent]);

  const runMission = useCallback((mid) => {
    if (mid === missionIdRef.current) { setup(mid); setTimeout(doLaunch, 90); }
    else { launchAfterSetupRef.current = true; setMissionId(mid); }
  }, [setup, doLaunch]);

  /* mission change → reset to front door (and auto-launch if requested) */
  useEffect(() => {
    setup(missionId);
    if (launchAfterSetupRef.current) {
      launchAfterSetupRef.current = false;
      const id = setTimeout(doLaunch, 110);
      return () => clearTimeout(id);
    }
  }, [missionId, setup, doLaunch]);

  /* ---- apply one beat ---- */
  const applyBeat = useCallback((beat) => {
    if (beat.node) {
      const [id, state] = beat.node;
      setNodeStates(prev => ({ ...prev, [id]: state }));
      if (state === 'active' || state === 'approval' || state === 'alert') setActiveNode(id);
    }
    setActiveAgent(beat.agent);
    setFireKey(k => k + 1);
    if (typeof beat.risk === 'number') setTargetRisk(beat.risk);
    if (beat.phase) setPhase(beat.phase);
    if (beat.mv != null) setMetricSeries(prev => [...prev, beat.mv].slice(-24));
    if (beat.mem) setMemory(prev => {
      const next = [...prev];
      beat.mem.forEach(op => {
        memVerRef.current += 1;
        const fact = { ...op, ver: memVerRef.current };
        const idx = next.findIndex(f => f.k === op.k);
        if (idx >= 0) next[idx] = fact; else next.push(fact);
      });
      return next;
    });
    if (beat.event) {
      appendEvent(beat.event);
      if (beat.event.tool) setActions(a => a + 1);
      if (beat.event.kind === 'alert') NerveAudio.play('alert');
    }
  }, [appendEvent]);

  /* ---- scheduler ---- */
  const tick = useCallback(() => {
    clearTimeout(eng.current.timer);
    if (!runningRef.current) return;
    const { beats, i } = eng.current;
    const beat = beats[i];
    if (!beat) return;
    applyBeat(beat);
    if (beat.approval) { setApproval(beat.approval); setPhoneStatus('pending'); setPhoneOpen(true); NerveAudio.play('approve'); return; }
    if (beat.done) { setDone(true); setActiveAgent(null); NerveAudio.play('resolve'); return; }
    eng.current.i = i + 1;
    eng.current.timer = setTimeout(tick, (beat.dur || 1500) / speedRef.current);
  }, [applyBeat]);

  /* kick / clear the scheduler on state changes */
  useEffect(() => {
    clearTimeout(eng.current.timer);
    if (stage === 'running' && running && !approval && !done) {
      eng.current.timer = setTimeout(tick, 320 / speedRef.current);
    }
    return () => clearTimeout(eng.current.timer);
  }, [stage, running, approval, done, missionId, tick]);

  /* ---- approval handlers ---- */
  const onApprove = () => {
    setApproval(null);
    setPhoneStatus('approved');
    setNodeStates(prev => ({ ...prev, [mission.approvalNode]: 'done' }));
    eng.current.i += 1; // step past the approval beat
  };

  const onReject = () => {
    NerveAudio.play('reject');
    if (rejectedRef.current || !mission.rejectFollow) {
      // already offered an alternative once → hard hold
      setApproval(null); setPhoneStatus('rejected'); setPhase('Held by human'); setActiveAgent(null);
      setTargetRisk(14);
      appendEvent({ kind: 'memory', agent: 'orchestrator', text: 'Held again. <span class="em">Standing down</span> — no action taken. Mission paused for your review.' });
      setNodeStates(prev => ({ ...prev, [mission.approvalNode]: 'pending' }));
      setDone(true);
      return;
    }
    setRejectedOnce(true);
    setPhoneStatus('rejected');
    // replace the remaining timeline with the "alternative" branch
    eng.current.beats.splice(eng.current.i);
    eng.current.beats.push(...cloneBeats(mission.rejectFollow));
    setApproval(null); // scheduler effect resumes from the new branch
  };

  const injectChaos = () => {
    if (chaosUsed || approval || done || stage !== 'running') return;
    setChaosUsed(true);
    setReplans(r => r + 1);
    eng.current.beats.splice(eng.current.i, 0, ...cloneBeats(mission.chaos));
    clearTimeout(eng.current.timer);
    if (runningRef.current) eng.current.timer = setTimeout(tick, 180 / speedRef.current);
  };

  const restart = useCallback(() => runMission(missionIdRef.current), [runMission]);
  const injectRef = useRef(injectChaos); injectRef.current = injectChaos;
  const restartRef = useRef(restart); restartRef.current = restart;

  /* phone default visibility */
  useEffect(() => { setPhoneOpen(!!t.phoneDefault); }, [missionId, t.phoneDefault]);

  /* auto chaos for unattended demo */
  useEffect(() => {
    if (!t.autoChaos || stage !== 'running') return;
    const id = setTimeout(() => injectRef.current && injectRef.current(), 7000 / (t.speed || 1));
    return () => clearTimeout(id);
  }, [stage, t.autoChaos, t.speed]);

  /* loop the demo */
  useEffect(() => {
    if (done && t.loop) {
      const id = setTimeout(() => restartRef.current && restartRef.current(), 4200);
      return () => clearTimeout(id);
    }
  }, [done, t.loop]);

  const dispBand = bandColor(dispRisk);
  const confidence = Math.max(2, Math.min(99, Math.round(100 - dispRisk)));
  const ticking = stage === 'running' && !approval && !done;
  const coherence = phase.indexOf('Re-planning') >= 0 ? 'reconciling' : 'coherent';

  const statusOf = (mid) => {
    if (mid !== missionId) return { label: 'idle', color: '#3a4745' };
    if (approval) return { label: 'awaiting', color: '#f4c25a' };
    if (done) return phoneStatus === 'rejected' ? { label: 'held', color: '#ff6a72' } : { label: 'done', color: '#46d39a' };
    if (stage === 'idle') return { label: 'ready', color: '#5d6e6c' };
    return { label: 'running', color: '#46d39a' };
  };

  return (
    <div className="app">
      {/* ===== header ===== */}
      <header className="hdr">
        <div className="wordmark">
          <svg className="mark" viewBox="0 0 28 28" fill="none">
            <path d="M3 19 C3 19 7 7 11 7 C15 7 13 21 17 21 C21 21 25 9 25 9" stroke="#3fe0c5" strokeWidth="2" strokeLinecap="round" />
            <circle cx="3" cy="19" r="2.4" fill="#9d8bff" />
            <circle cx="25" cy="9" r="2.4" fill="#3fe0c5" />
            <circle cx="14" cy="14" r="1.5" fill="#dcebe8" opacity="0.6" />
          </svg>
          <span className="name">N<b>E</b>RVE</span>
        </div>

        <div className="switcher">
          {Object.values(window.NERVE.MISSIONS).map(m => (
            <button key={m.id} data-on={missionId === m.id} onClick={() => setMissionId(m.id)}>
              <span className="dot" />{m.label}
            </button>
          ))}
        </div>

        <Fleet missions={Object.values(window.NERVE.MISSIONS)} currentId={missionId}
          statusOf={statusOf} awaiting={!!approval} onSelect={(mid) => setMissionId(mid)} />

        <div className="hdr-spacer" />

        <div className="hdr-status">
          <div className="statchip"><span className="k">Phase</span><span className="v">{phase}</span></div>
          <div className="statchip"><span className="k">Risk</span><span className="v tnum" style={{ color: dispBand }}>{Math.round(dispRisk)}</span></div>
        </div>

        <div className="ctrls">
          <button className="icon-btn" data-on={t.sound} title={t.sound ? 'Mute' : 'Sound on'} onClick={() => setTweak('sound', !t.sound)}>
            <CtrlIcon name={t.sound ? 'sound' : 'muted'} />
          </button>
          <button className="btn chaos" onClick={injectChaos} disabled={chaosUsed || !!approval || done || stage !== 'running'}>
            <CtrlIcon name="chaos" />{chaosUsed ? 'Chaos injected' : 'Inject chaos'}
          </button>
          <button className="btn" onClick={() => setRunning(r => !r)} disabled={stage !== 'running'}>
            <CtrlIcon name={running ? 'pause' : 'play'} />{running ? 'Pause' : 'Resume'}
          </button>
          <button className="btn" onClick={restart}>
            <CtrlIcon name="restart" />Restart
          </button>
        </div>
      </header>

      {/* ===== body ===== */}
      <div className="body">
        <div className="col left">
          <Vitals
            risk={dispRisk} confidence={confidence} running={ticking}
            activeAgent={activeAgent} fireKey={fireKey} agents={window.NERVE.AGENTS}
            replans={replans} actions={actions}
            series={metricSeries} metric={mission.metric} baseline={mission.metric.baseline}
            coherence={coherence}
          />
          <MemoryPanel facts={memory} />
        </div>

        <div className="col center">
          <div className="graph-head">
            <div className="goal-block">
              <div className="eyebrow">Mission · {mission.label}</div>
              <div className="goal" dangerouslySetInnerHTML={{ __html: mission.goal }} />
            </div>
            <div className="phase-badge">
              <span className="pd" style={{ background: dispBand, boxShadow: `0 0 8px ${dispBand}` }} />{phase}
            </div>
          </div>

          <div className="graph-region">
            {stage === 'idle' && <GoalLauncher mission={mission} onLaunch={runMission} />}

            {done && (phoneStatus === 'rejected'
              ? <div className="done-toast" style={{ background: 'rgba(255,106,114,0.10)', borderColor: 'rgba(255,106,114,0.32)', color: '#ff6a72' }}>✕ Held — awaiting your review</div>
              : <div className="done-toast">✓ Mission complete</div>
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
        <TweakSection label="Playback" />
        <TweakSlider label="Speed" value={t.speed} min={0.5} max={2} step={0.25} unit="×" onChange={v => setTweak('speed', v)} />
        <TweakToggle label="Loop the demo" value={t.loop} onChange={v => setTweak('loop', v)} />
        <TweakSection label="Chaos engine" />
        <TweakToggle label="Auto-inject chaos" value={t.autoChaos} onChange={v => setTweak('autoChaos', v)} />
        <TweakSection label="Signals" />
        <TweakToggle label="Ambient sound" value={t.sound} onChange={v => setTweak('sound', v)} />
        <TweakToggle label="Show Telegram by default" value={t.phoneDefault} onChange={v => setTweak('phoneDefault', v)} />
      </TweaksPanel>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
