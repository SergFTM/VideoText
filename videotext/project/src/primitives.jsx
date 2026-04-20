// Shared primitives: icons, sparkline, chips, modal, top bar

const Icon = {
  search: () => <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14" strokeLinecap="round"/></svg>,
  play:   () => <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor"><path d="M3 2l7 4-7 4z"/></svg>,
  pause:  () => <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor"><rect x="3" y="2" width="2.5" height="8"/><rect x="6.5" y="2" width="2.5" height="8"/></svg>,
  stop:   () => <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor"><rect x="2.5" y="2.5" width="7" height="7"/></svg>,
  arrow:  () => <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M3 6h6M6 3l3 3-3 3" strokeLinecap="round" strokeLinejoin="round"/></svg>,
  plus:   () => <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M6 2v8M2 6h8" strokeLinecap="round"/></svg>,
  check:  () => <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M2.5 6.5l2.5 2.5 4.5-5" strokeLinecap="round" strokeLinejoin="round"/></svg>,
  x:      () => <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M3 3l6 6M9 3l-6 6" strokeLinecap="round"/></svg>,
  copy:   () => <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.4"><rect x="3.5" y="3.5" width="6" height="6" rx="1"/><path d="M2 6.5V2.5A.5.5 0 012.5 2h4"/></svg>,
  ext:    () => <svg width="10" height="10" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M5 3H3v6h6V7M7 3h2v2M5 7l4-4" strokeLinecap="round"/></svg>,
  spark:  () => <svg width="10" height="10" viewBox="0 0 12 12" fill="currentColor"><path d="M6 1l1.2 3.5L10.5 6 7.2 7.5 6 11l-1.2-3.5L1.5 6l3.3-1.5z"/></svg>,
  mic:    () => <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.4"><rect x="4.5" y="1.5" width="3" height="6" rx="1.5"/><path d="M2.5 6a3.5 3.5 0 007 0M6 9.5v1.5"/></svg>,
  globe:  () => <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.2"><circle cx="6" cy="6" r="4.5"/><path d="M1.5 6h9M6 1.5c1.5 2 1.5 7 0 9M6 1.5c-1.5 2-1.5 7 0 9"/></svg>,
  setting:() => <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.3"><circle cx="6" cy="6" r="1.5"/><path d="M6 1v1.5M6 9.5V11M1 6h1.5M9.5 6H11M2.5 2.5l1 1M8.5 8.5l1 1M2.5 9.5l1-1M8.5 3.5l1-1"/></svg>,
};

function Chip({ children, color, style, className, onClick }) {
  const s = {
    ...(color ? { background: color.bg, color: color.fg, borderColor: 'transparent' } : null),
    ...style,
  };
  return <span className={`chip ${className || ''}`} style={s} onClick={onClick}>{children}</span>;
}

function StatusChip({ status }) {
  const map = {
    active:  { dot: 'var(--ok)',   bg: 'var(--ok-soft)',   fg: 'oklch(0.35 0.14 155)', label: 'active' },
    paused:  { dot: 'var(--warn)', bg: 'var(--warn-soft)', fg: 'oklch(0.40 0.15 75)',  label: 'paused' },
    stopped: { dot: 'var(--mute-2)', bg: 'var(--panel)',   fg: 'var(--mute)',           label: 'stopped'},
    draft:   { dot: 'var(--mute)', bg: 'var(--panel)',     fg: 'var(--mute)',           label: 'draft'  },
    approved:{ dot: 'var(--ok)',   bg: 'var(--ok-soft)',   fg: 'oklch(0.35 0.14 155)', label: 'approved'},
    rejected:{ dot: 'var(--bad)',  bg: 'var(--bad-soft)',  fg: 'oklch(0.42 0.17 25)',  label: 'rejected'},
  }[status] || { dot: 'var(--mute-2)', bg: 'var(--panel)', fg: 'var(--mute)', label: status };
  return (
    <span className="chip" style={{ background: map.bg, color: map.fg, borderColor: 'transparent' }}>
      <span className="chip-dot" style={{ background: map.dot }} />
      <span className="mono" style={{ fontSize: 10.5 }}>{map.label}</span>
    </span>
  );
}

function Sparkline({ data, color = 'var(--acc)', fill = 'var(--acc-soft)', height = 28 }) {
  const w = 120;
  const max = Math.max(...data, 1);
  const min = Math.min(...data, 0);
  const step = w / Math.max(data.length - 1, 1);
  const points = data.map((v, i) => [i * step, height - ((v - min) / (max - min || 1)) * (height - 4) - 2]);
  const path = points.map((p, i) => (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  const fillPath = path + ` L ${w} ${height} L 0 ${height} Z`;
  return (
    <svg viewBox={`0 0 ${w} ${height}`} preserveAspectRatio="none" style={{ width: '100%', height }}>
      <path d={fillPath} fill={fill} opacity="0.5" />
      <path d={path} stroke={color} strokeWidth="1.4" fill="none" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx={points[points.length - 1][0]} cy={points[points.length - 1][1]} r="2" fill={color} />
    </svg>
  );
}

function fmtDur(sec) {
  if (!sec) return '—';
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  if (m >= 60) return `${Math.floor(m/60)}:${(m%60).toString().padStart(2,'0')}:${s.toString().padStart(2,'0')}`;
  return `${m}:${s.toString().padStart(2,'0')}`;
}

function TopBar({ active, onTab, lang, onLang, t }) {
  const tabs = ['video', 'streams', 'news', 'assistant', 'settings'];
  return (
    <header style={{
      position: 'sticky', top: 0, zIndex: 30,
      background: 'color-mix(in oklch, var(--bg) 90%, transparent)',
      backdropFilter: 'blur(12px)',
      borderBottom: '1px solid var(--line)',
    }}>
      <div style={{
        maxWidth: 1280, margin: '0 auto', padding: '14px 32px',
        display: 'flex', alignItems: 'center', gap: 32,
      }}>
        {/* Wordmark */}
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexShrink: 0 }}>
          <span className="serif" style={{ fontSize: 26, letterSpacing: '-0.02em' }}>VideoText</span>
          <span className="mono" style={{ fontSize: 10, color: 'var(--mute-2)' }}>v0.4</span>
        </div>

        {/* Tabs */}
        <nav style={{ display: 'flex', gap: 2, flex: 1 }}>
          {tabs.map((id) => (
            <button
              key={id}
              onClick={() => onTab(id)}
              style={{
                padding: '8px 14px', borderRadius: 8,
                fontSize: 13.5, fontWeight: 500,
                color: active === id ? 'var(--ink)' : 'var(--mute)',
                background: active === id ? 'var(--surface)' : 'transparent',
                border: active === id ? '1px solid var(--line)' : '1px solid transparent',
                position: 'relative',
              }}
            >
              {t.tabs[id]}
              {active === id && (
                <span className="serif" style={{
                  position:'absolute', top:-4, right:-4,
                  fontSize: 14, color: 'var(--acc)', fontStyle:'italic',
                }}>·</span>
              )}
            </button>
          ))}
        </nav>

        {/* Right cluster */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexShrink: 0 }}>
          <button className="btn btn-ghost" style={{ padding: '6px 10px', fontSize: 12, color: 'var(--mute)' }}>
            <Icon.search />
            <span>Поиск</span>
            <span className="kbd">⌘K</span>
          </button>
          <div style={{ display: 'flex', background: 'var(--panel)', borderRadius: 8, padding: 2, gap: 2 }}>
            {['ru', 'en'].map((l) => (
              <button key={l} onClick={() => onLang(l)}
                style={{
                  padding: '4px 10px', fontSize: 11.5, fontWeight: 600,
                  borderRadius: 6, textTransform: 'uppercase', letterSpacing: '0.08em',
                  background: lang === l ? 'var(--surface)' : 'transparent',
                  color: lang === l ? 'var(--ink)' : 'var(--mute)',
                  boxShadow: lang === l ? '0 1px 2px rgba(0,0,0,0.04)' : 'none',
                }}>
                {l}
              </button>
            ))}
          </div>
          <div style={{
            width: 32, height: 32, borderRadius: 999,
            background: 'var(--ink)', color:'#fff',
            display:'flex', alignItems:'center', justifyContent:'center',
            fontSize: 12, fontWeight: 600,
          }}>AF</div>
        </div>
      </div>
    </header>
  );
}

function Modal({ open, onClose, children, width }) {
  React.useEffect(() => {
    if (!open) return;
    const h = (e) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="modal-backdrop fade-in" onClick={onClose}>
      <div className="modal" style={{ maxWidth: width || 720 }} onClick={(e) => e.stopPropagation()}>
        {children}
      </div>
    </div>
  );
}

function EditorialHeader({ eyebrow, title, accent, sub }) {
  return (
    <div style={{ marginBottom: 28 }}>
      <div className="label-xs" style={{ color: 'var(--acc)' }}>{eyebrow}</div>
      <h1 className="serif" style={{
        fontSize: 48, lineHeight: 1.05, margin: '10px 0 6px',
        letterSpacing: '-0.02em',
      }}>
        {title}{accent && <span style={{ fontStyle: 'italic', color: 'var(--acc)' }}> {accent}</span>}
      </h1>
      {sub && <p style={{ color: 'var(--mute)', fontSize: 15, maxWidth: 640, margin: 0 }}>{sub}</p>}
    </div>
  );
}

function Section({ label, right, children, style }) {
  return (
    <section style={{ marginBottom: 40, ...style }}>
      <div style={{
        display:'flex', alignItems:'baseline', justifyContent:'space-between',
        marginBottom: 14, gap: 16,
      }}>
        <div className="label-xs">{label}</div>
        {right}
      </div>
      {children}
    </section>
  );
}

Object.assign(window, {
  Icon, Chip, StatusChip, Sparkline, fmtDur,
  TopBar, Modal, EditorialHeader, Section,
});
