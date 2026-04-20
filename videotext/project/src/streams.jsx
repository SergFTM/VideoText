// Tab: Streams
function StreamsTab({ t, lang }) {
  const [showForm, setShowForm] = React.useState(false);
  const [streams, setStreams] = React.useState(SAMPLE_STREAMS);
  const [form, setForm] = React.useState({
    url: '', channel: '', speaker: '',
    intervalMin: 3, whisper: 'distil-large-v3',
    makeSummary: false, template: 'news', autoBrief: true,
  });

  function toggleStatus(id, next) {
    setStreams(streams.map(s => s.id === id ? { ...s, status: next, running: next === 'active' } : s));
  }

  function addStream() {
    if (!form.url || !form.channel) return;
    const newS = {
      id: 'str_' + Math.random().toString(36).slice(2, 8),
      url: form.url, channel: form.channel, speaker: form.speaker || null,
      status: 'active', running: true,
      intervalMin: form.intervalMin, whisper: form.whisper,
      chunksTotal: 0, itemsCount: 0, language: 'ru', cost: 0,
      uptime: '0мин', uptimeEn: '0m',
      spark: [0, 0, 0],
    };
    setStreams([newS, ...streams]);
    setForm({ url: '', channel: '', speaker: '', intervalMin: 3, whisper: 'distil-large-v3', makeSummary: false, template: 'news', autoBrief: true });
    setShowForm(false);
  }

  const totalItems = streams.reduce((s, x) => s + x.itemsCount, 0);
  const totalCost = streams.reduce((s, x) => s + x.cost, 0);
  const activeCount = streams.filter(s => s.status === 'active').length;

  return (
    <div className="fade-in">
      <EditorialHeader
        eyebrow="Live-мониторинг"
        title="Слушать эфир,"
        accent="вытягивать новости."
        sub={lang === 'ru'
          ? 'Режем live-поток на чанки, локальный whisper расшифровывает, Claude извлекает утверждения. Новости уходят в модерацию.'
          : 'Chop live streams into chunks, local whisper transcribes, Claude extracts claims. Items land in moderation.'}
      />

      {/* Stats strip */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 0,
        border: '1px solid var(--line)', borderRadius: 14, background: 'var(--surface)',
        marginBottom: 28, overflow: 'hidden',
      }}>
        <Stat label="Активно" value={activeCount} total={streams.length} />
        <Stat label="Новостей сегодня" value={totalItems} divider />
        <Stat label="Затраты за 24ч" value={'USD ' + totalCost.toFixed(2)} divider />
        <Stat label="GPU usage" value="74%" divider sub="RTX 2060 · 6GB" />
      </div>

      {/* New stream trigger */}
      <Section label={t.activeStreams + ' · ' + streams.length} right={
        <button className="btn btn-primary" style={{ fontSize: 12.5, padding: '7px 14px' }}
          onClick={() => setShowForm(!showForm)}>
          <Icon.plus />{showForm ? t.close : t.newStream}
        </button>
      }>
        {showForm && (
          <div className="card fade-in" style={{ padding: 20, marginBottom: 18, background: 'var(--panel)' }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginBottom: 14 }}>
              <div>
                <div className="label-xs" style={{ marginBottom: 6 }}>URL live-эфира</div>
                <input className="text-input mono" placeholder="https://youtube.com/watch?v=…"
                  value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} />
              </div>
              <div>
                <div className="label-xs" style={{ marginBottom: 6 }}>Название канала</div>
                <input className="text-input" placeholder="Bloomberg Live"
                  value={form.channel} onChange={(e) => setForm({ ...form, channel: e.target.value })} />
              </div>
              <div>
                <div className="label-xs" style={{ marginBottom: 6 }}>Ведущий</div>
                <input className="text-input" placeholder="опционально"
                  value={form.speaker} onChange={(e) => setForm({ ...form, speaker: e.target.value })} />
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '100px 1fr', gap: 10 }}>
                <div>
                  <div className="label-xs" style={{ marginBottom: 6 }}>Интервал</div>
                  <input className="text-input mono" type="number" min="1" max="30"
                    value={form.intervalMin} onChange={(e) => setForm({ ...form, intervalMin: +e.target.value })} />
                </div>
                <div>
                  <div className="label-xs" style={{ marginBottom: 6 }}>Whisper</div>
                  <select className="text-input" value={form.whisper}
                    onChange={(e) => setForm({ ...form, whisper: e.target.value })}>
                    <option>tiny</option><option>small</option><option>medium</option>
                    <option>large-v3</option><option>distil-large-v3</option>
                  </select>
                </div>
              </div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 14, fontSize: 12.5,
              paddingTop: 14, borderTop: '1px solid var(--line)' }}>
              <label style={{ display: 'flex', gap: 8, alignItems: 'center', cursor: 'pointer' }}>
                <input type="checkbox" checked={form.makeSummary}
                  onChange={(e) => setForm({ ...form, makeSummary: e.target.checked })} />
                Сводный бриф на весь стрим
              </label>
              {form.makeSummary && (
                <>
                  <SelectMini value={form.template} onChange={(v) => setForm({ ...form, template: v })}
                    options={[['news','news'], ['full','full + ТЗ']]} />
                  <CheckMini checked={form.autoBrief} onChange={(v) => setForm({ ...form, autoBrief: v })}>
                    авто на стопе
                  </CheckMini>
                </>
              )}
              <div style={{ flex: 1 }} />
              <button className="btn" onClick={() => setShowForm(false)}>Отмена</button>
              <button className="btn btn-primary" onClick={addStream} disabled={!form.url || !form.channel}>
                <Icon.play /> Запустить
              </button>
            </div>
          </div>
        )}

        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {streams.map((s) => <StreamRow key={s.id} s={s} t={t} lang={lang} onStatus={toggleStatus} />)}
        </div>
      </Section>
    </div>
  );
}

function Stat({ label, value, total, sub, divider }) {
  return (
    <div style={{
      padding: '18px 22px',
      borderLeft: divider ? '1px solid var(--line)' : 'none',
    }}>
      <div className="label-xs">{label}</div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 4 }}>
        <span className="serif" style={{ fontSize: 32, letterSpacing: '-0.02em' }}>{value}</span>
        {total !== undefined && <span className="mono" style={{ fontSize: 12, color: 'var(--mute-2)' }}>/ {total}</span>}
      </div>
      {sub && <div className="mono" style={{ fontSize: 10.5, color: 'var(--mute-2)', marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

function StreamRow({ s, t, lang, onStatus }) {
  return (
    <div className="card" style={{ padding: 18, display: 'grid',
      gridTemplateColumns: '1fr 140px 200px auto', gap: 20, alignItems: 'center',
    }}>
      {/* Channel info */}
      <div style={{ minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
          {s.status === 'active' && s.running && <span className="live-dot" />}
          <span className="serif" style={{ fontSize: 20, letterSpacing: '-0.01em' }}>{s.channel}</span>
          <StatusChip status={s.status} />
          {s.speaker && (
            <span style={{ color: 'var(--mute)', fontSize: 12 }}>
              · {s.speaker}
            </span>
          )}
        </div>
        <div className="mono" style={{ fontSize: 11, color: 'var(--mute-2)',
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', marginBottom: 6 }}>
          {s.url}
        </div>
        <div style={{ display: 'flex', gap: 14, fontSize: 11.5, color: 'var(--mute)' }}>
          <span><span className="mono" style={{ color: 'var(--ink)' }}>{s.intervalMin}</span>мин чанки</span>
          <span>whisper <span className="mono" style={{ color: 'var(--ink)' }}>{s.whisper}</span></span>
          <span>аптайм <span className="mono" style={{ color: 'var(--ink)' }}>{lang === 'ru' ? s.uptime : s.uptimeEn}</span></span>
        </div>
      </div>

      {/* Sparkline */}
      <div>
        <div className="label-xs" style={{ marginBottom: 4 }}>Темп новостей</div>
        <Sparkline data={s.spark} />
      </div>

      {/* Counters */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
        <div>
          <div className="label-xs">Чанков</div>
          <div className="serif" style={{ fontSize: 22 }}>{s.chunksTotal}</div>
        </div>
        <div>
          <div className="label-xs">Новостей</div>
          <div className="serif" style={{ fontSize: 22 }}>{s.itemsCount}</div>
        </div>
      </div>

      {/* Actions */}
      <div style={{ display: 'flex', gap: 6 }}>
        {s.status === 'active' ? (
          <button className="btn" style={{ padding: '6px 10px' }} onClick={() => onStatus(s.id, 'paused')}>
            <Icon.pause />
          </button>
        ) : s.status === 'paused' ? (
          <button className="btn btn-primary" style={{ padding: '6px 10px' }} onClick={() => onStatus(s.id, 'active')}>
            <Icon.play />
          </button>
        ) : (
          <button className="btn" style={{ padding: '6px 10px' }} onClick={() => onStatus(s.id, 'active')}>
            <Icon.play />
          </button>
        )}
        <button className="btn" style={{ padding: '6px 10px' }} onClick={() => onStatus(s.id, 'stopped')}>
          <Icon.stop />
        </button>
        <button className="btn btn-ghost" style={{ padding: '6px 10px', color: 'var(--mute)', fontSize: 12 }}>
          ред.
        </button>
      </div>
    </div>
  );
}

Object.assign(window, { StreamsTab });
