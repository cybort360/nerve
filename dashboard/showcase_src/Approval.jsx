/* NERVE — Approval moment: dashboard dock card + Telegram phone mirror */

function ImpactViz({ impact }) {
  if (!impact) return null;
  if (impact.kind === 'pods') {
    return (
      <div className="ac-impact">
        <div className="ai-top"><span className="ai-lbl">Blast radius — {impact.label}</span><span className="ai-sub">{impact.sub}</span></div>
        <div className="ac-pods">
          {Array.from({ length: impact.total }).map((_, i) => (
            <i key={i} className={i < impact.hit ? 'hit' : ''} style={{ animationDelay: (i * 28) + 'ms' }} />
          ))}
        </div>
      </div>
    );
  }
  if (impact.kind === 'cost') {
    return (
      <div className="ac-impact">
        <div className="ai-top"><span className="ai-lbl">{impact.label}</span><span className="ai-sub">{impact.sub}</span></div>
        <div className="ac-cost">
          <span className="acc-amt">{impact.amount}</span>
          <span className="acc-meta">Placed as a temporary hold.<br/>Released automatically if unused.</span>
        </div>
      </div>
    );
  }
  return null;
}

function ApprovalDock({ approval, onApprove, onReject }) {
  return (
    <div className="approval-dock">
      <div className="appcard">
        <div className="ac-top">
          <span className="ac-badge"><span className="ping" />Approval required</span>
        </div>
        <h3 className="ac-title">{approval.title}</h3>
        <div className="ac-detail" dangerouslySetInnerHTML={{ __html: approval.detail }} />
        <ImpactViz impact={approval.impact} />
        <div className="ac-rows">
          {approval.rows.map(([k, v], i) => (
            <div className="ac-r" key={i}><span className="k">{k}</span><span className="v">{v}</span></div>
          ))}
        </div>
        <div className="ac-btns">
          <button className="ac-btn reject" onClick={onReject}>{approval.reject}</button>
          <button className="ac-btn approve" onClick={onApprove}>{approval.approve}</button>
        </div>
        <div className="ac-hint">
          <CtrlIcon name="phone" />
          Also sent to your phone · one-tap on Telegram
        </div>
      </div>
    </div>
  );
}

function TelegramPhone({ open, approval, status, onApprove, onReject, onClose }) {
  if (!open) return null;
  return (
    <div className="phone">
      <button className="phone-close" onClick={onClose}>✕</button>
      <div className="phone-screen">
        <div className="tg-bar">
          <div className="tg-av">
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="#06090b" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M8 1.4l5 1.8v4c0 3.4-2.2 5.8-5 7.2-2.8-1.4-5-3.8-5-7.2v-4z" />
              <path d="M5.6 8.2l1.7 1.7 3-3.4" />
            </svg>
          </div>
          <div>
            <div className="tg-nm">NERVE</div>
            <div className="tg-sub">bot · online</div>
          </div>
        </div>
        <div className="tg-body">
          {status === 'pending' && approval && (
            <div className="tg-bubble">
              <div className="tgb-h"><span style={{ width: 6, height: 6, borderRadius: 9, background: '#f4c25a', display: 'inline-block' }} />Approval needed</div>
              <div className="tgb-t">{approval.title}</div>
              <div className="tgb-d" dangerouslySetInnerHTML={{ __html: approval.detail.replace(/<span class="[^"]*">/g, '').replace(/<\/span>/g, '') }} />
              <div className="tgb-btns">
                <div className="tgb-b n" onClick={onReject}>{approval.reject}</div>
                <div className="tgb-b y" onClick={onApprove}>✓ {approval.approve}</div>
              </div>
              <div className="tg-time">now</div>
            </div>
          )}
          {status === 'approved' && (
            <div className="tg-bubble">
              <div className="tgb-h" style={{ color: '#46d39a' }}><span style={{ width: 6, height: 6, borderRadius: 9, background: '#46d39a', display: 'inline-block' }} />Approved by you</div>
              <div className="tgb-d" style={{ margin: 0 }}>Executing now — I'll report back when it's done.</div>
              <div className="tg-time">now ✓✓</div>
            </div>
          )}
          {status === 'rejected' && (
            <div className="tg-bubble">
              <div className="tgb-h" style={{ color: '#ff6a72' }}><span style={{ width: 6, height: 6, borderRadius: 9, background: '#ff6a72', display: 'inline-block' }} />Held</div>
              <div className="tgb-d" style={{ margin: 0 }}>Standing down. No action taken — your call.</div>
              <div className="tg-time">now</div>
            </div>
          )}
          {status === 'idle' && (
            <div className="tg-resolved">No pending approvals. You'll be pinged here.</div>
          )}
        </div>
      </div>
    </div>
  );
}

window.ApprovalDock = ApprovalDock;
window.TelegramPhone = TelegramPhone;
