// Tab: Settings
function SettingsTab({ t, lang }) {
  const [activeSection, setActiveSection] = React.useState('integrations');
  const [keys, setKeys] = React.useState(
    INTEGRATIONS.reduce((acc, i) => ({ ...acc, [i.id]: i.configured }), {})
  );

  const sections = [
    ['integrations', 'Интеграции', INTEGRATIONS.length],
    ['storage', 'Хранилище', null],
    ['assistant', 'Ассистент', null],
    ['enrich', 'Обогащение', null],
    ['advanced', 'Продвинутые', null],
  ];

  return (
    <div className="fade-in">
      <EditorialHeader
        eyebrow="Настройки"
        title="Интеграции и"
        accent="режимы работы."
        sub={lang === 'ru'
          ? 'Ключи API, локальные движки, политика хранения, настройки ассистента и пайплайна обогащения.'
          : 'API keys, local engines, retention policy, assistant and enrichment pipeline.'}
      />

      <div style={{ display: 'grid', gridTemplateColumns: '220px 1fr', gap: 36 }}>
        {/* Nav */}
        <aside>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            {sections.map(([id, lbl, ct]) => (
              <button key={id} onClick={() => setActiveSection(id)}
                style={{
                  textAlign: 'left', padding: '9px 14px', borderRadius: 8,
                  fontSize: 13.5, display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                  background: activeSection === id ? 'var(--panel)' : 'transparent',
                  color: activeSection === id ? 'var(--ink)' : 'var(--mute)',
                  fontWeight: activeSection === id ? 500 : 400,
                }}>
                <span>{lbl}</span>
                {ct && <span className="mono" style={{ fontSize: 10.5, color: 'var(--mute-2)' }}>{ct}</span>}
              </button>
            ))}
          </div>
        </aside>

        {/* Content */}
        <div>
          {activeSection === 'integrations' && <IntegrationsSection keys={keys} setKeys={setKeys} lang={lang} />}
          {activeSection === 'storage' && <StorageSection />}
          {activeSection === 'assistant' && <AssistantSection />}
          {activeSection === 'enrich' && <EnrichSection />}
          {activeSection === 'advanced' && <AdvancedSection />}
        </div>
      </div>
    </div>
  );
}

function IntegrationsSection({ keys, setKeys, lang }) {
  return (
    <div>
      <div style={{ display: 'grid', gap: 12 }}>
        {INTEGRATIONS.map(i => (
          <div key={i.id} className="card" style={{ padding: 18, display: 'grid',
            gridTemplateColumns: 'auto 1fr auto auto', gap: 18, alignItems: 'center',
          }}>
            <div style={{
              width: 38, height: 38, borderRadius: 10,
              background: i.kind === 'cloud' ? 'var(--acc-soft)' : 'var(--panel)',
              color: i.kind === 'cloud' ? 'var(--acc-ink)' : 'var(--ink)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontFamily: "'Instrument Serif', serif", fontSize: 20,
            }}>
              {i.name[0]}
            </div>
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 2 }}>
                <span style={{ fontSize: 14.5, fontWeight: 500 }}>{i.name}</span>
                <Chip style={{ fontSize: 10 }}>{i.kind}</Chip>
                {keys[i.id] && <StatusChip status="approved" />}
              </div>
              <div style={{ fontSize: 12.5, color: 'var(--mute)' }}>
                {lang === 'ru' ? i.desc : i.descEn}
                {i.masked && <span className="mono" style={{ marginLeft: 10, color: 'var(--mute-2)' }}>{i.masked}</span>}
              </div>
            </div>
            <a href={'https://' + i.website} target="_blank" className="btn btn-ghost"
              style={{ padding: '4px 10px', fontSize: 11.5, color: 'var(--mute)' }}>
              {i.website} <Icon.ext />
            </a>
            <div className="toggle" onClick={() => setKeys({ ...keys, [i.id]: !keys[i.id] })}
              {...(keys[i.id] ? { className: 'toggle on' } : {})}
              style={{ cursor: 'pointer' }} />
          </div>
        ))}
      </div>
    </div>
  );
}

function StorageSection() {
  const stats = [
    { label: 'Размер БД', value: '124 MB', sub: 'videotext.db' },
    { label: 'Медиа-чанки', value: '3.2 GB', sub: '1 420 файлов' },
    { label: 'Изображения', value: '184 MB', sub: '246 файлов' },
    { label: 'Старейшая запись', value: '12 дн', sub: 'NewsItem #47' },
  ];
  return (
    <div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginBottom: 28 }}>
        {stats.map((s, i) => (
          <div key={i} className="card" style={{ padding: 16 }}>
            <div className="label-xs" style={{ marginBottom: 6 }}>{s.label}</div>
            <div className="serif" style={{ fontSize: 28, letterSpacing: '-0.02em' }}>{s.value}</div>
            <div className="mono" style={{ fontSize: 10.5, color: 'var(--mute-2)', marginTop: 2 }}>{s.sub}</div>
          </div>
        ))}
      </div>

      <Section label="Политика хранения">
        <div className="card" style={{ padding: 20 }}>
          {[
            ['Видео + транскрипты', 365, 3650],
            ['Чанки live-стримов (MP3)', 7, 60],
            ['Транскрипты чанков', 30, 365],
            ['Новости (одобренные)', 180, 3650],
            ['Новости (draft/rejected)', 14, 365],
          ].map(([label, val, max], i) => (
            <div key={i} style={{
              display: 'grid', gridTemplateColumns: '240px 1fr 80px', gap: 16, alignItems: 'center',
              padding: '14px 0', borderTop: i === 0 ? 'none' : '1px solid var(--line)',
            }}>
              <div style={{ fontSize: 13.5 }}>{label}</div>
              <input type="range" min={0} max={max} defaultValue={val} style={{ width: '100%' }} />
              <div className="mono" style={{ fontSize: 12, textAlign: 'right' }}>
                <span style={{ color: 'var(--ink)' }}>{val}</span>
                <span style={{ color: 'var(--mute-2)' }}> дн</span>
              </div>
            </div>
          ))}
          <div style={{ display: 'flex', gap: 10, marginTop: 18, paddingTop: 16, borderTop: '1px solid var(--line)' }}>
            <button className="btn">Dry run</button>
            <button className="btn btn-primary">Применить</button>
            <div style={{ flex: 1 }} />
            <span style={{ fontSize: 12, color: 'var(--mute)' }}>будет удалено ~412 МБ</span>
          </div>
        </div>
      </Section>
    </div>
  );
}

function AssistantSection() {
  return (
    <div className="card" style={{ padding: 24 }}>
      <Field label="Провайдер">
        <div style={{ display: 'flex', gap: 4, background: 'var(--panel)', padding: 3, borderRadius: 8, width: 'fit-content' }}>
          {['openai', 'anthropic', 'ollama'].map((p, i) => (
            <button key={p} style={{
              padding: '6px 14px', fontSize: 13, borderRadius: 6,
              background: i === 1 ? 'var(--surface)' : 'transparent',
              color: i === 1 ? 'var(--ink)' : 'var(--mute)',
              fontWeight: i === 1 ? 500 : 400,
            }}>{p}</button>
          ))}
        </div>
      </Field>
      <Field label="Модель">
        <input className="text-input mono" defaultValue="claude-sonnet-4-6" style={{ maxWidth: 360 }}/>
      </Field>
      <Field label="Кэш Q&A" sub="Сохранять ответы и возвращать их, если следующий вопрос семантически близок.">
        <div className="toggle on" style={{ cursor: 'pointer' }} />
      </Field>
      <Field label="Порог схожести для кэша">
        <div style={{ display: 'flex', alignItems: 'center', gap: 14, maxWidth: 360 }}>
          <input type="range" min="0.5" max="1" step="0.01" defaultValue="0.85" style={{ flex: 1 }} />
          <span className="mono" style={{ fontSize: 13 }}>0.85</span>
        </div>
      </Field>
      <Field label="Автоподтверждение действий" sub="Разрешить ассистенту менять настройки без отдельного клика.">
        <div className="toggle" style={{ cursor: 'pointer' }} />
      </Field>
    </div>
  );
}

function EnrichSection() {
  return (
    <div className="card" style={{ padding: 24 }}>
      <Field label="Модель для расширенного текста">
        <SelectMini value="gpt-4o-mini" onChange={()=>{}}
          options={[['gpt-4o-mini','gpt-4o-mini'], ['gpt-4o','gpt-4o'], ['claude-haiku','claude-haiku-4.5']]} />
      </Field>
      <Field label="Источник изображений">
        <div style={{ display: 'flex', gap: 6 }}>
          {['hybrid', 'dall-e-3', 'pexels', 'none'].map((v, i) => (
            <button key={v} style={{
              padding: '6px 12px', borderRadius: 8, fontSize: 12.5,
              background: i === 0 ? 'var(--ink)' : 'var(--surface)',
              color: i === 0 ? '#fff' : 'var(--mute)',
              border: '1px solid ' + (i === 0 ? 'var(--ink)' : 'var(--line)'),
            }}>{v}</button>
          ))}
        </div>
      </Field>
      <Field label="Дедуп-порог изображений">
        <div style={{ display: 'flex', alignItems: 'center', gap: 14, maxWidth: 360 }}>
          <input type="range" min="0.5" max="1" step="0.01" defaultValue="0.88" style={{ flex: 1 }} />
          <span className="mono" style={{ fontSize: 13 }}>0.88</span>
        </div>
      </Field>
      <Field label="Embeddings провайдер">
        <SelectMini value="fastembed" onChange={()=>{}}
          options={[['fastembed','fastembed (local)'], ['openai','text-embedding-3-small'], ['ollama','nomic-embed-text']]} />
      </Field>
    </div>
  );
}

function AdvancedSection() {
  return (
    <div className="card" style={{ padding: 24 }}>
      <Field label="Webhook-токен" sub="Требовать X-Webhook-Token на POST /briefs.">
        <input className="text-input mono" placeholder="необязательно" style={{ maxWidth: 360 }}/>
      </Field>
      <Field label="OUTPUT_DIR">
        <input className="text-input mono" defaultValue="./output" style={{ maxWidth: 360 }}/>
      </Field>
      <Field label="Cookies.txt" sub="Для yt-dlp при приватных/залогиненных каналах.">
        <button className="btn">Загрузить файл</button>
      </Field>
      <Field label="Debug-логи">
        <div className="toggle" style={{ cursor: 'pointer' }} />
      </Field>
    </div>
  );
}

function Field({ label, sub, children }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '240px 1fr', gap: 24,
      padding: '16px 0', borderTop: '1px solid var(--line)',
      borderTopColor: 'var(--line)' }}>
      <div>
        <div style={{ fontSize: 13.5, fontWeight: 500 }}>{label}</div>
        {sub && <div style={{ fontSize: 12, color: 'var(--mute)', marginTop: 2 }}>{sub}</div>}
      </div>
      <div>{children}</div>
    </div>
  );
}

Object.assign(window, { SettingsTab });
