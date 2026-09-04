# Verification Conveyor (Layer 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Превратить конвейер артефактов из пересказывающего в проверяющий: ресерч верифицирует утверждения против внешних источников, репорт сверяет бриф/расшифровку/ресерч и выносит машиночитаемый вердикт, гейты перестают врать, артефакты версионируются, AI-скиллы экспортируются готовым бандлом.

**Architecture:** Артефакты остаются свободным markdown; единственный машиночитаемый контракт — строка-маркер `<!-- verdict: confirmed|partial|refuted -->` в конце репорта, которую сервер парсит и хранит в `Expansion.verdict`. На ней строится единственный жёсткий стоп (ТЗ не генерится при `refuted`). `Expansion` версионируется по конвенции существующей модели `TranscriptEdit`. Внешняя проверка — серверный тул Anthropic `web_search_20250305`, только для стадии ресерча.

**Tech Stack:** Python 3.11, FastAPI, Prisma (SQLite), anthropic 0.96.0, Ollama (опционально), pytest + pytest-asyncio, ванильный JS фронтенд.

**Spec:** `docs/superpowers/specs/2026-09-04-verification-conveyor-design.md`

## Global Constraints

- Артефакты остаются markdown. Никакого structured output — 205 существующих строк `Expansion` и четыре потребителя (редактор, PDF-экспорт, MD-экспорт, AI-оценка) должны продолжать работать.
- Маркер вердикта: регулярка ровно `<!--\s*verdict:\s*(confirmed|partial|refuted)\s*-->`, без учёта регистра, берётся **последнее** вхождение в тексте.
- Отсутствие маркера = `verdict is None` и не блокирует ничего. Блокирует только явный `"refuted"`.
- Жёсткий стоп в конвейере ровно один: `mode="spec"` при `refuted` без `override`. Все прочие гейты остаются предупреждением.
- Внешний поиск включается только для `mode="research"` и только на Claude-ветке (`transcript_edit._is_claude(model)`).
- Все промпты и вся UI-копия — на русском, как в существующем коде.
- Миграция схемы — `prisma db push` (как в `DEPLOY.md`), не `prisma migrate`.
- Существующие 205 строк `Expansion` не удаляются и не переписываются: `version` backfill-ится значением `1`.
- Тесты пишутся против реальной SQLite через фикстуру `db` из `tests/conftest.py`, как в `tests/test_expansion_durable.py`.

---

### Task 1: Версионирование Expansion в схеме и store

**Files:**
- Modify: `prisma/schema.prisma:40-59`
- Modify: `store.py:549-695`
- Test: `tests/test_expansion_versions.py` (создать)

**Interfaces:**
- Consumes: ничего (первая задача).
- Produces:
  - `store.get_expansion(video_id: str, mode: str)` — возвращает строку с максимальным `version` (или `None`).
  - `store.get_expansion_version(video_id: str, mode: str, version: int)` — конкретная версия или `None`.
  - `store.list_expansion_versions(video_id: str, mode: str) -> list` — все версии, порядок `version desc`.
  - `store.list_expansions(video_id: str) -> list` — по одной (последней) строке на каждый режим.
  - `store.start_expansion(...)` — та же сигнатура, что сейчас, но **создаёт новую версию** и возвращает созданную строку.
  - `store.finish_expansion(*, video_id, mode, content_md, elapsed_ms, verdict=None)` — обновляет последнюю версию.
  - `store.fail_expansion(*, video_id, mode, error)` — обновляет последнюю версию.

- [ ] **Step 1: Обновить схему Prisma**

В `prisma/schema.prisma` заменить модель `Expansion` (строки 40-59) на:

```prisma
model Expansion {
  id            Int      @id @default(autoincrement())
  videoId       String
  video         Video    @relation(fields: [videoId], references: [id], onDelete: Cascade)
  mode          String   // "spec" | "research" | "report" | "uiux" | "ai_skills" | "ai_algorithms"
  version       Int      @default(1) // 1..N within (videoId, mode); @default backfills existing rows
  fromVersion   Int?     // lineage: which version this regeneration grew from
  verdict       String?  // mode="report" only: "confirmed" | "partial" | "refuted"
  sourceTitle   String
  sourceMd      String
  contextMode   String   // "brief" | "transcript" | "both"
  model         String
  numCtx        Int      @default(8192)
  contentMd     String
  inputChars    Int      @default(0)
  elapsedMs     Int      @default(0)
  status        String   @default("done") // "running" | "done" | "error"
  error         String?
  createdAt     DateTime @default(now())
  updatedAt     DateTime @updatedAt

  @@unique([videoId, mode, version])
}
```

- [ ] **Step 2: Применить схему и перегенерировать клиент**

Run: `.venv/Scripts/python.exe -m prisma db push`
Expected: `Your database is now in sync with your Prisma schema`, клиент перегенерирован. Существующие 205 строк получают `version=1`.

Проверить, что данные на месте:

Run: `.venv/Scripts/python.exe -c "import sqlite3;c=sqlite3.connect('prisma/videotext.db');print(c.execute('select count(*),min(version),max(version) from Expansion').fetchone())"`
Expected: `(205, 1, 1)`

- [ ] **Step 3: Написать падающие тесты версионирования**

Создать `tests/test_expansion_versions.py`:

```python
"""Versioning of Expansion rows.

Regeneration must append a version instead of overwriting, so two concurrent
clients (or two models) never clobber each other's artifact.
"""
import store


async def _start(video_id, mode, model="claude-sonnet-4-6"):
    return await store._start_expansion(
        video_id=video_id, mode=mode, source_title="бриф", source_md="",
        context_mode="both", model=model, num_ctx=32768, input_chars=100,
    )


async def test_first_start_creates_version_1(db):
    row = await _start("vidV", "research")
    assert row.version == 1
    assert row.fromVersion is None


async def test_second_start_appends_version_2(db):
    await _start("vidV", "research")
    second = await _start("vidV", "research", model="claude-opus-5")
    assert second.version == 2
    assert second.fromVersion == 1


async def test_get_expansion_returns_latest_version(db):
    await _start("vidV", "research")
    await store._finish_expansion(
        video_id="vidV", mode="research", content_md="первый", elapsed_ms=10)
    await _start("vidV", "research", model="claude-opus-5")
    await store._finish_expansion(
        video_id="vidV", mode="research", content_md="второй", elapsed_ms=20)

    current = await store._get_expansion("vidV", "research")
    assert current.version == 2
    assert current.contentMd == "второй"
    assert current.model == "claude-opus-5"


async def test_versions_are_per_mode(db):
    await _start("vidV", "research")
    report = await _start("vidV", "report")
    assert report.version == 1, "нумерация версий не должна быть сквозной по режимам"


async def test_list_expansion_versions_newest_first(db):
    await _start("vidV", "research")
    await store._finish_expansion(
        video_id="vidV", mode="research", content_md="a", elapsed_ms=1)
    await _start("vidV", "research")
    rows = await store._list_expansion_versions("vidV", "research")
    assert [r.version for r in rows] == [2, 1]


async def test_list_expansions_returns_one_row_per_mode(db):
    await _start("vidV", "research")
    await _start("vidV", "research")
    await _start("vidV", "report")
    rows = await store._list_expansions("vidV")
    modes = sorted(r.mode for r in rows)
    assert modes == ["report", "research"], "модалка UI не должна видеть дубликаты версий"
    research = next(r for r in rows if r.mode == "research")
    assert research.version == 2


async def test_fail_marks_latest_version_only(db):
    await _start("vidV", "research")
    await store._finish_expansion(
        video_id="vidV", mode="research", content_md="хороший", elapsed_ms=5)
    await _start("vidV", "research")
    await store._fail_expansion(video_id="vidV", mode="research", error="boom")

    versions = await store._list_expansion_versions("vidV", "research")
    assert versions[0].status == "error"
    assert versions[1].status == "done"
    assert versions[1].contentMd == "хороший", "предыдущая версия должна пережить ошибку"
```

- [ ] **Step 4: Запустить тесты и убедиться, что они падают**

Run: `.venv/Scripts/python.exe -m pytest tests/test_expansion_versions.py -v`
Expected: FAIL. `test_second_start_appends_version_2` падает с ошибкой уникального ключа или возвращает `version == 1`; `_list_expansion_versions` не существует (`AttributeError`).

- [ ] **Step 5: Переписать store-функции под версии**

В `store.py` заменить `_get_expansion` (строки 579-587), `_list_expansions` (590-599), `_start_expansion` (602-628), `_finish_expansion` (631-641), `_fail_expansion` (644-653):

```python
async def _get_expansion(video_id: str, mode: str):
    """Current artifact = highest version, whatever its status. An errored
    latest version must stay visible; the UI relies on seeing it."""
    db = Prisma()
    await db.connect()
    try:
        return await db.expansion.find_first(
            where={"videoId": video_id, "mode": mode},
            order={"version": "desc"},
        )
    finally:
        await db.disconnect()


async def _get_expansion_version(video_id: str, mode: str, version: int):
    db = Prisma()
    await db.connect()
    try:
        return await db.expansion.find_unique(
            where={"videoId_mode_version": {
                "videoId": video_id, "mode": mode, "version": version}},
        )
    finally:
        await db.disconnect()


async def _list_expansion_versions(video_id: str, mode: str):
    db = Prisma()
    await db.connect()
    try:
        return await db.expansion.find_many(
            where={"videoId": video_id, "mode": mode},
            order={"version": "desc"},
        )
    finally:
        await db.disconnect()


async def _list_expansions(video_id: str):
    """One row per mode — the current version. The UI pre-fills its modal from
    this, so older versions must not show up as extra artifacts."""
    db = Prisma()
    await db.connect()
    try:
        rows = await db.expansion.find_many(
            where={"videoId": video_id},
            order={"version": "desc"},
        )
        latest: dict[str, Any] = {}
        for r in rows:
            if r.mode not in latest:
                latest[r.mode] = r
        return sorted(latest.values(), key=lambda r: r.updatedAt, reverse=True)
    finally:
        await db.disconnect()


async def _start_expansion(
    *, video_id: str, mode: str, source_title: str, source_md: str,
    context_mode: str, model: str, num_ctx: int, input_chars: int,
):
    """Append a new running version. Never overwrites: two clients generating
    the same mode concurrently produce two versions instead of racing for one row."""
    db = Prisma()
    await db.connect()
    try:
        last = await db.expansion.find_first(
            where={"videoId": video_id, "mode": mode}, order={"version": "desc"},
        )
        version = (last.version + 1) if last else 1
        return await db.expansion.create(data={
            "videoId": video_id, "mode": mode, "version": version,
            "fromVersion": last.version if last else None,
            "sourceTitle": source_title, "sourceMd": source_md,
            "contextMode": context_mode, "model": model, "numCtx": num_ctx,
            "contentMd": "", "inputChars": input_chars, "elapsedMs": 0,
            "status": "running", "error": None,
        })
    finally:
        await db.disconnect()


async def _finish_expansion(
    *, video_id: str, mode: str, content_md: str, elapsed_ms: int,
    verdict: str | None = None,
):
    db = Prisma()
    await db.connect()
    try:
        last = await db.expansion.find_first(
            where={"videoId": video_id, "mode": mode}, order={"version": "desc"},
        )
        if not last:
            return None
        return await db.expansion.update(
            where={"id": last.id},
            data={"contentMd": content_md, "elapsedMs": elapsed_ms,
                  "status": "done", "error": None, "verdict": verdict},
        )
    finally:
        await db.disconnect()


async def _fail_expansion(*, video_id: str, mode: str, error: str):
    db = Prisma()
    await db.connect()
    try:
        last = await db.expansion.find_first(
            where={"videoId": video_id, "mode": mode}, order={"version": "desc"},
        )
        if not last:
            return None
        return await db.expansion.update(
            where={"id": last.id},
            data={"status": "error", "error": error[:2000]},
        )
    finally:
        await db.disconnect()
```

Удалить `_upsert_expansion` (строки 549-577) и его синхронную обёртку `upsert_expansion` (669-670) — мёртвый код: импортируется в `server.py:52`, но не вызывается нигде.

Добавить синхронные обёртки рядом с существующими (после `get_expansion`):

```python
def get_expansion_version(video_id: str, mode: str, version: int):
    return asyncio.run(_get_expansion_version(video_id, mode, version))


def list_expansion_versions(video_id: str, mode: str):
    return asyncio.run(_list_expansion_versions(video_id, mode))
```

Убедиться, что `from typing import Any` есть в импортах `store.py`; если нет — добавить.

- [ ] **Step 6: Убрать мёртвый импорт из server.py**

В `server.py:52` удалить `upsert_expansion,` из списка импортов `from store import (...)`.

- [ ] **Step 7: Запустить тесты**

Run: `.venv/Scripts/python.exe -m pytest tests/test_expansion_versions.py tests/test_expansion_durable.py -v`
Expected: PASS все. `test_expansion_durable.py` должен продолжать проходить без изменений — сигнатуры не менялись.

- [ ] **Step 8: Прогнать весь набор на регресс**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS. Если падает `tests/test_endpoints.py` из-за импорта `upsert_expansion` — значит шаг 6 не выполнен.

- [ ] **Step 9: Commit**

```bash
git add prisma/schema.prisma store.py server.py tests/test_expansion_versions.py
git commit -m "feat(expansion): version artifacts instead of overwriting

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Парсинг вердикта репорта

**Files:**
- Modify: `pipeline.py` (добавить функцию после импортов)
- Modify: `server.py:101-121` (`_run_expansion_job`)
- Test: `tests/test_verdict.py` (создать)

**Interfaces:**
- Consumes: `store.finish_expansion(..., verdict=...)` из Task 1.
- Produces: `pipeline.parse_verdict(md: str) -> str | None` — возвращает `"confirmed" | "partial" | "refuted" | None`.

- [ ] **Step 1: Написать падающие тесты парсера**

Создать `tests/test_verdict.py`:

```python
"""The one machine-readable contract in an otherwise free-form markdown artifact."""
import pipeline


def test_parses_each_valid_value():
    for value in ("confirmed", "partial", "refuted"):
        assert pipeline.parse_verdict(f"# Репорт\n\n<!-- verdict: {value} -->") == value


def test_missing_marker_returns_none():
    assert pipeline.parse_verdict("# Репорт\n\nНикакого маркера тут нет.") is None


def test_empty_text_returns_none():
    assert pipeline.parse_verdict("") is None
    assert pipeline.parse_verdict(None) is None


def test_last_marker_wins():
    """A model may quote the format in its own prose before emitting the real one."""
    md = ("Формат такой: <!-- verdict: confirmed -->\n"
          "...текст репорта...\n"
          "<!-- verdict: refuted -->")
    assert pipeline.parse_verdict(md) == "refuted"


def test_case_and_spacing_tolerant():
    assert pipeline.parse_verdict("<!--verdict:REFUTED-->") == "refuted"
    assert pipeline.parse_verdict("<!--   verdict:   partial   -->") == "partial"


def test_unknown_value_is_ignored():
    assert pipeline.parse_verdict("<!-- verdict: maybe -->") is None
```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `.venv/Scripts/python.exe -m pytest tests/test_verdict.py -v`
Expected: FAIL — `AttributeError: module 'pipeline' has no attribute 'parse_verdict'`.

- [ ] **Step 3: Реализовать парсер**

В `pipeline.py` добавить `import re` к импортам и следом за константой `GATE_PREDECESSOR` вставить:

```python
# The single machine-readable contract inside an otherwise free-form artifact:
# the report ends with a verdict marker the server can act on (see §2 of the spec).
_VERDICT_RE = re.compile(
    r"<!--\s*verdict:\s*(confirmed|partial|refuted)\s*-->", re.IGNORECASE)


def parse_verdict(md: str | None) -> str | None:
    """Last verdict marker in the text, lowercased. None if absent or unknown.

    Last-wins because a model may quote the format in its prose before emitting
    the real marker at the end.
    """
    if not md:
        return None
    matches = _VERDICT_RE.findall(md)
    return matches[-1].lower() if matches else None
```

- [ ] **Step 4: Запустить тесты**

Run: `.venv/Scripts/python.exe -m pytest tests/test_verdict.py -v`
Expected: PASS (6 тестов).

- [ ] **Step 5: Подключить парсер к сохранению артефакта**

В `server.py` в функции `_run_expansion_job` (строки 101-121) заменить вызов `finish_expansion`:

```python
            finish_expansion(video_id=video_id, mode=mode, content_md=full_text,
                             elapsed_ms=elapsed_ms,
                             verdict=pipeline.parse_verdict(full_text) if mode == "report" else None)
```

- [ ] **Step 6: Добавить verdict в API-ответ**

В `server.py` в `_expansion_to_dict` добавить строку после `"mode": e.mode,`:

```python
        "version": e.version,
        "from_version": e.fromVersion,
        "verdict": getattr(e, "verdict", None),
```

- [ ] **Step 7: Прогнать набор**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add pipeline.py server.py tests/test_verdict.py
git commit -m "feat(report): parse and persist problem-statement verdict

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Эндпоинты версий

**Files:**
- Modify: `server.py:777-805` (маршруты expansions)
- Test: `tests/test_expansion_version_endpoints.py` (создать)

**Interfaces:**
- Consumes: `store.list_expansion_versions`, `store.get_expansion_version` из Task 1.
- Produces:
  - `GET /videos/{video_id}/expansions/{mode}/versions` → `{"mode": str, "versions": [{version, model, status, verdict, chars, elapsed_ms, created_at}]}`
  - `GET /videos/{video_id}/expansions/{mode}?version=N` → тот же объект, что и без параметра, но конкретной версии.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_expansion_version_endpoints.py`:

```python
"""HTTP surface for the version selector in the artifacts UI."""
from fastapi.testclient import TestClient


def test_versions_route_lists_newest_first(monkeypatch):
    import server

    class Row:
        def __init__(self, version, model, chars):
            self.version, self.model, self.chars = version, model, chars
            self.status, self.verdict, self.elapsedMs = "done", None, 100
            self.contentMd = "x" * chars
            class _D:
                def isoformat(self): return "2026-09-04T00:00:00"
            self.createdAt = _D()

    monkeypatch.setattr(server, "list_expansion_versions",
                        lambda v, m: [Row(2, "claude-opus-5", 20), Row(1, "claude-sonnet-4-6", 10)])
    client = TestClient(server.app)
    r = client.get("/videos/vid1/expansions/research/versions")
    assert r.status_code == 200
    body = r.json()
    assert [v["version"] for v in body["versions"]] == [2, 1]
    assert body["versions"][0]["model"] == "claude-opus-5"
    assert body["versions"][0]["chars"] == 20


def test_versions_route_404_when_empty(monkeypatch):
    import server
    monkeypatch.setattr(server, "list_expansion_versions", lambda v, m: [])
    client = TestClient(server.app)
    assert client.get("/videos/vid1/expansions/research/versions").status_code == 404
```

- [ ] **Step 2: Запустить и убедиться в падении**

Run: `.venv/Scripts/python.exe -m pytest tests/test_expansion_version_endpoints.py -v`
Expected: FAIL — 404 от несуществующего маршрута вместо 200.

- [ ] **Step 3: Добавить импорты и маршрут**

В `server.py:45-53` добавить в импорт из `store`: `get_expansion_version, list_expansion_versions,`.

Вставить перед строкой `@app.get("/videos/{video_id}/expansions/{mode}.pdf")` (сейчас строка 777):

```python
@app.get("/videos/{video_id}/expansions/{mode}/versions")
def read_expansion_versions(video_id: str, mode: ExpandMode) -> dict:
    """Version list for the artifact selector. Bodies are omitted on purpose —
    the UI fetches one version's text only when the user picks it."""
    rows = list_expansion_versions(video_id, mode)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No '{mode}' expansion for {video_id}")
    return {
        "mode": mode,
        "versions": [{
            "version": r.version,
            "model": r.model,
            "status": getattr(r, "status", "done"),
            "verdict": getattr(r, "verdict", None),
            "chars": len(r.contentMd or ""),
            "elapsed_ms": r.elapsedMs,
            "created_at": r.createdAt.isoformat(),
        } for r in rows],
    }
```

**Важно:** маршрут `/versions` обязан быть объявлен **до** `/{mode}.pdf` и `/{mode}` — Starlette матчит в порядке объявления, а `{mode}` съедает точки и сегменты. Это тот же порядок, о котором предупреждает существующий комментарий над `.pdf`-маршрутом.

- [ ] **Step 4: Добавить параметр version в чтение артефакта**

Заменить `read_expansion` (сейчас строки 800-805):

```python
@app.get("/videos/{video_id}/expansions/{mode}")
def read_expansion(video_id: str, mode: ExpandMode, version: int | None = None) -> dict:
    """Current artifact, or a specific version when `?version=N` is given."""
    e = (get_expansion_version(video_id, mode, version) if version is not None
         else get_expansion(video_id, mode))
    if not e:
        detail = (f"No '{mode}' v{version} for {video_id}" if version is not None
                  else f"No '{mode}' expansion for {video_id}")
        raise HTTPException(status_code=404, detail=detail)
    return _expansion_to_dict(e)
```

- [ ] **Step 5: Запустить тесты**

Run: `.venv/Scripts/python.exe -m pytest tests/test_expansion_version_endpoints.py -v`
Expected: PASS (2 теста).

- [ ] **Step 6: Проверить, что маршрут не затенён**

Запустить сервер и убедиться, что `/versions` не поймался как `mode`:

Run: `.venv/Scripts/python.exe -c "from fastapi.testclient import TestClient; import server; c=TestClient(server.app); print(c.get('/videos/Io_f4G7a_Eo/expansions/research/versions').status_code)"`
Expected: `200` (или `404` с текстом про отсутствие экспаншена, но **не** `422` — 422 означал бы, что `versions` попал в `{mode}` и не прошёл валидацию Literal).

- [ ] **Step 7: Commit**

```bash
git add server.py tests/test_expansion_version_endpoints.py
git commit -m "feat(api): expansion version list and per-version read

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Промпты ресерча и репорта — проверка и сверка

**Files:**
- Modify: `local_llm.py:107-170` (`SYSTEM_PROMPTS`), `local_llm.py:220-270` (`build_expand_prompt`)
- Modify: `server.py:675-682` (вызов `build_expand_prompt`)
- Test: `tests/test_expand_modes.py` (дополнить), `tests/test_verification_prompts.py` (создать)

**Interfaces:**
- Consumes: ничего из предыдущих задач.
- Produces: `local_llm.build_expand_prompt(..., web_search_available: bool = False)` — новый именованный параметр с дефолтом; существующие вызовы не ломаются.

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/test_verification_prompts.py`:

```python
"""The research stage must verify, not retell; the report must reconcile.

These assert the prompt contract, not model behaviour — the point is that a weak
model gets the same instructions as Opus.
"""
import local_llm


def _build(mode, web_search_available=True):
    return local_llm.build_expand_prompt(
        mode=mode, video_title="Rough Volatility",
        section_title="бриф", section_md="",
        software_brief_json=None,
        full_brief_md="## Суть\nЛекция про rough volatility.",
        transcript_excerpt="наклон 0,1555 ... приращение в 1,21 раза",
        web_search_available=web_search_available,
    )


def test_research_demands_claim_verification():
    s = local_llm.SYSTEM_PROMPTS["research"].lower()
    assert "проверка утверждений" in s
    assert "арифметическ" in s
    assert "ангажирован" in s


def test_research_forbids_self_confirmation():
    s = local_llm.SYSTEM_PROMPTS["research"].lower()
    assert "сам себя" in s or "самого себя" in s


def test_report_demands_reconciliation_and_verdict():
    s = local_llm.SYSTEM_PROMPTS["report"].lower()
    assert "сверка" in s
    assert "вердикт" in s
    assert "<!-- verdict:" in s


def test_report_prompt_lists_all_three_verdict_values():
    s = local_llm.SYSTEM_PROMPTS["report"]
    for value in ("confirmed", "partial", "refuted"):
        assert value in s


def test_research_degrades_honestly_without_search():
    system_off, _ = _build("research", web_search_available=False)
    assert "не проверено" in system_off
    system_on, _ = _build("research", web_search_available=True)
    assert system_on != system_off


def test_degradation_only_touches_research():
    for mode in ("report", "spec", "uiux", "ai_algorithms", "ai_skills"):
        on, _ = _build(mode, web_search_available=True)
        off, _ = _build(mode, web_search_available=False)
        assert on == off, f"{mode} не должен зависеть от доступности поиска"
```

- [ ] **Step 2: Запустить и убедиться в падении**

Run: `.venv/Scripts/python.exe -m pytest tests/test_verification_prompts.py -v`
Expected: FAIL — `TypeError: build_expand_prompt() got an unexpected keyword argument 'web_search_available'` и ассерты по тексту промптов.

- [ ] **Step 3: Переписать промпт ресерча**

В `local_llm.py` заменить значение ключа `"research"` в `SYSTEM_PROMPTS`:

```python
    "research": (
        "Ты делаешь РЕСЕРЧ: не пересказать материал, а ПРОВЕРИТЬ его. Твоя работа —"
        " отделить проверяемые факты от оценок автора и допущений, найти расхождения"
        " и сказать, что подтверждается, а что нет.\n\n"
        "СЕКЦИИ: Задача / Контекст / Проверка утверждений / Арифметическая и"
        " логическая сверка / Ангажированность источника / Варианты решения (минимум 2) /"
        " Ограничения / Риски / Источники / Открытые вопросы.\n\n"
        "ПРОВЕРКА УТВЕРЖДЕНИЙ — таблица со столбцами: Утверждение | Где в источнике |"
        " Тип (факт / оценка автора / допущение) | Внешняя проверка | Статус"
        " (подтверждено / противоречит / не найдено / не проверено). Бери все ключевые"
        " утверждения, а не два примера.\n\n"
        "АРИФМЕТИЧЕСКАЯ И ЛОГИЧЕСКАЯ СВЕРКА — пересчитай КАЖДОЕ производное число"
        " (проценты, доходности, степени, доли, суммы) и назови расхождения явно."
        " Проверь, не противоречат ли утверждения друг другу.\n\n"
        "АНГАЖИРОВАННОСТЬ ИСТОЧНИКА — коммерческий интерес автора, самоцитирование,"
        " отсутствие независимого подтверждения.\n\n"
        "ЗАПРЕТЫ: транскрипт НЕ засчитывается как подтверждение самого себя — ссылка"
        " на исходное видео не является внешней проверкой; в 'Источники' попадают"
        " только внешние источники. Не делай выводов и не рекомендуй решение (это"
        " репорт); не пиши ТЗ; не придумывай реализацию. Нет данных — '—' или 'TBD'."
        " markdown-заголовки, таблицы и списки, без преамбул."
    ),
```

- [ ] **Step 4: Переписать промпт репорта**

Заменить значение ключа `"report"`:

```python
    "report": (
        "Ты составляешь РЕПОРТ: сверить источники между собой, сделать выводы и вынести"
        " вердикт по проблематике.\n\n"
        "СЕКЦИИ: Краткое резюме (2-3 предложения) / Что исследовали / Сверка /"
        " Основные выводы / Варианты решений + плюсы-минусы каждого / Риски /"
        " Вердикт по проблематике / Рекомендация + обоснование / Следующие действия.\n\n"
        "СВЕРКА — таблица со столбцами: Заявлено в брифе | Подтверждает ли расшифровка |"
        " Что добавил или опроверг ресерч | Статус. Именно здесь видно, где бриф"
        " опережает источник, а где ресерч нашёл противоречие.\n\n"
        "ВЕРДИКТ ПО ПРОБЛЕМАТИКЕ — 'подтверждена' / 'подтверждена частично' /"
        " 'не подтверждена', с обоснованием на 2-4 предложения. Затем ПОСЛЕДНЕЙ СТРОКОЙ"
        " всего документа выведи ровно один маркер, без кавычек и без блока кода:\n"
        "<!-- verdict: confirmed -->  — если проблематика подтверждена\n"
        "<!-- verdict: partial -->    — если подтверждена частично\n"
        "<!-- verdict: refuted -->    — если не подтверждена\n"
        "Маркер обязателен: без него следующий этап не сможет принять решение.\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не пишешь ТЗ; не описываешь реализацию; не перечисляешь всё"
        " найденное без выводов. markdown, без преамбул."
    ),
```

- [ ] **Step 5: Добавить деградацию без поиска**

В `local_llm.py` перед `def _format_context` вставить константу:

```python
# Appended to the research system prompt when no external search is available
# (Ollama branch, or the setting is off). Degrading silently would let the model
# invent sources; degrading loudly keeps the artifact honest.
_NO_SEARCH_NOTE = (
    "\n\nВНЕШНИЙ ПОИСК НЕДОСТУПЕН: в столбце 'Внешняя проверка' пиши '—', в"
    " столбце 'Статус' — 'не проверено'. Не выдумывай источники и ссылки."
    " Арифметическую и логическую сверку всё равно выполняй полностью."
)
```

В `build_expand_prompt` изменить сигнатуру и выбор системного промпта:

```python
def build_expand_prompt(
    *,
    mode: str,
    video_title: str,
    section_title: str,
    section_md: str,
    software_brief_json: dict | None,
    full_brief_md: str,
    transcript_excerpt: str,
    upstream: dict[str, str] | None = None,
    web_search_available: bool = False,
) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt) for any expand mode.

    `mode` ∈ {"spec", "research", "report", "uiux", "ai_skills", "ai_algorithms"}.
    Unknown modes fall back to "spec".
    """
    system = SYSTEM_PROMPTS.get(mode, SYSTEM_PROMPTS["spec"])
    if mode == "research" and not web_search_available:
        system += _NO_SEARCH_NOTE
```

Остальное тело функции не меняется.

- [ ] **Step 6: Обновить инструкции режимов**

В словаре `instruction` внутри `build_expand_prompt` заменить две строки:

```python
        "research":      "Подготовь ресерч по структуре из системного промпта. Проверяй, а не пересказывай.",
        "report":        "Составь репорт по структуре из системного промпта. Не забудь маркер вердикта последней строкой.",
```

- [ ] **Step 7: Запустить тесты**

Run: `.venv/Scripts/python.exe -m pytest tests/test_verification_prompts.py tests/test_expand_modes.py -v`
Expected: PASS. Если `test_expand_modes.py` падает — значит его хелпер `_build` вызывает `build_expand_prompt` без нового параметра; параметр имеет дефолт, поэтому падения быть не должно. При падении читать сообщение, а не «чинить» дефолт.

- [ ] **Step 8: Прокинуть флаг из server.py**

В `server.py` перед вызовом `build_expand_prompt` (сейчас строка 675) добавить:

```python
    # External verification exists only on the Claude branch; Ollama has no tools.
    web_search_available = bool(
        req.mode == "research"
        and transcript_edit._is_claude(model)
        and str(settings.get("research_web_search_enabled", "true")).lower() != "false"
    )
```

и передать `web_search_available=web_search_available,` последним аргументом в `build_expand_prompt`.

Убедиться, что `import transcript_edit` есть в `server.py`; если нет — добавить к остальным `# noqa: E402` импортам.

- [ ] **Step 9: Прогнать набор**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add local_llm.py server.py tests/test_verification_prompts.py
git commit -m "feat(prompts): research verifies claims, report reconciles and rules

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Промпты алгоритмов и AI-скиллов

**Files:**
- Modify: `local_llm.py` (ключи `ai_algorithms` и `ai_skills` в `SYSTEM_PROMPTS`)
- Test: `tests/test_verification_prompts.py` (дополнить)

**Interfaces:**
- Consumes: ничего.
- Produces: формат `## Скилл N. <Название>` + строка `**Slug:** <kebab-case>`, на который опирается парсер из Task 11.

- [ ] **Step 1: Дописать падающие тесты**

Добавить в конец `tests/test_verification_prompts.py`:

```python
def test_algorithms_demand_core_modules_and_ml_judgement():
    s = local_llm.SYSTEM_PROMPTS["ai_algorithms"].lower()
    assert "вычислительное ядро" in s
    assert "модульная декомпозиция" in s
    assert "ml" in s


def test_algorithms_allow_saying_no_to_ml():
    """Without an explicit out, models bolt ML onto problems that need an if."""
    s = local_llm.SYSTEM_PROMPTS["ai_algorithms"].lower()
    assert "правил достаточно" in s


def test_skills_prompt_pins_machine_parsable_shape():
    s = local_llm.SYSTEM_PROMPTS["ai_skills"]
    assert "## Скилл N." in s
    assert "**Slug:**" in s
    assert "Инструменты MCP" in s
```

- [ ] **Step 2: Запустить и убедиться в падении**

Run: `.venv/Scripts/python.exe -m pytest tests/test_verification_prompts.py -k "algorithms or skills" -v`
Expected: FAIL — три ассерта.

- [ ] **Step 3: Переписать промпт алгоритмов**

Заменить значение ключа `"ai_algorithms"`:

```python
    "ai_algorithms": (
        "Ты описываешь АЛГОРИТМЫ действий — пошагово, что происходит в системе, у"
        " пользователя или в AI-процессе. Выдели 1-4 алгоритма. Для КАЖДОГО:\n\n"
        "Название алгоритма / Тип [пользовательский | системный | AI | технический] /"
        " Цель / Предусловия / Входные данные (что брать, откуда) / Шаги (нумерованно:"
        " что взять, что с чем сравнить, что проверить) / Правила и условия (если X → Y) /"
        " Критерии выхода / Результат / Граничные случаи / Ошибки и обработка.\n\n"
        "После всех алгоритмов — три общие секции:\n"
        "ВЫЧИСЛИТЕЛЬНОЕ ЯДРО: какие расчёты выполняются, какая нужна точность, где"
        " узкое место по времени и памяти, что считается онлайн, а что заранее.\n"
        "МОДУЛЬНАЯ ДЕКОМПОЗИЦИЯ: таблица Модуль | Ответственность | Интерфейс"
        " (вход → выход) | Зависимости. Модуль — одна ответственность.\n"
        "НУЖЕН ЛИ ML: ответь честно. Если задача решается правилами и порогами —"
        " пиши 'нет, правил достаточно' и объясни почему. Если ML нужен — назови"
        " задачу, признаки, целевую метрику и baseline без ML. Не предлагай модель,"
        " не назвав признаки и метрику.\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не пишешь ТЗ (не фиксируешь требования); не пишешь AI-скилл (не"
        " повторяемая инструкция). Не выдумывай пороги/формулы — помечай '[TBD: ...]'."
        " markdown и нумерованные списки, без преамбул."
    ),
```

- [ ] **Step 4: Переписать промпт AI-скиллов**

Заменить значение ключа `"ai_skills"`:

```python
    "ai_skills": (
        "Ты проектируешь AI-СКИЛЛЫ — повторяемые инструкции для агента (Claude Code /"
        " Cursor). Скилл оправдан, если действие повторяется 3+ раза. Выдели 2-5 скиллов.\n\n"
        "ФОРМАТ СТРОГИЙ — по нему собирается пакет файлов, отклонения ломают сборку.\n"
        "Заголовок каждого скилла — ровно: '## Скилл N. <Название>'\n"
        "Сразу под заголовком — строка: '**Slug:** <имя-в-kebab-case>' (латиница,"
        " строчные, дефисы; это имя каталога).\n"
        "Затем строка: '**Описание:** <одно предложение, когда применять>'\n\n"
        "Далее секции скилла: Когда использовать / Когда НЕ использовать / Входные"
        " данные (обязательные / опциональные) / Что делать (шаги) / Что не делать /"
        " Формат результата / Критерии качества / Примеры (Вход / Выход) /"
        " Инструменты MCP.\n\n"
        "ИНСТРУМЕНТЫ MCP — для каждого тула: имя (snake_case), назначение, JSON-схема"
        " входа, JSON-схема выхода. Если скилл не требует тула — пиши 'не требуется'.\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не заменяешь алгоритм; не пишешь ТЗ; не создаёшь скилл из сырой"
        " идеи без предварительных этапов. Не выдумывай API/данные — 'TBD: уточнить'."
        " markdown, без преамбул."
    ),
```

- [ ] **Step 5: Запустить тесты**

Run: `.venv/Scripts/python.exe -m pytest tests/test_verification_prompts.py -v`
Expected: PASS все.

- [ ] **Step 6: Прогнать набор**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add local_llm.py tests/test_verification_prompts.py
git commit -m "feat(prompts): algorithms cover core/modules/ML, skills get parsable shape

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Чеклисты — тройки, human-пункты, новые стадии

**Files:**
- Modify: `pipeline.py:35-64` (`CHECKLISTS`), `pipeline.py:67-95` (`build_assess_prompt`, `parse_assessment`, `assess_checklist`)
- Modify: `server.py` (`assess_stage` — передать предыдущие items)
- Test: `tests/test_pipeline.py` (обновить), `tests/test_checklist_kinds.py` (создать)

**Interfaces:**
- Consumes: ничего.
- Produces:
  - `pipeline.CHECKLISTS: dict[str, list[tuple[str, str, str]]]` — тройки `(key, label, kind)`, `kind ∈ {"ai", "human"}`.
  - `pipeline.parse_assessment(stage, raw, previous=None)` — `previous` это список ранее сохранённых items (dict), из которых берётся `checked` для human-пунктов.
  - `pipeline.assess_checklist(stage, artifact_md, model=None, previous=None)`.

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/test_checklist_kinds.py`:

```python
"""Checklist items now carry a kind: some are AI-assessable, some are a human's call.

The bug this prevents: an AI assessment wiping the human's "принято к разработке"
tick every time the user re-runs it.
"""
import pipeline


def test_every_item_is_a_triple_with_valid_kind():
    for stage, items in pipeline.CHECKLISTS.items():
        for item in items:
            assert len(item) == 3, f"{stage}: ожидалась тройка (key, label, kind)"
            assert item[2] in ("ai", "human"), f"{stage}/{item[0]}: неизвестный kind"


def test_report_accepted_is_a_human_decision():
    kinds = {k: kind for k, _label, kind in pipeline.CHECKLISTS["report"]}
    assert kinds["accepted"] == "human", "согласование живёт вне текста артефакта"


def test_all_stages_have_checklists():
    assert set(pipeline.CHECKLISTS) == {
        "research", "report", "spec", "uiux", "ai_algorithms", "ai_skills"}


def test_research_has_verification_item():
    keys = [k for k, _l, _kind in pipeline.CHECKLISTS["research"]]
    assert "verified" in keys


def test_report_has_reconciliation_item():
    keys = [k for k, _l, _kind in pipeline.CHECKLISTS["report"]]
    assert "reconciled" in keys


def test_algorithms_single_item_no_longer_contradicts_the_prompt():
    """The prompt asks for 1-4 algorithms; the criterion must judge each one."""
    label = next(l for k, l, _kind in pipeline.CHECKLISTS["ai_algorithms"] if k == "single")
    assert "аждый" in label


def test_assess_prompt_hides_human_items_from_the_model():
    _system, user = pipeline.build_assess_prompt("report", "текст репорта")
    assert "reconciled" in user
    assert "accepted" not in user, "человеческий пункт не отдаём модели"


def test_parse_assessment_preserves_human_checked():
    previous = [{"key": "accepted", "checked": True, "ai_note": ""}]
    items = pipeline.parse_assessment("report", {"verdict": {"checked": True, "note": "ok"}},
                                      previous=previous)
    by_key = {i["key"]: i for i in items}
    assert by_key["accepted"]["checked"] is True, "AI-оценка стёрла решение человека"
    assert by_key["verdict"]["checked"] is True


def test_parse_assessment_human_defaults_false_without_previous():
    items = pipeline.parse_assessment("report", {})
    by_key = {i["key"]: i for i in items}
    assert by_key["accepted"]["checked"] is False


def test_parse_assessment_carries_kind_out():
    items = pipeline.parse_assessment("report", {})
    assert all("kind" in i for i in items)
```

- [ ] **Step 2: Запустить и убедиться в падении**

Run: `.venv/Scripts/python.exe -m pytest tests/test_checklist_kinds.py -v`
Expected: FAIL — тройки/kind отсутствуют, `uiux` и `ai_skills` нет в `CHECKLISTS`, `parse_assessment` не принимает `previous`.

- [ ] **Step 3: Переписать CHECKLISTS**

Заменить словарь `CHECKLISTS` в `pipeline.py` целиком:

```python
# Readiness checklists (docs/task-flow-v2.md §3), keyed by the stage being assessed.
# Each item is (key, label, kind). kind="human" means the criterion cannot be read
# off the artifact's text — only a person can close it, and an AI assessment must
# never reset it.
CHECKLISTS: dict[str, list[tuple[str, str, str]]] = {
    "research": [
        ("domain", "Описана предметная область задачи", "ai"),
        ("options", "Перечислены варианты решения (минимум 2)", "ai"),
        ("limits", "Указаны ограничения и риски", "ai"),
        ("open_q", "Сформулированы открытые вопросы", "ai"),
        ("sources", "Указаны источники или наблюдения", "ai"),
        ("verified", "Утверждения проверены против внешних источников", "ai"),
    ],
    "report": [
        ("verdict", "Сформулирован чёткий вывод (что рекомендуется)", "ai"),
        ("reconciled", "Проведена сверка брифа, расшифровки и ресерча", "ai"),
        ("justified", "Обоснован выбор варианта", "ai"),
        ("next", "Указаны следующие действия", "ai"),
        ("accepted", "Решение согласовано / принято к разработке", "human"),
    ],
    "spec": [
        ("goal_user", "Определены цель и пользователь", "ai"),
        ("func", "Перечислены все функциональные требования", "ai"),
        ("scenarios", "Указаны сценарии использования", "ai"),
        ("acceptance", "Определены критерии приёмки", "ai"),
    ],
    "uiux": [
        ("screens", "Описаны все экраны сценария", "ai"),
        ("states", "У каждого экрана заданы состояния (загрузка/успех/ошибка/пусто)", "ai"),
        ("transitions", "Указаны переходы при успехе и при ошибке", "ai"),
        ("matches_spec", "Сценарий не противоречит ТЗ", "ai"),
    ],
    "ai_algorithms": [
        ("single", "Каждый алгоритм описывает одно действие (не смешанное)", "ai"),
        ("repeat", "Действие будет повторяться 3+ раза", "ai"),
        ("inputs", "Входные данные чётко определены", "ai"),
        ("output", "Формат результата зафиксирован", "ai"),
        ("core", "Описаны вычислительное ядро и модульная декомпозиция", "ai"),
    ],
    "ai_skills": [
        ("threshold", "Каждый скилл проходит критерий 3+ повторов", "ai"),
        ("io", "У каждого скилла заданы вход и формат результата", "ai"),
        ("slug", "У каждого скилла есть slug в kebab-case", "ai"),
        ("mcp", "Для каждого скилла указаны MCP-тулы или явное «не требуется»", "ai"),
    ],
}
```

- [ ] **Step 4: Обновить build_assess_prompt, parse_assessment, assess_checklist**

Заменить три функции в `pipeline.py`:

```python
def build_assess_prompt(stage: str, artifact_md: str) -> tuple[str, str]:
    """(system, user) for assessing one stage's artifact against its checklist.

    Only "ai" items reach the model: a human-decision criterion has no evidence
    in the text, so asking the model about it produces a guaranteed false negative.
    """
    items = [(k, label) for k, label, kind in CHECKLISTS.get(stage, []) if kind == "ai"]
    criteria = "\n".join(f"- {key}: {label}" for key, label in items)
    system = (
        "Ты оцениваешь полноту артефакта по чеклисту готовности. Для КАЖДОГО пункта"
        " реши, выполнен ли он в тексте артефакта. Отвечай строго JSON-объектом, где"
        " ключ — код пункта, значение — {\"checked\": true|false, \"note\": \"<очень"
        " кратко почему>\"}. Никакого текста вне JSON."
    )
    user = (
        f"## Чеклист (код: критерий)\n{criteria}\n\n"
        f"## Артефакт этапа «{stage}»\n{artifact_md}"
    )
    return system, user


def parse_assessment(stage: str, raw: dict, previous: list[dict] | None = None) -> list[dict]:
    """Map a raw {key:{checked,note}} dict to the full ordered checklist item list.

    AI items take their value from the model. Human items keep whatever the user
    had already ticked (`previous`), because the model was never asked about them —
    resetting them here would silently undo the user's decision on every re-assess.
    """
    prior = {i.get("key"): i for i in (previous or [])}
    out = []
    for key, label, kind in CHECKLISTS.get(stage, []):
        if kind == "human":
            was = prior.get(key) or {}
            out.append({
                "key": key, "label": label, "kind": kind,
                "checked": bool(was.get("checked", False)),
                "ai_note": "решение человека",
            })
            continue
        entry = raw.get(key) or {}
        out.append({
            "key": key, "label": label, "kind": kind,
            "checked": bool(entry.get("checked", False)),
            "ai_note": str(entry.get("note", "")),
        })
    return out


def assess_checklist(stage: str, artifact_md: str, model: str | None = None,
                     previous: list[dict] | None = None) -> list[dict]:
    """Run Claude to assess the artifact; returns parsed checklist items."""
    import anthropic
    system, user = build_assess_prompt(stage, artifact_md)
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=brief.resolve_model(model or "claude-haiku-4-5"),
        max_tokens=1500,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    text = next((b.text for b in msg.content if b.type == "text"), "{}")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        # Best-effort: strip code fences if the model wrapped JSON.
        cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        raw = json.loads(cleaned) if cleaned.startswith("{") else {}
    return parse_assessment(stage, raw, previous=previous)
```

- [ ] **Step 5: Передать предыдущие items из server.py**

В `server.py` в `assess_stage` заменить вызов оценки:

```python
    prev = get_stage_gate(video_id, stage)
    previous_items = json.loads(prev.items) if prev else None
    try:
        items = pipeline.assess_checklist(stage, e.contentMd, previous=previous_items)
    except Exception as ex:
        raise HTTPException(status_code=502, detail=f"AI-оценка не удалась: {ex}")
```

`get_stage_gate` уже импортирован в `server.py:47`.

- [ ] **Step 6: Обновить три сломавшихся существующих теста**

Переход с двоек на тройки ломает **три** теста в `tests/test_pipeline.py` — два из них распаковывают элементы как `for key, _label in ...` и упадут с `ValueError: too many values to unpack`.

Заменить `test_checklists_present_for_gated_stages_only`:

```python
def test_checklists_cover_every_stage():
    assert set(pipeline.CHECKLISTS) == set(pipeline.STAGE_ORDER)
    assert len(pipeline.CHECKLISTS["research"]) == 6
    assert all(len(item) == 3 for item in pipeline.CHECKLISTS["research"])  # (key, label, kind)
```

В `test_build_assess_prompt_includes_artifact_and_keys` заменить последние две строки — в промпт теперь попадают только `ai`-пункты:

```python
    for key, _label, kind in pipeline.CHECKLISTS["research"]:
        if kind == "ai":
            assert key in user
```

В `test_parse_assessment_maps_to_items_with_labels` заменить строку со сравнением множеств:

```python
    assert {i["key"] for i in items} == {k for k, _l, _kind in pipeline.CHECKLISTS["research"]}
```

- [ ] **Step 7: Запустить тесты**

Run: `.venv/Scripts/python.exe -m pytest tests/test_checklist_kinds.py tests/test_pipeline.py -v`
Expected: PASS все.

- [ ] **Step 8: Прогнать набор**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add pipeline.py server.py tests/test_checklist_kinds.py tests/test_pipeline.py
git commit -m "fix(gates): human-decision items stop failing AI assessment

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Жёсткий стоп на вердикте refuted

**Files:**
- Modify: `server.py:610-620` (`ExpandSpecRequest`), `server.py:622-660` (`expand_spec`)
- Test: `tests/test_refuted_stop.py` (создать)

**Interfaces:**
- Consumes: `pipeline.parse_verdict` (Task 2), `Expansion.verdict` (Task 1).
- Produces: `ExpandSpecRequest.override: bool = False`; `POST /videos/{id}/expand-spec` отдаёт `409` при попытке сгенерировать ТЗ поверх опровергнутого репорта.

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/test_refuted_stop.py`:

```python
"""The pipeline's single hard stop: no ТЗ on top of a refuted problem statement.

Everything else stays a warning — see §6.3 of the design.
"""
from fastapi.testclient import TestClient


class _Report:
    def __init__(self, verdict):
        self.verdict, self.status, self.contentMd = verdict, "done", "текст"


def _client(monkeypatch, report_verdict):
    import server

    class _Video:
        id, title, briefs, segments = "vid1", "T", [object()], []

    monkeypatch.setattr(server, "get_video", lambda *a, **k: _Video())
    monkeypatch.setattr(server, "get_expansion",
                        lambda v, m: _Report(report_verdict) if m == "report" else None)
    # raise_server_exceptions=False: requests that pass the gate fall through into
    # real generation (settings, DB, thread launch) and blow up. The subject here is
    # the gate, so a downstream failure must surface as a 500 to assert against —
    # not as a test error that hides whether the gate fired.
    return TestClient(server.app, raise_server_exceptions=False)


def test_refuted_blocks_spec(monkeypatch):
    client = _client(monkeypatch, "refuted")
    r = client.post("/videos/vid1/expand-spec", json={"mode": "spec"})
    assert r.status_code == 409
    assert "не подтверждена" in r.json()["detail"]


def test_override_lets_it_through(monkeypatch):
    client = _client(monkeypatch, "refuted")
    r = client.post("/videos/vid1/expand-spec", json={"mode": "spec", "override": True})
    assert r.status_code != 409


def test_confirmed_and_partial_do_not_block(monkeypatch):
    for verdict in ("confirmed", "partial", None):
        client = _client(monkeypatch, verdict)
        r = client.post("/videos/vid1/expand-spec", json={"mode": "spec"})
        assert r.status_code != 409, f"вердикт {verdict} не должен блокировать"


def test_refuted_does_not_block_other_modes(monkeypatch):
    client = _client(monkeypatch, "refuted")
    for mode in ("research", "report", "uiux", "ai_algorithms", "ai_skills"):
        r = client.post("/videos/vid1/expand-spec", json={"mode": mode})
        assert r.status_code != 409, f"{mode} не должен блокироваться вердиктом"
```

- [ ] **Step 2: Запустить и убедиться в падении**

Run: `.venv/Scripts/python.exe -m pytest tests/test_refuted_stop.py -v`
Expected: FAIL — `test_refuted_blocks_spec` получает 200 вместо 409.

- [ ] **Step 3: Добавить поле override**

В `server.py` в `ExpandSpecRequest` добавить последним полем:

```python
    # Escape hatch for the one hard stop in the pipeline (refuted problem statement).
    override: bool = False
```

- [ ] **Step 4: Добавить проверку в expand_spec**

В `server.py` в `expand_spec` сразу после проверки `if not video.briefs:` вставить:

```python
    # The pipeline's only hard stop. docs/task-flow-v2.md §5: "ресерч показал, что
    # идея нежизнеспособна → стоп; возврат к брифу, переформулировка."
    if req.mode == "spec" and not req.override:
        rep = get_expansion(video_id, "report")
        if rep is not None and getattr(rep, "verdict", None) == "refuted":
            raise HTTPException(
                status_code=409,
                detail="Репорт вынес вердикт «проблематика не подтверждена» — ТЗ на"
                       " этом основании писать нельзя. Вернись к брифу и переформулируй"
                       " задачу, либо перегенерируй репорт. Если решение осознанное —"
                       " повтори запрос с override.",
            )
```

- [ ] **Step 5: Запустить тесты**

Run: `.venv/Scripts/python.exe -m pytest tests/test_refuted_stop.py -v`
Expected: PASS (4 теста).

- [ ] **Step 6: Прогнать набор**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add server.py tests/test_refuted_stop.py
git commit -m "feat(gates): block spec generation on a refuted problem statement

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Внешний поиск для ресерча

**Files:**
- Modify: `transcript_edit.py:114-127` (`_stream_claude`), `transcript_edit.py:129-141` (`stream_edit`)
- Modify: `local_llm.py:39-90` (`stream_chat`)
- Modify: `server.py:101-121` (`_run_expansion_job`), `server.py` (сборка настроек в `expand_spec`)
- Test: `tests/test_web_search_wiring.py` (создать)

**Interfaces:**
- Consumes: `web_search_available` из Task 4.
- Produces: `transcript_edit._stream_claude(system, user, model, tools=None)`; `local_llm.stream_chat(..., tools=None)`.

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/test_web_search_wiring.py`:

```python
"""Wiring only: that the search tool reaches the Anthropic call for research and
nowhere else. The search itself runs server-side at Anthropic — nothing to mock."""
import local_llm
import transcript_edit


def test_stream_chat_forwards_tools_to_claude(monkeypatch):
    seen = {}

    def fake_stream_claude(system, user, model, tools=None):
        seen["tools"] = tools
        yield "ok"

    monkeypatch.setattr(transcript_edit, "_stream_claude", fake_stream_claude)
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    list(local_llm.stream_chat(system="s", user="u", model="claude-sonnet-4-6", tools=tools))
    assert seen["tools"] == tools


def test_stream_chat_ignores_tools_on_ollama(monkeypatch):
    """Ollama has no server-side tools; passing them must not crash or leak into the payload."""
    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def __iter__(self): return iter([b'{"message":{"content":"hi"},"done":true}'])

    def fake_urlopen(req, timeout=None):
        captured["body"] = req.data.decode()
        return _Resp()

    monkeypatch.setattr(local_llm.urllib.request, "urlopen", fake_urlopen)
    out = list(local_llm.stream_chat(
        system="s", user="u", model="qwen2.5:7b",
        tools=[{"type": "web_search_20250305", "name": "web_search"}]))
    assert out == ["hi"]
    assert "web_search" not in captured["body"]


def test_research_tools_builder_respects_max_uses():
    tools = local_llm.web_search_tools(max_uses=3)
    assert tools == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]


def test_web_search_tools_clamps_nonsense():
    assert local_llm.web_search_tools(max_uses=0) is None
    assert local_llm.web_search_tools(max_uses=-2) is None
```

- [ ] **Step 2: Запустить и убедиться в падении**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web_search_wiring.py -v`
Expected: FAIL — `stream_chat` не принимает `tools`, `web_search_tools` не существует.

- [ ] **Step 3: Разрешить tools в Claude-стриме**

В `transcript_edit.py` заменить `_stream_claude`:

```python
def _stream_claude(system: str, user: str, model: str, tools: list[dict] | None = None) -> Iterator[str]:
    """Stream text deltas from Claude. System prompt is prompt-cached.

    `tools` carries Anthropic server-side tools (currently web search). The search
    loop runs at Anthropic; `text_stream` still yields only text deltas, so callers
    see the same contract with or without tools.
    """
    import anthropic
    client = anthropic.Anthropic()
    resolved = brief.resolve_model(model or None)
    kwargs = {
        "model": resolved,
        "max_tokens": 16000,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
    }
    if tools:
        kwargs["tools"] = tools
    with client.messages.stream(**kwargs) as stream:
        for text in stream.text_stream:
            yield text
```

- [ ] **Step 4: Прокинуть tools через stream_chat**

В `local_llm.py` изменить сигнатуру `stream_chat`, добавив `tools: list[dict] | None = None` последним параметром, и передать его в Claude-ветку:

```python
    import transcript_edit
    if transcript_edit._is_claude(model):
        yield from transcript_edit._stream_claude(system, user, model, tools=tools)
        return
```

Ollama-ветка ниже остаётся без изменений — `tools` туда сознательно не попадает.

Добавить рядом с константами в начале `local_llm.py`:

```python
# Anthropic server-side web search. Only the research stage gets it: the other
# five stages reason over upstream artifacts and have nothing to look up.
WEB_SEARCH_TOOL_TYPE = "web_search_20250305"


def web_search_tools(max_uses: int) -> list[dict] | None:
    """Tool block for the research stage, or None when search is disabled."""
    if max_uses is None or max_uses < 1:
        return None
    return [{"type": WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": int(max_uses)}]
```

- [ ] **Step 5: Запустить тесты**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web_search_wiring.py -v`
Expected: PASS (4 теста).

- [ ] **Step 6: Пробросить tools в фоновую генерацию**

В `server.py` в `_run_expansion_job` добавить параметр `tools=None` в сигнатуру и передать его в вызов `local_llm.stream_chat(...)` внутри функции.

В `expand_spec` перед `threading.Thread(...)` собрать тулы:

```python
    search_max_uses = int(settings.get("research_web_search_max_uses") or 5)
    tools = local_llm.web_search_tools(search_max_uses) if web_search_available else None
```

и добавить `"tools": tools,` в словарь `kwargs` потока.

- [ ] **Step 7: Зарегистрировать настройки**

В `store.py` в словарь `DEFAULT_SETTINGS` (начинается на строке 449) добавить два ключа в конец, после `"local_llm_max_transcript_chars"`:

```python
    # Research-stage external verification (Anthropic server-side web search).
    "research_web_search_enabled":  True,              # off → research degrades to "не проверено"
    "research_web_search_max_uses": 5,                 # searches per research run; 0 disables
```

Отдельного UI у этих ключей нет — как и у существующих `local_llm_*`, они правятся через `POST /settings`.

Run: `.venv/Scripts/python.exe -c "import store;s=store.get_all_settings();print(s.get('research_web_search_enabled'), s.get('research_web_search_max_uses'))"`
Expected: `true 5`

- [ ] **Step 8: Живая проверка поиска**

Запустить сервер и сгенерировать ресерч на видео с известной темой:

```bash
.venv/Scripts/python.exe -m uvicorn server:app --port 8000 &
curl -s -X POST http://127.0.0.1:8000/videos/Io_f4G7a_Eo/expand-spec \
  -H "Content-Type: application/json" \
  -d '{"mode":"research","context":"both","model":"claude-sonnet-4-6"}'
```

Дождаться `status: done`, скачать артефакт и проверить, что в разделе «Источники» появились внешние ссылки (`http`), а таблица «Проверка утверждений» заполнена не только значениями «не проверено».

Expected: в тексте есть хотя бы одна внешняя ссылка. Если все статусы «не проверено» — значит `web_search_available` посчитался `False`; проверить настройку и `_is_claude(model)`.

- [ ] **Step 9: Commit**

```bash
git add transcript_edit.py local_llm.py server.py store.py tests/test_web_search_wiring.py
git commit -m "feat(research): external verification via Anthropic web search

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Предупреждение о непринятых черновиках

**Files:**
- Modify: `server.py` (новый маршрут рядом с `read_doc_draft`, сейчас строка 1216)
- Modify: `static/editor-workspace.js` (рендер панели артефактов)
- Test: `tests/test_pending_drafts.py` (создать)

**Interfaces:**
- Consumes: `store.get_transcript_draft` (существует).
- Produces: `GET /videos/{video_id}/pending-drafts` → `{"drafts": [{kind, chars, updated_at}]}`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_pending_drafts.py`:

```python
"""Drafts never reach the expand prompt — _current_doc_text reads applied edits only.

Silence here cost a 24k-char brief draft on Io_f4G7a_Eo; the endpoint makes it visible.
"""
from fastapi.testclient import TestClient


class _Draft:
    def __init__(self, chars):
        self.contentMd = "x" * chars
        class _D:
            def isoformat(self): return "2026-09-04T00:00:00"
        self.updatedAt = _D()


def test_lists_only_kinds_that_have_drafts(monkeypatch):
    import server
    drafts = {"brief": _Draft(23984), "essence": _Draft(1250)}
    monkeypatch.setattr(server, "get_transcript_draft", lambda v, k: drafts.get(k))
    client = TestClient(server.app)
    body = client.get("/videos/vid1/pending-drafts").json()
    kinds = {d["kind"]: d["chars"] for d in body["drafts"]}
    assert kinds == {"brief": 23984, "essence": 1250}


def test_empty_when_nothing_pending(monkeypatch):
    import server
    monkeypatch.setattr(server, "get_transcript_draft", lambda v, k: None)
    client = TestClient(server.app)
    assert client.get("/videos/vid1/pending-drafts").json()["drafts"] == []
```

- [ ] **Step 2: Запустить и убедиться в падении**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pending_drafts.py -v`
Expected: FAIL — 404 от несуществующего маршрута.

- [ ] **Step 3: Добавить маршрут**

В `server.py` рядом с остальными draft-маршрутами вставить:

```python
@app.get("/videos/{video_id}/pending-drafts")
def read_pending_drafts(video_id: str) -> dict:
    """Drafts that exist but were never applied.

    `_current_doc_text` reads applied TranscriptEdit rows only, so an unapplied
    draft is invisible to the expand prompt. The UI warns before generating.
    """
    out = []
    for kind in ("transcript", "brief", "essence"):
        d = get_transcript_draft(video_id, kind)
        if d and (d.contentMd or "").strip():
            out.append({
                "kind": kind,
                "chars": len(d.contentMd),
                "updated_at": d.updatedAt.isoformat(),
            })
    return {"drafts": out}
```

- [ ] **Step 4: Запустить тесты**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pending_drafts.py -v`
Expected: PASS (2 теста).

- [ ] **Step 5: Показать предупреждение в UI**

В `static/editor-workspace.js` в функции, которая грузит состояние артефактов для видео (там же, где `state.afGates = await fetchJSON(...)`, сейчас строка 920), добавить загрузку черновиков:

```javascript
      try { state.afDrafts = (await fetchJSON(`/videos/${id}/pending-drafts`)).drafts || []; }
      catch (_) { state.afDrafts = []; }
```

В `renderWarningAndHint()` после существующей логики предупреждения добавить рендер второй плашки:

```javascript
    const dw = $('af-draft-warning');
    const KIND_RU = { transcript: 'расшифровки', brief: 'брифа', essence: 'сути' };
    const pending = state.afDrafts || [];
    if (pending.length) {
      dw.style.display = 'block';
      dw.textContent = '⚠ Непринятые черновики: '
        + pending.map(d => `${KIND_RU[d.kind] || d.kind} (${d.chars} симв.)`).join(', ')
        + ' — в промпт они не попадут. Примени их во вкладке «Расшифровки» или генерируй как есть.';
    } else { dw.style.display = 'none'; }
```

В `static/index.html` рядом с элементом `af-warning` добавить контейнер:

```html
<div id="af-draft-warning" class="editor-warning" style="display:none;"></div>
```

- [ ] **Step 6: Проверить в браузере**

Запустить сервер, открыть AI Editor → Артефакты → видео `Io_f4G7a_Eo`.
Expected: видна плашка «Непринятые черновики: брифа (23984 симв.), сути (1250 симв.)».

- [ ] **Step 7: Commit**

```bash
git add server.py static/editor-workspace.js static/index.html tests/test_pending_drafts.py
git commit -m "feat(editor): warn when unapplied drafts are missing from the prompt

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: UI — human-пункты, вердикт, селектор версий

**Files:**
- Modify: `static/editor-workspace.js:939-967` (`renderChecklist`, `renderWarningAndHint`), `static/editor-workspace.js:788-800` (`AF_HINT`, `gateClosed`)
- Modify: `static/index.html` (контейнер селектора версий)

**Interfaces:**
- Consumes: `kind` в items гейта (Task 6), `verdict`/`version` в `_expansion_to_dict` (Tasks 1-2), `GET .../versions` (Task 3).
- Produces: только UI, ничего не экспортирует.

- [ ] **Step 1: Пометить human-пункты в чеклисте**

В `renderChecklist()` заменить рендер строки пункта:

```javascript
      ${items.length ? items.map((it, i) => `
        <label style="display:flex; gap:8px; align-items:flex-start; font-size:12px; margin:3px 0;">
          <input type="checkbox" data-ci="${i}" ${it.checked ? 'checked' : ''}>
          <span>${escapeHtml(it.label)}
            ${it.kind === 'human' ? ' <em style="color:var(--mute-2);">— решает человек, AI не оценивает</em>'
              : (it.ai_note ? ` <em style="color:var(--mute-2);">— ${escapeHtml(it.ai_note)}</em>` : '')}
          </span>
        </label>`).join('') : '<em style="font-size:12px; color:var(--mute);">нет оценки — нажми «AI-оценка»</em>'}`;
```

- [ ] **Step 2: Показать вердикт репорта**

В `renderWarningAndHint()` добавить перед рендером подсказки:

```javascript
    const VERDICT_RU = {
      confirmed: ['✓ проблематика подтверждена', '#166534'],
      partial: ['◐ проблематика подтверждена частично', '#92400e'],
      refuted: ['✗ проблематика НЕ подтверждена — ТЗ на этом основании писать нельзя', '#b91c1c'],
    };
    const vb = $('af-verdict');
    const rep = state.afExpansions && state.afExpansions.report;
    const v = rep && rep.verdict && VERDICT_RU[rep.verdict];
    if (v) { vb.style.display = 'block'; vb.textContent = v[0]; vb.style.color = v[1]; }
    else { vb.style.display = 'none'; }
```

В `static/index.html` рядом с `af-warning` добавить:

```html
<div id="af-verdict" style="display:none; font-size:12px; font-weight:600; margin:4px 0;"></div>
```

- [ ] **Step 3: Обработать 409 при генерации ТЗ**

`generateArtifact()` (строка 876) сейчас ходит через `fetchJSON`, который бросает исключение на любой не-2xx — на 409 пользователь увидел бы голую ошибку вместо выбора. Заменить функцию целиком на сырой `fetch`:

```javascript
  async function generateArtifact(override = false) {
    const id = state.afSelectedId, mode = state.afMode;
    if (!id) return;
    const resp = await fetch(`/videos/${id}/expand-spec`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mode, model: $('af-model').value, context: $('af-context').value, override,
      }),
    });
    // The pipeline's one hard stop: a refuted problem statement blocks ТЗ.
    if (resp.status === 409) {
      const body = await resp.json().catch(() => ({}));
      if (confirm((body.detail || 'Этап заблокирован вердиктом репорта.')
          + '\n\nВсё равно сгенерировать ТЗ?')) {
        return generateArtifact(true);
      }
      return;
    }
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      alert('Не удалось запустить генерацию: ' + (body.detail || resp.status));
      return;
    }
    state.afExpansions[mode] = {
      mode, status: 'running',
      content_md: (state.afExpansions[mode] || {}).content_md || '',
    };
    renderArtifactStatus(state.afExpansions[mode]);
    startArtifactPolling();
  }
```

Обработчик на строке 1017 (`$('af-generate').addEventListener('click', () => generateArtifact())`) менять не нужно — дефолт `override = false` совпадает с текущим поведением.

- [ ] **Step 4: Добавить селектор версий**

В `static/index.html` рядом с заголовком артефакта добавить `<select id="af-version" style="display:none; font-size:11px;"></select>`.

В `editor-workspace.js` добавить функцию и вызывать её при смене стадии:

```javascript
  async function renderVersionPicker(stage) {
    const sel = $('af-version');
    let versions = [];
    try { versions = (await fetchJSON(
      `/videos/${state.afSelectedId}/expansions/${stage}/versions`)).versions || []; }
    catch (_) { sel.style.display = 'none'; return; }
    if (versions.length < 2) { sel.style.display = 'none'; return; }
    sel.style.display = '';
    sel.innerHTML = versions.map((v, i) =>
      `<option value="${v.version}"${i === 0 ? ' selected' : ''}>v${v.version} · ${escapeHtml(v.model)} · ${v.chars} симв.</option>`
    ).join('');
    sel.onchange = async () => {
      const e = await fetchJSON(
        `/videos/${state.afSelectedId}/expansions/${stage}?version=${sel.value}`);
      $('af-text').textContent = e.content_md || '';
    };
  }
```

Вызывать `renderVersionPicker(mode)` в конце `selectArtifactMode` — там же, где уже вызываются `renderStepper()`, `renderChecklist()`, `renderWarningAndHint()` (строки 862-865).

Просмотр старой версии сознательно не трогает `state.afExpansions[mode]`: там лежит текущая версия, на которую опираются степпер, вердикт и поллинг. Селектор меняет только текст в `af-text`.

- [ ] **Step 5: Проверить в браузере**

Открыть AI Editor → Артефакты → `Io_f4G7a_Eo` → Ресерч.
Expected: селектор версий виден (у этого видео их уже больше одной), переключение подгружает другой текст; на вкладке Репорт видна строка вердикта; в чеклисте репорта пункт «Решение согласовано» помечен как решение человека.

- [ ] **Step 6: Commit**

```bash
git add static/editor-workspace.js static/index.html
git commit -m "feat(ui): verdict banner, human-decision items, version picker

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: Сборка бандла скиллов и MCP-сервера

**Files:**
- Create: `skills_export.py`
- Modify: `server.py` (новый маршрут рядом с экспортами экспаншенов)
- Test: `tests/test_skills_export.py` (создать)

**Interfaces:**
- Consumes: формат `## Скилл N.` + `**Slug:**` из Task 5; `store.get_expansion` из Task 1.
- Produces:
  - `skills_export.parse_skills(md: str) -> list[dict]` — `[{"slug", "title", "description", "body"}]`
  - `skills_export.build_bundle(skills, spec_md, algorithms_md, video_title) -> bytes` (zip)
  - `GET /videos/{video_id}/skills-bundle.zip`

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/test_skills_export.py`:

```python
"""Turning the ai_skills artifact into a directory tree someone can actually run."""
import io
import zipfile

import skills_export

SAMPLE = """## Скилл 1. Rolling Universe Scan

**Slug:** rolling-universe-scan
**Описание:** Формирует корзину торгуемых пар без look-ahead bias.

**Когда использовать:**
- Нужна корзина на каждый месяц.

## Скилл 2. Momentum Score

**Slug:** momentum-score
**Описание:** Считает score по нескольким горизонтам.

**Когда использовать:**
- Корзина уже собрана.
"""


def test_parses_every_skill():
    skills = skills_export.parse_skills(SAMPLE)
    assert [s["slug"] for s in skills] == ["rolling-universe-scan", "momentum-score"]
    assert skills[0]["title"] == "Rolling Universe Scan"
    assert "look-ahead" in skills[0]["description"]


def test_body_excludes_the_next_skill():
    skills = skills_export.parse_skills(SAMPLE)
    assert "Momentum Score" not in skills[0]["body"]


def test_slug_falls_back_to_transliterated_title():
    md = "## Скилл 1. Проверка данных\n\nБез слага.\n"
    assert skills_export.parse_skills(md)[0]["slug"] == "proverka-dannyh"


def test_empty_input_yields_nothing():
    assert skills_export.parse_skills("") == []
    assert skills_export.parse_skills("Просто текст без заголовков") == []


def test_bundle_layout_and_frontmatter():
    skills = skills_export.parse_skills(SAMPLE)
    blob = skills_export.build_bundle(
        skills, spec_md="# ТЗ", algorithms_md="# Алгоритмы", video_title="Rough Volatility")
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = set(zf.namelist())
    assert "skills/rolling-universe-scan/SKILL.md" in names
    assert "skills/momentum-score/SKILL.md" in names
    assert "mcp_server/server.py" in names
    assert "mcp_server/requirements.txt" in names
    assert "README.md" in names

    skill_md = zf.read("skills/rolling-universe-scan/SKILL.md").decode()
    assert skill_md.startswith("---\n")
    assert "name: rolling-universe-scan" in skill_md
    assert "description:" in skill_md


def test_readme_carries_spec_and_algorithms():
    blob = skills_export.build_bundle(
        skills_export.parse_skills(SAMPLE),
        spec_md="# ТЗ\nсодержимое тз", algorithms_md="# Алгоритмы\nсодержимое алгоритмов",
        video_title="T")
    readme = zipfile.ZipFile(io.BytesIO(blob)).read("README.md").decode()
    assert "содержимое тз" in readme
    assert "содержимое алгоритмов" in readme


def test_generated_mcp_server_is_valid_python():
    blob = skills_export.build_bundle(
        skills_export.parse_skills(SAMPLE), spec_md="", algorithms_md="", video_title="T")
    src = zipfile.ZipFile(io.BytesIO(blob)).read("mcp_server/server.py").decode()
    compile(src, "server.py", "exec")  # syntax must be valid, not just plausible
```

- [ ] **Step 2: Запустить и убедиться в падении**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skills_export.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'skills_export'`.

- [ ] **Step 3: Написать модуль**

Создать `skills_export.py`:

```python
"""Turn the ai_skills artifact into a runnable bundle.

The ai_skills prompt (local_llm.SYSTEM_PROMPTS) pins a strict shape — `## Скилл N.`
headings with a `**Slug:**` line — precisely so this parser can be simple and the
failure mode obvious: no headings, no bundle, explicit error.

What ships is honest about what it is: the MCP prompts are complete (they carry the
skill text), the MCP tools are stubs with the algorithm's steps in the docstring.
Generating a working implementation from prose is not something we can do, so we
don't pretend to.
"""
from __future__ import annotations

import io
import json
import re
import zipfile

_SKILL_RE = re.compile(r"^##\s+Скилл\s+\d+\.\s*(.+?)\s*$", re.MULTILINE)
_SLUG_RE = re.compile(r"^\*\*Slug:\*\*\s*`?([a-z0-9][a-z0-9-]*)`?\s*$", re.MULTILINE)
_DESC_RE = re.compile(r"^\*\*Описание:\*\*\s*(.+?)\s*$", re.MULTILINE)

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
}


def slugify(title: str) -> str:
    """kebab-case ASCII slug. Used only when the model omitted the Slug line."""
    out = "".join(_TRANSLIT.get(ch, ch) for ch in (title or "").lower())
    out = re.sub(r"[^a-z0-9]+", "-", out).strip("-")
    return out or "skill"


def parse_skills(md: str | None) -> list[dict]:
    """Split the artifact into skills. Returns [] when nothing matches the shape."""
    if not md:
        return []
    heads = list(_SKILL_RE.finditer(md))
    skills = []
    for i, m in enumerate(heads):
        start = m.end()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(md)
        body = md[start:end].strip()
        title = m.group(1).strip()
        slug_m = _SLUG_RE.search(body)
        desc_m = _DESC_RE.search(body)
        skills.append({
            "slug": slug_m.group(1) if slug_m else slugify(title),
            "title": title,
            "description": desc_m.group(1) if desc_m else title,
            "body": body,
        })
    return skills


def _skill_md(skill: dict) -> str:
    """SKILL.md with YAML frontmatter Claude Code / Cursor can load directly."""
    desc = skill["description"].replace('"', "'")
    return (
        "---\n"
        f"name: {skill['slug']}\n"
        f"description: \"{desc}\"\n"
        "---\n\n"
        f"# {skill['title']}\n\n"
        f"{skill['body']}\n"
    )


def _mcp_server_py(skills: list[dict]) -> str:
    """stdio MCP server: prompts are complete, tools are honest stubs."""
    entries = json.dumps(
        [{"slug": s["slug"], "title": s["title"], "description": s["description"]}
         for s in skills],
        ensure_ascii=False, indent=4,
    )
    return f'''"""Generated MCP server — skills as prompts, algorithms as tool stubs.

Run:  python server.py     (stdio transport)

The prompts below are complete: each one serves the full SKILL.md text.
The tools are stubs on purpose — the algorithms artifact describes WHAT to do,
not the code. Fill in each TODO, or hand this file to an agent to implement.
"""
import pathlib

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("videotext-skills")

SKILLS = {entries}

_HERE = pathlib.Path(__file__).resolve().parent.parent


def _skill_text(slug: str) -> str:
    path = _HERE / "skills" / slug / "SKILL.md"
    return path.read_text(encoding="utf-8") if path.exists() else f"Скилл {{slug}} не найден"


for _s in SKILLS:
    def _make(slug=_s["slug"], description=_s["description"]):
        @mcp.prompt(name=slug, description=description)
        def _prompt() -> str:
            return _skill_text(slug)
        return _prompt
    _make()


@mcp.tool()
def list_skills() -> list[dict]:
    """List the skills bundled with this server."""
    return SKILLS


@mcp.tool()
def run_algorithm(name: str, payload: dict) -> dict:
    """Execute one of the algorithms from the source artifact.

    TODO: implement. See README.md for the algorithm definitions — each one lists
    its inputs, steps, exit criteria and edge cases. Dispatch on `name`.
    """
    raise NotImplementedError("Алгоритмы ещё не реализованы — см. README.md")


if __name__ == "__main__":
    mcp.run()
'''


def _readme(video_title: str, skills: list[dict], spec_md: str, algorithms_md: str) -> str:
    listing = "\n".join(f"- `{s['slug']}` — {s['description']}" for s in skills)
    return (
        f"# {video_title} — бандл скиллов\n\n"
        "Собрано автоматически из артефактов VideoText (стадии ТЗ, алгоритмы, AI-скиллы).\n\n"
        "## Состав\n\n"
        "- `skills/<slug>/SKILL.md` — готовые скиллы с frontmatter.\n"
        "- `mcp_server/` — MCP-сервер: промпты рабочие, тулы — заглушки с TODO.\n\n"
        "## Скиллы\n\n"
        f"{listing}\n\n"
        "## Установка\n\n"
        "```bash\n"
        "pip install -r mcp_server/requirements.txt\n"
        "python mcp_server/server.py\n"
        "```\n\n"
        "Скиллы: скопируй каталог `skills/` в `.claude/skills/` своего проекта.\n\n"
        "---\n\n"
        "## ТЗ\n\n"
        f"{spec_md or '_ТЗ не сгенерировано_'}\n\n"
        "---\n\n"
        "## Алгоритмы\n\n"
        f"{algorithms_md or '_Алгоритмы не сгенерированы_'}\n"
    )


def build_bundle(skills: list[dict], *, spec_md: str = "", algorithms_md: str = "",
                 video_title: str = "") -> bytes:
    """Zip with skills tree, MCP scaffold and a README carrying the upstream context."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for s in skills:
            zf.writestr(f"skills/{s['slug']}/SKILL.md", _skill_md(s))
        zf.writestr("mcp_server/server.py", _mcp_server_py(skills))
        zf.writestr("mcp_server/requirements.txt", "mcp>=1.2.0\n")
        zf.writestr("README.md", _readme(video_title, skills, spec_md, algorithms_md))
    return buf.getvalue()
```

Обрати внимание: `build_bundle` в тестах вызывается с именованными аргументами `spec_md=`, `algorithms_md=`, `video_title=` — сигнатура выше это допускает.

- [ ] **Step 4: Запустить тесты модуля**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skills_export.py -v`
Expected: PASS (7 тестов).

- [ ] **Step 5: Добавить маршрут**

В `server.py` рядом с `export_expansion_pdf` вставить:

```python
@app.get("/videos/{video_id}/skills-bundle.zip")
def export_skills_bundle(video_id: str):
    """Skills + MCP scaffold + README as one archive."""
    import skills_export
    e = get_expansion(video_id, "ai_skills")
    if not e or not (e.contentMd or "").strip():
        raise HTTPException(status_code=404, detail="Нет артефакта AI-скиллов")
    skills = skills_export.parse_skills(e.contentMd)
    if not skills:
        raise HTTPException(
            status_code=400,
            detail="Не удалось разобрать ни одного скилла — артефакт не соответствует"
                   " формату «## Скилл N.». Перегенерируй стадию AI-скиллы.",
        )
    spec = get_expansion(video_id, "spec")
    algos = get_expansion(video_id, "ai_algorithms")
    video = get_video(video_id)
    blob = skills_export.build_bundle(
        skills,
        spec_md=(spec.contentMd if spec else ""),
        algorithms_md=(algos.contentMd if algos else ""),
        video_title=(video.title if video else video_id),
    )
    return Response(
        content=blob,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="skills-{video_id}.zip"'},
    )
```

- [ ] **Step 6: Проверить маршрут вживую**

Run: `curl -s -o /tmp/b.zip -w "%{http_code}\n" http://127.0.0.1:8000/videos/Io_f4G7a_Eo/skills-bundle.zip && .venv/Scripts/python.exe -c "import zipfile;print(zipfile.ZipFile('/tmp/b.zip').namelist())"`
Expected: `200` и список файлов с `skills/.../SKILL.md`, `mcp_server/server.py`, `README.md`.

Если `400` — артефакт `ai_skills` для этого видео сгенерирован старым промптом; перегенерировать стадию (Task 5 уже поменял формат) и повторить.

- [ ] **Step 7: Добавить кнопку в UI**

Ссылки экспорта рендерятся в `static/editor-workspace.js` в **двух** местах — `selectArtifactMode` (строки 858-861) и `selectArtifactModeView` (строки 912-915). Обе строки собирают `$('af-export').innerHTML`; ZIP надо добавить в обе, иначе кнопка будет пропадать после каждого тика поллинга.

В `selectArtifactMode`:

```javascript
    $('af-export').innerHTML = (e && e.status === 'done')
      ? `<a href="/videos/${state.afSelectedId}/expansions/${mode}.md" target="_blank">.md</a>
         &nbsp; <a href="/videos/${state.afSelectedId}/expansions/${mode}.pdf" target="_blank">.pdf</a>
         ${mode === 'ai_skills'
           ? `&nbsp; <a href="/videos/${state.afSelectedId}/skills-bundle.zip" download>ZIP (скиллы + MCP)</a>`
           : ''}`
      : '';
```

В `selectArtifactModeView` — то же самое, но с `state.afMode` вместо локальной `mode`:

```javascript
    $('af-export').innerHTML = (e.status === 'done')
      ? `<a href="/videos/${state.afSelectedId}/expansions/${state.afMode}.md" target="_blank">.md</a>
         &nbsp; <a href="/videos/${state.afSelectedId}/expansions/${state.afMode}.pdf" target="_blank">.pdf</a>
         ${state.afMode === 'ai_skills'
           ? `&nbsp; <a href="/videos/${state.afSelectedId}/skills-bundle.zip" download>ZIP (скиллы + MCP)</a>`
           : ''}`
      : '';
```

- [ ] **Step 8: Прогнать набор**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add skills_export.py server.py static/editor-workspace.js tests/test_skills_export.py
git commit -m "feat(export): skills + MCP scaffold bundle as zip

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: Живая приёмка всего конвейера

**Files:**
- Modify: `docs/task-flow-v2.md` (синхронизировать с реализацией)

**Interfaces:**
- Consumes: всё выше.
- Produces: ничего кодового — это приёмочный прогон.

- [ ] **Step 1: Прогнать полный набор тестов**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS, ноль падений.

- [ ] **Step 2: Прогнать живую цепочку**

Запустить сервер и прогнать шесть стадий на `Io_f4G7a_Eo` тем же скриптом, что в аудите (создать `run_chain.py` во временной папке, если его нет):

```bash
for M in research report spec uiux ai_algorithms ai_skills; do
  curl -s -X POST http://127.0.0.1:8000/videos/Io_f4G7a_Eo/expand-spec \
    -H "Content-Type: application/json" \
    -d "{\"mode\":\"$M\",\"context\":\"both\",\"model\":\"claude-sonnet-4-6\"}"
done
```

Expected: все шесть `done`.

- [ ] **Step 3: Проверить, что ресерч реально проверяет**

Скачать артефакт ресерча и убедиться глазами:
- есть раздел «Проверка утверждений» с заполненной таблицей;
- есть раздел «Арифметическая и логическая сверка»;
- в «Источники» есть внешние ссылки (`http`), а не только транскрипт.

Expected: все три пункта выполнены. Если источников нет — вернуться к Task 8 Step 8.

- [ ] **Step 4: Проверить, что репорт сверяет и выносит вердикт**

Run: `.venv/Scripts/python.exe -c "import store,pipeline;e=store.get_expansion('Io_f4G7a_Eo','report');print(e.verdict);print('Сверка' in e.contentMd)"`
Expected: одно из `confirmed|partial|refuted` и `True`.

- [ ] **Step 5: Проверить гейты**

Прогнать AI-оценку на всех шести стадиях через `POST /videos/{id}/stage-assess/{stage}`.
Expected: `accepted` в репорте помечен как решение человека и не сброшен; `single` в алгоритмах больше не проваливается из-за наличия нескольких алгоритмов.

- [ ] **Step 6: Проверить версии**

Run: `curl -s http://127.0.0.1:8000/videos/Io_f4G7a_Eo/expansions/research/versions`
Expected: список из нескольких версий, новые сверху.

- [ ] **Step 7: Синхронизировать документ конвейера**

В `docs/task-flow-v2.md` обновить §3 (чеклисты — добавить новые пункты, отметить human-пункт), §5 (обратные петли — описать жёсткий стоп на `refuted`) и добавить упоминание внешней проверки в описание режима «Ресерч». Документ заявлен как канонический источник, и промпты теперь от него разошлись.

- [ ] **Step 8: Commit**

```bash
git add docs/task-flow-v2.md
git commit -m "docs(task-flow): sync canonical flow with the verification conveyor

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```
