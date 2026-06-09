/* NERVE — Vitals: ECG heartbeat (canvas) + confidence + agent hub */

const VITAL_BANDS = [
  { max: 22,  name: 'calm',     color: '#46d39a' },
  { max: 50,  name: 'steady',   color: '#3fe0c5' },
  { max: 72,  name: 'elevated', color: '#f4c25a' },
  { max: 101, name: 'critical', color: '#ff6a72' },
];
function bandFor(risk) {
  return VITAL_BANDS.find(b => risk < b.max) || VITAL_BANDS[VITAL_BANDS.length - 1];
}

/* PQRST-ish single-beat waveform, t in [0,1) */
function ecgWave(t) {
  const g = (mu, v) => Math.exp(-((t - mu) * (t - mu)) / (2 * v));
  return (
    0.13 * g(0.16, 0.0016)   // P
    - 0.11 * g(0.255, 0.0004) // Q
    + 1.00 * g(0.288, 0.00028) // R
    - 0.24 * g(0.318, 0.0006)  // S
    + 0.28 * g(0.49, 0.0030)   // T
  );
}

function ECG({ risk, running }) {
  const canvasRef = React.useRef(null);
  const stateRef = React.useRef({ buf: null, phase: 0, accum: 0, last: 0, risk: risk });
  stateRef.current.targetRisk = risk;
  stateRef.current.running = running;

  React.useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas.getContext('2d');
    let raf;
    const st = stateRef.current;

    function size() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = canvas.clientWidth, h = canvas.clientHeight;
      canvas.width = w * dpr; canvas.height = h * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      st.W = w; st.H = h;
      st.buf = new Array(Math.ceil(w)).fill(0);
    }
    size();
    const ro = new ResizeObserver(size); ro.observe(canvas);

    const PX_PER_SEC = 78;
    function frame(ts) {
      if (!st.last) st.last = ts;
      let dt = (ts - st.last) / 1000; st.last = ts;
      if (dt > 0.1) dt = 0.1;
      // ease displayed risk toward target
      st.risk += (st.targetRisk - st.risk) * Math.min(1, dt * 3);
      const r = st.risk;
      const bpm = 56 + r * 1.02;
      const phasePerPx = (bpm / 60) / PX_PER_SEC;
      const noise = (r / 100) * 0.05;
      const amp = st.H * 0.34;

      const W = st.W, H = st.H, mid = H * 0.56;
      // advance buffer
      let px = st.running ? PX_PER_SEC * dt + st.accum : st.accum;
      let whole = Math.floor(px);
      st.accum = px - whole;
      if (whole > W) whole = W;
      for (let i = 0; i < whole; i++) {
        st.phase += phasePerPx;
        if (st.phase >= 1) st.phase -= 1;
        let y = ecgWave(st.phase) * amp;
        if (noise) y += (Math.random() - 0.5) * noise * amp * 2;
        st.buf.push(y);
        if (st.buf.length > W) st.buf.shift();
      }

      const band = bandFor(r);
      const col = band.color;
      // draw
      ctx.clearRect(0, 0, W, H);
      // faint centerline
      ctx.strokeStyle = 'rgba(255,255,255,0.04)';
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(W, mid); ctx.stroke();

      const buf = st.buf;
      const n = buf.length;
      // glow trace
      ctx.lineJoin = 'round'; ctx.lineCap = 'round';
      ctx.shadowColor = col; ctx.shadowBlur = 9;
      ctx.strokeStyle = col; ctx.lineWidth = 1.7;
      ctx.beginPath();
      for (let i = 0; i < n; i++) {
        const x = W - (n - 1 - i);
        const y = mid - buf[i];
        // fade older samples by drawing a gradient? keep simple: skip
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
      ctx.shadowBlur = 0;

      // leading dot
      if (n) {
        const y = mid - buf[n - 1];
        ctx.fillStyle = col;
        ctx.beginPath(); ctx.arc(W - 1, y, 2.4, 0, 7); ctx.fill();
      }
      // left fade mask
      const grd = ctx.createLinearGradient(0, 0, W * 0.18, 0);
      grd.addColorStop(0, 'rgba(6,9,11,1)');
      grd.addColorStop(1, 'rgba(6,9,11,0)');
      ctx.fillStyle = grd; ctx.fillRect(0, 0, W * 0.18, H);

      raf = requestAnimationFrame(frame);
    }
    raf = requestAnimationFrame(frame);
    return () => { cancelAnimationFrame(raf); ro.disconnect(); };
  }, []);

  const band = bandFor(risk);
  const bpm = Math.round(56 + risk * 1.02);
  return (
    <div className="ecg-wrap">
      <canvas ref={canvasRef} className="ecg-canvas" />
      <div className="ecg-readout">
        <span className="big tnum" style={{ color: band.color }}>{Math.round(risk)}</span>
        <span className="unit">RISK<br/>INDEX</span>
      </div>
      <div className="ecg-band" style={{ color: band.color }}>{band.name}</div>
      <div className="ecg-bpm tnum">{bpm} bpm</div>
    </div>
  );
}

/* central orchestrator + 4 agents, signals fire into the hub */
function AgentHub({ activeAgent, fireKey, agents }) {
  const POS = {
    planner:   { x: 44,  y: 40 },
    execution: { x: 156, y: 40 },
    risk:      { x: 44,  y: 140 },
    auditor:   { x: 156, y: 140 },
  };
  const C = { x: 100, y: 90 };
  return (
    <svg className="hub-svg" viewBox="0 0 200 180" fill="none">
      {/* edges */}
      {agents.map(a => {
        const p = POS[a.id];
        const on = activeAgent === a.id;
        return (
          <line key={'e' + a.id} x1={p.x} y1={p.y} x2={C.x} y2={C.y}
            stroke={on ? a.color : 'rgba(120,200,190,0.12)'} strokeWidth={on ? 1.3 : 1}
            style={{ transition: 'stroke .3s', opacity: on ? 0.85 : 0.5 }} />
        );
      })}
      {/* traveling signal pulse */}
      {activeAgent && POS[activeAgent] && (
        <circle key={fireKey} r="3"
          fill={(agents.find(a => a.id === activeAgent) || {}).color || '#3fe0c5'}>
          <animate attributeName="cx" from={POS[activeAgent].x} to={C.x} dur="0.6s" fill="freeze" />
          <animate attributeName="cy" from={POS[activeAgent].y} to={C.y} dur="0.6s" fill="freeze" />
          <animate attributeName="opacity" from="1" to="0.15" dur="0.6s" fill="freeze" />
          <animate attributeName="r" from="3.4" to="1.5" dur="0.6s" fill="freeze" />
        </circle>
      )}
      {/* orchestrator core */}
      <circle cx={C.x} cy={C.y} r="20" fill="rgba(63,224,197,0.04)" stroke="rgba(120,200,190,0.18)" />
      <circle cx={C.x} cy={C.y} r="11" fill="rgba(8,12,14,0.9)" stroke="rgba(120,200,190,0.35)">
        <animate attributeName="r" values="11;13;11" dur="2.6s" repeatCount="indefinite" />
      </circle>
      <circle cx={C.x} cy={C.y} r="3.4" fill="#dcebe8">
        <animate attributeName="opacity" values="1;0.5;1" dur="2.6s" repeatCount="indefinite" />
      </circle>
      <text x={C.x} y={C.y + 33} textAnchor="middle" fontFamily="'IBM Plex Mono', monospace"
        fontSize="7" letterSpacing="1.4" fill="#5d6e6c">ORCHESTRATOR</text>
      {/* agent nodes */}
      {agents.map(a => {
        const p = POS[a.id];
        const on = activeAgent === a.id;
        return (
          <g key={a.id}>
            <circle cx={p.x} cy={p.y} r={on ? 9 : 7}
              fill={on ? a.color : 'rgba(8,12,14,0.9)'}
              stroke={a.color} strokeWidth="1.3"
              style={{ transition: 'all .3s', filter: on ? `drop-shadow(0 0 8px ${a.color})` : 'none' }} />
            <circle cx={p.x} cy={p.y} r="2.4" fill={on ? '#06090b' : a.color} style={{ transition: 'fill .3s' }} />
          </g>
        );
      })}
    </svg>
  );
}

function Vitals({ risk, confidence, running, activeAgent, fireKey, agents, agentStatus, replans, actions, series, metric, baseline, coherence }) {
  const confCol = confidence > 70 ? '#46d39a' : confidence > 45 ? '#3fe0c5' : confidence > 25 ? '#f4c25a' : '#ff6a72';
  const cohCol = coherence === 'reconciling' ? '#f4c25a' : '#46d39a';
  return (
    <>
      <div className="panel">
        <div className="panel-hd"><span className="ttl">Vitals</span>
          <span className="eyebrow" style={{ color: running ? '#46d39a' : '#5d6e6c' }}>{running ? '● live' : '○ paused'}</span>
        </div>
        <ECG risk={risk} running={running} />
        <div className="meter">
          <div className="meter-row"><span className="lbl">Confidence</span><span className="val tnum">{Math.round(confidence)}%</span></div>
          <div className="meter-track"><div className="meter-fill" style={{ width: confidence + '%', background: confCol }} /></div>
        </div>
        {series && <Sparkline series={series} metric={metric} baseline={baseline} />}
        <div style={{ display: 'flex', gap: 18, marginTop: 16, alignItems: 'flex-end' }}>
          <div className="statchip"><span className="k">Actions</span><span className="v tnum">{actions}</span></div>
          <div className="statchip"><span className="k">Re-plans</span><span className="v tnum" style={{ color: replans ? '#f4c25a' : undefined }}>{replans}</span></div>
          <div className="statchip" style={{ marginLeft: 'auto' }}><span className="k">State</span>
            <span className="coh"><span className="cd" style={{ background: cohCol, boxShadow: `0 0 7px ${cohCol}` }} />{coherence === 'reconciling' ? 'reconciling' : 'coherent'}</span>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-hd"><span className="ttl">Agents</span><span className="eyebrow">via orchestrator</span></div>
        <div className="hub-wrap">
          <AgentHub activeAgent={activeAgent} fireKey={fireKey} agents={agents} />
          <div className="agent-legend">
            {agents.map(a => (
              <div className="agent-row" key={a.id} data-active={activeAgent === a.id}>
                <span className="ad" style={{ background: a.color, boxShadow: activeAgent === a.id ? `0 0 8px ${a.color}` : 'none' }} />
                <span className="an">{a.name}</span>
                <span className="as">{a.role}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </>
  );
}

window.Vitals = Vitals;
