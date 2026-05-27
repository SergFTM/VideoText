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
    // Transcripts mode
    source: 'news',
    txVideos: [],
    txSelectedId: null,
    txOriginal: '',
    txVersions: [],
    txShowing: 'current',
    // Artifacts mode
    afVideos: [],
    afSelectedId: null,
    afMode: 'research',
    afExpansions: {},
    afPoll: null,
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

  // ── Transcripts mode ───────────────────────────────────────────────

  function setSource(src) {
    state.source = src;
    const on = (b, active) => {
      if (!b) return;
      b.style.background = active ? 'var(--ink)' : 'white';
      b.style.color = active ? 'white' : 'var(--ink)';
    };
    on($('editor-src-news'), src === 'news');
    on($('editor-src-transcripts'), src === 'transcripts');
    on($('editor-src-artifacts'), src === 'artifacts');
    $('editor-pane-news').style.display = src === 'news' ? '' : 'none';
    $('editor-pane-transcripts').style.display = src === 'transcripts' ? '' : 'none';
    $('editor-pane-artifacts').style.display = src === 'artifacts' ? '' : 'none';
    if (src === 'transcripts') loadTranscriptVideos();
    if (src === 'artifacts') loadArtifactVideos();
    if (src !== 'artifacts') stopArtifactPolling();
  }

  async function loadTranscriptVideos() {
    const list = $('tx-videos-list');
    list.innerHTML = '<em>Загружаю…</em>';
    try {
      state.txVideos = await fetchJSON('/videos');
      $('tx-videos-meta').textContent = `${state.txVideos.length} видео`;
      list.innerHTML = state.txVideos.map(v => `
        <div class="editor-item-row ${v.id === state.txSelectedId ? 'is-selected' : ''}" data-id="${v.id}">
          <div class="editor-item-row-title">${escapeHtml(v.title || v.id)}</div>
          <div class="editor-item-row-meta">${escapeHtml(v.id)}</div>
        </div>`).join('');
      list.querySelectorAll('.editor-item-row').forEach(el =>
        el.addEventListener('click', () => selectTranscriptVideo(el.dataset.id)));
    } catch (e) { list.innerHTML = `<em>Ошибка: ${escapeHtml(e.message)}</em>`; }
  }

  async function selectTranscriptVideo(id) {
    state.txSelectedId = id;
    $('tx-empty').style.display = 'none';
    $('tx-item').style.display = 'block';
    $('tx-title').textContent = 'Загружаю…';
    $('tx-preview').innerHTML = '';
    try {
      const t = await fetchJSON(`/videos/${id}/transcript`);
      state.txOriginal = t.original_md;
      $('tx-title').textContent = t.title || id;
      $('tx-meta').textContent = `оригинал ${t.original_chars} симв · версий ${t.versions}`;
      await loadTranscriptVersions(id);
      showTranscript('current');
      renderTranscriptActions();
    } catch (e) { $('tx-title').textContent = 'Ошибка: ' + e.message; }
  }

  async function loadTranscriptVersions(id) {
    state.txVersions = await fetchJSON(`/videos/${id}/transcript/edits`);
    renderTranscriptVersions();
  }

  function currentTranscriptText() {
    return state.txVersions.length ? state.txVersions[0].content_md : state.txOriginal;
  }

  function showTranscript(which) {
    state.txShowing = which;
    const on = (b, active) => {
      if (!b) return;
      b.style.background = active ? 'var(--ink)' : 'white';
      b.style.color = active ? 'white' : 'var(--ink)';
    };
    on($('tx-show-current'), which === 'current');
    on($('tx-show-original'), which === 'original');
    $('tx-text').textContent = which === 'original' ? state.txOriginal : currentTranscriptText();
  }

  function renderTranscriptVersions() {
    const box = $('tx-versions');
    if (!state.txVersions.length) { box.innerHTML = '<em>Версий пока нет — это оригинал.</em>'; return; }
    box.innerHTML = '<div class="editor-items-meta">версии</div>' + state.txVersions.map(v => `
      <div style="display:flex; gap:10px; align-items:center; padding:4px 0; font-size:12px;">
        <span>v${v.version} · ${escapeHtml(v.op)} · ${v.input_chars} симв</span>
        <button type="button" data-roll="${v.version}" style="cursor:pointer;">откатить</button>
        <a href="/videos/${state.txSelectedId}/transcript/edits/${v.version}.md" target="_blank">.md</a>
        <a href="/videos/${state.txSelectedId}/transcript/edits/${v.version}.pdf" target="_blank">.pdf</a>
      </div>`).join('');
    box.querySelectorAll('[data-roll]').forEach(el =>
      el.addEventListener('click', () => rollbackTranscript(Number(el.dataset.roll))));
  }

  async function rollbackTranscript(version) {
    await fetchJSON(`/videos/${state.txSelectedId}/transcript/edits/${version}/rollback`, { method: 'POST' });
    await loadTranscriptVersions(state.txSelectedId);
    showTranscript('current');
  }

  function renderTranscriptActions() {
    $('tx-actions').innerHTML = `
      <div class="editor-actions-bar">
        <button type="button" data-op="clean">почистить</button>
        <button type="button" data-op="structure">структурировать</button>
        <button type="button" data-op="improve">улучшить интерпретацию</button>
        <button type="button" data-op="expand_idea">расширить идею</button>
      </div>
      <input id="tx-instruction" type="text" placeholder="ТЗ / инструкция (необязательно)"
             style="width:100%; margin:8px 0; padding:6px 10px; border:1px solid var(--line); border-radius:6px; font-size:13px;">
      <div style="display:flex; gap:8px; align-items:center; margin-bottom:8px;">
        <label style="font-size:12px; color:var(--mute);">модель:</label>
        <select id="tx-model" style="font-size:12px; padding:4px 8px;"></select>
      </div>
      <div style="display:flex; gap:6px;">
        <input id="tx-chat-input" type="text" placeholder="чат: что сделать с текстом…"
               style="flex:1; padding:6px 10px; border:1px solid var(--line); border-radius:6px; font-size:13px;">
        <button type="button" id="tx-chat-send" style="cursor:pointer; padding:6px 14px;">→</button>
      </div>`;
    const sel = $('tx-model');
    sel.innerHTML = '<option value="claude-sonnet-4-6">claude-sonnet-4-6</option>';
    fetchJSON('/local-llm/models').then(d => {
      (d.models || []).forEach(m => {
        const o = document.createElement('option'); o.value = m.name; o.textContent = m.name; sel.appendChild(o);
      });
    }).catch(() => {});
    $('tx-actions').querySelectorAll('[data-op]').forEach(el =>
      el.addEventListener('click', () => runTranscriptEdit(el.dataset.op, $('tx-instruction').value)));
    $('tx-chat-send').addEventListener('click', () =>
      runTranscriptEdit('chat', $('tx-chat-input').value));
  }

  // Minimal line-level diff via LCS -> [{t:' '|'+'|'-', line}]
  function lineDiff(a, b) {
    const A = a.split('\n'), B = b.split('\n');
    const n = A.length, m = B.length;
    const dp = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
    for (let i = n - 1; i >= 0; i--)
      for (let j = m - 1; j >= 0; j--)
        dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    const out = []; let i = 0, j = 0;
    while (i < n && j < m) {
      if (A[i] === B[j]) { out.push({ t: ' ', line: A[i] }); i++; j++; }
      else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ t: '-', line: A[i] }); i++; }
      else { out.push({ t: '+', line: B[j] }); j++; }
    }
    while (i < n) out.push({ t: '-', line: A[i++] });
    while (j < m) out.push({ t: '+', line: B[j++] });
    return out;
  }

  function renderDiffHtml(oldText, newText) {
    return lineDiff(oldText, newText).map(d => {
      const style = d.t === '+' ? 'background:#f0fdf4;color:#166534;'
                  : d.t === '-' ? 'background:#fef2f2;color:#b91c1c;'
                  : 'color:var(--mute);';
      return `<div style="${style}">${escapeHtml(d.t + ' ' + d.line)}</div>`;
    }).join('');
  }

  async function runTranscriptEdit(op, instruction) {
    const id = state.txSelectedId;
    if (!id) return;
    const baseVersion = state.txVersions.length ? state.txVersions[0].version : null;
    const model = $('tx-model').value;
    const box = $('tx-preview');
    box.innerHTML = '<div class="editor-preview-loading">Генерирую…</div>';

    let proposed = '';
    try {
      const resp = await fetch(`/videos/${id}/transcript/edit`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ op, instruction, model, base_version: baseVersion }),
      });
      if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      box.innerHTML = '<pre id="tx-proposed" class="tx-text" style="white-space:pre-wrap; font-family:inherit; font-size:13px; line-height:1.55; max-height:340px; overflow-y:auto; background:var(--panel); padding:14px; border-radius:8px;"></pre>';
      const out = $('tx-proposed');
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const chunks = buf.split('\n\n'); buf = chunks.pop();
        for (const chunk of chunks) {
          if (!chunk.startsWith('data: ')) continue;
          let ev; try { ev = JSON.parse(chunk.slice(6)); } catch (_) { continue; }
          if (ev.type === 'delta') { proposed += ev.text; out.textContent = proposed; }
          else if (ev.type === 'error') throw new Error(ev.msg);
        }
      }
      const bar = document.createElement('div');
      bar.className = 'editor-preview-actions';
      bar.style.cssText = 'display:flex; gap:8px; margin-top:8px;';
      bar.innerHTML = `<button type="button" data-role="apply">✓ применить (новая версия)</button>
                       <button type="button" data-role="diff">показать дифф</button>
                       <button type="button" data-role="cancel">✕ отмена</button>`;
      box.appendChild(bar);
      bar.querySelector('[data-role="cancel"]').onclick = () => { box.innerHTML = ''; };
      bar.querySelector('[data-role="apply"]').onclick = () =>
        applyTranscriptEdit({ op, instruction, content_md: proposed, model, from_version: baseVersion });
      let showingDiff = false;
      bar.querySelector('[data-role="diff"]').onclick = (ev) => {
        showingDiff = !showingDiff;
        ev.target.textContent = showingDiff ? 'показать текст' : 'показать дифф';
        if (showingDiff) out.innerHTML = renderDiffHtml(currentTranscriptText(), proposed);
        else out.textContent = proposed;
      };
    } catch (e) {
      box.innerHTML = `<div class="editor-preview-error">Ошибка: ${escapeHtml(e.message)}</div>`;
    }
  }

  async function applyTranscriptEdit(payload) {
    const id = state.txSelectedId;
    await fetchJSON(`/videos/${id}/transcript/edits/apply`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    $('tx-preview').innerHTML = '<div class="editor-preview-ok">✓ Применено</div>';
    await loadTranscriptVersions(id);
    showTranscript('current');
  }

  // ── Artifacts mode (durable expand) ────────────────────────────────

  const AF_MODES = [
    ['research', 'Ресерч'], ['report', 'Репорт'], ['spec', 'ТЗ'],
    ['uiux', 'UI/UX'], ['ai_algorithms', 'Алгоритмы'], ['ai_skills', 'AI-скиллы'],
  ];

  async function loadArtifactVideos() {
    const list = $('af-videos-list');
    list.innerHTML = '<em>Загружаю…</em>';
    try {
      state.afVideos = await fetchJSON('/videos');
      $('af-videos-meta').textContent = `${state.afVideos.length} видео`;
      list.innerHTML = state.afVideos.map(v => `
        <div class="editor-item-row ${v.id === state.afSelectedId ? 'is-selected' : ''}" data-id="${v.id}">
          <div class="editor-item-row-title">${escapeHtml(v.title || v.id)}</div>
          <div class="editor-item-row-meta">${escapeHtml(v.id)}</div>
        </div>`).join('');
      list.querySelectorAll('.editor-item-row').forEach(el =>
        el.addEventListener('click', () => selectArtifactVideo(el.dataset.id)));
    } catch (e) { list.innerHTML = `<em>Ошибка: ${escapeHtml(e.message)}</em>`; }
  }

  async function selectArtifactVideo(id) {
    stopArtifactPolling();
    state.afSelectedId = id;
    $('af-empty').style.display = 'none';
    $('af-item').style.display = 'block';
    const v = state.afVideos.find(x => x.id === id);
    $('af-title').textContent = v ? (v.title || id) : id;
    const sel = $('af-model');
    sel.innerHTML = '';
    fetchJSON('/local-llm/models').then(d => {
      (d.models || []).forEach(m => {
        const o = document.createElement('option'); o.value = m.name; o.textContent = m.name; sel.appendChild(o);
      });
    }).catch(() => {});
    renderArtifactModes();
    await loadArtifactExpansions(id);
    selectArtifactMode(state.afMode);
  }

  function renderArtifactModes() {
    $('af-modes').innerHTML = AF_MODES.map(([k, label]) =>
      `<button type="button" data-mode="${k}">${label}</button>`).join('');
    $('af-modes').querySelectorAll('[data-mode]').forEach(el =>
      el.addEventListener('click', () => selectArtifactMode(el.dataset.mode)));
  }

  async function loadArtifactExpansions(id) {
    const rows = await fetchJSON(`/videos/${id}/expansions`);
    state.afExpansions = {};
    rows.forEach(r => { state.afExpansions[r.mode] = r; });
  }

  function selectArtifactMode(mode) {
    stopArtifactPolling();
    state.afMode = mode;
    $('af-modes').querySelectorAll('[data-mode]').forEach(el =>
      el.style.fontWeight = el.dataset.mode === mode ? '700' : '400');
    const e = state.afExpansions[mode];
    renderArtifactStatus(e);
    $('af-text').textContent = e ? (e.content_md || '') : '';
    $('af-export').innerHTML = (e && e.status === 'done')
      ? `<a href="/videos/${state.afSelectedId}/expansions/${mode}.md" target="_blank">.md</a>
         &nbsp; <a href="/videos/${state.afSelectedId}/expansions/${mode}.pdf" target="_blank">.pdf</a>`
      : '';
    if (e && e.status === 'running') startArtifactPolling();
  }

  function renderArtifactStatus(e) {
    const box = $('af-status');
    if (!e) { box.textContent = 'нет — нажми «сгенерировать»'; return; }
    if (e.status === 'running') box.textContent = '⏳ генерируется… (можно уйти с экрана — процесс не прервётся)';
    else if (e.status === 'error') box.textContent = '⚠️ ошибка: ' + (e.error || '');
    else box.textContent = '✅ готово';
  }

  async function generateArtifact() {
    const id = state.afSelectedId, mode = state.afMode;
    if (!id) return;
    await fetchJSON(`/videos/${id}/expand-spec`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode, model: $('af-model').value, context: $('af-context').value }),
    });
    state.afExpansions[mode] = { mode, status: 'running', content_md: (state.afExpansions[mode] || {}).content_md || '' };
    renderArtifactStatus(state.afExpansions[mode]);
    startArtifactPolling();
  }

  function startArtifactPolling() {
    if (state.afPoll) return;
    state.afPoll = setInterval(async () => {
      const id = state.afSelectedId, mode = state.afMode;
      if (!id) return;
      try {
        const r = await fetch(`/videos/${id}/expansions/${mode}`);
        if (r.status === 404) { stopArtifactPolling(); return; }
        const e = await r.json();
        state.afExpansions[mode] = e;
        if (state.afMode === mode) selectArtifactModeView(e);
        if (e.status !== 'running') stopArtifactPolling();
      } catch (_) { /* transient */ }
    }, 2000);
  }

  function stopArtifactPolling() {
    if (state.afPoll) { clearInterval(state.afPoll); state.afPoll = null; }
  }

  // Update only the view for the already-selected mode (no re-trigger of polling).
  function selectArtifactModeView(e) {
    renderArtifactStatus(e);
    $('af-text').textContent = e.content_md || '';
    $('af-export').innerHTML = (e.status === 'done')
      ? `<a href="/videos/${state.afSelectedId}/expansions/${state.afMode}.md" target="_blank">.md</a>
         &nbsp; <a href="/videos/${state.afSelectedId}/expansions/${state.afMode}.pdf" target="_blank">.pdf</a>`
      : '';
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

    // Source toggle (News | Transcripts) + transcript view toggle.
    $('editor-src-news').addEventListener('click', () => setSource('news'));
    $('editor-src-transcripts').addEventListener('click', () => setSource('transcripts'));
    $('editor-src-artifacts').addEventListener('click', () => setSource('artifacts'));
    $('tx-show-current').addEventListener('click', () => showTranscript('current'));
    $('tx-show-original').addEventListener('click', () => showTranscript('original'));
    $('af-generate').addEventListener('click', () => generateArtifact());
  }

  async function bootstrap() {
    if (state.loaded) return;
    state.loaded = true;
    wire();
    setSource('news');  // sets initial active styling on the toggle
    await Promise.all([loadStreams(), loadItems(), loadAllSessionsHistory(), loadDefaults()]);
  }

  function init() {
    const section = document.getElementById('editor-section');
    if (!section) return;
    const trigger = () => { if (section.offsetParent) bootstrap(); };
    new MutationObserver(trigger).observe(section, { attributes: true, attributeFilter: ['style', 'class'] });
    trigger();
  }

  // Deep-link entry point for the "подробнее" button on the Видео tab.
  // bootstrap() runs lazily when the editor section first becomes visible.
  window.editorSelectVideo = (id) => {
    bootstrap().then(() => { setSource('transcripts'); selectTranscriptVideo(id); });
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
