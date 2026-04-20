// Tab: Video
function VideoTab({ t, lang }) {
  const [url, setUrl] = React.useState('https://www.youtube.com/watch?v=C4KWsmezXm4');
  const [busy, setBusy] = React.useState(false);
  const [result, setResult] = React.useState({ ready: true });
  const [opts, setOpts] = React.useState({
    briefLang: 'ru', model: 'sonnet', format: 'markdown', backend: 'auto',
    onlyTranscript: false, forceRefresh: false,
  });
  const [activeVideo, setActiveVideo] = React.useState(SAMPLE_VIDEOS[0]);
  const [copyHint, setCopyHint] = React.useState('');

  function run() {
    setBusy(true); setResult({});
    setTimeout(() => { setBusy(false); setResult({ ready: true, fromCache: opts.forceRefresh ? false : true }); }, 1400);
  }

  return (
    <div className="fade-in">
      <EditorialHeader
        eyebrow="Видео → бриф"
        title="Превратить"
        accent="час просмотра в минуту чтения."
        sub={lang === 'ru'
          ? 'Вставь YouTube-ссылку — извлечём транскрипт, попросим Claude собрать бриф и сохраним в локальную БД.'
          : 'Paste a YouTube link — we pull the transcript, ask Claude for a brief, and save it locally.'}
      />

      {/* URL input */}
      <div className="card" style={{ padding: 20, marginBottom: 24, borderRadius: 14 }}>
        <div style={{ display: 'flex', gap: 10, alignItems: 'stretch' }}>
          <div style={{ flex: 1, position: 'relative' }}>
            <input
              className="text-input mono"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && !busy && run()}
              placeholder={t.videoInputPh}
              style={{ paddingLeft: 40 }}
            />
            <div style={{ position: 'absolute', left: 14, top: '50%', transform: 'translateY(-50%)', color: 'var(--mute-2)' }}>
              <Icon.search />
            </div>
          </div>
          <button
            className={busy ? 'btn' : 'btn btn-primary'}
            disabled={busy || !url}
            onClick={run}
            style={{ padding: '0 22px', minWidth: 130 }}
          >
            {busy ? (
              <><span style={{
                width: 10, height: 10, border: '1.5px solid currentColor', borderTopColor: 'transparent',
                borderRadius: 999, display: 'inline-block',
                animation: 'spin 0.9s linear infinite',
              }}/>{t.running}…</>
            ) : (<><Icon.arrow />{t.run}</>)}
          </button>
        </div>

        {/* Options row */}
        <div style={{
          display: 'flex', flexWrap: 'wrap', gap: 18,
          marginTop: 16, paddingTop: 16, borderTop: '1px solid var(--line)',
          fontSize: 13, color: 'var(--mute)',
        }}>
          <Inline label={t.lang}>
            <SelectMini value={opts.briefLang} onChange={(v) => setOpts({ ...opts, briefLang: v })}
              options={[['ru','ru'], ['en','en']]} />
          </Inline>
          <Inline label={t.model}>
            <SelectMini value={opts.model} onChange={(v) => setOpts({ ...opts, model: v })}
              options={[['sonnet','sonnet-4.6'], ['opus','opus-4.7'], ['haiku','haiku-4.5']]} />
          </Inline>
          <Inline label={t.format}>
            <SelectMini value={opts.format} onChange={(v) => setOpts({ ...opts, format: v })}
              options={[['markdown','markdown'], ['json','json']]} />
          </Inline>
          <Inline label={t.backend}>
            <SelectMini value={opts.backend} onChange={(v) => setOpts({ ...opts, backend: v })}
              options={[['auto','auto+fallback'], ['supadata','supadata'], ['ytdlp','yt-dlp']]} />
          </Inline>
          <CheckMini checked={opts.onlyTranscript} onChange={(v) => setOpts({ ...opts, onlyTranscript: v })}>
            {t.onlyTranscript}
          </CheckMini>
          <CheckMini checked={opts.forceRefresh} onChange={(v) => setOpts({ ...opts, forceRefresh: v })}>
            {t.forceRefresh}
          </CheckMini>
        </div>
      </div>

      {/* Video result */}
      {!busy && result.ready && (
        <div className="fade-in" style={{ display: 'grid', gridTemplateColumns: '1.1fr 1fr', gap: 28 }}>
          {/* LEFT: transcript + metadata */}
          <div>
            <div style={{ marginBottom: 20 }}>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 10 }}>
                {result.fromCache !== false && <Chip>⌁ {t.fromCache}</Chip>}
                <Chip><span className="mono" style={{ fontSize: 10.5 }}>{activeVideo.language}</span></Chip>
                <Chip><span className="mono" style={{ fontSize: 10.5 }}>{fmtDur(activeVideo.duration)}</span></Chip>
                <Chip><span className="mono" style={{ fontSize: 10.5 }}>supadata</span></Chip>
              </div>
              <h2 className="serif" style={{ fontSize: 30, lineHeight: 1.15, margin: '0 0 6px', letterSpacing: '-0.015em' }}>
                {activeVideo.title}
              </h2>
              <div className="mono" style={{ fontSize: 12, color: 'var(--mute-2)' }}>
                {activeVideo.id} <span className="sep">·</span> {SAMPLE_SEGMENTS.length} сегм. <span className="sep">·</span> 2847 слов
              </div>
            </div>

            <Section label={t.transcript} right={
              <div style={{ display: 'flex', gap: 10 }}>
                <button className="btn btn-ghost" style={{ padding: '4px 8px', fontSize: 12, color: 'var(--mute)' }}
                  onClick={() => { navigator.clipboard?.writeText(SAMPLE_SEGMENTS.map(s => s.text).join('\n')); setCopyHint('copy-ok'); setTimeout(() => setCopyHint(''), 1200); }}>
                  <Icon.copy /> {copyHint === 'copy-ok' ? '✓' : 'copy'}
                </button>
                <button className="btn btn-ghost" style={{ padding: '4px 8px', fontSize: 12, color: 'var(--mute)' }}>
                  <Icon.ext /> .txt
                </button>
              </div>
            }>
              <div className="card scroll-thin" style={{
                padding: '14px 18px', maxHeight: 440, overflowY: 'auto', fontSize: 13.5, lineHeight: 1.7,
              }}>
                {SAMPLE_SEGMENTS.map((s, i) => (
                  <div key={i} style={{ display: 'flex', gap: 14, paddingTop: i === 0 ? 0 : 6 }}>
                    <span className="mono" style={{
                      color: 'var(--mute-2)', fontSize: 11, width: 44, flexShrink: 0, paddingTop: 3,
                    }}>{fmtDur(s.start)}</span>
                    <span style={{ color: 'var(--ink-2)' }}>{s.text}</span>
                  </div>
                ))}
              </div>
            </Section>
          </div>

          {/* RIGHT: brief */}
          <div>
            <Section label={t.brief} right={
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 12 }}>
                <span className="mono" style={{ color: 'var(--mute)' }}>claude-sonnet-4.6</span>
                {result.fromCache ? (
                  <Chip style={{ background: 'var(--acc-soft)', color: 'var(--acc-ink)', borderColor: 'transparent' }}>
                    ⌁ 0 токенов
                  </Chip>
                ) : (
                  <span className="mono" style={{ color: 'var(--ink)' }}>$0.0847</span>
                )}
              </div>
            }>
              <article>
                {SAMPLE_BRIEF_SECTIONS.map((sec, i) => (
                  <div key={i} style={{
                    paddingBottom: 18, marginBottom: 18,
                    borderBottom: i < SAMPLE_BRIEF_SECTIONS.length - 1 ? '1px solid var(--line)' : 'none',
                  }}>
                    <h3 className="serif" style={{ fontSize: 22, margin: '0 0 8px', letterSpacing: '-0.01em' }}>
                      {sec.title}
                    </h3>
                    <div style={{ fontSize: 14, lineHeight: 1.65, color: 'var(--ink-2)', whiteSpace: 'pre-wrap' }}>
                      {sec.body.split('**').map((part, idx) =>
                        idx % 2 === 1 ? <strong key={idx}>{part}</strong> : <React.Fragment key={idx}>{part}</React.Fragment>
                      )}
                    </div>
                  </div>
                ))}
              </article>
              <div style={{
                display: 'flex', gap: 18, flexWrap: 'wrap',
                fontSize: 11, color: 'var(--mute-2)', marginTop: 10,
              }}>
                <span className="mono">in <span style={{ color: 'var(--ink)' }}>2 847</span></span>
                <span className="mono">out <span style={{ color: 'var(--ink)' }}>1 230</span></span>
                <span className="mono">cache r <span style={{ color: 'var(--ink)' }}>2 600</span></span>
              </div>
            </Section>
          </div>
        </div>
      )}

      {busy && (
        <div className="card" style={{ padding: 40, textAlign: 'center', color: 'var(--mute)' }}>
          <div className="serif" style={{ fontSize: 24, color: 'var(--ink)', marginBottom: 6 }}>
            Достаю транскрипт…
          </div>
          <div style={{ fontSize: 13 }}>Supadata → Claude. Обычно 5–15 секунд.</div>
        </div>
      )}

      {/* History */}
      <Section label={t.history + ' · ' + SAMPLE_VIDEOS.length} right={
        <button className="btn btn-ghost" style={{ padding: '4px 10px', fontSize: 12, color: 'var(--mute)' }}>обновить</button>
      } style={{ marginTop: 56 }}>
        <div className="card" style={{ overflow: 'hidden' }}>
          {SAMPLE_VIDEOS.map((v, i) => (
            <button key={v.id} onClick={() => setActiveVideo(v)}
              className="row-h"
              style={{
                width: '100%', padding: '14px 20px', textAlign: 'left',
                display: 'grid', gridTemplateColumns: '1fr auto auto auto auto',
                gap: 24, alignItems: 'center',
                borderTop: i === 0 ? 'none' : '1px solid var(--line)',
                background: activeVideo.id === v.id ? 'var(--panel)' : 'transparent',
              }}>
              <div style={{ minWidth: 0 }}>
                <div style={{
                  fontSize: 14.5, fontWeight: 500, marginBottom: 2,
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>{v.title}</div>
                <div className="mono" style={{ fontSize: 11, color: 'var(--mute-2)' }}>{v.id}</div>
              </div>
              <div className="mono" style={{ fontSize: 11.5, color: 'var(--mute)' }}>
                {fmtDur(v.duration)}
              </div>
              <div>
                <Chip><span className="mono" style={{ fontSize: 10.5 }}>{v.language}</span></Chip>
              </div>
              <div style={{ fontSize: 12, color: 'var(--mute)' }}>
                <span style={{ color: 'var(--ink)' }}>{v.briefs}</span> бриф{v.briefs > 1 ? 'а' : ''}
              </div>
              <div className="mono" style={{ fontSize: 12, color: 'var(--ink)', width: 72, textAlign: 'right' }}>
                ${v.cost.toFixed(4)}
              </div>
            </button>
          ))}
        </div>
      </Section>

      <style>{`@keyframes spin { to { transform: rotate(360deg) } }`}</style>
    </div>
  );
}

function Inline({ label, children }) {
  return (
    <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <span style={{ color: 'var(--mute)' }}>{label}</span>
      {children}
    </label>
  );
}

function SelectMini({ value, onChange, options }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}
      style={{
        border: 'none', borderBottom: '1px solid var(--line-2)',
        background: 'transparent', padding: '2px 16px 2px 4px',
        fontSize: 13, color: 'var(--ink)', cursor: 'pointer',
        appearance: 'none',
        backgroundImage: 'url("data:image/svg+xml;utf8,<svg xmlns=\'http://www.w3.org/2000/svg\' width=\'8\' height=\'8\' viewBox=\'0 0 8 8\'><path d=\'M1 3l3 3 3-3\' stroke=\'%239A959E\' fill=\'none\' stroke-width=\'1.2\'/></svg>")',
        backgroundRepeat: 'no-repeat', backgroundPosition: 'right 2px center',
      }}>
      {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
    </select>
  );
}

function CheckMini({ checked, onChange, children }) {
  return (
    <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span>{children}</span>
    </label>
  );
}

Object.assign(window, { VideoTab, Inline, SelectMini, CheckMini });
