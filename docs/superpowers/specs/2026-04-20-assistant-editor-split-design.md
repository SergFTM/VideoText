# Design: Разделение AI Assistant и AI Editor

**Дата:** 2026-04-20
**Статус:** Draft — ожидает апрува пользователя
**Скоуп:** архитектурный split текущего монолитного `assistant/` на две независимые персоны с общим ядром.

## 1. Мотивация

Сейчас `assistant/` (~1700 LOC) — одна универсальная AI-помощник сущность, которая смешивает две разные задачи:

- **Платформенные** — объяснить настройки, починить ошибки интеграций, рассказать про retention-политику.
- **Контентные** — улучшить заголовок news item, распознать текст на картинке, сгенерировать expanded text.

Это создаёт три проблемы:

1. **System prompt перегружен** — одна инструкция пытается быть и оператором платформы, и редактором контента. Результат — LLM путается в контексте.
2. **KB-контекст мусорит** — при запросе «как настроить OpenAI?» в выборку попадают документы про формат NewsItem, и наоборот.
3. **Tool whitelist смешан** — LLM может случайно вызвать `save_setting` при редактировании текста новости.

Решение — разделить на две *персоны* с разными промптами, KB-источниками, tools и UI-точками входа, но разделяющих единое ядро (chat-loop, cherry-picking, cache, провайдеры).

## 2. Роли

| Персона | Назначение | UI |
|---|---|---|
| **Platform** (AI Assistant) | Взаимодействие пользователя с панелью платформы: настройки, ошибки, интеграции, retention | Плавающая панель ([static/assistant-panel.js](../../../static/assistant-panel.js)), всегда доступна |
| **Editor** (AI Editor) | Взаимодействие с контентом: распознавание текста, улучшение заголовков/цитат, работа с лентой news items | Inline-контролы на карточках news item + меню-пункт `/editor` для истории и defaults |

«AI Content Manager», «Maker», «Content Maker» — синонимы Editor (один пункт меню с возможными алиасами в i18n).

## 3. Архитектура

### 3.1 Структура модуля

```
assistant/
├── core/                          # Общее ядро
│   ├── chat.py                    # SSE-стриминг chat-loop + tool-use
│   ├── providers.py               # openai | anthropic | ollama — унифицированный интерфейс
│   ├── context_builder.py         # cherry-picking (TF-IDF + embedding scoring)
│   ├── cache.py                   # fastembed Q&A cache, partition по persona
│   ├── analyzer.py                # TF-IDF + токенизация
│   ├── knowledge_base.py          # rebuild_kb(persona?) — билдер AssistantKB; persona=None перестраивает все источники
│   └── tools_runtime.py           # execute(tool, args, persona) — валидирует tool ∈ persona.whitelist + подставляет confirm
├── personas/
│   ├── base.py                    # BasePersona: system_prompt, tools, kb_sources, db_queries
│   ├── platform.py                # AI Assistant
│   └── editor.py                  # AI Editor
├── kb_sources/                    # KB loader'ы (один файл на источник)
│   ├── platform_docs.py           # DEPLOY.md + README.md + errors.yaml + kb_static.md
│   ├── project_docs.py            # AST-скан кода + docstrings
│   ├── settings_db.py             # live-snapshot AppSetting
│   ├── content_db.py              # NewsItem + StreamBrief
│   └── saved_kb.py                # kb_static.md + редакционные гайдлайны
├── tools/
│   ├── platform_tools.py          # test_integration, save_setting, save_api_key, ...
│   └── editor_tools.py            # recognize_text, improve_headline, rewrite_quote, ...
└── __init__.py                    # экспорт Assistant(persona="platform"|"editor")
```

**Правило изоляции:** persona хранит только данные (prompt, список KB-loader'ов, список tools, DB-фильтры). Вся логика (цикл, стриминг, кэш, cherry-picking) в `core/`. Добавить третью персону = написать один файл в `personas/`.

### 3.2 БД — partition по полю `persona`

Добавляем поле в три существующие таблицы:

```prisma
model AssistantSession {
  // ... existing fields
  persona String @default("platform")  // "platform" | "editor"
}

model AssistantMessage {
  // ... existing fields
  persona String @default("platform")
}

model AssistantCache {
  // ... existing fields
  persona String @default("platform")
}
```

Партиционирование обеспечивает: (а) история двух блоков не смешивается в UI, (б) cache-lookup использует `(question_hash, persona)` как ключ — вопрос в Editor не даёт hit по кэшу Platform.

### 3.3 Персоны — детально

#### Platform persona

- **KB sources:** `platform_docs` + `project_docs` + `settings_db`.
- **Tools (whitelist):**
  - `test_integration(provider)` — smoke-test коннектора
  - `save_setting(key, value, confirm=True)` — запись в AppSetting
  - `save_api_key(integration, key, confirm=True)` — запись в .env
  - `read_recent_errors(limit=20)` — последние Run.status="error" / Chunk.status="failed"
  - `storage_stats()` — дисковое использование и ретеншн
  - `list_streams(status)` — состояние live-стримов
- **Запрещено:** любые записи в NewsItem, генерация контента, OCR.

#### Editor persona

- **KB sources:** `platform_docs` (ограниченный — только про схему NewsItem) + `content_db` + `saved_kb`.
- **Tools (whitelist):**
  - `recognize_text(image_id | url)` — OCR через gpt-4o-mini vision
  - `improve_headline(item_id, style?, confirm=True)` — переписывает headline
  - `rewrite_quote(item_id, tone?, confirm=True)` — переписывает quote с сохранением смысла
  - `expand_text(item_id, length?, confirm=True)` — генерит/обновляет expandedText
  - `regenerate_image(item_id, concept?, confirm=True)` — новая иллюстрация через `enrich.py`
  - `suggest_tags(item_id)` — predicts tags, confirm=True для записи
  - `bulk_action(item_ids[], action, confirm=True)` — массовое применение
- **Запрещено:** save_setting, save_api_key, test_integration, read_recent_errors.

## 4. Data flow

### 4.1 Platform flow — пользователь открыл панель, спросил «почему dedup не работает?»

```
User → static/assistant-panel.js
  ↓ POST /chat/platform {question, session_id, ui_context}
server.py::chat_platform
  ↓ Persona=PlatformPersona()
  ↓ core/cache.py::lookup(question, persona="platform")
      ├─ HIT  → stream cached answer → AssistantMessage → done
      └─ MISS → continue
  ↓ core/context_builder.py::build(persona.kb_sources, persona.db_queries)
      ├─ TF-IDF топ-5 из AssistantKB (filtered by persona's KB kinds)
      ├─ live AppSetting snapshot (dedup_*, retain_*)
      └─ last 20 errors from Run/Chunk
  ↓ core/chat.py::run_loop(system=persona.system_prompt, tools=persona.tools, context)
      ├─ LLM → tool_call → core/tools_runtime.execute(tool, args, persona.whitelist)
      ├─ SSE events: {type:"thinking"} → {type:"tool_call"} → {type:"text"}
      └─ final answer
  ↓ core/cache.py::save(question, answer, persona="platform")
  ↓ AssistantMessage(persona="platform") persisted
  ↓ SSE stream closes
```

### 4.2 Editor flow — пользователь нажал «улучшить заголовок» на карточке

```
User → card[data-item-id=42] → click "✨ Улучшить заголовок"
  ↓ static/editor-inline.js — inline popover рядом с карточкой
  ↓ POST /chat/editor {action:"improve_headline", item_id:42, style:"острее"}
server.py::chat_editor
  ↓ Persona=EditorPersona()
  ↓ Whitelist check: improve_headline ∈ editor.tools ✓
  ↓ core/context_builder.py::build(editor.kb_sources, {item_id:42, stream_id:s.id})
      ├─ last 10 news items from same stream (tone context)
      └─ item 42 full content + tags + attribution
  ↓ core/chat.py::run_loop → improve_headline tool
      ↓ returns {old, new, diff, tool_call_id} БЕЗ записи
  ↓ SSE stream → frontend рисует diff-preview
  ↓ User clicks "применить"
  ↓ POST /chat/editor/apply {item_id, field:"headline", value:"...", tool_call_id}
  ↓ store.py::update_news_item_headline(item_id, new_value)
  ↓ response: updated card JSON
```

### 4.3 Confirm-flow — ключевая деталь

**Все write-tools возвращают preview (old/new/diff), НЕ пишут в БД.** Фактическую запись делает отдельный эндпоинт `/chat/editor/apply` или `/chat/platform/apply` после явного клика пользователя.

Параметр `confirm=True` в сигнатурах write-tools — это **контракт**, а не флаг поведения: наличие параметра маркирует tool как требующий apply-подтверждения. `core/tools_runtime.execute` распознаёт такие tools и гарантирует что они возвращают preview-dict, а не выполняют запись. Фактическая запись идёт через отдельную функцию в `store.py`, вызываемую только из `/apply` эндпоинта с проверкой `tool_call_id`.

- **Почему не write+undo:** LLM-переписывания часто мажут (смена языка, обрезка смысла). Preview ловит это ДО записи. Undo-стек потребовал бы snapshot-таблицы + TTL + GC — ~200 LOC на фичу, которую использует один tool.
- **Force overwrite:** `/apply` не проверяет `updatedAt` перед записью (single-user localhost, concurrent edit нереалистичен, YAGNI).

## 5. Endpoint naming

Симметричный формат:

| Endpoint | Персона | Назначение |
|---|---|---|
| `POST /chat/platform` | Platform | Чат-turn с SSE-стримом |
| `POST /chat/platform/apply` | Platform | Финализация write-tool после preview |
| `POST /chat/editor` | Editor | Чат-turn / inline-action с SSE-стримом |
| `POST /chat/editor/apply` | Editor | Финализация write-tool после preview |
| `GET /chat/{persona}/sessions` | обе | История сессий персоны |
| `POST /chat/{persona}/refresh-kb` | обе | Перестройка AssistantKB для данной персоны |
| `GET /chat/{persona}/cache` | обе | Кэш Q&A |

Legacy alias `POST /assistant/chat` → `POST /chat/platform` держим 2 релиза, потом удаляем.

## 6. Error handling

### Таксономия

1. **LLM provider errors** — SSE event `{type:"error", kind:"provider", retryable}`. Без auto-retry (экономия токенов); пользователь нажимает «повторить» вручную.
2. **Tool execution errors** — tool возвращает `{ok:false, error}` вместо исключения. LLM видит ошибку и может переиграть. 3 tool-fail подряд → chat-loop прерывается.
3. **KB build errors (AST parse / file read)** — `rebuild_kb` пропускает сломанный файл, логирует `<parse failed: ...>` с низким weight. Endpoint возвращает `{ok:true, counts, skipped:[...]}`.
4. **Context overflow** — `context_builder` считает токены, сжимает по `persona.kb_sources[i].priority`. Для Platform: `live_errors > settings_snapshot > platform_docs > project_docs`. Для Editor: `current_item > recent_items_of_same_stream > platform_docs`.
5. **Concurrent edit (Editor)** — force overwrite без проверки версии (single-user, YAGNI).
6. **SSE disconnect mid-stream** — backend доводит tool execution до конца, сохраняет AssistantMessage. Если фронт переподключится с тем же session_id в течение 30s, получает буфер последних событий.
7. **fastembed model download failure** — cache graceful-degrade: `cache.enabled = False` для сессии. Chat работает без cache-hit оптимизации. `/health` показывает `cache_degraded: true`.
8. **Vision OCR нечитаемо** — `recognize_text` возвращает `{ok:false, reason}`, LLM просит upload другой картинки.

### Логирование

- Все ошибки в `AssistantMessage.content` как JSON (`role="tool"` для tool-errors, `role="system"` для internal).
- Platform может читать свои ошибки через `read_recent_errors` → self-debugging loop.
- Editor-ошибки доступны в `/editor` меню-странице (последние 50 failed actions).

## 7. Testing

Лёгкий набор — ловит регрессии в критичных местах, не создаёт test-debt.

1. **`test_persona_isolation.py`** — для каждой персоны прогнать реестр tools, убедиться что чужие отклоняются на границе `server.py` + `core/tools_runtime.execute`. ~14 assert'ов, ~50 LOC.
2. **`test_context_builder.py`** — фиксированный набор вопросов → snapshot результата cherry-picking (id из AssistantKB + top-N DB rows). Одна фикстура на персону.
3. **`test_confirm_flow.py`** — E2E для Editor: preview → apply. Проверяет что NewsItem не меняется до /apply, и меняется после. httpx AsyncClient + Prisma `:memory:` fixture.
4. **`test_cache_partition.py`** — Q&A saved under persona=platform → same question in persona=editor → cache miss.
5. **`test_smoke.py`** — один интеграционный прогон на персону через реального провайдера (Haiku для дешевизны). Gate by `RUN_SMOKE=1` env.

**Вне scope:** unit-тесты на каждую tool (обёртки над store.py / external API — ценности мало), моки LLM (дают false confidence), load-тесты (single-user app).

## 8. Migration plan

### Phase 0 — подготовка (без breaking changes)

1. Создать `assistant/core/`, перенести внутрь `chat.py`, `context_builder.py`, `cache.py`, `analyzer.py`, `knowledge_base.py`. Обновить импорты. Existing behaviour = Platform persona по умолчанию. Frontend не трогается.
2. Prisma migration: добавить `persona String @default("platform")` в `AssistantSession`, `AssistantMessage`, `AssistantCache`. Reformat schema, `prisma db push`.
3. Выделить `personas/base.py` + `personas/platform.py` из монолита. Один persona, всё ещё работает как раньше.

**Точка отката:** всё работает как раньше, просто раскручено.

### Phase 1 — Editor persona (backend)

4. Создать `personas/editor.py` + `tools/editor_tools.py` с заглушками (`improve_headline` → `{ok:true, old, new:old+" [stub]", diff}`).
5. Добавить `POST /chat/editor` + `POST /chat/editor/apply`. Переименовать `/assistant/chat` → `/chat/platform` с legacy-alias.
6. Реализовать tools по одному, каждый — отдельный коммит. Порядок: `improve_headline`, `rewrite_quote`, `suggest_tags`, `expand_text`, `regenerate_image`, `recognize_text`, `bulk_action`.

**Точка отката:** каждый tool ревертится независимо.

### Phase 2 — Frontend

7. `static/assistant-panel.js` → указывает на `/chat/platform`.
8. Новый `static/editor-inline.js` + компонент diff-preview. Встраивается в карточку news item через событие `onAttachActions(card)`.
9. Пункт меню «AI Editor» → `/editor` (страница с историей + defaults).

### Phase 3 — Cleanup

10. Убрать legacy-alias `/assistant/chat`.
11. Удалить `static/assistant-tab.js` если больше не нужен (или репурпоснуть под `/editor` страницу).

## 9. Out of scope

- Поддержка персон помимо Platform и Editor (Maker/Content Manager — алиасы Editor).
- Multi-user коллаборация / concurrent edit protection.
- Write+undo для tools (preview+apply закрывает use-case).
- Auto-retry при provider errors.
- Unit-тесты на каждую tool.
- Моки LLM.

## 10. Открытые вопросы

_Нет. Все решения закреплены через Q&A перед написанием спеки._
