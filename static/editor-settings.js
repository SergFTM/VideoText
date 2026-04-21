/* AI Editor settings page — history of edit sessions + editor defaults.
 *
 * Bootstraps when the #editor tab is activated. Calls:
 *   GET  /chat/editor/sessions  — history
 *   GET  /settings              — current defaults
 *   POST /settings              — save defaults
 */
(function () {
  async function loadHistory() {
    const box = document.getElementById('editor-actions-history');
    if (!box) return;
    try {
      const data = await fetch('/chat/editor/sessions?limit=50').then(r => r.json());
      if (!data.length) {
        box.innerHTML = '<em>История пуста.</em>';
        return;
      }
      box.innerHTML = data.map(s => `
        <div class="editor-history-row">
          <div class="editor-history-title">${escapeHtml(s.title || 'Без темы')}</div>
          <div class="editor-history-meta">${escapeHtml(s.updated_at)}</div>
        </div>
      `).join('');
    } catch (e) {
      box.innerHTML = `<em>Не удалось загрузить: ${escapeHtml(e.message || String(e))}</em>`;
    }
  }

  async function loadDefaults() {
    const box = document.getElementById('editor-defaults');
    if (!box) return;
    try {
      const settings = await fetch('/settings').then(r => r.json());
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
      const select = document.getElementById('editor-length');
      if (settings.editor_default_length) select.value = settings.editor_default_length;

      document.getElementById('editor-save-defaults').onclick = async () => {
        const hint = document.getElementById('editor-save-hint');
        hint.textContent = 'Сохраняю…';
        try {
          await fetch('/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              editor_default_style:  document.getElementById('editor-style').value,
              editor_default_length: document.getElementById('editor-length').value,
            }),
          });
          hint.textContent = '✓ Сохранено';
          setTimeout(() => { hint.textContent = ''; }, 2000);
        } catch (e) {
          hint.textContent = '✕ Ошибка: ' + (e.message || e);
        }
      };
    } catch (e) {
      box.innerHTML = `<em>Не удалось загрузить: ${escapeHtml(e.message || String(e))}</em>`;
    }
  }

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  }
  function escapeAttr(s) {
    return escapeHtml(s).replace(/"/g, '&quot;');
  }

  function init() {
    // Alpine uses x-show — the editor section may render hidden. We load data
    // the first time the tab is shown, then cache.
    let loaded = false;
    const section = document.getElementById('editor-section');
    if (!section) return;

    // MutationObserver: watch for x-show / style.display changes on the section.
    const trigger = () => {
      if (loaded) return;
      if (!section.offsetParent) return;  // still hidden
      loaded = true;
      loadHistory();
      loadDefaults();
    };
    const obs = new MutationObserver(trigger);
    obs.observe(section, { attributes: true, attributeFilter: ['style', 'class'] });
    trigger();  // in case it's already visible on load
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
