/* NERVE — v2 surfaces: Sparkline, MemoryPanel, GoalLauncher, Fleet, audio */

/* ---------------- metric formatting ---------------- */
function fmtMetric(v, metric) {
  if (v == null) return '—';
  if (metric.unit === 'ms') return v >= 1000 ? (v / 1000).toFixed(1) + 's' : Math.round(v) + 'ms';
  if (metric.unit === '$') return '$' + Math.round(v).toLocaleString();
  return String(v);
}

/* ---------------- recovery sparkline ---------------- */
function Sparkline({ series, metric, baseline }) {
  const ref = React.useRef(null);
  React.useEffect(() => {
    const cv = ref.current; if (!cv) return;
    const ctx = cv.getContext('2d');
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = cv.clientWidth, h = cv.clientHeight;
    cv.width = w * dpr; cv.height = h * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const data = series.filter(v => v != null);
    if (data.length < 2) return;
    const pad = 6;
    let mn = Math.min(...data), mx = Math.max(...data);
    if (baseline != null) mn = Math.min(mn, baseline);
    if (mx - mn < 1) mx = mn + 1;
    const X = i => pad + (i / (series.length - 1)) * (w - pad * 2);
    const Y = v => h - pad - ((v - mn) / (mx - mn)) * (h - pad * 2);

    // baseline guide
    if (baseline != null) {
      ctx.strokeStyle = 'rgba(255,255,255,0.08)'; ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(pad, Y(baseline)); ctx.lineTo(w - pad, Y(baseline)); ctx.stroke();
      ctx.setLineDash([]);
    }
    // trend colour: comparing last two real points
    const last = data[data.length - 1], prev = data[data.length - 2];
    const improving = metric.lowerBetter ? last <= prev : last >= prev;
    const settled = Math.abs(last - (baseline ?? last)) < (mx - mn) * 0.12;
    const col = settled ? '#46d39a' : (improving ? '#3fe0c5' : '#f4c25a');

    // build point list carrying last value through nulls
    let lastV = null; const pts = series.map((v, i) => { if (v != null) lastV = v; return lastV == null ? null : { x: X(i), y: Y(lastV) }; }).filter(Boolean);
    // area fill
    const g = ctx.createLinearGradient(0, 0, 0, h);
    g.addColorStop(0, col + '33'); g.addColorStop(1, col + '00');
    ctx.beginPath(); ctx.moveTo(pts[0].x, h - pad);
    pts.forEach(p => ctx.lineTo(p.x, p.y));
    ctx.lineTo(pts[pts.length - 1].x, h - pad); ctx.closePath();
    ctx.fillStyle = g; ctx.fill();
    // line
    ctx.beginPath(); pts.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
    ctx.strokeStyle = col; ctx.lineWidth = 1.6; ctx.lineJoin = 'round'; ctx.shadowColor = col; ctx.shadowBlur = 6; ctx.stroke(); ctx.shadowBlur = 0;
    // head dot
    const hd = pts[pts.length - 1];
    ctx.fillStyle = col; ctx.beginPath(); ctx.arc(hd.x, hd.y, 2.4, 0, 7); ctx.fill();
  }, [series, baseline, metric]);

  const lastReal = [...series].reverse().find(v => v != null);
  const lastV = lastReal == null ? null : lastReal;
  const prevReal = [...series].reverse().filter(v => v != null)[1];
  const improving = lastV != null && prevReal != null && (metric.lowerBetter ? lastV <= prevReal : lastV >= prevReal);
  const col = lastV == null ? 'var(--text-mid)' : (improving ? '#3fe0c5' : '#f4c25a');
  return (
    <div className="spark">
      <div className="spark-row">
        <span className="lbl">{metric.label}</span>
        <span className="val" style={{ color: col }}>{fmtMetric(lastV, metric)}</span>
      </div>
      <canvas ref={ref} className="spark-canvas" />
    </div>
  );
}

/* ---------------- shared-memory panel ---------------- */
function MemoryPanel({ facts }) {
  return (
    <div className="panel">
      <div className="panel-hd"><span className="ttl">Working memory</span><span className="eyebrow">{facts.length} beliefs</span></div>
      <div className="mem-list">
        {facts.length === 0 && <div className="mem-empty">No beliefs yet — memory fills as NERVE reasons.</div>}
        {facts.map(f => {
          const confCol = f.conf >= 0.8 ? '#46d39a' : f.conf >= 0.5 ? '#f4c25a' : '#ff6a72';
          return (
            <div className="mem-fact" data-op={f.op} key={f.k + '-' + f.ver}>
              <div className="mf-top">
                <span className="mf-k">{f.label}</span>
                <span className="mf-conf" style={{ color: confCol }}>{Math.round(f.conf * 100)}%</span>
              </div>
              <div className="mf-v">{f.v}</div>
              <span className="mf-bar" style={{ width: (f.conf * 100) + '%', background: confCol }} />
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* ---------------- goal launcher (front door) ---------------- */
function GoalLauncher({ mission, onLaunch }) {
  const [text, setText] = React.useState(mission.goalText);
  React.useEffect(() => { setText(mission.goalText); }, [mission.id]);
  const missions = Object.values(window.NERVE.MISSIONS);
  const submit = () => onLaunch(mission.id, text);
  return (
    <div className="launcher">
      <div className="launcher-card">
        <div className="lc-eyebrow"><span className="lc-pulse" /><span>New mission · give NERVE a goal</span></div>
        <div className="lc-field">
          <textarea value={text} spellCheck={false}
            onChange={e => setText(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); } }} />
          <button className="lc-run" onClick={submit} title="Run mission"><CtrlIcon name="send" /></button>
        </div>
        <div className="lc-sugs">
          {missions.map(m => (
            <button className="lc-sug" key={m.id} onClick={() => onLaunch(m.id, m.goalText)}>
              <span className="sd" style={{ background: m.id === 'incident' ? '#ff6a72' : '#3fe0c5' }} />{m.label}
            </button>
          ))}
        </div>
        <div className="lc-hint">NERVE will decompose the goal into a task graph, then run it — pausing for your approval before anything consequential.</div>
      </div>
    </div>
  );
}

/* ---------------- fleet roster ---------------- */
function Fleet({ missions, currentId, statusOf, awaiting, onSelect }) {
  const [open, setOpen] = React.useState(false);
  const ref = React.useRef(null);
  React.useEffect(() => {
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', h); return () => document.removeEventListener('mousedown', h);
  }, []);
  const ghosts = window.NERVE.FLEET_GHOSTS;
  const liveCount = missions.length + ghosts.filter(g => g.status === 'running').length;
  return (
    <div className="fleet" ref={ref}>
      <button className="fleet-btn" onClick={() => setOpen(o => !o)}>
        <span className="fdots">
          {missions.map(m => { const s = statusOf(m.id); return <i key={m.id} style={{ background: s.color, boxShadow: `0 0 6px ${s.color}` }} />; })}
          {ghosts.map(g => <i key={g.id} style={{ background: g.status === 'running' ? '#46d39a' : '#3a4745' }} />)}
        </span>
        Fleet · {liveCount}{awaiting ? ' · 1 awaiting you' : ''}
      </button>
      {open && (
        <div className="fleet-pop">
          <div className="fp-hd">Active missions</div>
          {missions.map(m => {
            const s = statusOf(m.id);
            return (
              <div className="fleet-item" key={m.id} onClick={() => { onSelect(m.id); setOpen(false); }}>
                <span className="fi-dot" style={{ background: s.color, boxShadow: `0 0 7px ${s.color}` }} />
                <div className="fi-body"><div className="fi-nm">{m.label}</div><div className="fi-sub">{m.id === currentId ? 'on screen' : 'tap to open'}</div></div>
                <span className="fi-st" style={{ color: s.color }}>{s.label}</span>
              </div>
            );
          })}
          <div className="fp-hd">Background</div>
          {ghosts.map(g => (
            <div className="fleet-item ghost" key={g.id}>
              <span className="fi-dot" style={{ background: g.status === 'running' ? '#46d39a' : '#3a4745' }} />
              <div className="fi-body"><div className="fi-nm">{g.label}</div><div className="fi-sub">{g.sub}</div></div>
              <span className="fi-st" style={{ color: g.status === 'running' ? '#46d39a' : '#5d6e6c' }}>{g.status}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---------------- ambient audio ---------------- */
const NerveAudio = {
  ctx: null, enabled: false,
  ensure() { if (!this.ctx) { try { this.ctx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) {} } return this.ctx; },
  play(type) {
    if (!this.enabled) return;
    const ctx = this.ensure(); if (!ctx) return;
    if (ctx.state === 'suspended') ctx.resume();
    const now = ctx.currentTime;
    const tones = {
      alert:   [{ f: 340, t: 0 }, { f: 300, t: 0.14 }],
      approve: [{ f: 540, t: 0 }, { f: 760, t: 0.1 }],
      resolve: [{ f: 520, t: 0 }, { f: 680, t: 0.1 }, { f: 880, t: 0.2 }],
      reject:  [{ f: 300, t: 0 }],
      tick:    [{ f: 660, t: 0 }],
    };
    const seq = tones[type] || tones.tick;
    seq.forEach(({ f, t }) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = type === 'alert' ? 'sawtooth' : 'sine';
      o.frequency.value = f;
      const st = now + t;
      const vol = type === 'tick' ? 0.04 : 0.10;
      g.gain.setValueAtTime(0, st);
      g.gain.linearRampToValueAtTime(vol, st + 0.012);
      g.gain.exponentialRampToValueAtTime(0.0001, st + 0.22);
      o.connect(g); g.connect(ctx.destination);
      o.start(st); o.stop(st + 0.24);
    });
  },
};

window.Sparkline = Sparkline;
window.MemoryPanel = MemoryPanel;
window.GoalLauncher = GoalLauncher;
window.Fleet = Fleet;
window.NerveAudio = NerveAudio;
window.fmtMetric = fmtMetric;
