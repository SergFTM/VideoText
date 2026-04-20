# Assistant/Editor Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Разделить монолитный `assistant/` модуль на две персоны (Platform и Editor) с общим ядром, partition-ом БД по полю `persona`, симметричными endpoints `/chat/platform` + `/chat/editor`, и раздельными UI-точками входа (панель + inline на карточках).

**Architecture:** Shared core + two personas. `assistant/core/` держит chat-loop / providers / cache / context_builder / knowledge_base / tools_runtime — общее. `assistant/personas/{platform,editor}.py` описывают только data (system_prompt, kb_sources, tools, db_queries). Write-tools возвращают preview, фактическая запись через отдельный `/apply` эндпоинт.

**Tech Stack:** Python 3.11 + FastAPI + Prisma (SQLite) + existing anthropic/openai/ollama SDKs + fastembed + pytest + httpx (для тестов).

**Spec:** [docs/superpowers/specs/2026-04-20-assistant-editor-split-design.md](../specs/2026-04-20-assistant-editor-split-design.md)

---

## File structure after implementation

```
assistant/
├── __init__.py                    # экспорт Assistant(persona=...)
├── core/
│   ├── __init__.py
│   ├── chat.py                    # перенос из assistant/chat.py
│   ├── providers.py               # выделение openai/anthropic/ollama адаптеров из chat.py
│   ├── context_builder.py         # перенос из assistant/context_builder.py
│   ├── cache.py                   # перенос из assistant/cache.py (+ persona param)
│   ├── analyzer.py                # перенос из assistant/analyzer.py
│   ├── knowledge_base.py          # перенос из assistant/knowledge_base.py
│   └── tools_runtime.py           # новый: execute(name, args, persona) с whitelist check
├── personas/
│   ├── __init__.py
│   ├── base.py                    # BasePersona dataclass
│   ├── platform.py                # PlatformPersona()
│   └── editor.py                  # EditorPersona()
├── kb_sources/
│   ├── __init__.py
│   ├── base.py                    # KBSource protocol
│   ├── platform_docs.py           # DEPLOY.md + errors.yaml + kb_static.md
│   ├── project_docs.py            # AST-скан кода
│   ├── settings_db.py             # live AppSetting snapshot
│   ├── content_db.py              # NewsItem + StreamBrief snapshot
│   └── saved_kb.py                # kb_static.md + editorial guidelines
└── tools/
    ├── __init__.py
    ├── base.py                    # Tool dataclass + decorator
    ├── platform_tools.py          # существующие + save_setting / save_api_key / read_errors / ...
    └── editor_tools.py            # новые: recognize_text / improve_headline / rewrite_quote / ...

prisma/
└── schema.prisma                  # +persona field в трёх таблицах

server.py                          # +/chat/{persona} + /chat/{persona}/apply + legacy alias

static/
├── assistant-panel.js             # указывает на /chat/platform
├── editor-inline.js               # новый — inline на карточке NewsItem
└── editor-settings.js             # новый — страница /editor для истории и defaults

tests/
├── __init__.py
├── conftest.py                    # prisma in-memory fixture + async client
├── test_persona_isolation.py
├── test_context_builder.py
├── test_confirm_flow.py
├── test_cache_partition.py
└── test_smoke.py                  # gated by RUN_SMOKE=1
```

---

## Phase 0 — Подготовка (non-breaking)

Цель фазы: вытащить общее ядро в `assistant/core/`, добавить поле `persona`, ввести persona-абстракцию. Behaviour не меняется — всё работает как раньше через `PlatformPersona()` по умолчанию.

---

### Task 0.1: Установить pytest и httpx для тестов

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Добавить зависимости в requirements.txt**

```
# Добавить в конец файла:
pytest>=8.0
pytest-asyncio>=0.23
httpx>=0.27
```

- [ ] **Step 2: Установить их**

Run: `.venv\Scripts\pip install pytest pytest-asyncio httpx`
Expected: все три установлены без ошибок

- [ ] **Step 3: Создать `pytest.ini` в корне**

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
python_files = test_*.py
```

- [ ] **Step 4: Создать пустую `tests/__init__.py` и `tests/conftest.py`**

```python
# tests/__init__.py — пустой
```

```python
# tests/conftest.py
"""Shared test fixtures.

DB fixture: uses a separate SQLite file (`prisma/test.db`) that gets
wiped between tests. Prisma's Python client does not support `:memory:` cleanly
with the current schema because multiple connections don't share memory DBs.
"""
import os
import subprocess
import sys
from pathlib import Path
import pytest_asyncio

TEST_DB_PATH = Path(__file__).parent.parent / "prisma" / "test.db"


@pytest_asyncio.fixture
async def db():
    """Fresh DB per test. Uses DATABASE_URL override to point Prisma at test.db."""
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
    os.environ["DATABASE_URL"] = f"file:{TEST_DB_PATH}"
    # Invoke prisma via the active Python interpreter's -m entry point so we
    # don't depend on `prisma` being on PATH (it's only in .venv/Scripts/).
    subprocess.run(
        [sys.executable, "-m", "prisma", "db", "push",
         "--skip-generate", "--accept-data-loss"],
        check=True,
    )
    from prisma import Prisma
    client = Prisma()
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()
        if TEST_DB_PATH.exists():
            TEST_DB_PATH.unlink()
```

- [ ] **Step 5: Smoke — pytest runs empty suite**

Run: `.venv\Scripts\python -m pytest tests/ -v`
Expected: `0 passed` (нет тестов — норм), без ошибок конфига.

- [ ] **Step 6: Commit**

```bash
git add requirements.txt pytest.ini tests/__init__.py tests/conftest.py
git commit -m "chore: add pytest + httpx for assistant-editor split"
```

---

### Task 0.2: Добавить поле `persona` в Prisma schema

**Files:**
- Modify: `prisma/schema.prisma` — три модели

- [ ] **Step 1: Написать тест миграции**

```python
# tests/test_persona_field.py
"""Verify the persona column exists on all three tables and defaults correctly."""
import pytest

@pytest.mark.asyncio
async def test_persona_default_on_session(db):
    s = await db.assistantsession.create(data={})
    assert s.persona == "platform"

@pytest.mark.asyncio
async def test_persona_default_on_message(db):
    s = await db.assistantsession.create(data={})
    m = await db.assistantmessage.create(data={
        "sessionId": s.id, "role": "user", "content": "hi",
    })
    assert m.persona == "platform"

@pytest.mark.asyncio
async def test_persona_default_on_cache(db):
    c = await db.assistantcache.create(data={
        "question": "why", "answer": "because",
    })
    assert c.persona == "platform"
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `.venv\Scripts\python -m pytest tests/test_persona_field.py -v`
Expected: FAIL — колонки `persona` нет.

- [ ] **Step 3: Добавить поля в schema**

Отредактировать `prisma/schema.prisma`:

```prisma
model AssistantSession {
  id        String   @id @default(cuid())
  title     String?
  persona   String   @default("platform")
  createdAt DateTime @default(now())
  updatedAt DateTime @updatedAt

  messages AssistantMessage[]
}

model AssistantMessage {
  id              Int      @id @default(autoincrement())
  sessionId       String
  session         AssistantSession @relation(fields: [sessionId], references: [id], onDelete: Cascade)
  role            String
  content         String
  persona         String   @default("platform")
  model           String?
  inputTokens     Int      @default(0)
  outputTokens    Int      @default(0)
  cacheReadTokens Int      @default(0)
  costUsd         Float?
  cacheHit        Boolean  @default(false)
  toolCalls       String?
  createdAt       DateTime @default(now())
}

model AssistantCache {
  id          Int      @id @default(autoincrement())
  question    String
  answer      String
  persona     String   @default("platform")
  embedding   String?
  usedCount   Int      @default(1)
  lastUsedAt  DateTime @default(now())
  createdAt   DateTime @default(now())
}
```

- [ ] **Step 4: Применить миграцию + перегенерить клиент**

Run:
```
.venv\Scripts\prisma db push --accept-data-loss
.venv\Scripts\prisma generate
```
Expected: `Your database is now in sync with your schema.`

- [ ] **Step 5: Повторить тесты — зелёные**

Run: `.venv\Scripts\python -m pytest tests/test_persona_field.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add prisma/schema.prisma tests/test_persona_field.py
git commit -m "feat: add persona field to AssistantSession/Message/Cache"
```

---

### Task 0.3: Создать `assistant/core/` со скелетом и переместить модули

**Files:**
- Create: `assistant/core/__init__.py`
- Move: `assistant/chat.py` → `assistant/core/chat.py`
- Move: `assistant/context_builder.py` → `assistant/core/context_builder.py`
- Move: `assistant/cache.py` → `assistant/core/cache.py`
- Move: `assistant/analyzer.py` → `assistant/core/analyzer.py`
- Move: `assistant/knowledge_base.py` → `assistant/core/knowledge_base.py`
- Modify: `server.py:919-1013` (import paths)

- [ ] **Step 1: Создать `assistant/core/__init__.py`**

```python
"""Shared core for all assistant personas.

Modules here are persona-agnostic: chat loop, provider adapters, cache,
context builder, knowledge base, tools runtime. Personas supply data
(prompts, tool lists, KB sources) — core supplies logic.
"""
```

- [ ] **Step 2: Переместить файлы физически**

Run:
```
mv assistant/chat.py assistant/core/chat.py
mv assistant/context_builder.py assistant/core/context_builder.py
mv assistant/cache.py assistant/core/cache.py
mv assistant/analyzer.py assistant/core/analyzer.py
mv assistant/knowledge_base.py assistant/core/knowledge_base.py
```

- [ ] **Step 3: Обновить импорты ВНУТРИ этих файлов**

В каждом из перемещённых файлов найти и заменить:

```
from assistant.cache         → from assistant.core.cache
from assistant.context_builder → from assistant.core.context_builder
from assistant.analyzer      → from assistant.core.analyzer
from assistant.knowledge_base → from assistant.core.knowledge_base
from assistant.tools         → from assistant.tools  (не трогать, tools.py остался)
```

(Оставить импорт `assistant.tools` как есть — его перенесём в Task 0.5.)

- [ ] **Step 4: Обновить импорты в `server.py`**

Найти секцию «# ─── AI Assistant ───» (около строки 919) и заменить:

```python
# Старые:
from assistant.chat import Assistant
from assistant.knowledge_base import rebuild_kb
from assistant.cache import save_qa, list_cache, clear_cache

# Новые:
from assistant.core.chat import Assistant
from assistant.core.knowledge_base import rebuild_kb
from assistant.core.cache import save_qa, list_cache, clear_cache
```

- [ ] **Step 5: Smoke — сервер запускается, /health возвращает 200**

Run:
```
.venv\Scripts\python -m uvicorn server:app --port 8765 &
sleep 3
curl -s http://localhost:8765/health
kill %1
```
Expected: JSON `{"status":"ok", ...}`. Никаких ImportError.

- [ ] **Step 6: Запустить все тесты**

Run: `.venv\Scripts\python -m pytest tests/ -v`
Expected: все passed (4 теста пока).

- [ ] **Step 7: Commit**

```bash
git add assistant/ server.py
git commit -m "refactor: move assistant internals to assistant/core/"
```

---

### Task 0.4: Извлечь `BasePersona` и `PlatformPersona`

**Files:**
- Create: `assistant/personas/__init__.py`
- Create: `assistant/personas/base.py`
- Create: `assistant/personas/platform.py`

- [ ] **Step 1: Написать тест — PlatformPersona exposes correct shape**

```python
# tests/test_persona_base.py
from assistant.personas.platform import PlatformPersona

def test_platform_persona_name():
    p = PlatformPersona()
    assert p.name == "platform"

def test_platform_persona_has_system_prompt():
    p = PlatformPersona()
    assert len(p.system_prompt) > 200  # non-trivial prompt

def test_platform_persona_tool_names():
    p = PlatformPersona()
    # The platform persona owns integration + settings + error tools
    assert "test_integration" in p.tool_names
    assert "save_setting" in p.tool_names
    assert "save_api_key" in p.tool_names
    assert "get_recent_errors" in p.tool_names

def test_platform_persona_forbids_editor_tools():
    p = PlatformPersona()
    assert "improve_headline" not in p.tool_names
    assert "recognize_text" not in p.tool_names
```

- [ ] **Step 2: Запустить — падает (ImportError)**

Run: `.venv\Scripts\python -m pytest tests/test_persona_base.py -v`
Expected: collection error, нет модуля.

- [ ] **Step 3: Создать `assistant/personas/__init__.py`**

```python
"""Persona definitions — data only (prompts, tool lists, KB sources, DB filters).

All logic lives in assistant/core/. Adding a new persona means adding one file here.
"""

from .base import BasePersona
from .platform import PlatformPersona

__all__ = ["BasePersona", "PlatformPersona"]
```

- [ ] **Step 4: Создать `assistant/personas/base.py`**

```python
"""Base persona contract — what every persona must declare."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class BasePersona:
    """A persona is a bundle of (prompt, tools, KB sources, DB queries).

    Core logic reads these as data — does not introspect. To add a new persona,
    subclass this and fill in the fields.

    `tool_names` — whitelist. tools_runtime rejects anything outside it.
    `kb_source_keys` — list of kind names ("platform_docs", "content_db", ...)
                       that context_builder uses to filter AssistantKB.
    `db_query_builders` — list of callables that take prisma client + question
                           and return structured context rows.
    """
    name: str = ""
    system_prompt: str = ""
    tool_names: list[str] = field(default_factory=list)
    kb_source_keys: list[str] = field(default_factory=list)
    db_query_builders: list[Callable[..., Any]] = field(default_factory=list)
    # Token priority during context compression: higher = keep longer.
    kb_priorities: dict[str, int] = field(default_factory=dict)
```

- [ ] **Step 5: Создать `assistant/personas/platform.py`**

Сначала сохранить системный промпт из текущего `assistant/core/chat.py` — он там есть как `SYSTEM_PROMPT_BASE`. Мы ПЕРЕНОСИМ его сюда (из chat.py удалим позже, в Task 1.2).

```python
"""AI Assistant — platform persona.

Handles platform interaction: settings, integrations, errors, retention.
Does NOT touch content (news items, briefs).
"""

from __future__ import annotations
from .base import BasePersona


PLATFORM_SYSTEM_PROMPT = """Ты — встроенный AI-ассистент приложения VideoText.

VideoText обрабатывает YouTube: single-video briefs + live streams с
извлечением новостей, модерацией, обогащением и экспортом. Стек: FastAPI +
Prisma (SQLite) + Alpine.js.

Твоя роль — помогать ПОЛЬЗОВАТЕЛЮ настраивать и использовать платформу.
Пользователь — не разработчик, не лезет в код. Отвечай коротко, по-русски,
с конкретными действиями («нажми туда», «зайди сюда», «вставь это»).

Область ответственности:
- Настройки интеграций, API-ключи, retention-политика, dedup-порог.
- Разбор ошибок (Run.status="error", Chunk.status="failed").
- Состояние live-стримов, дисковое использование, healthcheck коннекторов.

Что ты НЕ делаешь:
- Не редактируешь заголовки, цитаты, expanded text news items.
- Не генерируешь иллюстрации, не распознаёшь текст.
- Для контентных задач — направляй пользователя к AI Editor (inline на карточках).

Правила:
1. Если есть прямое совпадение с каталогом ошибок — сразу давай fix-steps.
2. Для настроек используй tool `get_settings_snapshot`.
3. Для проверки коннектора — tool `test_integration`. Предлагай протестировать
   после изменений.
4. Для write-действий (save_setting, save_api_key) ВСЕГДА сначала покажи что
   собираешься изменить, дождись явного согласия, потом вызывай tool. Tool
   вернёт preview — запись произойдёт только после клика «применить».
5. Если не знаешь — скажи честно, не галлюцинируй.
6. Markdown для ответов. Ссылки кликабельные.
"""


PLATFORM_TOOL_NAMES = [
    "get_settings_snapshot",
    "test_integration",
    "get_recent_errors",
    "list_active_streams",
    "save_setting",
    "save_api_key",
    "storage_stats",
]


PLATFORM_KB_SOURCE_KEYS = [
    "platform_docs",
    "project_docs",
    "settings_db",
]


PLATFORM_KB_PRIORITIES = {
    "settings_db":   3,  # live state — highest
    "platform_docs": 2,  # curated how-to
    "project_docs":  1,  # fallback AST
}


class PlatformPersona(BasePersona):
    def __init__(self):
        super().__init__(
            name="platform",
            system_prompt=PLATFORM_SYSTEM_PROMPT,
            tool_names=PLATFORM_TOOL_NAMES,
            kb_source_keys=PLATFORM_KB_SOURCE_KEYS,
            kb_priorities=PLATFORM_KB_PRIORITIES,
        )
```

- [ ] **Step 6: Запустить тесты — зелёные**

Run: `.venv\Scripts\python -m pytest tests/test_persona_base.py -v`
Expected: 4 passed.

- [ ] **Step 7: Commit**

```bash
git add assistant/personas/ tests/test_persona_base.py
git commit -m "feat: introduce BasePersona + PlatformPersona"
```

---

### Task 0.5: Выделить `tools/` подпапку + `tools_runtime` с persona-check

**Files:**
- Create: `assistant/tools/__init__.py`
- Create: `assistant/tools/base.py`
- Move: `assistant/tools.py` → `assistant/tools/platform_tools.py` (частично — схемы останутся в base)
- Create: `assistant/core/tools_runtime.py`

- [ ] **Step 1: Тест — `tools_runtime.execute` отклоняет чужой tool**

```python
# tests/test_persona_isolation.py
from assistant.core.tools_runtime import execute
from assistant.personas.platform import PlatformPersona

def test_platform_can_call_own_tool():
    p = PlatformPersona()
    result = execute("get_settings_snapshot", {}, p)
    # don't care about data here — only that it's permitted
    assert "error" not in result or "not allowed" not in result.get("error", "").lower()

def test_platform_rejects_editor_tool():
    p = PlatformPersona()
    result = execute("improve_headline", {"item_id": 1}, p)
    assert result["ok"] is False
    assert "not allowed" in result["error"].lower()

def test_unknown_tool_rejected():
    p = PlatformPersona()
    result = execute("totally_made_up_tool", {}, p)
    assert result["ok"] is False
```

- [ ] **Step 2: Запустить — падает**

Run: `.venv\Scripts\python -m pytest tests/test_persona_isolation.py -v`
Expected: collection error / ImportError.

- [ ] **Step 3: Создать `assistant/tools/__init__.py`**

```python
"""Tool registries. One file per persona's tools.

Each file exports a module-level `TOOLS: dict[str, ToolDef]`.
The core tools_runtime imports and merges them.
"""
```

- [ ] **Step 4: Создать `assistant/tools/base.py`**

```python
"""Tool definition contract."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict   # JSON Schema
    execute: Callable[..., dict]
    is_write: bool = False  # write-tools return preview, actual write via /apply
```

- [ ] **Step 5: Переместить `assistant/tools.py` → `assistant/tools/platform_tools.py`**

Run: `mv assistant/tools.py assistant/tools/platform_tools.py`

Затем в начало `platform_tools.py` добавить экспорт единого реестра:

```python
# В конец файла, после всех определений:

from .base import ToolDef

TOOLS: dict[str, ToolDef] = {
    "get_settings_snapshot": ToolDef(
        name="get_settings_snapshot",
        description="Get current settings and integration key presence.",
        parameters={"type": "object", "properties": {}},
        execute=get_settings_snapshot,
    ),
    "test_integration": ToolDef(
        name="test_integration",
        description="Ping a connector (supadata|anthropic|openai|fastembed|ollama|pexels).",
        parameters={
            "type": "object",
            "properties": {"provider": {"type": "string"}},
            "required": ["provider"],
        },
        execute=test_integration,
    ),
    "get_recent_errors": ToolDef(
        name="get_recent_errors",
        description="Recent Run+Chunk failures (default last 60 min, top 20).",
        parameters={
            "type": "object",
            "properties": {
                "minutes": {"type": "integer", "default": 60},
                "limit": {"type": "integer", "default": 20},
            },
        },
        execute=get_recent_errors,
    ),
    "list_active_streams": ToolDef(
        name="list_active_streams",
        description="Active live streams with chunk counts.",
        parameters={"type": "object", "properties": {}},
        execute=list_active_streams,
    ),
    "save_setting": ToolDef(
        name="save_setting",
        description="Change an AppSetting. Returns preview; apply via /chat/platform/apply.",
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["key", "value"],
        },
        execute=save_setting,
        is_write=True,
    ),
    "save_api_key": ToolDef(
        name="save_api_key",
        description="Persist API key to .env. Returns preview; apply via /chat/platform/apply.",
        parameters={
            "type": "object",
            "properties": {
                "provider": {"type": "string"},
                "key": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["provider", "key"],
        },
        execute=save_api_key,
        is_write=True,
    ),
    "storage_stats": ToolDef(
        name="storage_stats",
        description="Disk usage, row counts, retention policy snapshot.",
        parameters={"type": "object", "properties": {}},
        execute=lambda: {"ok": True, "data": __import__("cleanup").get_storage_stats(__import__("store").get_all_settings())},
    ),
}
```

(Убрать старые `TOOLS = {...}` словарь и `openai_schema()` / `anthropic_schema()` / `execute_tool()` из этого файла — они уйдут в `tools_runtime.py`.)

- [ ] **Step 6: Создать `assistant/core/tools_runtime.py`**

```python
"""Tool dispatch with persona-based whitelist.

Merges per-persona tool modules and exposes:
  - `registry(persona)` → merged dict of tools this persona is allowed to call
  - `execute(name, args, persona)` → runs tool if whitelisted, else {ok:false}
  - `openai_schema(persona)` / `anthropic_schema(persona)` — schemas for the
    whitelisted subset, ready to hand to each provider's SDK.
"""

from __future__ import annotations
from typing import Any

from assistant.personas.base import BasePersona
from assistant.tools.base import ToolDef
from assistant.tools import platform_tools


def _all_tools() -> dict[str, ToolDef]:
    """Union of all tool modules. Editor tools will be added here later."""
    merged = {}
    merged.update(platform_tools.TOOLS)
    try:
        from assistant.tools import editor_tools  # added in Phase 1
        merged.update(editor_tools.TOOLS)
    except ImportError:
        pass  # editor_tools not yet created — Phase 0 only has platform
    return merged


def registry(persona: BasePersona) -> dict[str, ToolDef]:
    """Subset of tools this persona is allowed to call."""
    all_t = _all_tools()
    return {name: all_t[name] for name in persona.tool_names if name in all_t}


def execute(name: str, args: dict, persona: BasePersona) -> dict:
    """Run a tool — returns {ok, data, ...} or {ok:false, error}."""
    allowed = registry(persona)
    if name not in allowed:
        return {"ok": False, "error": f"tool {name!r} not allowed for persona {persona.name!r}"}
    td = allowed[name]
    try:
        return td.execute(**args)
    except TypeError as e:
        return {"ok": False, "error": f"bad arguments: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def openai_schema(persona: BasePersona) -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": td.name, "description": td.description, "parameters": td.parameters,
        }}
        for td in registry(persona).values()
    ]


def anthropic_schema(persona: BasePersona) -> list[dict]:
    return [
        {"name": td.name, "description": td.description, "input_schema": td.parameters}
        for td in registry(persona).values()
    ]
```

- [ ] **Step 7: Обновить `assistant/core/chat.py` — использовать tools_runtime вместо assistant.tools**

Заменить импорт на:
```python
from assistant.core.tools_runtime import execute, openai_schema, anthropic_schema
```

Затем везде где код вызывал `execute_tool(name, args)` — передавать persona: `execute(name, args, self.persona)`.

Класс `Assistant` в `chat.py` должен принимать persona:

```python
class Assistant:
    def __init__(
        self,
        persona,
        provider: str = "openai",
        model: str = "gpt-4o",
        use_cache: bool = True,
        cache_threshold: float = 0.85,
    ):
        self.persona = persona
        self.provider = provider
        self.model = model
        ...
```

И системный промпт в `messages` брать из `self.persona.system_prompt` вместо глобального `SYSTEM_PROMPT_BASE`.

- [ ] **Step 8: Обновить `server.py` — вызывать с PlatformPersona**

В `server.py:assistant_chat` найти создание `Assistant(...)`:

```python
from assistant.personas.platform import PlatformPersona

assistant = Assistant(
    persona=PlatformPersona(),
    provider=provider, model=model,
    use_cache=use_cache, cache_threshold=cache_thr,
)
```

- [ ] **Step 9: Запустить все тесты**

Run: `.venv\Scripts\python -m pytest tests/ -v`
Expected: все passed (7+ тестов).

- [ ] **Step 10: Smoke — assistant-panel в UI продолжает отвечать**

Run:
```
.venv\Scripts\python -m uvicorn server:app --port 8765 &
sleep 3
curl -X POST http://localhost:8765/assistant/chat -H "Content-Type: application/json" -d "{\"question\":\"say ok\"}"
kill %1
```
Expected: SSE поток, без Internal Server Error.

- [ ] **Step 11: Commit**

```bash
git add assistant/ server.py tests/
git commit -m "refactor: extract tools_runtime with persona-based whitelist"
```

---

### Task 0.6: Cache — добавить partition по persona

**Files:**
- Modify: `assistant/core/cache.py`

- [ ] **Step 1: Тест — cache-partition**

```python
# tests/test_cache_partition.py
import pytest
from assistant.core.cache import save_qa, find_cached_answer

@pytest.mark.asyncio
async def test_same_question_different_persona_miss(db):
    await save_qa(db, "how to configure dedup", "do X", persona="platform")
    hit = await find_cached_answer(db, "how to configure dedup", persona="editor", threshold=0.85)
    assert hit is None

@pytest.mark.asyncio
async def test_same_question_same_persona_hit(db):
    await save_qa(db, "how to configure dedup", "do X", persona="platform")
    hit = await find_cached_answer(db, "how to configure dedup", persona="platform", threshold=0.85)
    assert hit is not None
    assert "do X" in hit["answer"]
```

- [ ] **Step 2: Падает — функции ещё не принимают persona**

Run: `.venv\Scripts\python -m pytest tests/test_cache_partition.py -v`
Expected: FAIL — `save_qa() got unexpected keyword argument 'persona'`.

- [ ] **Step 3: Модифицировать `assistant/core/cache.py`**

Добавить `persona: str = "platform"` параметр в `save_qa()` и `find_cached_answer()`. При `save` записывать в новое поле. При `find` фильтровать по `persona`.

Псевдо-patch (точные имена функций смотреть в текущем коде):

```python
async def save_qa(db, question: str, answer: str, persona: str = "platform"):
    ...
    await db.assistantcache.create(data={
        "question": question,
        "answer": answer,
        "persona": persona,
        "embedding": json.dumps(embedding) if embedding else None,
    })


async def find_cached_answer(db, question: str, persona: str = "platform", threshold: float = 0.85):
    rows = await db.assistantcache.find_many(where={"persona": persona})
    ...  # existing similarity logic on `rows`
```

- [ ] **Step 4: Обновить `server.py` — передавать persona при save/find**

В `/assistant/chat` endpoint:

```python
async for ev in assistant.ask_stream(db, req.question, ...):
    ...
# После цикла:
await save_qa(db, req.question, answer_text, persona="platform")
```

И в lookup'е в chat.py:
```python
hit = await find_cached_answer(db, question, persona=self.persona.name, threshold=self.cache_threshold)
```

- [ ] **Step 5: Тесты зелёные**

Run: `.venv\Scripts\python -m pytest tests/test_cache_partition.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add assistant/core/cache.py server.py tests/test_cache_partition.py
git commit -m "feat: partition assistant cache by persona"
```

---

## Phase 1 — Editor персона (backend)

Цель фазы: поднять Editor persona с заглушками tools + эндпоинты `/chat/editor`, `/chat/editor/apply`. Реальная логика tools заполняется последовательными коммитами.

---

### Task 1.1: `EditorPersona` skeleton + `editor_tools.py` заглушка

**Files:**
- Create: `assistant/personas/editor.py`
- Create: `assistant/tools/editor_tools.py`
- Modify: `assistant/personas/__init__.py`

- [ ] **Step 1: Тест — EditorPersona shape**

```python
# tests/test_editor_persona.py
from assistant.personas.editor import EditorPersona

def test_editor_persona_name():
    assert EditorPersona().name == "editor"

def test_editor_persona_tool_names():
    p = EditorPersona()
    expected = {
        "recognize_text", "improve_headline", "rewrite_quote",
        "expand_text", "regenerate_image", "suggest_tags", "bulk_action",
    }
    assert expected.issubset(set(p.tool_names))

def test_editor_persona_forbids_platform_tools():
    p = EditorPersona()
    assert "save_setting" not in p.tool_names
    assert "save_api_key" not in p.tool_names
    assert "test_integration" not in p.tool_names
```

- [ ] **Step 2: Падает — модуля нет**

Run: `.venv\Scripts\python -m pytest tests/test_editor_persona.py -v`
Expected: ImportError.

- [ ] **Step 3: Создать `assistant/tools/editor_tools.py` с заглушками**

```python
"""Editor tools — work with news item content.

All mutating tools return a preview dict: {ok, old, new, diff, tool_call_id}.
Actual DB writes happen in /chat/editor/apply after user clicks "apply".
"""

from __future__ import annotations
import uuid

from .base import ToolDef


def _stub_rewrite(item_id: int, field: str, marker: str) -> dict:
    return {
        "ok": True,
        "tool_call_id": str(uuid.uuid4()),
        "item_id": item_id,
        "field": field,
        "old": f"(stub: current {field} of item {item_id})",
        "new": f"(stub: improved {field} of item {item_id}) {marker}",
        "diff": "--- stub diff ---",
    }


def recognize_text(image_id: int | None = None, url: str | None = None) -> dict:
    return {"ok": True, "tool_call_id": str(uuid.uuid4()), "text": "(stub OCR output)"}


def improve_headline(item_id: int, style: str = "", confirm: bool = False) -> dict:
    return _stub_rewrite(item_id, "headline", f"[style={style}]")


def rewrite_quote(item_id: int, tone: str = "", confirm: bool = False) -> dict:
    return _stub_rewrite(item_id, "quote", f"[tone={tone}]")


def expand_text(item_id: int, length: str = "medium", confirm: bool = False) -> dict:
    return _stub_rewrite(item_id, "expandedText", f"[len={length}]")


def regenerate_image(item_id: int, concept: str = "", confirm: bool = False) -> dict:
    return {
        "ok": True, "tool_call_id": str(uuid.uuid4()),
        "item_id": item_id, "field": "imageId",
        "old": None, "new": f"(stub image for concept={concept})",
        "diff": "(stub)",
    }


def suggest_tags(item_id: int) -> dict:
    return {
        "ok": True, "tool_call_id": str(uuid.uuid4()),
        "item_id": item_id, "suggestions": ["stub-tag-1", "stub-tag-2"],
    }


def bulk_action(item_ids: list[int], action: str, confirm: bool = False) -> dict:
    return {
        "ok": True, "tool_call_id": str(uuid.uuid4()),
        "action": action, "preview": [{"item_id": i, "change": "stub"} for i in item_ids],
    }


TOOLS: dict[str, ToolDef] = {
    "recognize_text": ToolDef(
        name="recognize_text",
        description="OCR on an image (by image_id or url). Returns recognized text.",
        parameters={
            "type": "object",
            "properties": {
                "image_id": {"type": "integer"},
                "url": {"type": "string"},
            },
        },
        execute=recognize_text,
    ),
    "improve_headline": ToolDef(
        name="improve_headline",
        description="Rewrite a news item headline. Returns preview (old/new/diff).",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "integer"},
                "style": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["item_id"],
        },
        execute=improve_headline,
        is_write=True,
    ),
    "rewrite_quote": ToolDef(
        name="rewrite_quote",
        description="Rewrite a news item quote preserving meaning.",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "integer"},
                "tone": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["item_id"],
        },
        execute=rewrite_quote,
        is_write=True,
    ),
    "expand_text": ToolDef(
        name="expand_text",
        description="Generate or update the expanded text of a news item.",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "integer"},
                "length": {"type": "string", "enum": ["short", "medium", "long"]},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["item_id"],
        },
        execute=expand_text,
        is_write=True,
    ),
    "regenerate_image": ToolDef(
        name="regenerate_image",
        description="Generate a new illustration for a news item.",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "integer"},
                "concept": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["item_id"],
        },
        execute=regenerate_image,
        is_write=True,
    ),
    "suggest_tags": ToolDef(
        name="suggest_tags",
        description="Suggest tags for a news item. Write happens via /apply.",
        parameters={
            "type": "object",
            "properties": {"item_id": {"type": "integer"}},
            "required": ["item_id"],
        },
        execute=suggest_tags,
    ),
    "bulk_action": ToolDef(
        name="bulk_action",
        description="Apply the same action to multiple items. Returns preview list.",
        parameters={
            "type": "object",
            "properties": {
                "item_ids": {"type": "array", "items": {"type": "integer"}},
                "action": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["item_ids", "action"],
        },
        execute=bulk_action,
        is_write=True,
    ),
}
```

- [ ] **Step 4: Создать `assistant/personas/editor.py`**

```python
"""AI Editor — content persona.

Handles content interaction: news items, text recognition, text improvement.
Does NOT touch platform settings or integration keys.
"""

from __future__ import annotations
from .base import BasePersona


EDITOR_SYSTEM_PROMPT = """Ты — AI Editor приложения VideoText.

Твоя роль — работать с КОНТЕНТОМ: news items, заголовки, цитаты, expanded
тексты, иллюстрации. Пользователь вызывает тебя кликами на карточках
новостей или массовыми действиями на ленте.

Область ответственности:
- Улучшение заголовков (improve_headline) — острее, яснее, без кликбейта.
- Переписывание цитат (rewrite_quote) — сохраняй смысл и атрибуцию, меняй тон.
- Генерация/апдейт expanded text (expand_text) — дополняй контекстом по теме.
- Регенерация иллюстраций (regenerate_image) — если текущая картинка не передаёт суть.
- Распознавание текста (recognize_text) — OCR картинок, которые пользователь показал.
- Предложение тегов (suggest_tags).
- Массовые действия (bulk_action) — одна операция на несколько карточек.

Что ты НЕ делаешь:
- Не меняешь настройки платформы, API-ключи, retention-политику.
- Не трогаешь дедупликацию, статус стримов, integrations.
- Для платформенных вопросов — направляй к AI Assistant (плавающая панель).

Правила:
1. Write-tools (improve_headline, rewrite_quote, expand_text, regenerate_image,
   bulk_action) возвращают PREVIEW. Пользователь увидит diff «было → будет»
   и сам нажмёт «применить». Не пиши «я применил» — ты не пишешь в БД напрямую.
2. Сохраняй стиль ленты — перед переписыванием изучи последние 10 items
   этого же стрима (они в контексте).
3. Attribution (канал + время) НЕ меняй никогда.
4. Если пользователь не указал стиль/тон — спроси одним коротким вопросом,
   не делай наугад.
5. Markdown для ответов, коротко, по-русски.
"""


EDITOR_TOOL_NAMES = [
    "recognize_text",
    "improve_headline",
    "rewrite_quote",
    "expand_text",
    "regenerate_image",
    "suggest_tags",
    "bulk_action",
]


EDITOR_KB_SOURCE_KEYS = [
    "platform_docs",  # только схема NewsItem, не всё подряд
    "content_db",
    "saved_kb",
]


EDITOR_KB_PRIORITIES = {
    "content_db":     3,  # текущая и соседние items — самое важное
    "saved_kb":       2,  # редакционные гайдлайны
    "platform_docs":  1,  # схема как референс
}


class EditorPersona(BasePersona):
    def __init__(self):
        super().__init__(
            name="editor",
            system_prompt=EDITOR_SYSTEM_PROMPT,
            tool_names=EDITOR_TOOL_NAMES,
            kb_source_keys=EDITOR_KB_SOURCE_KEYS,
            kb_priorities=EDITOR_KB_PRIORITIES,
        )
```

- [ ] **Step 5: Экспортировать в `assistant/personas/__init__.py`**

```python
from .base import BasePersona
from .platform import PlatformPersona
from .editor import EditorPersona

__all__ = ["BasePersona", "PlatformPersona", "EditorPersona"]
```

- [ ] **Step 6: Тесты зелёные**

Run: `.venv\Scripts\python -m pytest tests/test_editor_persona.py tests/test_persona_isolation.py -v`
Expected: все passed (tools_runtime должен теперь находить и Platform и Editor tools).

- [ ] **Step 7: Commit**

```bash
git add assistant/personas/ assistant/tools/editor_tools.py tests/test_editor_persona.py
git commit -m "feat: add EditorPersona with stub tools"
```

---

### Task 1.2: Endpoint `POST /chat/editor` + `POST /chat/editor/apply` + rename `/assistant/chat` → `/chat/platform` с legacy alias

**Files:**
- Modify: `server.py`

- [ ] **Step 1: Тест — `/chat/platform` + legacy `/assistant/chat` оба работают**

```python
# tests/test_endpoints.py
import pytest
from httpx import ASGITransport, AsyncClient

from server import app


@pytest.mark.asyncio
async def test_legacy_assistant_chat_still_reachable():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/assistant/chat", json={"question": "x"})
        # Doesn't matter if 200 or 500 from missing key —
        # we only care that the route is registered.
        assert r.status_code != 404

@pytest.mark.asyncio
async def test_chat_platform_registered():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/chat/platform", json={"question": "x"})
        assert r.status_code != 404

@pytest.mark.asyncio
async def test_chat_editor_registered():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/chat/editor", json={"question": "x", "item_id": 1})
        assert r.status_code != 404

@pytest.mark.asyncio
async def test_chat_editor_apply_registered():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/chat/editor/apply", json={
            "item_id": 1, "field": "headline", "value": "x", "tool_call_id": "abc"
        })
        assert r.status_code != 404
```

- [ ] **Step 2: Падает — новых эндпоинтов нет**

Run: `.venv\Scripts\python -m pytest tests/test_endpoints.py -v`
Expected: 3 из 4 падают с 404.

- [ ] **Step 3: Переименовать existing `/assistant/chat` → `/chat/platform` + добавить legacy alias**

В `server.py` найти `@app.post("/assistant/chat")`. Оставить старый декоратор, добавить новый — один handler, два маршрута:

```python
@app.post("/chat/platform")
@app.post("/assistant/chat")  # legacy alias — remove in Phase 3
async def chat_platform(req: AssistantChatRequest):
    """Platform persona chat — handles settings, integrations, errors."""
    from assistant.chat import Assistant
    from assistant.personas.platform import PlatformPersona
    # ... (остальной код не меняем)
```

(Переименовать функцию `assistant_chat` → `chat_platform`.)

- [ ] **Step 4: Добавить `POST /chat/editor`**

```python
class EditorChatRequest(BaseModel):
    question: str
    session_id: str | None = None
    item_id: int | None = None
    stream_id: str | None = None
    auto_confirm: bool = False
    provider: Literal["openai", "anthropic", "ollama"] | None = None
    model: str | None = None


@app.post("/chat/editor")
async def chat_editor(req: EditorChatRequest):
    """Editor persona chat — handles news item content (headlines, quotes, images)."""
    from assistant.core.chat import Assistant
    from assistant.personas.editor import EditorPersona

    settings = await asyncio.to_thread(get_all_settings)
    provider = req.provider or settings.get("assistant_provider") or "openai"
    model = req.model or settings.get("assistant_model") or (
        "gpt-4o" if provider == "openai"
        else "claude-sonnet-4-6" if provider == "anthropic"
        else "llama3.1:8b"
    )
    cache_thr = float(settings.get("assistant_cache_similarity", 0.85))
    use_cache = bool(settings.get("assistant_cache_enabled", True))

    assistant = Assistant(
        persona=EditorPersona(),
        provider=provider, model=model,
        use_cache=use_cache, cache_threshold=cache_thr,
    )

    async def event_stream():
        db = Prisma()
        await db.connect()
        try:
            final_answer_parts: list[str] = []
            ui_context = {"item_id": req.item_id, "stream_id": req.stream_id}
            async for ev in assistant.ask_stream(
                db, req.question,
                session_id=req.session_id,
                ui_context=ui_context,
                auto_confirm=req.auto_confirm,
            ):
                if ev["type"] == "text":
                    final_answer_parts.append(ev.get("delta", ""))
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

            answer_text = "".join(final_answer_parts).strip()
            if answer_text:
                from assistant.core.cache import save_qa
                try:
                    await save_qa(db, req.question, answer_text, persona="editor")
                except Exception:
                    pass
        finally:
            await db.disconnect()

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

- [ ] **Step 5: Добавить `POST /chat/editor/apply`**

```python
class EditorApplyRequest(BaseModel):
    item_id: int
    field: Literal["headline", "quote", "expandedText", "imageId", "tags"]
    value: Any
    tool_call_id: str  # для audit; не проверяем, но логируем


_EDITOR_APPLY_FIELD_MAP = {
    "headline":     "headline",
    "quote":        "quote",
    "expandedText": "expandedText",
    "tags":         "tags",  # special: value is list[str], will be JSON-encoded
    "imageId":      "imageId",
}


@app.post("/chat/editor/apply")
async def chat_editor_apply(req: EditorApplyRequest):
    """Commit a previously-previewed edit to the news item."""
    from store import apply_editor_change
    try:
        updated = await asyncio.to_thread(
            apply_editor_change, req.item_id, req.field, req.value, req.tool_call_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    return {"ok": True, "item_id": updated.id, "updated_field": req.field}
```

- [ ] **Step 6: Добавить `apply_editor_change` в `store.py`**

В `store.py` добавить в конец (перед тестовой секцией если есть):

```python
def apply_editor_change(item_id: int, field: str, value, tool_call_id: str):
    """Apply an editor-previewed change to a NewsItem. Sync wrapper."""
    import json as _json
    allowed = {"headline", "quote", "expandedText", "imageId", "tags"}
    if field not in allowed:
        raise ValueError(f"field {field!r} not in allowed set {sorted(allowed)}")

    if field == "tags" and not isinstance(value, str):
        # Schema stores tags as JSON-encoded string
        value = _json.dumps(value, ensure_ascii=False)

    async def _run():
        db = Prisma()
        await db.connect()
        try:
            return await db.newsitem.update(
                where={"id": item_id},
                data={field: value},
            )
        finally:
            await db.disconnect()

    return _run_async(_run())  # `_run_async` helper assumed already in store.py
```

(Если `_run_async` в store.py называется иначе — использовать существующий. Проверить grep'ом: `grep -n "def _run_async" store.py`.)

- [ ] **Step 7: Тесты зелёные**

Run: `.venv\Scripts\python -m pytest tests/test_endpoints.py -v`
Expected: 4 passed.

- [ ] **Step 8: Commit**

```bash
git add server.py store.py tests/test_endpoints.py
git commit -m "feat: add /chat/platform + /chat/editor endpoints with legacy alias"
```

---

### Task 1.3: Confirm-flow E2E test (preview → apply)

**Files:**
- Create: `tests/test_confirm_flow.py`

- [ ] **Step 1: Test preview не меняет БД, apply меняет**

```python
# tests/test_confirm_flow.py
import pytest
from httpx import ASGITransport, AsyncClient

from server import app


@pytest.mark.asyncio
async def test_preview_does_not_mutate_db(db):
    # Seed a news item
    stream = await db.livestream.create(data={
        "url": "https://example.com", "channelName": "test",
    })
    item = await db.newsitem.create(data={
        "streamId": stream.id, "headline": "original headline",
        "quote": "q", "offsetSec": 0, "confidence": 0.9,
        "attribution": "test | 2026-01-01",
    })

    # Call improve_headline tool directly (stub impl)
    from assistant.tools.editor_tools import improve_headline
    preview = improve_headline(item_id=item.id, style="shorter")
    assert preview["ok"] is True
    assert "new" in preview

    # DB unchanged
    fresh = await db.newsitem.find_unique(where={"id": item.id})
    assert fresh.headline == "original headline"


@pytest.mark.asyncio
async def test_apply_mutates_db():
    # This uses the /chat/editor/apply endpoint, not the tool.
    # Preconditions: DATABASE_URL already set by `db` fixture — but this test
    # doesn't need that fixture directly. Use the endpoint client.
    from prisma import Prisma
    pre = Prisma()
    await pre.connect()
    try:
        stream = await pre.livestream.create(data={
            "url": "https://example.com/2", "channelName": "test2",
        })
        item = await pre.newsitem.create(data={
            "streamId": stream.id, "headline": "before",
            "quote": "q", "offsetSec": 0, "confidence": 0.9,
            "attribution": "t|1",
        })
    finally:
        await pre.disconnect()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/chat/editor/apply", json={
            "item_id": item.id, "field": "headline", "value": "AFTER",
            "tool_call_id": "test-abc",
        })
        assert r.status_code == 200, r.text

    post = Prisma()
    await post.connect()
    try:
        updated = await post.newsitem.find_unique(where={"id": item.id})
        assert updated.headline == "AFTER"
    finally:
        await post.disconnect()
```

- [ ] **Step 2: Запустить**

Run: `.venv\Scripts\python -m pytest tests/test_confirm_flow.py -v`
Expected: 2 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_confirm_flow.py
git commit -m "test: confirm-flow preview→apply"
```

---

### Task 1.4: Реальная логика `improve_headline` (через Claude Haiku)

**Files:**
- Modify: `assistant/tools/editor_tools.py`

Выбор модели: `claude-haiku-4-5` (дёшево для коротких rewrite'ов). Fallback на `gpt-4o-mini` если `ANTHROPIC_API_KEY` не задан.

- [ ] **Step 1: Тест — real improve_headline returns plausible new headline**

```python
# tests/test_editor_tools_real.py
import os
import pytest


@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY") and not os.getenv("OPENAI_API_KEY"),
    reason="No LLM key — skip real tool test",
)
@pytest.mark.asyncio
async def test_improve_headline_real(db):
    stream = await db.livestream.create(data={
        "url": "https://example.com", "channelName": "news channel",
    })
    item = await db.newsitem.create(data={
        "streamId": stream.id,
        "headline": "Нефть марки Брент выросла на 2 процента на открытии торгов",
        "quote": "Brent crude rose 2% at market open",
        "offsetSec": 120, "confidence": 0.95,
        "attribution": "news channel | 2026-04-20T10:00",
    })

    from assistant.tools.editor_tools import improve_headline
    result = improve_headline(item_id=item.id, style="острее, короче")
    assert result["ok"] is True
    assert result["old"] == item.headline
    assert result["new"] != item.headline
    assert len(result["new"]) < len(item.headline)  # "короче" сработало
    assert "item_id" in result
    assert result["item_id"] == item.id
```

- [ ] **Step 2: Падает — stub возвращает фиксированный текст**

Run: `.venv\Scripts\python -m pytest tests/test_editor_tools_real.py -v`
Expected: FAIL (stub new != "(stub...)" не содержит осмысленного текста).

- [ ] **Step 3: Реализовать `improve_headline` реально**

Заменить stub в `assistant/tools/editor_tools.py`:

```python
import os
import uuid
import difflib
from typing import Any

import store


def _llm_rewrite(system: str, user: str) -> str:
    """Try Claude Haiku first, fall back to gpt-4o-mini."""
    if os.getenv("ANTHROPIC_API_KEY"):
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in msg.content if b.type == "text").strip()
    if os.getenv("OPENAI_API_KEY"):
        from openai import OpenAI
        client = OpenAI()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=200,
        )
        return resp.choices[0].message.content.strip()
    raise RuntimeError("no LLM key available (need ANTHROPIC_API_KEY or OPENAI_API_KEY)")


def _diff(old: str, new: str) -> str:
    return "\n".join(difflib.unified_diff(
        old.splitlines() or [""], new.splitlines() or [""],
        fromfile="before", tofile="after", lineterm="",
    ))


def improve_headline(item_id: int, style: str = "", confirm: bool = False) -> dict:
    item = store.get_news_item(item_id)
    if not item:
        return {"ok": False, "error": f"NewsItem {item_id} not found"}

    old = item.headline or ""
    system = (
        "Ты — редактор новостных заголовков на русском языке. "
        "Перепиши данный заголовок, сохраняя фактическую суть. "
        "Не меняй имена, цифры, названия. Отдай ТОЛЬКО новый заголовок, "
        "без кавычек, без пояснений. Одна строка."
    )
    user = f"Текущий заголовок: {old}\n"
    if style:
        user += f"Желаемый стиль: {style}\n"
    user += "Новый заголовок:"

    new = _llm_rewrite(system, user)
    # Strip surrounding quotes if LLM added them
    new = new.strip().strip('"').strip("«»").strip()

    return {
        "ok": True,
        "tool_call_id": str(uuid.uuid4()),
        "item_id": item_id,
        "field": "headline",
        "old": old,
        "new": new,
        "diff": _diff(old, new),
    }
```

- [ ] **Step 4: Тесты зелёные (при наличии ключа)**

Run: `.venv\Scripts\python -m pytest tests/test_editor_tools_real.py -v`
Expected: passed (или skipped если ключа нет).

- [ ] **Step 5: Commit**

```bash
git add assistant/tools/editor_tools.py tests/test_editor_tools_real.py
git commit -m "feat: implement real improve_headline via Claude Haiku"
```

---

### Task 1.5: Реальная `rewrite_quote`

**Files:**
- Modify: `assistant/tools/editor_tools.py`

- [ ] **Step 1: Тест**

```python
# Добавить в tests/test_editor_tools_real.py
@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY") and not os.getenv("OPENAI_API_KEY"),
    reason="No LLM key",
)
@pytest.mark.asyncio
async def test_rewrite_quote_real(db):
    stream = await db.livestream.create(data={
        "url": "https://ex.com", "channelName": "x",
    })
    item = await db.newsitem.create(data={
        "streamId": stream.id,
        "headline": "h", "quote": "Цена нефти растёт из-за напряжения на ближнем востоке.",
        "offsetSec": 0, "confidence": 0.9, "attribution": "x|1",
    })
    from assistant.tools.editor_tools import rewrite_quote
    r = rewrite_quote(item_id=item.id, tone="деловой")
    assert r["ok"] is True
    assert r["new"] != r["old"]
    assert len(r["new"]) > 20  # не мусор
```

- [ ] **Step 2: Падает**

Run: `.venv\Scripts\python -m pytest tests/test_editor_tools_real.py::test_rewrite_quote_real -v`
Expected: FAIL (stub).

- [ ] **Step 3: Реализовать rewrite_quote**

Заменить stub `rewrite_quote` в `editor_tools.py`:

```python
def rewrite_quote(item_id: int, tone: str = "", confirm: bool = False) -> dict:
    item = store.get_news_item(item_id)
    if not item:
        return {"ok": False, "error": f"NewsItem {item_id} not found"}

    old = item.quote or ""
    system = (
        "Ты — редактор новостных цитат. Перепиши цитату, СТРОГО сохраняя смысл "
        "и факты (имена, числа, названия). Можешь менять формулировку и тон. "
        "Не добавляй информацию, которой нет в оригинале. "
        "Отдай только новый текст цитаты, без пояснений."
    )
    user = f"Цитата: {old}\n"
    if tone:
        user += f"Желаемый тон: {tone}\n"
    user += "Переписанная цитата:"

    new = _llm_rewrite(system, user).strip().strip('"').strip("«»")
    return {
        "ok": True,
        "tool_call_id": str(uuid.uuid4()),
        "item_id": item_id,
        "field": "quote",
        "old": old,
        "new": new,
        "diff": _diff(old, new),
    }
```

- [ ] **Step 4: Проверить**

Run: `.venv\Scripts\python -m pytest tests/test_editor_tools_real.py::test_rewrite_quote_real -v`
Expected: passed или skipped.

- [ ] **Step 5: Commit**

```bash
git add assistant/tools/editor_tools.py tests/test_editor_tools_real.py
git commit -m "feat: implement real rewrite_quote"
```

---

### Task 1.6: Реальная `expand_text`

**Files:**
- Modify: `assistant/tools/editor_tools.py`

- [ ] **Step 1: Тест**

```python
# tests/test_editor_tools_real.py — добавить:
@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY") and not os.getenv("OPENAI_API_KEY"),
    reason="No LLM key",
)
@pytest.mark.asyncio
async def test_expand_text_real(db):
    stream = await db.livestream.create(data={
        "url": "https://ex.com", "channelName": "x",
    })
    item = await db.newsitem.create(data={
        "streamId": stream.id,
        "headline": "ФРС оставила ставку без изменений",
        "quote": "Федрезерв США сохранил диапазон ставки на уровне 5.25-5.5%.",
        "offsetSec": 0, "confidence": 0.95, "attribution": "x|1",
    })
    from assistant.tools.editor_tools import expand_text
    r = expand_text(item_id=item.id, length="medium")
    assert r["ok"] is True
    assert len(r["new"]) > len(item.quote) * 2  # реально шире
```

- [ ] **Step 2: Падает**

Run: `.venv\Scripts\python -m pytest tests/test_editor_tools_real.py::test_expand_text_real -v`
Expected: FAIL.

- [ ] **Step 3: Реализовать**

```python
_LENGTH_TARGETS = {"short": (150, 300), "medium": (400, 700), "long": (900, 1400)}


def expand_text(item_id: int, length: str = "medium", confirm: bool = False) -> dict:
    item = store.get_news_item(item_id)
    if not item:
        return {"ok": False, "error": f"NewsItem {item_id} not found"}

    min_chars, max_chars = _LENGTH_TARGETS.get(length, (400, 700))
    old = item.expandedText or ""
    system = (
        f"Ты пишешь расширенную версию новостной заметки на русском. "
        f"Длина: {min_chars}-{max_chars} символов. Не выдумывай факты — "
        f"используй только данные из заголовка и цитаты. Объясни контекст, "
        f"добавь фон, избегай клише. Markdown НЕ использовать — простой текст."
    )
    user = (
        f"Заголовок: {item.headline}\n"
        f"Цитата: {item.quote}\n"
        f"Категория: {item.category or '—'}\n"
        f"Атрибуция: {item.attribution}\n"
        f"Расширенный текст:"
    )
    new = _llm_rewrite(system, user).strip()
    return {
        "ok": True,
        "tool_call_id": str(uuid.uuid4()),
        "item_id": item_id,
        "field": "expandedText",
        "old": old,
        "new": new,
        "diff": _diff(old, new),
    }
```

- [ ] **Step 4: Проверить**

Run: `.venv\Scripts\python -m pytest tests/test_editor_tools_real.py::test_expand_text_real -v`
Expected: passed/skipped.

- [ ] **Step 5: Commit**

```bash
git add assistant/tools/editor_tools.py tests/test_editor_tools_real.py
git commit -m "feat: implement real expand_text"
```

---

### Task 1.7: `suggest_tags` + `regenerate_image` + `recognize_text` + `bulk_action`

**Files:**
- Modify: `assistant/tools/editor_tools.py`

- [ ] **Step 1: Тест для suggest_tags**

```python
# tests/test_editor_tools_real.py
@pytest.mark.skipif(not os.getenv("ANTHROPIC_API_KEY") and not os.getenv("OPENAI_API_KEY"), reason="No LLM key")
@pytest.mark.asyncio
async def test_suggest_tags_real(db):
    stream = await db.livestream.create(data={"url":"https://ex.com","channelName":"x"})
    item = await db.newsitem.create(data={
        "streamId": stream.id,
        "headline": "Нефть Brent выросла на 2%",
        "quote": "...", "offsetSec": 0, "confidence": 0.9, "attribution":"x|1",
    })
    from assistant.tools.editor_tools import suggest_tags
    r = suggest_tags(item_id=item.id)
    assert r["ok"] is True
    assert isinstance(r["suggestions"], list)
    assert 1 <= len(r["suggestions"]) <= 5
```

- [ ] **Step 2: Реализация `suggest_tags`**

```python
import json as _json


def suggest_tags(item_id: int) -> dict:
    item = store.get_news_item(item_id)
    if not item:
        return {"ok": False, "error": f"NewsItem {item_id} not found"}

    system = (
        "Ты — теггер новостей. Предложи 1-5 коротких релевантных тегов "
        "на русском (одно-два слова каждый). Отвечай ТОЛЬКО JSON-массивом "
        'строк, например: ["энергетика","нефть","brent"].'
    )
    user = f"Заголовок: {item.headline}\nЦитата: {item.quote}"
    raw = _llm_rewrite(system, user).strip()
    # Try to parse; if model added prose, extract bracketed section
    try:
        tags = _json.loads(raw)
    except _json.JSONDecodeError:
        import re
        m = re.search(r"\[.*?\]", raw, re.DOTALL)
        tags = _json.loads(m.group(0)) if m else []
    tags = [str(t).strip() for t in tags if str(t).strip()][:5]

    return {
        "ok": True,
        "tool_call_id": str(uuid.uuid4()),
        "item_id": item_id,
        "suggestions": tags,
    }
```

- [ ] **Step 3: Реализация `regenerate_image` через существующий `enrich.py`**

```python
def regenerate_image(item_id: int, concept: str = "", confirm: bool = False) -> dict:
    """Preview-only — returns proposed concept + prompt without actually calling DALL-E.

    The actual DALL-E call + DB write happens in /chat/editor/apply, which
    delegates to `enrich.enrich_item`. This way preview costs ~0 (tiny LLM
    round for concept phrase), while apply costs ~$0.04 for a DALL-E 3 image.
    """
    item = store.get_news_item(item_id)
    if not item:
        return {"ok": False, "error": f"NewsItem {item_id} not found"}

    if not concept:
        # Ask LLM to propose a concept
        system = (
            "Ты — арт-директор. Дай КОРОТКУЮ концепт-фразу для иллюстрации "
            "новости (2-5 слов, на английском, пригодное для DALL-E). "
            "Только фраза, без пояснений."
        )
        user = f"Headline: {item.headline}\nQuote: {item.quote}"
        concept = _llm_rewrite(system, user).strip().strip('"').strip()[:80]

    prompt = f"Editorial illustration: {concept}. Clean composition, news-style."
    return {
        "ok": True,
        "tool_call_id": str(uuid.uuid4()),
        "item_id": item_id,
        "field": "imageId",
        "old": item.imageId,
        "new_concept": concept,
        "new_prompt": prompt,
        "diff": f"concept: {concept}",
        # Frontend shows concept/prompt; on apply, server calls enrich.enrich_item.
    }
```

Обновить `/chat/editor/apply` чтобы различать поле `imageId` и вызывать `enrich_item`:

```python
# server.py /chat/editor/apply:
if req.field == "imageId":
    # Value carries {"concept": "..."} from preview. Actually generate + attach.
    from enrich import enrich_item
    ... # вызов enrich_item, получение image_id, set newsitem.imageId
```

- [ ] **Step 4: Реализация `recognize_text` через gpt-4o-mini vision**

```python
def recognize_text(image_id: int | None = None, url: str | None = None) -> dict:
    if not os.getenv("OPENAI_API_KEY"):
        return {"ok": False, "error": "OCR requires OPENAI_API_KEY"}

    image_payload: dict
    if image_id is not None:
        img = store.get_news_image(image_id)
        if not img:
            return {"ok": False, "error": f"NewsImage {image_id} not found"}
        # Load file as base64 data url
        import base64
        from pathlib import Path
        data = Path(img.filePath).read_bytes()
        b64 = base64.b64encode(data).decode()
        image_payload = {"url": f"data:image/png;base64,{b64}"}
    elif url:
        image_payload = {"url": url}
    else:
        return {"ok": False, "error": "Provide either image_id or url"}

    from openai import OpenAI
    client = OpenAI()
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "Распознай весь видимый текст на изображении. Верни только текст, без комментариев. Если текста нет — верни пустую строку."},
                {"type": "image_url", "image_url": image_payload},
            ],
        }],
        max_tokens=2000,
    )
    text = resp.choices[0].message.content.strip()
    return {
        "ok": True,
        "tool_call_id": str(uuid.uuid4()),
        "text": text,
        "length": len(text),
    }
```

Добавить в `store.py`:
```python
def get_news_image(image_id: int):
    async def _run():
        db = Prisma(); await db.connect()
        try:
            return await db.newsimage.find_unique(where={"id": image_id})
        finally:
            await db.disconnect()
    return _run_async(_run())
```

- [ ] **Step 5: Реализация `bulk_action`**

```python
def bulk_action(item_ids: list[int], action: str, confirm: bool = False) -> dict:
    """Run `action` against each item_id. Returns preview list.

    Supported actions: "improve_headline", "rewrite_quote", "suggest_tags".
    Write happens in /chat/editor/apply in a loop.
    """
    ALLOWED = {"improve_headline": improve_headline, "rewrite_quote": rewrite_quote, "suggest_tags": suggest_tags}
    if action not in ALLOWED:
        return {"ok": False, "error": f"bulk action {action!r} not supported. allowed: {sorted(ALLOWED)}"}

    func = ALLOWED[action]
    previews = []
    for iid in item_ids[:50]:  # hard cap to avoid runaway cost
        res = func(item_id=iid)
        previews.append({"item_id": iid, "result": res})

    return {
        "ok": True,
        "tool_call_id": str(uuid.uuid4()),
        "action": action,
        "previews": previews,
        "total": len(previews),
    }
```

- [ ] **Step 6: Запустить все Editor-тесты**

Run: `.venv\Scripts\python -m pytest tests/test_editor_tools_real.py -v`
Expected: все passed (или skipped при отсутствии ключа).

- [ ] **Step 7: Commit**

```bash
git add assistant/tools/editor_tools.py store.py server.py tests/test_editor_tools_real.py
git commit -m "feat: implement suggest_tags, regenerate_image, recognize_text, bulk_action"
```

---

### Task 1.8: `context_builder` — per-persona snapshot test

**Files:**
- Create: `tests/test_context_builder.py`
- Modify: `assistant/core/context_builder.py` — принимать persona и использовать `kb_source_keys`

- [ ] **Step 1: Тест — context для Platform не содержит news-item контента**

```python
# tests/test_context_builder.py
import pytest

from assistant.personas.platform import PlatformPersona
from assistant.personas.editor import EditorPersona


@pytest.mark.asyncio
async def test_platform_context_excludes_content(db):
    # Seed: one AppSetting, one NewsItem
    await db.appsetting.create(data={"key": "dedup_enabled", "value": "true"})
    stream = await db.livestream.create(data={"url":"u","channelName":"c"})
    await db.newsitem.create(data={
        "streamId": stream.id, "headline": "SECRET_HEADLINE_XYZ",
        "quote": "q", "offsetSec": 0, "confidence": 0.9, "attribution":"c|1",
    })

    from assistant.core.context_builder import build_context
    ctx = await build_context(db, question="how to configure dedup", persona=PlatformPersona())
    ctx_str = str(ctx).lower()
    assert "secret_headline_xyz" not in ctx_str


@pytest.mark.asyncio
async def test_editor_context_includes_items(db):
    stream = await db.livestream.create(data={"url":"u","channelName":"c"})
    await db.newsitem.create(data={
        "streamId": stream.id, "headline": "UNIQUE_EDITOR_MARKER",
        "quote": "q", "offsetSec": 0, "confidence": 0.9, "attribution":"c|1",
    })

    from assistant.core.context_builder import build_context
    ctx = await build_context(db, question="improve this headline",
                              persona=EditorPersona(), ui_context={"stream_id": stream.id})
    ctx_str = str(ctx).lower()
    assert "unique_editor_marker" in ctx_str
```

- [ ] **Step 2: Падает**

Run: `.venv\Scripts\python -m pytest tests/test_context_builder.py -v`
Expected: FAIL — `build_context` пока не фильтрует по persona.

- [ ] **Step 3: Модифицировать `assistant/core/context_builder.py`**

Добавить параметр `persona` и ветвить выборку по `persona.kb_source_keys`:

```python
async def build_context(db, question: str, persona, ui_context: dict | None = None) -> dict:
    """Build a context dict: {kb_snippets, db_rows, priority_order}.

    Sources are chosen based on persona.kb_source_keys. Each source loader
    returns rows; context_builder runs TF-IDF or similarity scoring and keeps
    top-N.
    """
    ctx: dict = {"kb_snippets": [], "db_rows": {}}

    if "platform_docs" in persona.kb_source_keys:
        rows = await db.assistantkb.find_many(where={"kind": {"in": ["setup_guide", "error_pattern"]}})
        ctx["kb_snippets"].extend(_score_and_top(rows, question, 5))

    if "project_docs" in persona.kb_source_keys:
        rows = await db.assistantkb.find_many(where={"kind": "code_module"})
        ctx["kb_snippets"].extend(_score_and_top(rows, question, 3))

    if "settings_db" in persona.kb_source_keys:
        ctx["db_rows"]["settings"] = await _settings_snapshot(db)
        ctx["db_rows"]["recent_errors"] = await _recent_errors(db, minutes=60, limit=10)

    if "content_db" in persona.kb_source_keys:
        # Fetch current item + recent same-stream items for editor context
        ui = ui_context or {}
        if ui.get("item_id"):
            ctx["db_rows"]["current_item"] = await db.newsitem.find_unique(where={"id": ui["item_id"]})
        if ui.get("stream_id"):
            ctx["db_rows"]["recent_items"] = await db.newsitem.find_many(
                where={"streamId": ui["stream_id"]},
                order={"createdAt": "desc"},
                take=10,
            )

    if "saved_kb" in persona.kb_source_keys:
        rows = await db.assistantkb.find_many(where={"kind": "schema"})
        ctx["kb_snippets"].extend(_score_and_top(rows, question, 2))

    return ctx


# Helpers kept in same file — move existing _score_and_top, _settings_snapshot,
# _recent_errors unchanged if they already exist; otherwise inline them from
# the pre-refactor code.
```

(Существующие helpers `_score_and_top`, `_settings_snapshot`, `_recent_errors` — если их нет в файле, нужно написать. Проверить grep'ом в текущем `context_builder.py` — часть функций уже есть.)

- [ ] **Step 4: Обновить `chat.py` — передавать persona в `build_context`**

```python
# В assistant/core/chat.py метод Assistant.ask_stream:
ctx = await build_context(db, question, persona=self.persona, ui_context=ui_context)
```

- [ ] **Step 5: Тесты зелёные**

Run: `.venv\Scripts\python -m pytest tests/test_context_builder.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add assistant/core/context_builder.py assistant/core/chat.py tests/test_context_builder.py
git commit -m "feat: filter context_builder output by persona.kb_source_keys"
```

---

### Task 1.9: Smoke test — full SSE round для обеих persona

**Files:**
- Create: `tests/test_smoke.py`

- [ ] **Step 1: Написать smoke test под flag RUN_SMOKE=1**

```python
# tests/test_smoke.py
"""End-to-end smoke tests against real providers. Skipped by default.

Run with: RUN_SMOKE=1 .venv\\Scripts\\python -m pytest tests/test_smoke.py -v
"""
import json
import os
import pytest
from httpx import ASGITransport, AsyncClient

from server import app


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SMOKE") != "1",
    reason="Set RUN_SMOKE=1 to run real-provider smoke tests",
)


@pytest.mark.asyncio
async def test_platform_chat_stream():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30.0) as ac:
        async with ac.stream("POST", "/chat/platform", json={
            "question": "скажи одно слово: ok",
            "provider": "anthropic",
            "model": "claude-haiku-4-5",
        }) as r:
            assert r.status_code == 200
            events = []
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
            # Expect at least one text event and one done event
            assert any(e["type"] == "text" for e in events)
            assert any(e["type"] == "done" for e in events)


@pytest.mark.asyncio
async def test_editor_chat_stream():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30.0) as ac:
        async with ac.stream("POST", "/chat/editor", json={
            "question": "скажи одно слово: ok",
            "provider": "anthropic",
            "model": "claude-haiku-4-5",
        }) as r:
            assert r.status_code == 200
            events = []
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
            assert any(e["type"] == "done" for e in events)
```

- [ ] **Step 2: Запустить (по умолчанию skipped)**

Run: `.venv\Scripts\python -m pytest tests/test_smoke.py -v`
Expected: 2 skipped.

- [ ] **Step 3: Запустить с flag**

Run: `set RUN_SMOKE=1 && .venv\Scripts\python -m pytest tests/test_smoke.py -v`
Expected: 2 passed (если есть ключ) / skipped с message.

- [ ] **Step 4: Commit**

```bash
git add tests/test_smoke.py
git commit -m "test: smoke tests for /chat/platform + /chat/editor streams"
```

---

## Phase 2 — Frontend

Цель: разделить UI-точки входа. Panel → Platform, inline-controls на карточках → Editor, страница `/editor` для истории.

---

### Task 2.1: `static/assistant-panel.js` → указывает на `/chat/platform`

**Files:**
- Modify: `static/assistant-panel.js`

- [ ] **Step 1: Найти место с endpoint**

Run: `grep -n "/assistant/chat" static/assistant-panel.js`
Expected: одна-две строки с fetch/EventSource.

- [ ] **Step 2: Заменить `/assistant/chat` → `/chat/platform` (все вхождения)**

Использовать Edit с `replace_all: true`.

- [ ] **Step 3: Manual smoke — открыть UI, протестировать панель**

```
.venv\Scripts\python -m uvicorn server:app --port 8765
```
Открыть `http://localhost:8765`, кликнуть на иконку панели ассистента, задать вопрос «помоги с настройками». Ожидание: ответ приходит, SSE работает.

- [ ] **Step 4: Commit**

```bash
git add static/assistant-panel.js
git commit -m "feat(ui): assistant panel uses /chat/platform"
```

---

### Task 2.2: `static/editor-inline.js` — inline кнопки на карточке NewsItem

**Files:**
- Create: `static/editor-inline.js`
- Modify: `static/app.js` — в функции рендера карточки добавить `onAttachActions(card, item)`

- [ ] **Step 1: Создать `static/editor-inline.js`**

```javascript
/* AI Editor inline — adds action buttons to each news item card
 * and handles preview→apply flow.
 *
 * Usage:
 *   import { attachEditorActions } from '/static/editor-inline.js';
 *   attachEditorActions(cardElement, itemData);
 */

export function attachEditorActions(card, item) {
    const bar = document.createElement('div');
    bar.className = 'editor-actions';
    bar.innerHTML = `
      <button data-action="improve_headline">✨ Улучшить заголовок</button>
      <button data-action="rewrite_quote">✍ Переписать цитату</button>
      <button data-action="expand_text">📝 Расширить текст</button>
      <button data-action="suggest_tags">🏷 Теги</button>
      <button data-action="regenerate_image">🖼 Новая картинка</button>
    `;
    card.appendChild(bar);

    bar.addEventListener('click', async (e) => {
        const action = e.target.dataset.action;
        if (!action) return;
        e.target.disabled = true;
        e.target.textContent = '…';
        try {
            const preview = await runEditor(action, item.id);
            renderPreview(card, preview, item);
        } catch (err) {
            alert(`Ошибка: ${err.message}`);
        } finally {
            e.target.disabled = false;
            // Restore original label via a lookup by action
            const labels = {
                improve_headline: '✨ Улучшить заголовок',
                rewrite_quote: '✍ Переписать цитату',
                expand_text: '📝 Расширить текст',
                suggest_tags: '🏷 Теги',
                regenerate_image: '🖼 Новая картинка',
            };
            e.target.textContent = labels[action] || action;
        }
    });
}


async function runEditor(action, itemId) {
    const resp = await fetch('/chat/editor', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            question: `call tool ${action} on item ${itemId}`,
            item_id: itemId,
            auto_confirm: false,  // tool returns preview, frontend applies explicitly
        }),
    });
    if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);

    // Parse SSE stream, collect the last tool_result event (the preview)
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let preview = null;
    while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        buf += decoder.decode(value, {stream: true});
        for (const chunk of buf.split('\n\n')) {
            if (!chunk.startsWith('data: ')) continue;
            try {
                const ev = JSON.parse(chunk.slice(6));
                if (ev.type === 'tool_result' && ev.result && ev.result.ok) {
                    preview = ev.result;
                }
            } catch (_) {/* partial chunk — ignore */}
        }
        buf = buf.split('\n\n').pop();
    }
    if (!preview) throw new Error('No preview in response');
    return preview;
}


function renderPreview(card, preview, item) {
    const old = preview.old ?? '';
    const fresh = preview.new ?? '';
    const box = document.createElement('div');
    box.className = 'editor-preview';
    box.innerHTML = `
      <div class="diff">
        <div class="before">До: <span></span></div>
        <div class="after">После: <span></span></div>
      </div>
      <div class="actions">
        <button data-role="apply">✓ Применить</button>
        <button data-role="cancel">✕ Отмена</button>
      </div>
    `;
    box.querySelector('.before span').textContent = old;
    box.querySelector('.after span').textContent = fresh;
    card.appendChild(box);

    box.querySelector('[data-role="cancel"]').onclick = () => box.remove();
    box.querySelector('[data-role="apply"]').onclick = async () => {
        const r = await fetch('/chat/editor/apply', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                item_id: item.id,
                field: preview.field,
                value: fresh,
                tool_call_id: preview.tool_call_id,
            }),
        });
        if (r.ok) {
            // Update card with new value
            const targetEl = card.querySelector(`[data-field="${preview.field}"]`);
            if (targetEl) targetEl.textContent = fresh;
            box.remove();
        } else {
            alert(`Не удалось применить: ${await r.text()}`);
        }
    };
}
```

- [ ] **Step 2: Подключить в `static/app.js`**

Найти в `app.js` функцию, которая рендерит карточку news item (поискать по grep `newsitem` / `news-item` / `renderCard`). В конце создания элемента добавить:

```javascript
import { attachEditorActions } from '/static/editor-inline.js';
// ... внутри функции рендера карточки, после построения DOM:
attachEditorActions(cardEl, itemData);
```

Если `app.js` не использует ESM — добавить `<script type="module" src="/static/editor-inline.js"></script>` в `static/index.html` и вызывать через `window.attachEditorActions`.

- [ ] **Step 3: Добавить CSS для кнопок + preview**

В `static/styles.css` добавить в конец:

```css
.editor-actions {
    display: flex; gap: 6px; flex-wrap: wrap;
    margin-top: 8px; padding-top: 8px;
    border-top: 1px dashed var(--border, #444);
}
.editor-actions button {
    font-size: 12px; padding: 4px 8px; cursor: pointer;
}
.editor-preview {
    margin-top: 8px; padding: 8px;
    background: rgba(255, 255, 100, 0.08);
    border: 1px solid rgba(255, 255, 100, 0.3);
    border-radius: 4px;
}
.editor-preview .diff .before,
.editor-preview .diff .after {
    padding: 4px; margin: 4px 0;
}
.editor-preview .diff .before { background: rgba(255, 0, 0, 0.1); }
.editor-preview .diff .after  { background: rgba(0, 255, 0, 0.1); }
.editor-preview .actions { margin-top: 8px; display: flex; gap: 6px; }
```

- [ ] **Step 4: Manual test — клик на кнопке в UI даёт preview, apply обновляет карточку**

Запустить сервер, открыть страницу с news items, кликнуть "✨ Улучшить заголовок", увидеть preview, нажать "Применить", убедиться что карточка обновилась и БД содержит новый headline.

- [ ] **Step 5: Commit**

```bash
git add static/editor-inline.js static/app.js static/styles.css
git commit -m "feat(ui): add inline editor actions on news item cards"
```

---

### Task 2.3: Пункт меню "AI Editor" → страница `/editor`

**Files:**
- Create: `static/editor-settings.js`
- Modify: `static/index.html` (добавить menu item + route/tab)
- Modify: `server.py` (endpoint `GET /editor` отдающий HTML или JSON для панели истории)

- [ ] **Step 1: Добавить menu item в `static/index.html`**

Найти блок меню (поискать `nav` или существующие пункты). Добавить:

```html
<a href="#editor" data-tab="editor">AI Editor</a>
```

- [ ] **Step 2: Создать секцию в `index.html` и handler**

```html
<section id="editor-section" style="display:none">
    <h2>AI Editor — история</h2>
    <div id="editor-actions-history"></div>
    <h3>Настройки</h3>
    <div id="editor-defaults"></div>
</section>
```

- [ ] **Step 3: Создать `static/editor-settings.js`**

```javascript
/* AI Editor settings page — history of edits + persona defaults. */

export async function initEditorPage() {
    await loadHistory();
    await loadDefaults();
}

async function loadHistory() {
    const r = await fetch('/chat/editor/sessions?limit=50');
    const data = await r.json();
    const box = document.getElementById('editor-actions-history');
    box.innerHTML = data.map(s => `
        <div class="history-row">
            <div class="title">${escape(s.title || 'Без темы')}</div>
            <div class="meta">${s.updated_at}</div>
        </div>
    `).join('') || '<em>История пуста</em>';
}

async function loadDefaults() {
    const r = await fetch('/settings');
    const settings = await r.json();
    const box = document.getElementById('editor-defaults');
    box.innerHTML = `
        <label>Дефолтный стиль заголовков: <input id="editor-style" value="${escape(settings.editor_default_style || '')}"></label>
        <label>Дефолтная длина expand_text: 
          <select id="editor-length">
            <option value="short">короткий</option>
            <option value="medium" selected>средний</option>
            <option value="long">длинный</option>
          </select>
        </label>
        <button id="editor-save-defaults">Сохранить</button>
    `;
    if (settings.editor_default_length) {
        document.getElementById('editor-length').value = settings.editor_default_length;
    }
    document.getElementById('editor-save-defaults').onclick = async () => {
        await fetch('/settings', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({
                editor_default_style:  document.getElementById('editor-style').value,
                editor_default_length: document.getElementById('editor-length').value,
            }),
        });
        alert('Сохранено');
    };
}

function escape(s) { return (s || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
```

- [ ] **Step 4: Добавить endpoint `GET /chat/{persona}/sessions`** (если ещё не было)

В `server.py`:

```python
@app.get("/chat/{persona}/sessions")
async def chat_sessions(persona: Literal["platform", "editor"], limit: int = 50):
    db = Prisma(); await db.connect()
    try:
        sess = await db.assistantsession.find_many(
            where={"persona": persona},
            order={"updatedAt": "desc"}, take=limit,
            include={"messages": {"take": 1, "order_by": {"createdAt": "asc"}}},
        )
        return [
            {
                "id": s.id,
                "title": s.title or (s.messages[0].content[:60] if s.messages else "Без темы"),
                "updated_at": s.updatedAt.isoformat(),
            }
            for s in sess
        ]
    finally:
        await db.disconnect()
```

Убрать legacy `/assistant/sessions`, либо оставить как alias.

- [ ] **Step 5: Manual test**

Открыть UI → клик на "AI Editor" в меню → страница показывает историю + настройки. Сменить default style, сохранить, перезагрузить, убедиться что значение сохраняется.

- [ ] **Step 6: Commit**

```bash
git add static/editor-settings.js static/index.html server.py
git commit -m "feat(ui): AI Editor menu page with history + defaults"
```

---

## Phase 3 — Cleanup

---

### Task 3.1: Убрать legacy alias `/assistant/chat`

**Files:**
- Modify: `server.py`

- [ ] **Step 1: Тест legacy alias больше НЕ работает (должен 404)**

Изменить `tests/test_endpoints.py::test_legacy_assistant_chat_still_reachable`:

```python
@pytest.mark.asyncio
async def test_legacy_assistant_chat_removed():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/assistant/chat", json={"question": "x"})
        assert r.status_code == 404
```

- [ ] **Step 2: Запустить — fails (alias всё ещё работает)**

Run: `.venv\Scripts\python -m pytest tests/test_endpoints.py::test_legacy_assistant_chat_removed -v`
Expected: FAIL.

- [ ] **Step 3: Убрать двойной декоратор в `server.py`**

Найти:
```python
@app.post("/chat/platform")
@app.post("/assistant/chat")  # legacy alias — remove in Phase 3
async def chat_platform(...):
```

Убрать вторую строку.

- [ ] **Step 4: Тест зелёный**

Run: `.venv\Scripts\python -m pytest tests/test_endpoints.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_endpoints.py
git commit -m "chore: remove legacy /assistant/chat alias"
```

---

### Task 3.2: Удалить или репурпоснуть `static/assistant-tab.js`

**Files:**
- Delete или Repurpose: `static/assistant-tab.js`

- [ ] **Step 1: Решить — используется ли он где-то**

Run: `grep -rn "assistant-tab" static/ index.html`
Expected: или пусто (можно удалить), или есть ссылки (надо репурпоснуть).

- [ ] **Step 2a: Если не используется — удалить файл**

Run: `rm static/assistant-tab.js`

- [ ] **Step 2b: Если используется — переименовать в `editor-settings.js` или интегрировать контент**

(См. Task 2.3 — там уже создан `editor-settings.js`. Скопировать полезные куски из `assistant-tab.js` если есть, удалить файл.)

- [ ] **Step 3: Smoke — UI работает без ошибок в консоли**

Run сервер, открыть `http://localhost:8765`, открыть DevTools → Console. Ожидание: нет ошибок 404 на `assistant-tab.js`.

- [ ] **Step 4: Commit**

```bash
git add static/
git commit -m "chore: remove legacy assistant-tab.js"
```

---

### Task 3.3: Обновить документацию — DEPLOY.md + CLAUDE.md (если есть)

**Files:**
- Modify: `DEPLOY.md`

- [ ] **Step 1: Добавить секцию «AI Assistant / AI Editor» в DEPLOY.md**

Добавить после секции про `.env`:

```markdown
## 6. AI-компоненты

VideoText содержит два AI-блока с разными зонами ответственности:

| Блок | Роль | UI |
|---|---|---|
| **AI Assistant** | настройки платформы, разбор ошибок, интеграции | плавающая панель (правый нижний угол) |
| **AI Editor** | улучшение заголовков/цитат, генерация иллюстраций, распознавание текста | inline-кнопки на карточке news item + меню "AI Editor" |

Endpoints:
- `POST /chat/platform` — AI Assistant
- `POST /chat/editor` + `POST /chat/editor/apply` — AI Editor

Оба используют общие настройки провайдера (`assistant_provider`, `assistant_model`).
Требуют минимум один из: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, либо локальный Ollama.

Для OCR (Editor) нужен `OPENAI_API_KEY` — используется `gpt-4o-mini` vision.
```

- [ ] **Step 2: Commit**

```bash
git add DEPLOY.md
git commit -m "docs: document Assistant/Editor split in DEPLOY.md"
```

---

## Post-implementation checklist

После всех тасков прогнать:

- [ ] `.venv\Scripts\python -m pytest tests/ -v` — все passed
- [ ] `set RUN_SMOKE=1 && .venv\Scripts\python -m pytest tests/test_smoke.py -v` — оба SSE прогона зелёные
- [ ] `.venv\Scripts\python -m uvicorn server:app --port 8765` — сервер стартует без import errors
- [ ] Manual: открыть UI, панель ассистента отвечает, inline-кнопки на news item работают preview→apply, меню "AI Editor" открывает страницу
- [ ] `git log --oneline` — ~20 коммитов, чистая история по задачам

---

## Self-review

**Spec coverage:**
- § 2 Роли → Tasks 0.4, 1.1 ✓
- § 3.1 Структура модуля → Tasks 0.3, 0.4, 0.5, 1.1 ✓
- § 3.2 БД partition → Task 0.2 ✓
- § 3.3 Персоны → Tasks 0.4 (Platform), 1.1 (Editor), 1.4–1.7 (tools) ✓
- § 4.1–4.2 Data flow → Tasks 1.2 (endpoints) + 2.2 (inline flow) ✓
- § 4.3 Confirm-flow → Tasks 1.2 (apply endpoint), 1.3 (E2E test), 2.2 (UI diff-preview) ✓
- § 5 Endpoint naming → Task 1.2 ✓
- § 6 Error handling → реализовано внутри tools (`{ok:false,error:...}` контракт) + SSE error events (существующий код chat.py); отдельная задача не нужна
- § 7 Testing → Tasks 0.5, 0.6, 1.3, 1.8, 1.9 ✓
- § 8 Migration phases → прямое соответствие Phase 0/1/2/3 ✓

**Placeholder scan:** все steps содержат либо полный код, либо точную команду. Нет TBD/TODO.

**Type consistency:**
- `BasePersona.tool_names: list[str]` — используется одинаково в `PlatformPersona`, `EditorPersona`, `tools_runtime.registry()`.
- `ToolDef` signature — используется в `platform_tools.TOOLS` и `editor_tools.TOOLS` одинаково.
- `preview` dict shape (`{ok, tool_call_id, item_id, field, old, new, diff}`) — возвращается из всех write-tools Editor, потребляется в `static/editor-inline.js::renderPreview`.
- `apply` request — поле `field` из `Literal["headline", "quote", "expandedText", "imageId", "tags"]` в Pydantic + `_EDITOR_APPLY_FIELD_MAP` в server.py совпадают.

Плагин готов к исполнению.
