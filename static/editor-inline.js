/* AI Editor inline — adds action buttons to each news item card
 * and handles preview → apply flow.
 *
 * Integration: renders into an element with class `editor-actions-slot` on
 * each news-item card. Alpine renders the cards; this script finds slots
 * after Alpine mounts (via MutationObserver) and attaches.
 */

(function () {
  const ACTIONS = [
    { key: 'improve_headline', label: '✨ Заголовок',   quick: true  },
    { key: 'rewrite_quote',    label: '✍ Цитата',      quick: true  },
    { key: 'expand_text',      label: '📝 Расширить',  quick: true  },
    { key: 'suggest_tags',     label: '🏷 Теги',        quick: true  },
    { key: 'regenerate_image', label: '🖼 Картинка',   quick: false },
  ];

  async function runTool(action, itemId) {
    const resp = await fetch('/chat/editor', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question: `call tool ${action} on item ${itemId}`,
        item_id: itemId,
        auto_confirm: false,
      }),
    });
    if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);

    // Parse SSE stream; keep last tool_result with ok:true.
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
      buf = chunks.pop();  // keep partial tail
      for (const chunk of chunks) {
        if (!chunk.startsWith('data: ')) continue;
        let ev;
        try { ev = JSON.parse(chunk.slice(6)); } catch (_) { continue; }
        if (ev.type === 'tool_result' && ev.result && ev.result.ok) {
          preview = ev.result;
        } else if (ev.type === 'error') {
          errMsg = ev.msg || 'stream error';
        } else if (ev.type === 'tool_result' && ev.result && ev.result.ok === false) {
          errMsg = ev.result.error || 'tool error';
        }
      }
    }

    if (!preview) {
      throw new Error(errMsg || 'No preview returned from editor');
    }
    return preview;
  }

  function renderDiff(preview) {
    const old_ = preview.old == null ? '' : String(preview.old);
    const fresh = preview.new == null
      ? (preview.suggestions ? preview.suggestions.join(', ') : '')
      : (Array.isArray(preview.new) ? preview.new.join(', ') : String(preview.new));
    const box = document.createElement('div');
    box.className = 'editor-preview';
    box.innerHTML = `
      <div class="editor-preview-diff">
        <div class="editor-preview-before"><span class="editor-preview-label">До</span><span class="editor-preview-text"></span></div>
        <div class="editor-preview-after"><span class="editor-preview-label">После</span><span class="editor-preview-text"></span></div>
      </div>
      <div class="editor-preview-actions">
        <button type="button" data-role="apply">✓ Применить</button>
        <button type="button" data-role="cancel">✕ Отмена</button>
      </div>
    `;
    box.querySelector('.editor-preview-before .editor-preview-text').textContent = old_;
    box.querySelector('.editor-preview-after  .editor-preview-text').textContent = fresh;
    return box;
  }

  async function applyEdit(preview) {
    // Value shape differs by field:
    //   - headline/quote/expandedText → string
    //   - tags → array of strings (server JSON-encodes)
    //   - imageId → concept string
    let value;
    if (preview.field === 'tags') {
      value = Array.isArray(preview.new) ? preview.new : (preview.suggestions || []);
    } else if (preview.field === 'imageId') {
      value = preview.new_concept || preview.new || '';
    } else {
      value = preview.new;
    }

    const r = await fetch('/chat/editor/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        item_id: preview.item_id,
        field: preview.field,
        value,
        tool_call_id: preview.tool_call_id,
      }),
    });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  }

  function updateCardAfterApply(card, preview) {
    // Best-effort: find a DOM element with data-field matching and update its text.
    // If no such hook exists (usual for Alpine-rendered cards), trigger a refetch
    // via a CustomEvent the Alpine app listens to.
    const target = card.querySelector(`[data-field="${preview.field}"]`);
    if (target && typeof preview.new === 'string') {
      target.textContent = preview.new;
    }
    // Signal the app to refresh data (optional — Alpine may handle its own polling).
    card.dispatchEvent(new CustomEvent('editor-applied', {
      bubbles: true,
      detail: { item_id: preview.item_id, field: preview.field, value: preview.new },
    }));
  }

  function attachActions(slot, itemId, card) {
    if (slot.dataset.editorAttached === '1') return;
    slot.dataset.editorAttached = '1';

    const bar = document.createElement('div');
    bar.className = 'editor-actions';
    for (const a of ACTIONS) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.dataset.action = a.key;
      btn.textContent = a.label;
      bar.appendChild(btn);
    }
    slot.appendChild(bar);

    bar.addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-action]');
      if (!btn) return;
      const origLabel = btn.textContent;
      btn.disabled = true;
      btn.textContent = '…';
      try {
        const preview = await runTool(btn.dataset.action, Number(itemId));
        const diffEl = renderDiff(preview);
        slot.appendChild(diffEl);
        diffEl.querySelector('[data-role="cancel"]').onclick = () => diffEl.remove();
        diffEl.querySelector('[data-role="apply"]').onclick = async () => {
          try {
            await applyEdit(preview);
            updateCardAfterApply(card, preview);
            diffEl.remove();
          } catch (err) {
            alert('Не удалось применить: ' + err.message);
          }
        };
      } catch (err) {
        alert('Ошибка: ' + err.message);
      } finally {
        btn.disabled = false;
        btn.textContent = origLabel;
      }
    });
  }

  // Find all slots rendered so far, and watch for new ones added by Alpine.
  function scan() {
    for (const slot of document.querySelectorAll('.editor-actions-slot:not([data-editor-attached="1"])')) {
      const itemId = slot.dataset.itemId;
      if (!itemId) continue;
      const card = slot.closest('[data-news-card]') || slot.parentElement;
      attachActions(slot, itemId, card);
    }
  }

  function init() {
    scan();
    const obs = new MutationObserver(() => scan());
    obs.observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
