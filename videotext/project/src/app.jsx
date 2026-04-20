// App shell
function App() {
  const [tab, setTab] = React.useState(() => localStorage.getItem('vt_tab') || 'video');
  const [lang, setLang] = React.useState(() => localStorage.getItem('vt_lang') || 'ru');
  const t = I18N[lang];

  React.useEffect(() => localStorage.setItem('vt_tab', tab), [tab]);
  React.useEffect(() => localStorage.setItem('vt_lang', lang), [lang]);

  return (
    <div>
      <TopBar active={tab} onTab={setTab} lang={lang} onLang={setLang} t={t} />
      <main style={{ maxWidth: 1280, margin: '0 auto', padding: '40px 32px 80px' }}>
        {tab === 'video'     && <VideoTab t={t} lang={lang} />}
        {tab === 'streams'   && <StreamsTab t={t} lang={lang} />}
        {tab === 'news'      && <NewsTab t={t} lang={lang} />}
        {tab === 'assistant' && <AssistantTab t={t} lang={lang} />}
        {tab === 'settings'  && <SettingsTab t={t} lang={lang} />}
      </main>
      <footer style={{
        maxWidth: 1280, margin: '0 auto', padding: '20px 32px 40px',
        borderTop: '1px solid var(--line)',
        display: 'flex', alignItems: 'center', gap: 16, fontSize: 11.5, color: 'var(--mute-2)',
      }}>
        <span className="serif" style={{ fontSize: 16, color: 'var(--ink)' }}>VideoText</span>
        <span>локальный инструмент · SQLite + Prisma · работает на твоей машине</span>
        <div style={{ flex: 1 }} />
        <a href="#" style={{ color: 'var(--mute)' }}>Swagger /docs ↗</a>
        <span className="mono">v0.4.0</span>
      </footer>
    </div>
  );
}

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(<App />);
