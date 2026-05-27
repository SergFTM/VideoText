// VideoText — main Alpine component (shell state + all tab-level business logic).
// Split from index.html inline <script> during design-handoff refactor.
// Behavior in this commit: 1:1 with previous inline version.

function app() {
  return {
    form: {
      url: '', brief_lang: 'ru', model: '', format: 'markdown',
      backend: 'auto', no_brief: false, force_refresh: false,
    },
    busy: false,
    showHelp: false,
    showStreamsHelp: false,
    showNewsHelp: false,
    log: [],
    logExpanded: false,
    briefingStreamId: null,
    streamBriefs: [],

    // Language (RU default; switcher coming in commit 4)
    lang: 'ru',

    // View routing — 4 tabs, persisted in URL hash.
    // The 5th tab (Assistant) is added in a later commit.
    activeView: 'video',
    // Tabs only carry id + icon now — labels resolve via t('nav.' + id) at render time
    // so the language switcher updates them without re-mounting the array.
    tabs: [
      { id: 'video',    icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="2" y="3" width="12" height="10" rx="1.5"/><path d="M7 6l3 2-3 2V6z" fill="currentColor"/></svg>' },
      { id: 'streams',  icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="8" cy="8" r="2"/><path d="M4.5 4.5a5 5 0 000 7M11.5 4.5a5 5 0 010 7M2 2a8 8 0 000 12M14 2a8 8 0 010 12"/></svg>' },
      { id: 'news',     icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="2" y="2" width="12" height="12" rx="1.5"/><path d="M5 6h6M5 9h6M5 12h4"/></svg>' },
      { id: 'editor',   icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M3 12l2-2 6-6 2 2-6 6-2 2z"/><path d="M11 3l2 2"/><path d="M3 13h10"/></svg>' },
      { id: 'settings', icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="8" cy="8" r="2"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.5 1.5M11.5 11.5L13 13M3 13l1.5-1.5M11.5 4.5L13 3"/></svg>' },
    ],

    get currentTab() {
      return this.tabs.find(t => t.id === this.activeView) || this.tabs[0];
    },
    tabLabel(id) { return this.t('nav.' + id); },

    tabBadge(id) {
      if (id === 'streams') {
        const drafts = (this.newsItems || []).filter(i => i.status === 'draft').length;
        if (drafts > 0) return String(drafts);
        const n = (this.streams || []).filter(s => s.status !== 'stopped').length;
        return n > 0 ? String(n) : '';
      }
      if (id === 'news') {
        const n = (this.newsItems || []).filter(i => i.status === 'approved').length;
        return n > 0 ? String(n) : '';
      }
      return '';
    },
    result: {},
    config: { supadata: false, anthropic: false, default_model: 'claude-sonnet-4-6' },
    integrations: {},       // from GET /config/integrations
    newKeys: {},            // per-integration pending key input
    revealKey: {},          // show/hide toggle per field
    editingKey: {},         // per-integration: true when input is expanded for editing
    savingKey: null,        // id of integration currently being saved
    testResult: {},
    history: [],

    // Phase 1 — live streams
    showStreamForm: false,
    editingStreamId: null,
    streamForm: {
      url: '', channel_name: '', speaker_default: '',
      interval_min: 5, whisper_model: 'medium',
      make_summary_brief: false, brief_template: 'news',
      auto_brief_on_stop: false,
    },
    streams: [],
    newsItems: [],
    newsFilter: { stream_id: '', status: '' },
    // News tab moderation-status chip filter (UI-side; doesn't refetch from /news-items)
    newsListFilter: 'approved',  // approved | draft | rejected | all
    _liveRefreshTimer: null,

    showLoadFromHistory: false,
    selectedArchivedIds: [],
    busyBatchLoad: false,

    hideDupesInModeration: false,

    // News detail modal
    detailItem: null,
    busyEnrich: false,

    // Command palette (⌘K / Ctrl+K)
    cmdkOpen: false,
    cmdkQuery: '',
    cmdkSelectedIndex: 0,

    // Settings — side-nav within the tab (5 subsections)
    settingsSection: 'integrations',  // integrations | storage | assistant | enrich | advanced
    storageStats: null,
    cleanupReport: null,
    busyCleanup: false,
    busyCookies: false,
    gpu: { available: false },
    showSettings: false,
    savingSettings: false,
    settingsSaved: false,
    settings: {
      default_brief_model: '', default_brief_lang: 'ru', default_brief_format: 'markdown',
      default_backend: 'auto', default_whisper_model: 'medium',
      default_stream_interval_min: 5, default_brief_template: 'news',
      default_auto_brief_on_stop: false,
    },

    async init() {
      // Restore active view + language
      const hash = (location.hash || '').replace('#', '');
      if (this.tabs.some(t => t.id === hash)) this.activeView = hash;
      const savedLang = localStorage.getItem('vt_lang');
      if (savedLang === 'ru' || savedLang === 'en') this.lang = savedLang;

      window.addEventListener('hashchange', () => {
        const h = (location.hash || '').replace('#', '');
        if (this.tabs.some(t => t.id === h)) this.activeView = h;
      });

      await Promise.all([
        this.loadConfig(), this.loadHistory(),
        this.loadStreams(), this.loadNewsItems(),
        this.loadIntegrations(),
      ]);
      this._applySettingsToForms();
      await this.loadAllStreamBriefs();
      this._liveRefreshTimer = setInterval(() => {
        this.loadStreams();
        this.loadNewsItems();
        this.loadAllStreamBriefs();
        this.loadGpu();
      }, 10000);
      this.loadGpu();  // initial fetch

      // Lazy-load storage stats first time the user opens that subsection.
      // When user switches mode in the expand modal, swap to that mode's saved
      // version (or empty placeholder). Avoids confusion where switching to
      // 'report' still showed the previous 'spec' text.
      this.$watch('specExpand.mode', () => {
        if (this.specExpand.open && !this.specExpand.streaming) {
          this.preloadExpansion();
        }
      });
      this.$watch('settingsSection', (s) => {
        if (s === 'storage' && !this.storageStats) this.loadStorageStats();
      });

      // ⌘K / Ctrl+K — open command palette from anywhere.
      window.addEventListener('keydown', (e) => {
        if (e.key === 'k' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); this.openCmdk(); }
        if (e.key === 'Escape' && this.cmdkOpen) this.closeCmdk();
      });
    },

    t(key) { return window.tr(this.lang, key); },

    // Brief count with proper RU pluralization (1 бриф / 2 брифа / 5 брифов).
    // EN keeps it simple: "1 brief" / "2 briefs".
    pluralBriefs(n) {
      n = +n || 0;
      if (this.lang === 'en') return n + ' brief' + (n === 1 ? '' : 's');
      const mod10 = n % 10, mod100 = n % 100;
      if (mod10 === 1 && mod100 !== 11) return n + ' бриф';
      if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return n + ' брифа';
      return n + ' брифов';
    },

    setLang(l) {
      this.lang = l;
      localStorage.setItem('vt_lang', l);
    },

    // ── Command palette ──────────────────────────────────────────────
    openCmdk() {
      this.cmdkOpen = true;
      this.cmdkQuery = '';
      this.cmdkSelectedIndex = 0;
      this.$nextTick(() => { if (this.$refs.cmdkInput) this.$refs.cmdkInput.focus(); });
    },

    closeCmdk() { this.cmdkOpen = false; },

    // Searches across loaded data — videos, streams, news, settings sections.
    // Returns flat list grouped by category. Limited to top-N per group.
    get cmdkResults() {
      const q = (this.cmdkQuery || '').trim().toLowerCase();
      if (q.length < 2) return [];
      const out = [];
      const matches = (text) => (text || '').toLowerCase().includes(q);

      // Videos
      const videos = (this.history || []).filter(h => h.type === 'video' && matches(h.title));
      videos.slice(0, 5).forEach(v => out.push({
        category: this.t('search.cat_videos'),
        label: v.title,
        sublabel: v.id,
        action: () => { this.setView('video'); this.loadVideo(v.id); this.closeCmdk(); },
      }));

      // Streams
      const streams = (this.streams || []).filter(s => matches(s.channel_name) || matches(s.url));
      streams.slice(0, 5).forEach(s => out.push({
        category: this.t('search.cat_streams'),
        label: s.channel_name,
        sublabel: s.status + ' · ' + s.url,
        action: () => { this.setView('streams'); this.closeCmdk(); },
      }));

      // News (search headlines + quotes)
      const news = (this.newsItems || []).filter(n => matches(n.headline) || matches(n.quote));
      news.slice(0, 8).forEach(n => out.push({
        category: this.t('search.cat_news'),
        label: n.headline,
        sublabel: n.status + ' · ' + (n.category || '—'),
        action: () => { this.setView('news'); this.closeCmdk(); this.openNewsDetail(n.id); },
      }));

      // Settings sections
      const settings = [
        { id: 'integrations', label: this.t('section.connectors') },
        { id: 'storage',      label: this.t('section.storage') },
        { id: 'assistant',    label: this.t('section.assistant_cfg') },
        { id: 'enrich',       label: this.t('section.enrichment') },
        { id: 'advanced',     label: this.t('section.advanced') },
      ].filter(s => matches(s.label));
      settings.forEach(s => out.push({
        category: this.t('search.cat_settings'),
        label: s.label,
        sublabel: 'Settings → ' + s.id,
        action: () => { this.setView('settings'); this.settingsSection = s.id; this.closeCmdk(); },
      }));

      return out;
    },

    cmdkPick(idx) {
      const results = this.cmdkResults;
      const item = results[idx ?? this.cmdkSelectedIndex];
      if (item) item.action();
    },

    cmdkMove(delta) {
      const max = this.cmdkResults.length - 1;
      if (max < 0) { this.cmdkSelectedIndex = 0; return; }
      this.cmdkSelectedIndex = Math.max(0, Math.min(max, this.cmdkSelectedIndex + delta));
    },

    toggleAssistant() {
      // Cross-component handshake: assistant-panel.js listens for this event
      // and toggles its own `open` state. Keeps both Alpine roots independent.
      window.dispatchEvent(new CustomEvent('assistant:toggle'));
    },

    setView(id) {
      this.activeView = id;
      if (location.hash !== '#' + id) {
        history.replaceState(null, '', '#' + id);
      }
      window.scrollTo({ top: 0, behavior: 'auto' });
    },

    // Deep-link from the Видео tab into the transcript AI-editor for a video.
    openTranscriptEditor(videoId) {
      if (!videoId) return;
      this.setView('editor');
      // editor-workspace.js exposes this once its IIFE has run.
      if (window.editorSelectVideo) window.editorSelectVideo(videoId);
    },

    _applySettingsToForms() {
      if (!this.form.url) {
        this.form.brief_lang = this.settings.default_brief_lang;
        this.form.fmt = this.settings.default_brief_format;
        this.form.format = this.settings.default_brief_format;
        this.form.model = this.settings.default_brief_model;
        this.form.backend = this.settings.default_backend;
      }
      if (!this.streamForm.url) {
        this.streamForm.whisper_model = this.settings.default_whisper_model;
        this.streamForm.interval_min = this.settings.default_stream_interval_min;
        this.streamForm.brief_template = this.settings.default_brief_template;
        this.streamForm.auto_brief_on_stop = this.settings.default_auto_brief_on_stop;
      }
    },

    logLine(msg, level = 'info') {
      const t = new Date().toLocaleTimeString('ru-RU', { hour12: false });
      this.log.push({ t, msg, level });
      if (this.log.length > 100) this.log = this.log.slice(-100);
      if (level === 'error') this.logExpanded = true;
    },

    async loadConfig() {
      try {
        this.config = await (await fetch('/config')).json();
        if (this.config.settings) {
          this.settings = { ...this.settings, ...this.config.settings };
        }
      } catch (e) { this.logLine('не удалось /config: ' + e, 'error'); }
    },

    async loadIntegrations() {
      try { this.integrations = await (await fetch('/config/integrations')).json(); }
      catch (e) { this.logLine('не удалось загрузить интеграции: ' + e.message, 'error'); }
    },

    async saveKey(id) {
      const key = (this.newKeys[id] || '').trim();
      if (!key) return;
      this.savingKey = id;
      try {
        const r = await fetch('/config/keys', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ integration: id, key }),
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({ detail: r.statusText }));
          throw new Error(err.detail || 'HTTP ' + r.status);
        }
        this.newKeys[id] = '';
        this.revealKey[id] = false;
        this.logLine('ключ ' + id + ' сохранён в .env', 'ok');
        await this.loadIntegrations();
        await this.loadConfig();
      } catch (e) {
        this.logLine('не удалось сохранить ключ: ' + e.message, 'error');
      } finally {
        this.savingKey = null;
      }
    },

    copyInstallCmd(cmd) {
      if (!cmd) return;
      navigator.clipboard.writeText(cmd).then(() => {
        this.logLine('команда скопирована: ' + cmd, 'ok');
      });
    },

    async saveSettings() {
      this.savingSettings = true;
      this.settingsSaved = false;
      try {
        const r = await fetch('/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.settings),
        });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        this.settings = await r.json();
        this.settingsSaved = true;
        setTimeout(() => { this.settingsSaved = false; }, 3000);
      } catch (e) {
        this.logLine('не удалось сохранить настройки: ' + e.message, 'error');
      } finally {
        this.savingSettings = false;
      }
    },

    async loadGpu() {
      try {
        const r = await fetch('/system/gpu');
        if (r.ok) this.gpu = await r.json();
      } catch (_) { /* silent — GPU stat is optional */ }
    },

    async loadStorageStats() {
      try {
        const r = await fetch('/storage/stats');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        this.storageStats = await r.json();
      } catch (e) { this.logLine('storage/stats: ' + e.message, 'error'); }
    },

    async uploadCookies(file) {
      if (!file) return;
      this.busyCookies = true;
      try {
        const fd = new FormData();
        fd.append('file', file);
        const r = await fetch('/config/cookies', { method: 'POST', body: fd });
        if (!r.ok) {
          const err = await r.json().catch(() => ({ detail: r.statusText }));
          throw new Error(err.detail || 'HTTP ' + r.status);
        }
        const data = await r.json();
        this.config = { ...this.config, cookies_path: data.path };
        this.logLine('cookies saved: ' + data.path, 'ok');
      } catch (e) {
        this.logLine('cookies upload: ' + e.message, 'error');
      } finally {
        this.busyCookies = false;
      }
    },

    async runCleanup(commit) {
      this.busyCleanup = true;
      this.cleanupReport = null;
      try {
        const r = await fetch('/storage/cleanup', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ commit }),
        });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        this.cleanupReport = await r.json();
        this.logLine(commit ? 'cleanup applied' : 'cleanup dry-run done', 'ok');
        if (commit) await this.loadStorageStats();
      } catch (e) {
        this.logLine('cleanup: ' + e.message, 'error');
      } finally {
        this.busyCleanup = false;
      }
    },

    async resetSettings() {
      if (!confirm('Сбросить все настройки к дефолтам?')) return;
      this.settings = {
        default_brief_model: '', default_brief_lang: 'ru', default_brief_format: 'markdown',
        default_backend: 'auto', default_whisper_model: 'medium',
        default_stream_interval_min: 5, default_brief_template: 'news',
        default_auto_brief_on_stop: false,
      };
      await this.saveSettings();
    },

    async loadHistory() {
      try {
        const [videos, streams] = await Promise.all([
          fetch('/videos').then(r => r.json()),
          fetch('/streams').then(r => r.json()),
        ]);
        const rows = [];
        for (const v of videos) {
          rows.push({
            type: 'video', id: v.id,
            title: v.title || '(без названия)',
            language: v.language, duration: v.duration,
            briefs_count: v.briefs_count, total_cost: v.total_cost,
            created_at: v.created_at, sort_key: v.created_at,
          });
        }
        for (const s of streams) {
          if (s.status !== 'stopped') continue;
          rows.push({
            type: 'stream', id: s.id,
            title: s.channel_name,
            subtitle_parts: [
              s.id.slice(0, 12), s.whisper_model,
              'чанков ' + s.chunks_total,
              'новостей ' + s.news_items_count,
            ],
            briefs_count: null, total_cost: null,
            created_at: s.updated_at || s.created_at,
            sort_key: s.updated_at || s.created_at,
            _raw: s,
          });
        }
        rows.sort((a, b) => (b.sort_key || '').localeCompare(a.sort_key || ''));
        this.history = rows;
      } catch (e) { this.logLine('не удалось загрузить историю: ' + e.message, 'error'); }
    },

    get activeStreams() {
      return (this.streams || []).filter(s => s.status !== 'stopped');
    },
    get historyVideos() {
      return (this.history || []).filter(h => h.type === 'video');
    },
    get historyStreams() {
      return (this.history || []).filter(h => h.type === 'stream');
    },
    get draftNews() {
      return (this.newsItems || []).filter(i => i.status === 'draft');
    },
    get visibleDraftNews() {
      return this.hideDupesInModeration
        ? this.draftNews.filter(i => !i.duplicate_of_id)
        : this.draftNews;
    },
    get approvedNews() {
      return (this.newsItems || []).filter(i => i.status === 'approved');
    },
    get rejectedNews() {
      return (this.newsItems || []).filter(i => i.status === 'rejected');
    },
    // News tab visible feed honoring the chip-filter
    get visibleNews() {
      const items = (this.newsItems || []).filter(i =>
        this.hideDupesInModeration ? !i.duplicate_of_id : true
      );
      if (this.newsListFilter === 'all') return items;
      return items.filter(i => i.status === this.newsListFilter);
    },
    // Short channel name for the news card meta-row.
    // attribution looks like "Источник: <channel>, <timestamp>[, <speaker>]"
    // — extract just <channel> for compact display.
    newsSource(item) {
      if (!item || !item.attribution) return '';
      const m = item.attribution.match(/Источник:\s*([^,]+?)(?:\s*,|$)/i);
      return m ? m[1].trim() : item.attribution;
    },

    async loadStreams() {
      try { this.streams = await (await fetch('/streams')).json(); }
      catch (e) { /* silent — polling */ }
    },

    async loadNewsItems() {
      try {
        const params = new URLSearchParams();
        if (this.newsFilter.stream_id) params.set('stream_id', this.newsFilter.stream_id);
        if (this.newsFilter.status) params.set('status', this.newsFilter.status);
        const q = params.toString();
        this.newsItems = await (await fetch('/news-items' + (q ? '?' + q : ''))).json();
      } catch (e) { /* silent */ }
    },

    toggleStreamForm() {
      if (this.showStreamForm) {
        this.showStreamForm = false;
        this.editingStreamId = null;
        this._resetStreamForm();
      } else {
        this.showStreamForm = true;
      }
    },

    _resetStreamForm() {
      this.streamForm = {
        url: '', channel_name: '', speaker_default: '',
        interval_min: this.settings.default_stream_interval_min || 5,
        whisper_model: this.settings.default_whisper_model || 'medium',
        make_summary_brief: false,
        brief_template: this.settings.default_brief_template || 'news',
        auto_brief_on_stop: this.settings.default_auto_brief_on_stop || false,
      };
    },

    openEditStream(s) {
      this.editingStreamId = s.id;
      this.streamForm = {
        url: s.url, channel_name: s.channel_name,
        speaker_default: s.speaker_default || '',
        interval_min: s.interval_min, whisper_model: s.whisper_model,
        make_summary_brief: s.make_summary_brief,
        brief_template: s.brief_template,
        auto_brief_on_stop: s.auto_brief_on_stop,
      };
      this.showStreamForm = true;
      setTimeout(() => window.scrollTo({ top: document.querySelector('section:nth-of-type(1)').offsetTop, behavior: 'smooth' }), 50);
    },

    async saveStream() {
      const f = this.streamForm;
      if (!f.url || !f.channel_name) return;
      const isEdit = !!this.editingStreamId;
      this.logLine((isEdit ? 'сохраняю: ' : 'создаю мониторинг: ') + f.channel_name);
      try {
        const r = await fetch(
          isEdit ? ('/streams/' + this.editingStreamId) : '/streams',
          {
            method: isEdit ? 'PATCH' : 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(f),
          }
        );
        if (!r.ok) {
          const err = await r.json().catch(() => ({ detail: r.statusText }));
          throw new Error(err.detail || 'HTTP ' + r.status);
        }
        const data = await r.json();
        this.logLine(isEdit ? ('обновлено: ' + data.id) : ('стрим запущен: ' + data.id), 'ok');
        this.showStreamForm = false;
        this.editingStreamId = null;
        this._resetStreamForm();
        await this.loadStreams();
      } catch (e) {
        this.logLine('не удалось: ' + e.message, 'error');
      }
    },

    async _postStreamAction(id, action) {
      const r = await fetch('/streams/' + id + '/' + action, { method: 'POST' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    },

    async pauseStream(id) {
      try { await this._postStreamAction(id, 'pause'); this.logLine('пауза стрима', 'ok'); await this.loadStreams(); }
      catch (e) { this.logLine(e.message, 'error'); }
    },

    async resumeStream(id) {
      try { await this._postStreamAction(id, 'resume'); this.logLine('стрим продолжен', 'ok'); await this.loadStreams(); }
      catch (e) { this.logLine(e.message, 'error'); }
    },

    toggleLoadFromHistory() {
      this.showLoadFromHistory = !this.showLoadFromHistory;
      if (!this.showLoadFromHistory) this.selectedArchivedIds = [];
    },

    selectAllArchived() {
      this.selectedArchivedIds = this.historyStreams.map(h => h.id);
    },

    async loadSelectedArchivedStreams() {
      if (this.selectedArchivedIds.length === 0) return;
      this.busyBatchLoad = true;
      const ids = [...this.selectedArchivedIds];
      this.logLine('запускаю ' + ids.length + ' стрим(ов) из истории…');
      let ok = 0, err = 0;
      for (const id of ids) {
        try {
          await this._postStreamAction(id, 'resume');
          ok++;
        } catch (e) {
          err++;
          this.logLine('не удалось запустить ' + id.slice(0, 12) + ': ' + e.message, 'error');
        }
      }
      this.logLine('запущено: ' + ok + (err ? ', ошибок: ' + err : ''), err ? 'error' : 'ok');
      this.selectedArchivedIds = [];
      this.showLoadFromHistory = false;
      await this.loadStreams();
      await this.loadHistory();
    },

    async stopStreamConfirm(id) {
      if (!confirm('Остановить мониторинг? Данные и новости сохранятся в БД.')) return;
      try { await this._postStreamAction(id, 'stop'); this.logLine('стрим остановлен', 'ok'); await this.loadStreams(); }
      catch (e) { this.logLine(e.message, 'error'); }
    },

    async deleteStreamConfirm(id) {
      if (!confirm('УДАЛИТЬ стрим со всеми чанками, транскриптами и новостями? Действие необратимо.')) return;
      try {
        const r = await fetch('/streams/' + id, { method: 'DELETE' });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        this.logLine('стрим удалён', 'ok');
        await this.loadStreams();
        await this.loadNewsItems();
      } catch (e) { this.logLine(e.message, 'error'); }
    },

    async approveItem(id) {
      try {
        await fetch('/news-items/' + id + '/approve', { method: 'POST' });
        await this.loadNewsItems();
      } catch (e) { this.logLine(e.message, 'error'); }
    },

    async rejectItem(id) {
      try {
        await fetch('/news-items/' + id + '/reject', { method: 'POST' });
        await this.loadNewsItems();
      } catch (e) { this.logLine(e.message, 'error'); }
    },

    async openNewsDetail(id) {
      try {
        const r = await fetch('/news-items/' + id);
        if (!r.ok) throw new Error('HTTP ' + r.status);
        this.detailItem = await r.json();
      } catch (e) {
        this.logLine('не удалось открыть новость: ' + e.message, 'error');
      }
    },

    closeNewsDetail() {
      this.detailItem = null;
    },

    async enrichNewsItem(id) {
      if (!id) return;
      this.busyEnrich = true;
      this.logLine('обогащаю через AI (текст + картинка)…');
      try {
        const r = await fetch('/news-items/' + id + '/enrich', { method: 'POST' });
        if (!r.ok) {
          const err = await r.json().catch(() => ({ detail: r.statusText }));
          throw new Error(err.detail || 'HTTP ' + r.status);
        }
        const data = await r.json();
        this.logLine(
          'готово: expanded ' + data.expanded_text.length + ' знаков' +
          (data.image_id
            ? ' + ' + (data.image_reused
                ? 'переиспользована картинка (concept: ' + data.image_concept + ')'
                : 'новая картинка (concept: ' + data.image_concept + ')')
            : '') +
          ' · $' + (data.cost_usd || 0).toFixed(4),
          'ok',
        );
        await this.openNewsDetail(id);
        await this.loadNewsItems();
      } catch (e) {
        this.logLine('обогащение не удалось: ' + e.message, 'error');
      } finally {
        this.busyEnrich = false;
      }
    },

    async makeStreamBrief(streamId, template) {
      template = template || 'news';
      this.briefingStreamId = streamId;
      this.logLine('генерирую сводный бриф (' + template + ')…');
      try {
        const r = await fetch('/streams/' + streamId + '/brief', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ template }),
        });
        if (!r.ok) throw new Error((await r.json()).detail || 'HTTP ' + r.status);
        const data = await r.json();
        this.logLine('бриф #' + data.id + ' готов (' + data.chunks_covered + ' чанков, $' + (data.cost_usd || 0).toFixed(4) + ')', 'ok');
        await this.loadAllStreamBriefs();
      } catch (e) {
        this.logLine('бриф не получился: ' + e.message, 'error');
      } finally {
        this.briefingStreamId = null;
      }
    },

    async loadAllStreamBriefs() {
      const all = [];
      for (const s of this.streams) {
        try {
          const briefs = await (await fetch('/streams/' + s.id + '/briefs')).json();
          for (const b of briefs) {
            all.push({ ...b, stream_name: s.channel_name, stream_id: s.id });
          }
        } catch (e) { /* silent */ }
      }
      all.sort((a, b) => b.created_at.localeCompare(a.created_at));
      this.streamBriefs = all;
    },

    async exportNewsItems(fmt) {
      try {
        const body = {
          format: fmt,
          stream_id: this.newsFilter.stream_id || null,
          status: 'approved',
        };
        const r = await fetch('/export', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const blob = await r.blob();
        const disp = r.headers.get('Content-Disposition') || '';
        const match = disp.match(/filename="([^"]+)"/);
        const filename = match ? match[1] : ('export.' + fmt);
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = filename;
        document.body.appendChild(a); a.click(); a.remove();
        URL.revokeObjectURL(url);
        this.logLine('экспорт ' + fmt.toUpperCase() + ' → ' + filename, 'ok');
      } catch (e) {
        this.logLine('экспорт не удался: ' + e.message, 'error');
      }
    },

    async run() {
      if (!this.form.url || this.busy) return;
      this.busy = true;
      this.result = {};
      this.logLine('запускаю: ' + this.form.url);

      try {
        const body = {
          url: this.form.url,
          brief_lang: this.form.brief_lang,
          format: this.form.format,
          backend: this.form.backend,
          no_brief: this.form.no_brief,
          force_refresh: this.form.force_refresh,
        };
        if (this.form.model) body.model = this.form.model;

        const r = await fetch('/briefs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({ detail: r.statusText }));
          throw new Error(err.detail || 'HTTP ' + r.status);
        }
        const data = await r.json();
        if (data.transcript_from_cache) {
          this.logLine('транскрипт из БД (0 вызовов к Supadata)', 'ok');
        } else {
          const path = (data.backends_tried || []).join(' → ');
          const pathStr = path ? ' через ' + path : '';
          const fallbackMark = (data.backends_tried || []).length > 1 ? ' (был fallback)' : '';
          this.logLine('транскрипт готов' + pathStr + fallbackMark + ', ' + (data.duration || 0) + 's', 'ok');
        }
        if (data.brief) {
          if (data.from_cache) {
            this.logLine('бриф из БД (#' + data.brief_id + ', 0 токенов потрачено)', 'ok');
          } else {
            this.logLine('бриф готов, ≈$' + (data.cost_usd || 0).toFixed(4), 'ok');
          }
        }

        const full = await fetch('/videos/' + data.video_id + '?segments=true').then(r => r.json());
        this.result = {
          video_id: data.video_id,
          title: data.title || full.title,
          duration: data.duration || full.duration,
          language: full.language,
          source: full.source,
          transcript_path: data.transcript_path,
          transcript_from_cache: data.transcript_from_cache,
          backends_tried: data.backends_tried || [],
          segments: full.segments || [],
          brief: data.brief,
          model: full.brief?.model,
          cost_usd: data.cost_usd,
          from_cache: data.from_cache,
          brief_usage: (!data.from_cache && full.brief) ? {
            input_tokens: full.brief.input_tokens,
            output_tokens: full.brief.output_tokens,
            cache_read_input_tokens: full.brief.cache_read_tokens,
            cache_creation_input_tokens: full.brief.cache_write_tokens,
          } : null,
        };
        await this.loadHistory();
      } catch (e) {
        this.logLine(e.message, 'error');
      } finally {
        this.busy = false;
      }
    },

    async openStreamFromHistory(h) {
      this.newsFilter.stream_id = h.id;
      await this.loadNewsItems();
      this.logLine('открыт стрим из истории: ' + h.title, 'ok');
      const labels = Array.from(document.querySelectorAll('.label'));
      const target = labels.find(el => el.textContent.trim() === 'прямые эфиры');
      if (target) {
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    },

    async loadVideo(videoId) {
      this.busy = true;
      try {
        const full = await fetch('/videos/' + videoId + '?segments=true').then(r => r.json());
        this.result = {
          video_id: full.id,
          title: full.title,
          duration: full.duration,
          language: full.language,
          source: full.source,
          segments: full.segments || [],
          brief: full.brief?.content_md || '',
          model: full.brief?.model,
          cost_usd: full.brief?.cost_usd,
          brief_usage: full.brief ? {
            input_tokens: full.brief.input_tokens,
            output_tokens: full.brief.output_tokens,
            cache_read_input_tokens: full.brief.cache_read_tokens,
            cache_creation_input_tokens: full.brief.cache_write_tokens,
          } : null,
        };
        this.logLine('из БД: ' + (full.title || videoId), 'ok');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      } catch (e) {
        this.logLine(e.message, 'error');
      } finally {
        this.busy = false;
      }
    },

    async testApi(which) {
      this.testResult[which] = { ok: false, msg: 'проверяю…' };
      let res;
      try {
        const r = await fetch('/config/test?provider=' + which, { method: 'POST' });
        res = await r.json();
        this.testResult[which] = res;
      } catch (e) {
        res = { ok: false, msg: e.message };
        this.testResult[which] = res;
      }
      if (res && res.ok === false) {
        window.dispatchEvent(new CustomEvent('assistant:error', {
          detail: { provider: which, message: res.msg || 'неизвестная ошибка', source: 'config/test' },
        }));
      }
    },

    renderMarkdown(text) {
      if (!text) return '';
      return marked.parse(text, { breaks: true });
    },

    splitBriefSections(md) {
      if (!md) return [];
      const pattern = /^## (.+?)\r?\n([\s\S]*?)(?=\r?\n## |$)/gm;
      const out = [];
      let m;
      while ((m = pattern.exec(md)) !== null) {
        out.push({ title: m[1].trim(), body: m[2].trim() });
      }
      if (out.length === 0) out.push({ title: 'Бриф', body: md.trim() });
      return out;
    },

    isSpecSection(title) {
      const t = (title || '').toLowerCase();
      return t.includes('черновик тз') || t.includes('software brief')
          || (t.includes('тз') && t.includes('черновик'));
    },

    copySection(title, body) {
      const full = '## ' + title + '\n' + body;
      navigator.clipboard.writeText(full).then(() => {
        this.logLine('скопировано: ' + title, 'ok');
      }).catch((e) => {
        this.logLine('копирование не удалось: ' + e.message, 'error');
      });
    },

    downloadSection(title, body, filename) {
      const safeName = (filename || title.replace(/\s+/g, '_') + '.md')
        .replace(/[<>:"|?*\/\\]/g, '_');
      const content = '# ' + title + '\n\n' + body + '\n';
      const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = safeName;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      this.logLine('скачано: ' + safeName, 'ok');
    },

    // ─── Expand spec via local Ollama ───────────────────────────────
    // State for the modal lives on the root component so Alpine can react.
    // `streaming` is the in-flight flag, `text` accumulates SSE deltas.
    specExpand: {
      open: false, streaming: false, text: '', model: '',
      sourceTitle: '', sourceBody: '', videoId: '',
      models: [], abort: null,
      // Output mode — which kind of expanded artefact to produce.
      //   'spec'     → expanded technical spec (default for ТЗ section)
      //   'research' → deep analytical research (default for content sections)
      //   'report'   → executive-style report
      mode: 'spec',
      // Context source for the prompt.
      context: 'both', // 'brief' | 'transcript' | 'both'
      meta: null,  // {model, mode, num_ctx, transcript_chars, brief_chars, context_mode}
      // Phase progression for the live activity indicator:
      //   idle      → before first click
      //   connecting→ POST in flight, waiting for HTTP headers
      //   prefill   → meta frame received but no tokens yet (Ollama is loading model + processing prompt)
      //   generating→ first delta arrived, tokens streaming
      //   done | error
      phase: 'idle',
      startedAt: 0,            // ms timestamp when run started
      firstTokenAt: 0,         // ms when first delta arrived (used for tok/s calc)
      charsPerSec: 0,          // smoothed rolling estimate of generation speed
      savedAt: '',             // ISO timestamp of the persisted row (from updatedAt) — empty = unsaved/new
    },

    // Returns a context-aware placeholder for the empty state of the modal.
    // Reflects both the mode and the actual section title the user clicked from.
    expandPlaceholder() {
      const s = this.specExpand;
      const title = s.sourceTitle || 'секцию';
      const ctx = (s.context === 'transcript') ? 'расшифровку'
                : (s.context === 'brief')      ? 'бриф'
                : 'бриф и расшифровку';
      const action = ({
        spec:          `расширит «${title}» в подробное ТЗ`,
        research:      `подготовит ресерч по «${title}»`,
        report:        `составит репорт по «${title}»`,
        ai_skills:     `спроектирует возможные AI-скиллы из «${title}»`,
        ai_algorithms: `опишет алгоритмы действий для AI из «${title}»`,
      })[s.mode] || `расширит «${title}»`;
      return `Нажми «сгенерировать» — модель прочитает ${ctx} и ${action}.`;
    },

    // Fetch saved expansion for current (video, mode) and pre-fill the modal.
    // No-op when no row exists — UI shows the placeholder text instead.
    async preloadExpansion() {
      const s = this.specExpand;
      if (!s.videoId || !s.mode) return;
      try {
        const r = await fetch(`/videos/${s.videoId}/expansions/${s.mode}`);
        if (r.status === 404) {
          s.text = '';
          s.meta = null;
          s.savedAt = '';
          return;
        }
        if (!r.ok) return;
        const j = await r.json();
        s.text = j.content_md || '';
        s.savedAt = j.updated_at || j.created_at || '';
        // Synthesize a meta frame so the footer shows context info from saved row.
        s.meta = {
          model: j.model, mode: j.mode, num_ctx: j.num_ctx,
          context_mode: j.context_mode, transcript_chars: 0, brief_chars: 0,
        };
        s.phase = s.text ? 'done' : 'idle';
      } catch (_) { /* offline / endpoint missing — silent fallback */ }
    },

    // Human-readable "saved 5 minutes ago" / "saved today at 14:23" / "saved 2026-04-30".
    // Three thresholds chosen for the three things a user actually cares about:
    //   <60min: "is this fresh?"  →  relative
    //   today:  "did I do this today?"  →  time only
    //   older:  "when exactly?"  →  absolute date
    expandSavedLabel() {
      const iso = this.specExpand.savedAt;
      if (!iso) return '';
      const saved = new Date(iso);
      if (isNaN(saved.getTime())) return '';
      const now = new Date();
      const diffMin = Math.floor((now - saved) / 60000);
      if (diffMin < 1)  return 'сохранено только что';
      if (diffMin < 60) return `сохранено ${diffMin} мин назад`;
      const sameDay = saved.toDateString() === now.toDateString();
      const hh = String(saved.getHours()).padStart(2, '0');
      const mm = String(saved.getMinutes()).padStart(2, '0');
      if (sameDay) return `сохранено сегодня в ${hh}:${mm}`;
      const yyyy = saved.getFullYear();
      const m    = String(saved.getMonth() + 1).padStart(2, '0');
      const d    = String(saved.getDate()).padStart(2, '0');
      return `сохранено ${yyyy}-${m}-${d}`;
    },

    // Open the PDF endpoint in a new tab. Browser handles the download dialog.
    downloadExpansionPdf() {
      const s = this.specExpand;
      if (!s.videoId || !s.mode) return;
      window.open(`/videos/${s.videoId}/expansions/${s.mode}.pdf`, '_blank');
    },

    // Human-readable label + spinner for each phase.
    expandPhaseLabel() {
      const s = this.specExpand;
      switch (s.phase) {
        case 'connecting': return '🔌 подключаюсь к Ollama…';
        case 'prefill':    return '📖 модель читает контекст и греет KV-cache (на первом запуске может уйти 10–60 сек)…';
        case 'generating': {
          const chars = s.text.length;
          const speed = s.charsPerSec ? ' · ' + s.charsPerSec.toFixed(0) + ' симв./с' : '';
          return '✍️ генерирую: ' + chars + ' симв.' + speed;
        }
        case 'done':       return '✅ готово · ' + s.text.length + ' симв.';
        case 'error':      return '⚠️ ошибка — см. вывод';
        default:           return '';
      }
    },

    async openExpandSpec(videoId, sectionTitle, sectionBody) {
      this.specExpand.open = true;
      this.specExpand.text = '';
      this.specExpand.streaming = false;
      this.specExpand.videoId = videoId;
      this.specExpand.sourceTitle = sectionTitle;
      this.specExpand.sourceBody = sectionBody;
      // Pick a sensible default mode based on which section was clicked.
      this.specExpand.mode = this.isSpecSection(sectionTitle) ? 'spec' : 'research';
      this.specExpand.phase = 'idle';
      this.specExpand.text = '';
      this.specExpand.meta = null;
      // Preload any existing expansion for this (video, mode) so user can re-read,
      // re-download, or regenerate over previous output. Watcher below also re-runs
      // on mode switch.
      this.preloadExpansion();
      // Lazy-load installed-model list once per modal open.
      try {
        const r = await fetch('/local-llm/models');
        const j = await r.json();
        this.specExpand.models = (j.models || []).map(m => m.name).filter(Boolean);
        if (!this.specExpand.model && this.specExpand.models.length) {
          this.specExpand.model = this.specExpand.models.includes('qwen2.5:7b')
            ? 'qwen2.5:7b' : this.specExpand.models[0];
        }
        if (!j.reachable) {
          this.specExpand.text = '⚠️ Ollama не отвечает на ' + (j.endpoint || 'localhost:11434')
            + '. Запусти ollama и установи модель: `ollama pull qwen2.5:7b`';
        }
      } catch (e) {
        this.specExpand.text = 'не удалось получить список моделей: ' + e.message;
      }
    },

    closeExpandSpec() {
      if (this.specExpand.abort) { try { this.specExpand.abort.abort(); } catch (_) {} }
      this.specExpand.open = false;
      this.specExpand.streaming = false;
    },

    async runExpandSpec() {
      if (this.specExpand.streaming) return;
      if (!this.specExpand.model) {
        this.logLine('выбери модель Ollama', 'error');
        return;
      }
      const s = this.specExpand;
      s.streaming = true;
      s.text = '';
      s.meta = null;
      s.savedAt = '';
      s.phase = 'connecting';
      s.startedAt = Date.now();
      s.firstTokenAt = 0;
      s.charsPerSec = 0;
      const ctrl = new AbortController();
      s.abort = ctrl;
      try {
        const resp = await fetch(`/videos/${s.videoId}/expand-spec`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            section_md: s.sourceBody,
            section_title: s.sourceTitle,
            mode: s.mode,
            model: s.model,
            context: s.context,
          }),
          signal: ctrl.signal,
        });
        if (!resp.ok || !resp.body) {
          s.text = 'ошибка сервера: ' + resp.status;
          s.phase = 'error';
          return;
        }
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          // Split SSE messages (each ends with \n\n).
          let idx;
          while ((idx = buf.indexOf('\n\n')) >= 0) {
            const raw = buf.slice(0, idx).trim();
            buf = buf.slice(idx + 2);
            if (!raw.startsWith('data:')) continue;
            try {
              const ev = JSON.parse(raw.slice(5).trim());
              if (ev.type === 'meta') {
                s.meta = ev;
                if (s.phase === 'connecting') s.phase = 'prefill';
              } else if (ev.type === 'delta') {
                if (!s.firstTokenAt) {
                  s.firstTokenAt = Date.now();
                  s.phase = 'generating';
                }
                s.text += ev.text;
                // Update rolling speed estimate every ~250ms worth of accumulated chars.
                const elapsed = (Date.now() - s.firstTokenAt) / 1000;
                if (elapsed > 0.5) s.charsPerSec = s.text.length / elapsed;
              } else if (ev.type === 'done') {
                s.phase = 'done';
                // Server upserts the row before sending 'done'. If saved_id is present,
                // the row exists — set savedAt to "now" so the footer reflects fresh state
                // immediately, without a round-trip to refetch updatedAt.
                if (ev.saved_id) s.savedAt = new Date().toISOString();
              } else if (ev.type === 'error') {
                s.text += '\n\n[error] ' + ev.msg;
                s.phase = 'error';
              }
            } catch (_) { /* swallow malformed SSE chunks */ }
          }
        }
        if (s.phase !== 'done' && s.phase !== 'error') s.phase = 'done';
      } catch (e) {
        if (e.name !== 'AbortError') {
          s.text += '\n\n[fetch error] ' + e.message;
          s.phase = 'error';
        } else {
          s.phase = 'idle';
        }
      } finally {
        s.streaming = false;
        s.abort = null;
      }
    },

    copyExpandedSpec() {
      if (!this.specExpand.text) return;
      navigator.clipboard.writeText(this.specExpand.text);
      this.logLine('расширенное ТЗ скопировано', 'ok');
    },

    downloadExpandedSpec() {
      if (!this.specExpand.text) return;
      // Filename + heading reflect the mode so a user with multiple downloads
      // can tell ТЗ / ресерч / репорт apart at a glance.
      const modeMeta = {
        spec:     { label: 'Расширенное ТЗ', prefix: 'tz' },
        research: { label: 'Исследование',   prefix: 'research' },
        report:   { label: 'Репорт',         prefix: 'report' },
      }[this.specExpand.mode] || { label: 'Расширение', prefix: 'expand' };
      this.downloadSection(
        modeMeta.label,
        this.specExpand.text,
        `${modeMeta.prefix}-${this.specExpand.videoId || 'brief'}.md`,
      );
    },

    copyBrief() {
      if (this.result.brief) {
        navigator.clipboard.writeText(this.result.brief);
        this.logLine('скопировано', 'ok');
      }
    },

    // Per-category chip color. Mirrors the prototype's CATEGORY_COLORS map.
    // Returns an inline style string ready to drop into :style="...".
    categoryChipStyle(cat) {
      // Distinct hue per category — picked across the OKLCH wheel so adjacent
      // chips read as different topics at a glance. Lightness fixed at 0.95
      // for backgrounds, 0.40-0.42 for text.
      const map = {
        finance:  { bg: 'oklch(0.95 0.04 75)',  fg: 'oklch(0.42 0.14 75)'  }, // warm amber
        markets:  { bg: 'oklch(0.95 0.04 95)',  fg: 'oklch(0.42 0.14 95)'  }, // gold
        macro:    { bg: 'oklch(0.95 0.04 50)',  fg: 'oklch(0.42 0.14 50)'  }, // copper
        business: { bg: 'oklch(0.95 0.05 110)', fg: 'oklch(0.40 0.14 110)' }, // olive
        tech:     { bg: 'oklch(0.95 0.03 265)', fg: 'oklch(0.40 0.15 265)' }, // blue
        ai:       { bg: 'oklch(0.94 0.05 155)', fg: 'oklch(0.40 0.13 155)' }, // green
        science:  { bg: 'oklch(0.95 0.03 195)', fg: 'oklch(0.40 0.14 195)' }, // teal
        sports:   { bg: 'oklch(0.95 0.04 140)', fg: 'oklch(0.40 0.13 140)' }, // forest
        culture:  { bg: 'oklch(0.95 0.03 320)', fg: 'oklch(0.40 0.13 320)' }, // magenta
        policy:   { bg: 'oklch(0.95 0.03 25)',  fg: 'oklch(0.42 0.16 25)'  }, // red
        politics: { bg: 'oklch(0.95 0.04 10)',  fg: 'oklch(0.42 0.16 10)'  }, // crimson
      };
      const c = map[cat] || { bg: 'var(--panel)', fg: 'var(--mute)' };
      return `background: ${c.bg}; color: ${c.fg}; border-color: transparent;`;
    },

    // Build a sparkline path for a stream, synthesised from chunks_total
    // (we don't yet keep per-chunk news rate in the DB — once we do,
    // swap `data` for the real timeseries). Returns {points, last} for SVG.
    streamSpark(s) {
      const n = Math.max(1, Math.min(s.chunks_total || 0, 24));
      // Mild upward curve so the line looks alive even without real data.
      const data = Array.from({ length: n }, (_, i) => 1 + i * 0.3 + (i % 3));
      const w = 120, h = 28;
      const max = Math.max(...data, 1);
      const min = Math.min(...data, 0);
      const step = w / Math.max(data.length - 1, 1);
      const range = (max - min) || 1;
      const pts = data.map((v, i) => [
        i * step,
        h - ((v - min) / range) * (h - 4) - 2,
      ]);
      const path = pts.map((p, i) => (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
      const fillPath = path + ` L ${w} ${h} L 0 ${h} Z`;
      const last = pts[pts.length - 1];
      return { w, h, path, fillPath, lastX: last[0], lastY: last[1] };
    },

    fmtTime(sec) {
      sec = Math.floor(sec);
      const m = Math.floor(sec / 60);
      const s = sec % 60;
      return m + ':' + String(s).padStart(2, '0');
    },

    fmtDuration(sec) {
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = sec % 60;
      return h > 0
        ? (h + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0'))
        : (m + ':' + String(s).padStart(2, '0'));
    },
  };
}

window.app = app;
