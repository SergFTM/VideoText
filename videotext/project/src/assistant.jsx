// Tab: Assistant
function AssistantTab({ t, lang }) {
  const [messages, setMessages] = React.useState(ASSISTANT_MESSAGES_INIT);
  const [input, setInput] = React.useState('');
  const [thinking, setThinking] = React.useState(false);
  const endRef = React.useRef(null);

  React.useEffect(() => { endRef.current?.scrollIntoView?.({behavior:'smooth', block:'end'}); }, [messages, thinking]);

  function send() {
    if (!input.trim()) return;
    const q = input.trim();
    setMessages([...messages, { role: 'user', text: q }]);
    setInput(''); setThinking(true);
    setTimeout(() => {
      setMessages(m => [...m, { role: 'assistant', text: 'Это мокнутый ответ. В проде здесь приходят стримовые дельты от провайдера (OpenAI / Claude / Ollama).' }]);
      setThinking(false);
    }, 900);
  }

  const suggestions = [
    'покажи самые дорогие брифы за неделю',
    'почему dedup не срабатывает на похожих новостях?',
    'как включить fastembed вместо openai?',
    'сколько GPU-памяти занимает distil-large-v3?',
  ];

  return (
    <div className="fade-in" style={{ display: 'grid', gridTemplateColumns: '240px 1fr', gap: 28 }}>
      {/* Sidebar: sessions */}
      <aside>
        <div className="label-xs" style={{ marginBottom: 12 }}>Сессии</div>
        <button className="btn" style={{ width: '100%', justifyContent: 'flex-start', marginBottom: 12 }}>
          <Icon.plus /> Новый чат
        </button>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          {[
            { title: 'Почему завис чанк 47', active: true, date: 'сегодня' },
            { title: 'Как обновить промпт', active: false, date: 'вчера' },
            { title: 'Разбор ошибок Supadata', active: false, date: '15 апр' },
            { title: 'Тюнинг whisper-модели', active: false, date: '12 апр' },
          ].map((s, i) => (
            <button key={i} style={{
              textAlign: 'left', padding: '8px 12px', borderRadius: 8, fontSize: 13,
              background: s.active ? 'var(--panel)' : 'transparent',
              color: s.active ? 'var(--ink)' : 'var(--mute)',
            }}>
              <div style={{ fontWeight: s.active ? 500 : 400,
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }}>{s.title}</div>
              <div className="mono" style={{ fontSize: 10.5, color: 'var(--mute-2)', marginTop: 1 }}>{s.date}</div>
            </button>
          ))}
        </div>
      </aside>

      {/* Chat */}
      <div>
        <EditorialHeader
          eyebrow="AI-ассистент"
          title="Задай вопрос —"
          accent="или попроси сделать."
          sub={lang === 'ru'
            ? 'Знает про пайплайн, БД, интеграции, коды ошибок. Может менять настройки и запускать задачи (с твоим подтверждением).'
            : 'Knows the pipeline, DB, integrations, error codes. Can change settings and trigger tasks (with your confirmation).'}
        />

        <div className="card" style={{ padding: 0, minHeight: 480, display: 'flex', flexDirection: 'column' }}>
          <div style={{ flex: 1, padding: '20px 24px', overflowY: 'auto', maxHeight: 520 }} className="scroll-thin">
            {messages.map((m, i) => (
              <div key={i} style={{ marginBottom: 20, display: 'flex', gap: 12 }}>
                <div style={{
                  width: 26, height: 26, borderRadius: 999, flexShrink: 0,
                  background: m.role === 'user' ? 'var(--ink)' : 'var(--acc-soft)',
                  color: m.role === 'user' ? '#fff' : 'var(--acc-ink)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: 11, fontWeight: 600, marginTop: 2,
                }}>
                  {m.role === 'user' ? 'AF' : <span className="serif" style={{ fontSize: 15, fontStyle: 'italic' }}>a</span>}
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="label-xs" style={{ marginBottom: 4 }}>
                    {m.role === 'user' ? 'Ты' : 'Ассистент'}
                  </div>
                  <div style={{ fontSize: 14, lineHeight: 1.65, color: 'var(--ink-2)', whiteSpace: 'pre-wrap' }}>
                    {m.text.split('`').map((p, idx) =>
                      idx % 2 === 1
                        ? <code key={idx} className="mono" style={{
                            background: 'var(--panel)', padding: '1px 5px', borderRadius: 4, fontSize: 12.5,
                          }}>{p}</code>
                        : <React.Fragment key={idx}>{p}</React.Fragment>
                    )}
                  </div>
                  {m.hasAction && (
                    <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
                      <button className="btn btn-primary" style={{ fontSize: 12 }}>
                        <Icon.check /> Да, переключи
                      </button>
                      <button className="btn" style={{ fontSize: 12 }}>Нет, я сам</button>
                    </div>
                  )}
                </div>
              </div>
            ))}
            {thinking && (
              <div style={{ display: 'flex', gap: 12, color: 'var(--mute)', fontSize: 13 }}>
                <div style={{ width: 26, height: 26 }} />
                <div className="fade-in">
                  думаю<span style={{ animation: 'blink 1s infinite' }}>…</span>
                </div>
              </div>
            )}
            <div ref={endRef} />
          </div>

          {/* Input */}
          <div style={{ borderTop: '1px solid var(--line)', padding: 16 }}>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 12 }}>
              {suggestions.map((s, i) => (
                <button key={i} className="chip" style={{ cursor: 'pointer' }}
                  onClick={() => setInput(s)}>
                  {s}
                </button>
              ))}
            </div>
            <div style={{ display: 'flex', gap: 10, alignItems: 'stretch' }}>
              <textarea
                className="text-input"
                rows={1}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }}
                placeholder={t.askPlaceholder}
                style={{ resize: 'none', flex: 1, minHeight: 44 }}
              />
              <button className="btn btn-primary" disabled={!input.trim()} onClick={send}>
                <Icon.arrow />
              </button>
            </div>
            <div style={{ display: 'flex', gap: 14, fontSize: 11, color: 'var(--mute-2)', marginTop: 8 }}>
              <span><span className="kbd">⏎</span> отправить</span>
              <span><span className="kbd">⇧⏎</span> новая строка</span>
              <div style={{ flex: 1 }} />
              <span>провайдер <span className="mono" style={{ color: 'var(--mute)' }}>claude-sonnet-4.6</span></span>
              <span>кэш <span style={{ color: 'var(--ok)' }}>on</span></span>
            </div>
          </div>
        </div>

        <style>{`@keyframes blink { 50% { opacity: 0.3 } }`}</style>
      </div>
    </div>
  );
}

Object.assign(window, { AssistantTab });
