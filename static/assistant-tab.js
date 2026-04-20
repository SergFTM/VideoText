// VideoText — full-page Assistant tab.
// Wraps the same /assistant/chat SSE protocol as the floating panel, plus
// session-history support (load/save sessions via /assistant/sessions).
// Floating panel stays as the quick-access affordance; this tab is for
// long conversations where the user wants to see history and switch threads.

function assistantTab() {
  return {
    // ── Conversation state
    sessions: [],            // [{id, title, updated_at}]
    activeSessionId: null,   // null = unsaved draft
    messages: [],            // [{role, content, tool_events?, cache_hit?, cost_usd?}]
    input: '',
    streaming: false,
    streamingStatus: '',
    lastContextChars: 0,
    totalCost: 0,

    suggestions: [
      'покажи самые дорогие брифы за неделю',
      'почему dedup не срабатывает на похожих новостях?',
      'как включить fastembed вместо openai?',
      'сколько GPU-памяти занимает distil-large-v3?',
    ],

    async init() {
      await this.loadSessions();
    },

    async loadSessions() {
      try {
        const r = await fetch('/assistant/sessions?limit=50');
        if (!r.ok) return;
        this.sessions = await r.json();
      } catch (_) { /* silent */ }
    },

    async openSession(id) {
      try {
        const r = await fetch('/assistant/sessions/' + id);
        if (!r.ok) return;
        const data = await r.json();
        this.activeSessionId = id;
        // Map DB messages → UI message shape
        this.messages = (data.messages || [])
          .filter(m => m.role !== 'tool')
          .map(m => ({
            role: m.role,
            content: m.content,
            cache_hit: m.cache_hit,
          }));
      } catch (_) { /* silent */ }
    },

    newChat() {
      this.activeSessionId = null;
      this.messages = [];
      this.input = '';
      this.totalCost = 0;
      this.lastContextChars = 0;
    },

    pickSuggestion(s) {
      this.input = s;
      this.send();
    },

    async send() {
      const q = this.input.trim();
      if (!q || this.streaming) return;
      this.input = '';
      this.messages.push({ role: 'user', content: q });
      this._scrollDown();

      this.streaming = true;
      this.streamingStatus = window.tr(window.app?.lang || 'ru', 'asst.thinking');

      const reply = { role: 'assistant', content: '', tool_events: [], cache_hit: false, cost_usd: 0 };
      this.messages.push(reply);

      try {
        const resp = await fetch('/assistant/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            question: q,
            session_id: this.activeSessionId,
            ui_context: { tab: 'assistant' },
          }),
        });
        if (!resp.ok || !resp.body) throw new Error('HTTP ' + resp.status);

        const reader = resp.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          const parts = buf.split('\n\n');
          buf = parts.pop() || '';
          for (const part of parts) {
            const line = part.split('\n').find(l => l.startsWith('data: '));
            if (!line) continue;
            let ev; try { ev = JSON.parse(line.slice(6)); } catch { continue; }
            this._applyEvent(ev, reply);
            this._scrollDown();
          }
        }
      } catch (e) {
        reply.content += '\n\n⚠ ошибка: ' + e.message;
      } finally {
        this.streaming = false;
        this.streamingStatus = '';
        this._scrollDown();
        // Refresh session list — backend may have just created one
        this.loadSessions();
      }
    },

    _applyEvent(ev, msg) {
      switch (ev.type) {
        case 'context':
          this.lastContextChars = ev.chars;
          this.streamingStatus = window.tr(window.app?.lang || 'ru', 'state.processing');
          break;
        case 'cache_hit':
          msg.cache_hit = true;
          msg.similarity = ev.similarity;
          msg.content = ev.answer;
          break;
        case 'text':
          msg.content += ev.delta;
          break;
        case 'tool_call':
          this.streamingStatus = (ev.name || 'tool') + '…';
          msg.tool_events.push({ id: Date.now() + Math.random(), name: ev.name, args: ev.args, result: null });
          break;
        case 'tool_result': {
          const last = [...msg.tool_events].reverse().find(e => e.name === ev.name && !e.result);
          if (last) last.result = ev.result;
          break;
        }
        case 'done':
          msg.cost_usd = ev.cost_usd || 0;
          this.totalCost += ev.cost_usd || 0;
          break;
        case 'error':
          msg.content += '\n\n⚠ ' + ev.msg;
          break;
      }
    },

    renderMd(txt) {
      try { return window.marked ? marked.parse(txt || '') : (txt || ''); }
      catch { return txt || ''; }
    },

    fmtRelTime(iso) {
      if (!iso) return '';
      const t = new Date(iso);
      const now = new Date();
      const diffMin = Math.floor((now - t) / 60000);
      if (diffMin < 1) return 'только что';
      if (diffMin < 60) return diffMin + ' мин назад';
      const diffH = Math.floor(diffMin / 60);
      if (diffH < 24) return diffH + ' ч назад';
      const diffD = Math.floor(diffH / 24);
      if (diffD < 7) return diffD + ' дн назад';
      return t.toLocaleDateString('ru-RU', { day: '2-digit', month: 'short' });
    },

    _scrollDown() {
      this.$nextTick(() => {
        const s = this.$refs.scroller;
        if (s) s.scrollTop = s.scrollHeight;
      });
    },
  };
}

window.assistantTab = assistantTab;
