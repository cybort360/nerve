/* NERVE — Mission graph. Nodes light up; signals fire down edges. */

function edgePath(s, t) {
  const dx = t.x - s.x;
  const c = Math.max(40, Math.abs(dx) * 0.5);
  return `M ${s.x} ${s.y} C ${s.x + c} ${s.y} ${t.x - c} ${t.y} ${t.x} ${t.y}`;
}

function MissionGraph({ mission, nodeStates, fireKey, activeNode, tension }) {
  const stageRef = React.useRef(null);
  const [dim, setDim] = React.useState({ w: 760, h: 460 });

  React.useEffect(() => {
    const el = stageRef.current;
    const ro = new ResizeObserver(() => {
      setDim({ w: el.clientWidth, h: el.clientHeight });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const px = (n) => ({ x: (n.x / 100) * dim.w, y: (n.y / 100) * dim.h });
  const byId = {};
  mission.nodes.forEach(n => byId[n.id] = n);

  const visible = (id) => {
    const st = nodeStates[id] || 'pending';
    const node = byId[id];
    if (st === 'hidden') return false;
    if (node && node.chaos && st === 'pending') return false;
    return true;
  };

  const allEdges = [...mission.edges, ...(mission.chaosEdges || [])];

  return (
    <div className="graph-stage" ref={stageRef} data-tension={tension ? 'high' : 'low'}>
      <svg className="graph-svg" viewBox={`0 0 ${dim.w} ${dim.h}`} preserveAspectRatio="none">
        <defs>
          <filter id="edgeglow" x="-30%" y="-30%" width="160%" height="160%">
            <feGaussianBlur stdDeviation="2.4" result="b" />
            <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
          </filter>
        </defs>
        {allEdges.map(([a, b], i) => {
          if (!visible(a) || !visible(b)) return null;
          const s = px(byId[a]), t = px(byId[b]);
          const sState = nodeStates[a] || 'pending';
          const tState = nodeStates[b] || 'pending';
          const d = edgePath(s, t);
          const lit = sState === 'done' || sState === 'alert';
          const targetActive = tState === 'active' || tState === 'approval' || tState === 'alert';
          let col = 'rgba(120,200,190,0.13)';
          if (lit) col = '#2f6f66';
          if (lit && targetActive) {
            if (tState === 'approval') col = '#8a6d2e';
            else if (tState === 'alert') col = '#8a3b3f';
            else col = '#3fe0c5';
          }
          return (
            <path key={a + b} d={d} fill="none" stroke={col}
              strokeWidth={lit ? 1.5 : 1}
              style={{ transition: 'stroke .4s, stroke-width .4s' }} />
          );
        })}
        {/* traveling pulse on edges leading into the active node */}
        {activeNode && allEdges.filter(([a, b]) => b === activeNode && visible(a) && visible(b) && (nodeStates[a] === 'done' || nodeStates[a] === 'alert'))
          .map(([a, b]) => {
            const s = px(byId[a]), t = px(byId[b]);
            const d = edgePath(s, t);
            const st = nodeStates[b];
            const col = st === 'approval' ? '#f4c25a' : st === 'alert' ? '#ff6a72' : '#3fe0c5';
            return (
              <g key={fireKey + a + b}>
                <circle r="3.2" fill={col} style={{ filter: `drop-shadow(0 0 5px ${col})` }}>
                  <animateMotion dur="0.65s" fill="freeze" path={d} />
                  <animate attributeName="opacity" from="1" to="0.2" dur="0.65s" fill="freeze" />
                </circle>
              </g>
            );
          })}
      </svg>

      {mission.nodes.map(n => {
        if (!visible(n.id)) return null;
        const st = nodeStates[n.id] || 'pending';
        const p = { left: n.x + '%', top: n.y + '%' };
        const agentColor = window.NERVE.AGENT_COLOR[n.agent] || '#5d6e6c';
        return (
          <div key={n.id} className={'gnode' + (n.chaos ? ' chaos-born' : '')} data-state={st} style={p}>
            <div className="gn-top">
              <Icon name={n.icon} className="gn-ic" />
              <span className="gn-agent" style={{ color: (st === 'active' || st === 'approval' || st === 'alert') ? agentColor : undefined }}>
                {n.agent === 'orchestrator' ? 'HUMAN' : n.agent}
              </span>
            </div>
            <div className="gn-label">{n.label}</div>
            <div className="gn-status"><i /></div>
          </div>
        );
      })}
    </div>
  );
}

window.MissionGraph = MissionGraph;
