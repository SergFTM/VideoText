// Tab: News
function NewsTab({ t, lang }) {
  const [news, setNews] = React.useState(SAMPLE_NEWS);
  const [filter, setFilter] = React.useState('draft');
  const [openItem, setOpenItem] = React.useState(null);
  const [hideDupes, setHideDupes] = React.useState(false);

  const filtered = news.filter(n => {
    if (filter !== 'all' && n.status !== filter) return false;
    if (hideDupes && n.isDuplicate) return false;
    return true;
  });

  function setStatus(id, status) {
    setNews(news.map(n => n.id === id ? { ...n, status } : n));
  }

  const counts = {
    all: news.length,
    draft: news.filter(n => n.status === 'draft').length,
    approved: news.filter(n => n.status === 'approved').length,
    rejected: news.filter(n => n.status === 'rejected').length,
  };

  return (
    <div className="fade-in">
      <EditorialHeader
        eyebrow="Лента новостей"
        title="Сегодня —"
        accent={counts.draft + ' на столе редактора.'}
        sub={lang === 'ru'
          ? 'Каждая новость вытащена из live-эфира, разобрана на headline + quote + категорию. Одобри, отклони или перенастрой.'
          : 'Every story was pulled live, split into headline + quote + category. Approve, reject, or tune.'}
      />

      {/* Filter tabs */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 18, flexWrap: 'wrap' }}>
        {[
          ['draft', 'Модерация'],
          ['approved', 'Одобрено'],
          ['rejected', 'Отклонено'],
          ['all', 'Всё'],
        ].map(([id, lbl]) => (
          <button key={id} onClick={() => setFilter(id)}
            style={{
              padding: '6px 12px', borderRadius: 8, fontSize: 13,
              background: filter === id ? 'var(--ink)' : 'var(--surface)',
              color: filter === id ? '#fff' : 'var(--mute)',
              border: '1px solid ' + (filter === id ? 'var(--ink)' : 'var(--line)'),
              fontWeight: 500,
            }}>
            {lbl}
            <span className="mono" style={{
              marginLeft: 8, fontSize: 10.5,
              color: filter === id ? 'rgba(255,255,255,0.6)' : 'var(--mute-2)',
            }}>{counts[id]}</span>
          </button>
        ))}
        <div style={{ flex: 1 }} />
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: 'var(--mute)' }}>
          <input type="checkbox" checked={hideDupes} onChange={(e) => setHideDupes(e.target.checked)} />
          Скрыть дубликаты
        </label>
        <button className="btn" style={{ padding: '6px 12px', fontSize: 12 }}>
          Экспорт ↓
        </button>
      </div>

      {/* Cards grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 16 }}>
        {filtered.map(n => (
          <NewsCard key={n.id} item={n} onOpen={() => setOpenItem(n)}
            onApprove={() => setStatus(n.id, 'approved')}
            onReject={() => setStatus(n.id, 'rejected')} />
        ))}
      </div>

      {filtered.length === 0 && (
        <div className="card" style={{ padding: 60, textAlign: 'center', color: 'var(--mute)' }}>
          <div className="serif" style={{ fontSize: 24, color: 'var(--ink)', marginBottom: 6 }}>Пусто в этой вкладке</div>
          <div style={{ fontSize: 13 }}>Новости появятся по мере обработки чанков активных стримов.</div>
        </div>
      )}

      {/* Detail modal */}
      <Modal open={!!openItem} onClose={() => setOpenItem(null)} width={780}>
        {openItem && <NewsDetail item={openItem} onClose={() => setOpenItem(null)}
          onApprove={() => { setStatus(openItem.id, 'approved'); setOpenItem(null); }}
          onReject={() => { setStatus(openItem.id, 'rejected'); setOpenItem(null); }} />}
      </Modal>
    </div>
  );
}

function NewsCard({ item, onOpen, onApprove, onReject }) {
  const cat = CATEGORY_COLORS[item.category] || CATEGORY_COLORS.other;
  return (
    <article className="card" style={{
      padding: 18, cursor: 'pointer', display: 'flex', flexDirection: 'column',
      opacity: item.isDuplicate ? 0.65 : 1, position: 'relative',
    }} onClick={onOpen}>
      {/* Top meta */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
        <Chip color={cat} style={{ textTransform: 'uppercase', letterSpacing: '0.08em', fontSize: 10 }}>
          {item.category}
        </Chip>
        <span className="mono" style={{ fontSize: 11, color: 'var(--mute)' }}>{item.source}</span>
        <span style={{ color: 'var(--mute-2)' }}>·</span>
        <span className="mono" style={{ fontSize: 11, color: 'var(--mute)' }}>{item.time}</span>
        <div style={{ flex: 1 }} />
        <StatusChip status={item.status} />
      </div>

      {/* Headline */}
      <h3 className="serif" style={{
        fontSize: 22, lineHeight: 1.2, letterSpacing: '-0.015em',
        margin: '0 0 10px', color: 'var(--ink)',
      }}>
        {item.headline}
      </h3>

      {/* Quote */}
      <blockquote style={{
        margin: '0 0 14px', padding: '8px 0 8px 14px',
        borderLeft: '2px solid var(--acc)',
        fontSize: 13.5, lineHeight: 1.55, color: 'var(--ink-2)',
        fontStyle: 'italic',
      }}>
        «{item.quote}»
      </blockquote>

      {/* Tags */}
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 14 }}>
        {item.tags.map(tag => (
          <span key={tag} className="mono" style={{
            fontSize: 10.5, color: 'var(--mute)',
          }}>#{tag}</span>
        ))}
      </div>

      {/* Footer */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10, marginTop: 'auto',
        paddingTop: 12, borderTop: '1px solid var(--line)',
      }}>
        {/* Confidence bar */}
        <div style={{ flex: 1 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10.5, color: 'var(--mute)', marginBottom: 3 }}>
            <span>confidence</span>
            <span className="mono">{(item.confidence * 100).toFixed(0)}%</span>
          </div>
          <div style={{ height: 3, background: 'var(--panel)', borderRadius: 99, overflow: 'hidden' }}>
            <div style={{
              width: (item.confidence * 100) + '%', height: '100%',
              background: item.confidence > 0.9 ? 'var(--ok)' : item.confidence > 0.8 ? 'var(--acc)' : 'var(--warn)',
            }} />
          </div>
        </div>

        {item.status === 'draft' && (
          <div style={{ display: 'flex', gap: 4 }}>
            <button className="btn btn-ghost" style={{ padding: '4px 8px', fontSize: 11, color: 'var(--bad)' }}
              onClick={(e) => { e.stopPropagation(); onReject(); }}>
              <Icon.x /> reject
            </button>
            <button className="btn btn-acc" style={{ padding: '4px 10px', fontSize: 11 }}
              onClick={(e) => { e.stopPropagation(); onApprove(); }}>
              <Icon.check /> approve
            </button>
          </div>
        )}
      </div>

      {item.isDuplicate && (
        <div style={{
          position: 'absolute', top: 14, right: 14,
          fontSize: 10, padding: '2px 8px', borderRadius: 99,
          background: 'var(--warn-soft)', color: 'oklch(0.40 0.14 75)',
          fontFamily: "'JetBrains Mono', monospace",
        }}>
          🔁 dup {(item.dupSim * 100).toFixed(0)}%
        </div>
      )}
    </article>
  );
}

function NewsDetail({ item, onClose, onApprove, onReject }) {
  const cat = CATEGORY_COLORS[item.category] || CATEGORY_COLORS.other;
  return (
    <div>
      <div style={{
        padding: '18px 24px', display: 'flex', alignItems: 'center', gap: 10,
        borderBottom: '1px solid var(--line)',
      }}>
        <Chip color={cat} style={{ textTransform: 'uppercase', letterSpacing: '0.08em', fontSize: 10 }}>
          {item.category}
        </Chip>
        <span className="mono" style={{ fontSize: 11.5, color: 'var(--mute)' }}>
          {item.source}{item.speaker ? ' · ' + item.speaker : ''} · {item.time}
        </span>
        <div style={{ flex: 1 }} />
        <StatusChip status={item.status} />
        <button className="btn btn-ghost" style={{ padding: '4px 8px' }} onClick={onClose}>
          <Icon.x />
        </button>
      </div>

      <div style={{ padding: '28px 32px', overflowY: 'auto' }}>
        <h2 className="serif" style={{ fontSize: 34, lineHeight: 1.1, letterSpacing: '-0.02em', margin: '0 0 18px' }}>
          {item.headline}
        </h2>

        {/* Image placeholder */}
        <div className="placeholder-img" style={{ height: 200, borderRadius: 10, marginBottom: 18 }}>
          концепт: {item.concept}
        </div>

        <blockquote className="serif" style={{
          margin: '0 0 22px', padding: '0 0 0 20px',
          borderLeft: '3px solid var(--acc)',
          fontSize: 20, fontStyle: 'italic', lineHeight: 1.35, color: 'var(--ink)',
        }}>
          «{item.quote}»
        </blockquote>

        <div style={{
          fontSize: 14, lineHeight: 1.7, color: 'var(--ink-2)', marginBottom: 24,
        }}>
          <p style={{ margin: '0 0 12px' }}>
            Расширенный текст появится после обогащения через GPT-4o mini. Claude уже разметил утверждение по категории
            <em> {item.category}</em>, достал ключевые теги и оценил достоверность в
            <strong> {(item.confidence * 100).toFixed(0)}%</strong>.
          </p>
          <p style={{ margin: 0, color: 'var(--mute)' }}>
            Чанк-источник: chunk #47 стрима «{item.source}», смещение 42мин 18сек от начала эфира.
          </p>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 16,
          padding: '16px 0', borderTop: '1px solid var(--line)', borderBottom: '1px solid var(--line)',
        }}>
          <Meta label="Теги" value={<div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            {item.tags.map(tg => <span key={tg} className="mono" style={{ fontSize: 11, color: 'var(--mute)' }}>#{tg}</span>)}
          </div>} />
          <Meta label="Длительность" value={<span className="mono">{item.duration}</span>} />
          <Meta label="Концепт" value={item.concept} />
        </div>
      </div>

      {/* Action bar */}
      <div style={{
        padding: '14px 24px', display: 'flex', gap: 10,
        borderTop: '1px solid var(--line)', background: 'var(--panel)',
      }}>
        <button className="btn btn-ghost" style={{ color: 'var(--mute)' }}>
          <Icon.spark /> Обогатить
        </button>
        <div style={{ flex: 1 }} />
        {item.status === 'draft' && (
          <>
            <button className="btn" style={{ color: 'var(--bad)' }} onClick={onReject}>
              <Icon.x /> Отклонить
            </button>
            <button className="btn btn-acc" onClick={onApprove}>
              <Icon.check /> Одобрить
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function Meta({ label, value }) {
  return (
    <div>
      <div className="label-xs" style={{ marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 13 }}>{value}</div>
    </div>
  );
}

Object.assign(window, { NewsTab });
