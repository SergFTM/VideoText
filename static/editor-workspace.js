/* AI Editor workspace — 2-column page:
 *   LEFT: filters + items list
 *   RIGHT: selected item full view + ТЗ + AI actions + preview/apply + collapsed history/defaults
 *
 * Loads data lazily when #editor-section becomes visible. Minimal state:
 *   - selectedId: current item id
 *   - filters: {status, stream_id, q}
 *   - pendingPreview: null | {field, old, new, diff, tool_call_id, item_id, ...}
 */
(function () {
  const state = {
    loaded: false,
    selectedId: null,
    filters: { status: '', stream_id: '', q: '' },
    items: [],
    streamOptions: [],
    pendingPreview: null,
  };

  function $(id) { return document.getElementById(id); }
  function escapeHtml(s) { return String(s ?? '').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])); }
  function escapeAttr(s) { return escapeHtml(s).replace(/"/g, '&quot;'); }

  // ── Data loaders ────────────────────────────────────────────────

  async function fetchJSON(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  }

  async function loadStreams() {
    try {
      const streams = await fetchJSON('/streams');
      state.streamOptions = streams;
      const sel = $('editor-filter-stream');
      for (const s of streams) {
        const opt = document.createElement('option');
        opt.value = s.id;
        opt.textContent = s.channel_name || s.url;
        sel.appendChild(opt);
      }
    } catch (e) { /* best effort */ }
  }

  async function loadItems() {
    const q = new URLSearchParams();
    if (state.filters.status) q.set('status', state.filters.status);
    if (state.filters.stream_id) q.set('stream_id', state.filters.stream_id);
    if (state.filters.q) q.set('q', state.filters.q);
    q.set('limit', '100');
    const list = $('editor-items-list');
    list.innerHTML = '<em>Загружаю…</em>';
    try {
      state.items = await fetchJSON('/news-items?' + q.toString());
      renderItemsList();
    } catch (e) {
      list.innerHTML = `<em>Не удалось загрузить: ${escapeHtml(e.message)}</em>`;
    }
  }

  function renderItemsList() {
    const list = $('editor-items-list');
    $('editor-items-meta').textContent = `${state.items.length} items`;
    if (!state.items.length) {
      list.innerHTML = '<em>Нет итемов по этому фильтру.</em>';
      return;
    }
    list.innerHTML = state.items.map(it => {
      const selected = it.id === state.selectedId;
      return `
        <div class="editor-item-row ${selected ? 'is-selected' : ''}" data-id="${it.id}">
          <div class="editor-item-row-title">#${it.id} ${escapeHtml(it.headline)}</div>
          <div class="editor-item-row-meta">${escapeHtml(it.stream_name || '—')} · ${escapeHtml(it.status)}</div>
        </div>
      `;
    }).join('');
    list.querySelectorAll('.editor-item-row').forEach(el => {
      el.addEventListener('click', () => selectItem(Number(el.dataset.id)));
    });
  }

  async function selectItem(id) {
    state.selectedId = id;
    state.pendingPreview = null;
    $('editor-preview-container').innerHTML = '';
    renderItemsList();  // refresh selected state
    $('editor-empty').style.display = 'none';
    $('editor-item').style.display = 'block';

    $('editor-item-title').textContent = 'Загружаю item #' + id + '…';

    try {
      const item = await fetchJSON('/news-items/' + id);
      renderItem(item);
      loadItemHistory(id);  // fire-and-forget
    } catch (e) {
      $('editor-item-title').textContent = 'Ошибка: ' + e.message;
    }
  }

  function renderItem(item) {
    $('editor-item-title').textContent = `Item #${item.id} — ${item.stream_name || '—'}`;
    $('editor-item-status').textContent = item.status;
    $('editor-item-status').dataset.status = item.status;

    $('editor-field-headline').textContent = item.headline || '—';
    $('editor-field-quote').textContent = item.quote || '—';
    $('editor-field-expanded').innerHTML = item.expanded_text
      ? escapeHtml(item.expanded_text)
      : '<em>(не сгенерирован)</em>';
    $('editor-field-tags').textContent = (item.tags || []).join(', ') || '—';
    $('editor-field-attrib').textContent = item.attribution || '—';
    $('editor-field-category').textContent = item.category || '—';
    $('editor-field-confidence').textContent = (item.confidence != null ? item.confidence.toFixed(2) : '—');

    if (item.image && item.image.url) {
      $('editor-field-image-wrap').style.display = 'block';
      $('editor-field-image').src = item.image.url;
      $('editor-field-image-concept').textContent = item.image.concept || '';
    } else {
      $('editor-field-image-wrap').style.display = 'none';
    }
  }

  async function loadItemHistory(itemId) {
    const box = $('editor-item-history');
    box.innerHTML = '<em>—</em>';
    // No dedicated endpoint yet — filter all editor sessions by question-pattern.
    // For now show a placeholder. Full per-item history needs backend enhancement.
    box.innerHTML = '<em>Per-item история: пока не реализована (нужен отдельный endpoint).</em>';
  }

  async function loadAllSessionsHistory() {
    const box = $('editor-actions-history');
    try {
      const data = await fetchJSON('/chat/editor/sessions?limit=50');
      box.innerHTML = data.length
        ? data.map(s => `
            <div class="editor-history-row">
              <div class="editor-history-title">${escapeHtml(s.title || 'Без темы')}</div>
              <div class="editor-history-meta">${escapeHtml(s.updated_at)}</div>
            </div>
          `).join('')
        : '<em>История пуста.</em>';
    } catch (e) {
      box.innerHTML = `<em>Ошибка: ${escapeHtml(e.message)}</em>`;
    }
  }

  async function loadDefaults() {
    const box = $('editor-defaults');
    try {
      const settings = await fetchJSON('/settings');
      box.innerHTML = `
        <div class="editor-default-row">
          <label for="editor-style">Дефолтный стиль заголовков</label>
          <input id="editor-style" type="text" value="${escapeAttr(settings.editor_default_style || '')}" placeholder="например, «острее, короче»">
        </div>
        <div class="editor-default-row">
          <label for="editor-length">Дефолтная длина расширенного текста</label>
          <select id="editor-length">
            <option value="short">короткий</option>
            <option value="medium">средний</option>
            <option value="long">длинный</option>
          </select>
        </div>
        <button type="button" id="editor-save-defaults">Сохранить настройки</button>
        <div id="editor-save-hint" class="editor-save-hint"></div>
      `;
      if (settings.editor_default_length) $('editor-length').value = settings.editor_default_length;
      $('editor-save-defaults').onclick = async () => {
        const hint = $('editor-save-hint');
        hint.textContent = 'Сохраняю…';
        try {
          await fetchJSON('/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              editor_default_style: $('editor-style').value,
              editor_default_length: $('editor-length').value,
            }),
          });
          hint.textContent = '✓ Сохранено';
          setTimeout(() => { hint.textContent = ''; }, 2000);
        } catch (e) { hint.textContent = '✕ ' + e.message; }
      };
    } catch (e) {
      box.innerHTML = `<em>Ошибка: ${escapeHtml(e.message)}</em>`;
    }
  }

  // ── Tool invocation (SSE) ──────────────────────────────────────────

  async function runTool(tool) {
    if (!state.selectedId) return;
    const tzInput = $('editor-tz-input');
    const tz = (tzInput.value || '').trim();

    // Build a tool-directive question. The LLM will route to the right tool.
    // The ТЗ becomes the style/tone hint for rewrite tools.
    const question = tz
      ? `Используй инструмент ${tool} для item ${state.selectedId}. ТЗ: ${tz}`
      : `Используй инструмент ${tool} для item ${state.selectedId}.`;

    const previewBox = $('editor-preview-container');
    previewBox.innerHTML = `<div class="editor-preview-loading">Обрабатываю ${tool}…</div>`;

    try {
      const resp = await fetch('/chat/editor', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question,
          item_id: state.selectedId,
          auto_confirm: false,
        }),
      });
      if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      let preview = null;
      let errMsg = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const chunks = buf.split('\n\n');
        buf = chunks.pop();
        for (const chunk of chunks) {
          if (!chunk.startsWith('data: ')) continue;
          let ev;
          try { ev = JSON.parse(chunk.slice(6)); } catch (_) { continue; }
          if (ev.type === 'tool_result' && ev.result) {
            if (ev.result.ok) preview = ev.result;
            else errMsg = ev.result.error || 'tool error';
          } else if (ev.type === 'error') {
            errMsg = ev.msg || 'stream error';
          }
        }
      }

      if (!preview) throw new Error(errMsg || 'No preview returned');

      state.pendingPreview = preview;
      renderPreview(preview);
    } catch (e) {
      previewBox.innerHTML = `<div class="editor-preview-error">Ошибка: ${escapeHtml(e.message)}</div>`;
    }
  }

  function renderPreview(preview) {
    const box = $('editor-preview-container');
    const old_ = preview.old == null ? '' : String(preview.old);
    const fresh = preview.new == null
      ? (preview.suggestions ? preview.suggestions.join(', ') : '')
      : (Array.isArray(preview.new) ? preview.new.join(', ') : String(preview.new));

    box.innerHTML = `
      <div class="editor-preview-card">
        <div class="editor-preview-field">Поле: <b>${escapeHtml(preview.field || '?')}</b></div>
        <div class="editor-preview-diff">
          <div class="editor-preview-before"><span class="editor-preview-label">До</span><span class="editor-preview-text"></span></div>
          <div class="editor-preview-after"><span class="editor-preview-label">После</span><span class="editor-preview-text"></span></div>
        </div>
        <div class="editor-preview-actions">
          <button type="button" data-role="apply">✓ Применить</button>
          <button type="button" data-role="cancel">✕ Отмена</button>
        </div>
      </div>
    `;
    box.querySelector('.editor-preview-before .editor-preview-text').textContent = old_;
    box.querySelector('.editor-preview-after  .editor-preview-text').textContent = fresh;

    box.querySelector('[data-role="cancel"]').onclick = () => {
      state.pendingPreview = null;
      box.innerHTML = '';
    };
    box.querySelector('[data-role="apply"]').onclick = () => applyPreview();
  }

  async function applyPreview() {
    const p = state.pendingPreview;
    if (!p) return;
    const box = $('editor-preview-container');

    let value;
    if (p.field === 'tags') {
      value = Array.isArray(p.new) ? p.new : (p.suggestions || []);
    } else if (p.field === 'imageId') {
      value = p.new_concept || p.new || '';
    } else {
      value = p.new;
    }

    try {
      await fetchJSON('/chat/editor/apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          item_id: p.item_id,
          field: p.field,
          value,
          tool_call_id: p.tool_call_id,
        }),
      });
      // Re-fetch and re-render the item to reflect the change.
      state.pendingPreview = null;
      box.innerHTML = '<div class="editor-preview-ok">✓ Применено</div>';
      setTimeout(() => { box.innerHTML = ''; }, 1500);
      await selectItem(p.item_id);
    } catch (e) {
      box.innerHTML += `<div class="editor-preview-error">Не удалось применить: ${escapeHtml(e.message)}</div>`;
    }
  }

  // ── Wire filters + actions ─────────────────────────────────────────

  function wire() {
    $('editor-filter-status').addEventListener('change', (e) => {
      state.filters.status = e.target.value; loadItems();
    });
    $('editor-filter-stream').addEventListener('change', (e) => {
      state.filters.stream_id = e.target.value; loadItems();
    });
    let qTimer;
    $('editor-filter-q').addEventListener('input', (e) => {
      state.filters.q = e.target.value;
      clearTimeout(qTimer);
      qTimer = setTimeout(loadItems, 300);  // debounce
    });

    document.querySelectorAll('.editor-actions-bar button[data-tool]').forEach(btn => {
      btn.addEventListener('click', () => runTool(btn.dataset.tool));
    });
  }

  async function bootstrap() {
    if (state.loaded) return;
    state.loaded = true;
    wire();
    await Promise.all([loadStreams(), loadItems(), loadAllSessionsHistory(), loadDefaults()]);
  }

  function init() {
    const section = document.getElementById('editor-section');
    if (!section) return;
    const trigger = () => { if (section.offsetParent) bootstrap(); };
    new MutationObserver(trigger).observe(section, { attributes: true, attributeFilter: ['style', 'class'] });
    trigger();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
