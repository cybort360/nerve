/* NERVE — inline icon set. Stroke-based, 16px grid. */
const ICON_PATHS = {
  pulse:  '<path d="M1 8h3.2l1.8-5 3 11 2-6 1.5 3H15"/>',
  git:    '<circle cx="4" cy="4" r="2"/><circle cx="4" cy="12" r="2"/><circle cx="12" cy="9" r="2"/><path d="M4 6v4M6 4h3a3 3 0 0 1 3 3"/>',
  radius: '<circle cx="8" cy="8" r="6.2"/><circle cx="8" cy="8" r="1.4" fill="currentColor" stroke="none"/><path d="M8 8l4.2-4.2"/>',
  target: '<circle cx="8" cy="8" r="6.2"/><circle cx="8" cy="8" r="2.6"/>',
  route:  '<circle cx="3.5" cy="3.5" r="1.8"/><circle cx="12.5" cy="12.5" r="1.8"/><path d="M3.5 5.3V9a2.5 2.5 0 0 0 2.5 2.5h4.7"/>',
  shield: '<path d="M8 1.4l5 1.8v4c0 3.4-2.2 5.8-5 7.2-2.8-1.4-5-3.8-5-7.2v-4z"/><path d="M5.6 8.2l1.7 1.7 3-3.4"/>',
  bolt:   '<path d="M8.6 1L3 9h4l-.6 6L13 7H9z"/>',
  check:  '<circle cx="8" cy="8" r="6.4"/><path d="M5.2 8.2l1.9 1.9 3.8-4.2"/>',
  alert:  '<path d="M8 1.6l6.5 11.6H1.5z"/><path d="M8 6v3.4M8 11.2v.1"/>',
  ticket: '<path d="M2 5.5A1.5 1.5 0 0 1 3.5 4h9A1.5 1.5 0 0 1 14 5.5V7a1.2 1.2 0 0 0 0 2v1.5A1.5 1.5 0 0 1 12.5 12h-9A1.5 1.5 0 0 1 2 10.5V9a1.2 1.2 0 0 0 0-2z"/><path d="M9 4.5v7" stroke-dasharray="1.3 1.3"/>',
  bed:    '<path d="M2 4v9M2 8h12a0 0 0 0 1 0 0v5M2 10.5h12"/><path d="M4.5 8V6.5A1.5 1.5 0 0 1 6 5h4a1.5 1.5 0 0 1 1.5 1.5V8"/>',
  scale:  '<path d="M8 2v12M3 5h10"/><path d="M3 5L1.4 9h3.2zM13 5l-1.6 4h3.2z"/><path d="M5 14h6"/>',
  star:   '<path d="M8 1.6l1.9 3.9 4.3.6-3.1 3 .7 4.2L8 11.4 4.3 13.3l.7-4.2-3.1-3 4.3-.6z"/>',
};

function Icon({ name, className, style }) {
  const p = ICON_PATHS[name] || ICON_PATHS.target;
  return (
    <svg className={className} style={style} viewBox="0 0 16 16" fill="none"
      stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"
      dangerouslySetInnerHTML={{ __html: p }} />
  );
}

/* control-bar icons (separate, 14px feel) */
function CtrlIcon({ name }) {
  const paths = {
    play:    '<path d="M4 2.5l9 5.5-9 5.5z" fill="currentColor" stroke="none"/>',
    pause:   '<rect x="3.5" y="3" width="3" height="10" rx="1" fill="currentColor" stroke="none"/><rect x="9.5" y="3" width="3" height="10" rx="1" fill="currentColor" stroke="none"/>',
    restart: '<path d="M13 8a5 5 0 1 1-1.5-3.6"/><path d="M13 2v3h-3"/>',
    chaos:   '<path d="M8 1.6l6.5 11.6H1.5z"/><path d="M8 6v3.4M8 11.4v.1"/>',
    phone:   '<rect x="4.5" y="1.5" width="7" height="13" rx="1.6"/><path d="M7 12.5h2"/>',
    send:    '<path d="M14 2L7 9M14 2l-4.5 12-2.5-5-5-2.5z"/>',
    sound:   '<path d="M3 6v4h2.5L9 13V3L5.5 6z"/><path d="M11 6.5a2.2 2.2 0 0 1 0 3M12.6 5a4.4 4.4 0 0 1 0 6"/>',
    muted:   '<path d="M3 6v4h2.5L9 13V3L5.5 6z"/><path d="M11.5 6.5l3 3M14.5 6.5l-3 3"/>',
  };
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"
      strokeLinecap="round" strokeLinejoin="round"
      dangerouslySetInnerHTML={{ __html: paths[name] || '' }} />
  );
}

window.Icon = Icon;
window.CtrlIcon = CtrlIcon;
