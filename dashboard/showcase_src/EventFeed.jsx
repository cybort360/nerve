/* NERVE — Live event feed with token-streaming on the newest line. */

function stripTags(html) {
  return html.replace(/<[^>]+>/g, '');
}

function EventFeed({ events }) {
  const ref = React.useRef(null);
  const [typed, setTyped] = React.useState({ seq: -1, len: 0, done: true });
  const timerRef = React.useRef(null);

  const last = events[events.length - 1];

  // start a typewriter pass whenever a new last-event arrives
  React.useEffect(() => {
    if (!last) return;
    if (last.seq === typed.seq) return;
    clearInterval(timerRef.current);
    const plain = stripTags(last.text);
    const step = Math.max(1, Math.ceil(plain.length / 40));
    setTyped({ seq: last.seq, len: 0, done: false });
    timerRef.current = setInterval(() => {
      setTyped(prev => {
        const nl = prev.len + step;
        if (nl >= plain.length) { clearInterval(timerRef.current); return { seq: last.seq, len: plain.length, done: true }; }
        return { ...prev, len: nl };
      });
    }, 18);
    return () => clearInterval(timerRef.current);
  }, [last && last.seq]); // eslint-disable-line

  React.useEffect(() => {
    const el = ref.current; if (el) el.scrollTop = el.scrollHeight;
  }, [events.length, typed.len]);

  return (
    <>
      <div className="panel-hd" style={{ padding: '18px 20px 12px', margin: 0, borderBottom: '1px solid var(--line)' }}>
        <span className="ttl">Reasoning feed</span>
        <span className="eyebrow">{events.length} events</span>
      </div>
      <div className="feed" ref={ref}>
        {events.length === 0 && (
          <div className="feed-empty">Awaiting a goal…</div>
        )}
        {events.map((e) => {
          const col = window.NERVE.AGENT_COLOR[e.agent] || '#5d6e6c';
          const name = e.agent === 'orchestrator' ? 'Orchestrator' : e.agent[0].toUpperCase() + e.agent.slice(1);
          const isLast = last && e.seq === last.seq;
          const typing = isLast && typed.seq === e.seq && !typed.done;
          return (
            <div className="evt" data-kind={e.kind} key={e.seq}>
              <div className="evt-rail"><span className="line" /><span className="dot" /></div>
              <div className="evt-body">
                <div className="evt-meta">
                  <span className="evt-agent" style={{ color: col }}>{name}</span>
                  <span className="evt-time tnum">{e.time}</span>
                </div>
                {typing
                  ? <div className="evt-text">{stripTags(e.text).slice(0, typed.len)}<span className="caret" /></div>
                  : <div className="evt-text" dangerouslySetInnerHTML={{ __html: e.text }} />}
                {e.tool && !typing && <div className="evt-tool"><span className="tg" />{e.tool}</div>}
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

window.EventFeed = EventFeed;
