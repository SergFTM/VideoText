// VideoText — floating assistant panel (Alpine component).
// Scope: quick-access chat from any screen. Full Assistant tab (with sessions)
// lives in app.js. This panel auto-opens on connector errors via the
// `assistant:error` window event.

function assistantPanel() {
  return {
    open: false,
    pulse: false,
    input: '',
    messages: [],
    streaming: false,
    streamingStatus: '',
    lastContextChars: 0,
    totalCost: 0,
    settings: {},
    pendingWrite: null,
    _pendingResolve: null,

    quickActions: [
      { icon: '🔑', label: 'Какие ключи не настроены?', prompt: 'Посмотри текущие настройки и скажи какие коннекторы ещё не настроены и что делать.' },
      { icon: '⚠️', label: 'Есть ли свежие ошибки?',  prompt: 'Проверь через tool get_recent_errors нет ли ошибок за последний час и объясни что делать.' },
      { icon: '📺', label: 'Что сейчас стримится?',   prompt: 'Покажи активные стримы и последние новости оттуда.' },
      { icon: '🧪', label: 'Проверь все коннекторы',  prompt: 'Протестируй supadata, anthropic, openai и pexels через tool test_integration и доложи результат.' },
    ],

    async init() {
      // NOTE on Alpine reactivity: `this` inside a plain window.addEventListener
      // callback is NOT the reactive proxy — it's the raw scope object. Writing
      // `this.open = true` there would silently update the scope without
      // triggering any x-show re-evaluation. Idiomatic fix: use Alpine's own
      // `@event.window` directive (see index.html), which resolves identifiers
      // against the proxy. The markup dispatches `assistant:toggle` via
      // `toggleAssistant()` in app.js; here we only fetch settings + wire the
      // error-handoff listener (which calls _handoff — that method writes to
      // `this` but is invoked via Alpine's own magic through .onclick, so `this`
      // there is the proxy again).
      try {
        const r = await fetch('/settings');
        this.settings = await r.json();
      } catch {}
      this.checkErrorsForPulse();
      setInterval(() => this.checkErrorsForPulse(), 30000);
    },

    // Called from Alpine-bound `@assistant:error.window` — `this` is the proxy.
    handleError(detail) {
      this._handoff(detail || {});
    },

    // Called from Alpine-bound `@assistant:toggle.window` — `this` is the proxy.
    handleToggle() {
      this.open = !this.open;
      if (this.open) this.pulse = false;
    },

    _handoff({ provider, message, source }) {
      this.open = true;
      this.pulse = false;
      const prompt = provider
        ? `Я только что нажал «проверить» на коннекторе «${provider}» и получил ошибку:\n\n\`${message}\`\n\nРазбери что это значит и что мне делать дальше. Если нужно — вызови tool test_integration повторно или предложи save_api_key.`
        : `Произошла ошибка: \`${message}\`. Разбери что делать.`;
      this.input = prompt;
      this.streamingStatus = 'автозапуск через 0.5с…';
      this.$nextTick(() => { if (this.$refs.input) this.$refs.input.focus(); });
      clearTimeout(this._handoffTimer);
      this._handoffTimer = setTimeout(() => {
        if (this.input === prompt && !this.streaming) this.send();
      }, 500);
    },

    async checkErrorsForPulse() {
      try {
        const r = await fetch('/streams');
        const data = await r.json();
        const streams = Array.isArray(data) ? data : (data.streams || []);
        const recentErrors = streams.some(s => s.status === 'error' || s.error);
        this.pulse = recentErrors && !this.open;
      } catch {}
    },

    renderMd(txt) {
      try { return window.marked ? marked.parse(txt || '') : (txt || ''); } catch { return txt || ''; }
    },

    // Read current language from <body lang="...">, which app() keeps in sync
    // via :lang="lang". Avoids having to share Alpine state across roots.
    t(key) {
      const lang = document.body.getAttribute('lang') || 'ru';
      return window.tr ? window.tr(lang, key) : key;
    },

    ask(prompt) {
      this.input = prompt;
      this.send();
    },

    clearConversation() {
      this.messages = [];
      this.totalCost = 0;
      this.lastContextChars = 0;
    },

    async send() {
      const question = this.input.trim();
      if (!question || this.streaming) return;
      this.input = '';
      this.messages.push({ role: 'user', content: question });
      this._scrollDown();

      this.streaming = true;
      this.streamingStatus = 'думает…';

      const ui_ctx = {
        tab: (Alpine.store && Alpine.store('app')?.activeView)
             || document.querySelector('[x-data^="app"]')?._x_dataStack?.[0]?.activeView,
      };

      const assistantMsg = { role: 'assistant', content: '', tool_events: [], cache_hit: false, cost_usd: 0 };
      this.messages.push(assistantMsg);

      try {
        const resp = await fetch('/assistant/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ question, ui_context: ui_ctx, auto_confirm: !!this.settings.assistant_auto_confirm_writes }),
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
            const payload = line.slice(6);
            let ev; try { ev = JSON.parse(payload); } catch { continue; }
            this._handleEvent(ev, assistantMsg);
            this._scrollDown();
          }
        }
      } catch (e) {
        assistantMsg.content += '\n\n⚠ ошибка: ' + e.message;
      } finally {
        this.streaming = false;
        this.streamingStatus = '';
        this._scrollDown();
      }
    },

    _handleEvent(ev, msg) {
      switch (ev.type) {
        case 'context':
          this.lastContextChars = ev.chars;
          this.streamingStatus = 'обрабатывает контекст…';
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
          this.streamingStatus = 'вызывает ' + ev.name + '…';
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

    confirmWrite(ok) {
      if (this._pendingResolve) { this._pendingResolve(ok); this._pendingResolve = null; }
      this.pendingWrite = null;
    },

    _scrollDown() {
      this.$nextTick(() => { const s = this.$refs.scroller; if (s) s.scrollTop = s.scrollHeight; });
    },
  };
}

window.assistantPanel = assistantPanel;
