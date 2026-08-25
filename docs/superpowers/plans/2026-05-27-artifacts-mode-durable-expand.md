# Артефакты mode + durable generation + v2 prompts — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move expand-artifacts into an AI Editor "Артефакты" mode with durable (navigation-proof) background generation; rewrite all mode prompts to the v2 flow + add a UI/UX mode; refine the transcript-editor ops + add a "расширить идею" op.

**Architecture:** Three independently-shippable layers in one plan. **Layer 2b** (first, quick win on the already-built transcript editor): rewrite `transcript_edit.py` op prompts + add `expand_idea`. **Layer 1**: `Expansion` gains `status`/`error`; `POST /expand-spec` becomes fire-and-forget (daemon thread persists the result regardless of client), with a startup sweep; new "Артефакты" source mode polls status. **Layer 2**: rewrite `local_llm.py` expand prompts to the v2 templates + add `uiux` mode.

**Tech Stack:** Python 3.13, FastAPI, Prisma (SQLite), Anthropic + Ollama, Alpine.js + vanilla JS, pytest.

**Spec:** `docs/superpowers/specs/2026-05-27-artifacts-mode-durable-expand-design.md`
**Prompt source of truth:** `docs/task-flow-v2.md`

---

## File Structure

- **Modify** `transcript_edit.py` — rewrite `clean`/`structure`/`improve` prompts; add `expand_idea` (layer 2b).
- **Modify** `static/editor-workspace.js` — `expand_idea` button + reorder transcript ops (2b); add "Артефакты" source mode + polling (layer 1); `uiux` mode button (layer 2).
- **Modify** `prisma/schema.prisma` — `Expansion.status` + `Expansion.error` (layer 1).
- **Modify** `store.py` — `start_expansion` / `finish_expansion` / `fail_expansion` / `sweep_running_expansions` (layer 1).
- **Modify** `server.py` — durable `expand_spec`, lifespan sweep, `_expansion_to_dict` status, `ExpandMode += uiux` (layers 1+2).
- **Modify** `local_llm.py` — rewrite expand `SYSTEM_PROMPTS` + add `uiux` (layer 2).
- **Modify** `static/index.html` — "Артефакты" pane; remove `specExpand` modal + per-section "🦙 расширить" buttons; cache-bust bumps.
- **Modify** `static/app.js` — remove `specExpand` state + methods + `isSpecSection`.
- **Modify** `tests/test_transcript_edit.py`, `tests/test_expand_modes.py` — extend for new ops/modes.

---

# ═══ LAYER 2b — transcript editor ops (FIRST, quick win) ═══

## Task 1: Rewrite transcript ops + add `expand_idea`

**Files:**
- Modify: `transcript_edit.py` (`SYSTEM_PROMPTS`)
- Test: `tests/test_transcript_edit.py`

- [ ] **Step 1: Update the test for the new op + boundaries**

In `tests/test_transcript_edit.py`, change `_OPS` and add boundary assertions:

```python
_OPS = ["improve", "structure", "clean", "chat", "expand_idea"]


def test_clean_forbids_adding_new():
    assert "не добав" in te.SYSTEM_PROMPTS["clean"].lower()


def test_expand_idea_allows_new_and_is_registered():
    s = te.SYSTEM_PROMPTS["expand_idea"].lower()
    assert "расшир" in s or "развив" in s
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_transcript_edit.py -q`
Expected: FAIL — `test_all_ops_registered` (expand_idea missing) + `test_expand_idea_allows_new_and_is_registered` (KeyError).

- [ ] **Step 3: Rewrite the prompts + add `expand_idea`**

Replace the whole `SYSTEM_PROMPTS` dict in `transcript_edit.py` with:

```python
SYSTEM_PROMPTS: dict[str, str] = {
    "clean": (
        "Ты — редактор расшифровок. Режим ПОЧИСТИТЬ. Задача: сделать сырой текст"
        " грамотным и читаемым.\n\n"
        "ЧТО ДЕЛАЕШЬ: убираешь повторы и слова-паразиты; исправляешь речевые ошибки;"
        " расставляешь пунктуацию и регистр; делаешь текст связным и читаемым.\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не меняешь смысл; НЕ добавляешь ничего нового — ни идей, ни"
        " пояснений, ни примеров; не разбиваешь на разделы. Верни ТОЛЬКО очищенный"
        " полный текст в markdown, без преамбул."
    ),
    "structure": (
        "Ты — редактор расшифровок. Режим СТРУКТУРИРОВАТЬ. Задача: разложить понятный,"
        " но идущий потоком текст по полкам.\n\n"
        "ЧТО ДЕЛАЕШЬ: делишь на смысловые разделы с заголовками (##); выделяешь шаги,"
        " пункты и подпункты; сохраняешь оригинальную мысль.\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не превращаешь в полноценную статью; НЕ добавляешь новых идей и"
        " пояснений; не переписываешь смысл. Верни ТОЛЬКО структурированный полный текст"
        " в markdown, без преамбул."
    ),
    "improve": (
        "Ты — редактор расшифровок. Режим УЛУЧШИТЬ ИНТЕРПРЕТАЦИЮ. Задача: восстановить и"
        " объяснить смысл, когда расшифровка неточная, но понятно, что хотел сказать"
        " человек.\n\n"
        "ЧТО ДЕЛАЕШЬ: восстанавливаешь смысл; формулируешь мысль понятнее; объясняешь, о"
        " чём речь; убираешь хаос устной речи; можешь слегка уточнить формулировки.\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не фантазируешь и не выдумываешь фактов; не развиваешь мысль в"
        " новое (это режим «расширить идею»); не превращаешь в статью. Верни ТОЛЬКО"
        " улучшенный полный текст в markdown, без преамбул."
    ),
    "expand_idea": (
        "Ты — редактор-автор. Режим РАСШИРИТЬ ИДЕЮ. Задача: из короткой мысли сделать"
        " полноценное объяснение — инструкцию, урок или раздел документации.\n\n"
        "ЧТО ДЕЛАЕШЬ: развиваешь исходную мысль; добавляешь пояснения и примеры;"
        " показываешь пользу; можешь предложить дополнительные сценарии; можешь оформить"
        " как статью / инструкцию / обучающий блок.\n\n"
        "Это ЕДИНСТВЕННЫЙ режим, которому РАЗРЕШЕНО добавлять новое от AI. Опирайся на"
        " исходный текст, не противоречь ему и не выдумывай фактов, которых он не"
        " подразумевает. Верни ТОЛЬКО полный текст в markdown, без преамбул."
    ),
    "chat": (
        "Ты — редактор расшифровок видео. Тебе дан полный текст расшифровки и инструкция"
        " пользователя, что с ним сделать. Выполни инструкцию над всем текстом.\n\n"
        "ЗАПРЕТЫ: если инструкция не требует сокращения — ничего не выбрасывай. Не"
        " добавляй фактов, которых нет в тексте. Верни ТОЛЬКО полный новый текст в"
        " markdown, без преамбул и комментариев."
    ),
}
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_transcript_edit.py -q`
Expected: PASS (all, including the two new tests).

- [ ] **Step 5: Commit**

```bash
git add transcript_edit.py tests/test_transcript_edit.py
git commit -m "feat(transcript): v2 op prompts (clean/structure/improve) + расширить идею"
```

## Task 2: Transcript editor UI — `expand_idea` button + reorder

**Files:**
- Modify: `static/editor-workspace.js` (`renderTranscriptActions`)
- Modify: `static/index.html` (cache-bust `editor-workspace.js?v=2` → `?v=3`)

- [ ] **Step 1: Add the button + progression order**

In `renderTranscriptActions()` replace the `.editor-actions-bar` block with the four ops in progression order:

```javascript
      <div class="editor-actions-bar">
        <button type="button" data-op="clean">почистить</button>
        <button type="button" data-op="structure">структурировать</button>
        <button type="button" data-op="improve">улучшить интерпретацию</button>
        <button type="button" data-op="expand_idea">расширить идею</button>
      </div>
```

(The existing `[data-op]` click wiring already routes any op through `runTranscriptEdit(el.dataset.op, ...)`, so no handler change is needed.)

- [ ] **Step 2: Cache-bust**

In `static/index.html` change `editor-workspace.js?v=2` to `?v=3`.

- [ ] **Step 3: Verify live**

Restart/refresh, AI Editor → Расшифровки → pick a video. Click "расширить идею" with a short transcript; confirm a preview streams and apply creates a version. Click "почистить" and confirm output keeps meaning without adding new ideas.

- [ ] **Step 4: Commit**

```bash
git add static/editor-workspace.js static/index.html
git commit -m "feat(editor-ui): расширить идею op + ops progression order"
```

---

# ═══ LAYER 1 — durable generation + Артефакты mode ═══

## Task 3: Schema — `Expansion.status` + `error`

**Files:**
- Modify: `prisma/schema.prisma` (`Expansion` model)

- [ ] **Step 1: Add the fields**

In `model Expansion { ... }`, after `elapsedMs Int @default(0)`, add:

```prisma
  status        String   @default("done") // "running" | "done" | "error"
  error         String?
```

- [ ] **Step 2: Regenerate + push**

Run:
```bash
.venv/Scripts/python.exe -m prisma generate
.venv/Scripts/python.exe -m prisma db push
```
Expected: client regenerated; DB in sync.

- [ ] **Step 3: Verify column exists**

Run:
```bash
.venv/Scripts/python.exe -c "import sqlite3; c=sqlite3.connect('prisma/videotext.db'); print([r[1] for r in c.execute('PRAGMA table_info(Expansion)')])"
```
Expected: list includes `status` and `error`.

- [ ] **Step 4: Commit**

```bash
git add prisma/schema.prisma
git commit -m "feat(db): Expansion.status + error for durable generation"
```

## Task 4: Store — durable expansion lifecycle

**Files:**
- Modify: `store.py` (near the existing expansion functions ~line 545-607)
- Test: `tests/test_expansion_durable.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_expansion_durable.py`:

```python
"""Durable-expansion store lifecycle: start(running) -> finish(done) | fail(error),
and the startup sweep that clears orphaned 'running' rows."""
import pytest
import store


async def _seed_video(db):
    await db.video.create(data={"id": "vidE", "url": "u", "source": "test"})


@pytest.mark.asyncio
async def test_start_then_finish(db):
    await _seed_video(db)
    r = await store._start_expansion(
        video_id="vidE", mode="research", source_title="ТЗ", source_md="x",
        context_mode="both", model="m", num_ctx=8192, input_chars=10,
    )
    assert r.status == "running"
    done = await store._finish_expansion(
        video_id="vidE", mode="research", content_md="готовый текст", elapsed_ms=42,
    )
    assert done.status == "done"
    assert done.contentMd == "готовый текст"
    assert done.elapsedMs == 42


@pytest.mark.asyncio
async def test_start_then_fail_keeps_old_content(db):
    await _seed_video(db)
    await store._start_expansion(
        video_id="vidE", mode="report", source_title="", source_md="",
        context_mode="brief", model="m", num_ctx=8192, input_chars=1,
    )
    failed = await store._fail_expansion(video_id="vidE", mode="report", error="boom")
    assert failed.status == "error"
    assert failed.error == "boom"


@pytest.mark.asyncio
async def test_sweep_running_to_error(db):
    await _seed_video(db)
    await store._start_expansion(
        video_id="vidE", mode="spec", source_title="", source_md="",
        context_mode="brief", model="m", num_ctx=8192, input_chars=1,
    )
    n = await store._sweep_running_expansions()
    assert n >= 1
    row = await store._get_expansion("vidE", "spec")
    assert row.status == "error"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_expansion_durable.py -q`
Expected: FAIL — `AttributeError: module 'store' has no attribute '_start_expansion'`.

- [ ] **Step 3: Add the lifecycle functions to `store.py`**

Add after `_list_expansions` (and before the sync wrappers block):

```python
async def _start_expansion(
    *, video_id: str, mode: str, source_title: str, source_md: str,
    context_mode: str, model: str, num_ctx: int, input_chars: int,
):
    """UPSERT status=running. On update, preserve the previous contentMd so the
    UI keeps showing the old artifact while the new one regenerates."""
    db = Prisma()
    await db.connect()
    try:
        return await db.expansion.upsert(
            where={"videoId_mode": {"videoId": video_id, "mode": mode}},
            data={
                "create": {
                    "videoId": video_id, "mode": mode, "sourceTitle": source_title,
                    "sourceMd": source_md, "contextMode": context_mode, "model": model,
                    "numCtx": num_ctx, "contentMd": "", "inputChars": input_chars,
                    "elapsedMs": 0, "status": "running", "error": None,
                },
                "update": {
                    "sourceTitle": source_title, "sourceMd": source_md,
                    "contextMode": context_mode, "model": model, "numCtx": num_ctx,
                    "inputChars": input_chars, "status": "running", "error": None,
                },
            },
        )
    finally:
        await db.disconnect()


async def _finish_expansion(*, video_id: str, mode: str, content_md: str, elapsed_ms: int):
    db = Prisma()
    await db.connect()
    try:
        return await db.expansion.update(
            where={"videoId_mode": {"videoId": video_id, "mode": mode}},
            data={"contentMd": content_md, "elapsedMs": elapsed_ms,
                  "status": "done", "error": None},
        )
    finally:
        await db.disconnect()


async def _fail_expansion(*, video_id: str, mode: str, error: str):
    db = Prisma()
    await db.connect()
    try:
        return await db.expansion.update(
            where={"videoId_mode": {"videoId": video_id, "mode": mode}},
            data={"status": "error", "error": error[:2000]},
        )
    finally:
        await db.disconnect()


async def _sweep_running_expansions() -> int:
    """Mark orphaned running rows (from a crash/restart) as error. Returns count."""
    db = Prisma()
    await db.connect()
    try:
        return await db.expansion.update_many(
            where={"status": "running"},
            data={"status": "error", "error": "прервано рестартом"},
        )
    finally:
        await db.disconnect()
```

Add sync wrappers at the end of the sync-wrappers area:

```python
def start_expansion(**kwargs):
    return asyncio.run(_start_expansion(**kwargs))


def finish_expansion(**kwargs):
    return asyncio.run(_finish_expansion(**kwargs))


def fail_expansion(**kwargs):
    return asyncio.run(_fail_expansion(**kwargs))


def sweep_running_expansions() -> int:
    return asyncio.run(_sweep_running_expansions())
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_expansion_durable.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add store.py tests/test_expansion_durable.py
git commit -m "feat(store): durable expansion lifecycle (start/finish/fail/sweep)"
```

## Task 5: Server — fire-and-forget `expand_spec` + sweep + status in dict

**Files:**
- Modify: `server.py` (imports ~44; lifespan ~56-62; `_expansion_to_dict`; `expand_spec` 496-592)
- Test: `tests/test_expansion_durable.py` (add a job test)

- [ ] **Step 1: Write the failing test for the job runner**

Append to `tests/test_expansion_durable.py`:

```python
@pytest.mark.asyncio
async def test_run_expansion_job_persists_without_client(db, monkeypatch):
    """The background job must persist status=done even though no HTTP client is
    connected — that's the whole point of durability."""
    import server
    await _seed_video(db)
    await store._start_expansion(
        video_id="vidE", mode="research", source_title="", source_md="",
        context_mode="brief", model="m", num_ctx=8192, input_chars=1,
    )
    # Stub the LLM stream so the test is offline/deterministic.
    monkeypatch.setattr(server.local_llm, "stream_chat",
                        lambda **kw: iter(["часть1 ", "часть2"]))
    server._run_expansion_job(
        video_id="vidE", mode="research", system="s", user="u",
        model="m", num_ctx=8192, temperature=0.3,
    )
    row = await store._get_expansion("vidE", "research")
    assert row.status == "done"
    assert row.contentMd == "часть1 часть2"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_expansion_durable.py::test_run_expansion_job_persists_without_client -q`
Expected: FAIL — `AttributeError: module 'server' has no attribute '_run_expansion_job'` (and `local_llm` not imported at module level — fixed in Step 4).

- [ ] **Step 3: Add store imports + lifespan sweep**

In `server.py` extend the `from store import (...)` block with:
```python
    fail_expansion, finish_expansion, start_expansion, sweep_running_expansions,
```
Add a module-level `import local_llm` near the other imports (so the job + monkeypatch can reach it):
```python
import local_llm                        # noqa: E402
```
Change the lifespan to sweep orphaned running rows on startup:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await orchestrator.startup()
    try:
        swept = await asyncio.to_thread(sweep_running_expansions)
        if swept:
            print(f"[expand] swept {swept} orphaned 'running' expansion(s) -> error")
    except Exception as e:
        print(f"[expand] sweep failed: {e}")
    try:
        yield
    finally:
        await orchestrator.shutdown()
```

- [ ] **Step 4: Replace the `expand_spec` endpoint + add the job runner**

Replace the whole `expand_spec` function (and its docstring) with the fire-and-forget version + a module-level job runner and concurrency guard. Add near the top of the file (after `app = FastAPI(...)`):

```python
import threading
_expansion_jobs: set[tuple[str, str]] = set()
_expansion_jobs_lock = threading.Lock()


def _run_expansion_job(*, video_id, mode, system, user, model, num_ctx, temperature):
    """Runs in a daemon thread. Streams the LLM fully, then persists. Never tied
    to the HTTP request, so client disconnect/navigation cannot abort it."""
    import time
    started = time.monotonic()
    try:
        chunks = list(local_llm.stream_chat(
            system=system, user=user, model=model,
            num_ctx=num_ctx, temperature=temperature,
        ))
        full_text = "".join(chunks).strip()
        if full_text:
            finish_expansion(video_id=video_id, mode=mode, content_md=full_text,
                             elapsed_ms=int((time.monotonic() - started) * 1000))
        else:
            fail_expansion(video_id=video_id, mode=mode, error="пустой ответ модели")
    except Exception as e:
        fail_expansion(video_id=video_id, mode=mode, error=f"{type(e).__name__}: {e}")
    finally:
        with _expansion_jobs_lock:
            _expansion_jobs.discard((video_id, mode))
```

Then the new endpoint:

```python
@app.post("/videos/{video_id}/expand-spec")
def expand_spec(video_id: str, req: ExpandSpecRequest) -> dict:
    """Fire-and-forget: start a durable background generation and return immediately.
    The result is persisted to Expansion regardless of whether the client stays
    connected. The UI polls GET /videos/{id}/expansions/{mode} for status."""
    video = get_video(video_id, with_segments=True)
    if not video:
        raise HTTPException(status_code=404, detail=f"Video {video_id} not found")
    if not video.briefs:
        raise HTTPException(status_code=400, detail="У видео нет брифа")

    key = (video_id, req.mode)
    existing = get_expansion(video_id, req.mode)
    if existing and existing.status == "running":
        return {"status": "running", "mode": req.mode, "already": True}
    with _expansion_jobs_lock:
        if key in _expansion_jobs:
            return {"status": "running", "mode": req.mode, "already": True}
        _expansion_jobs.add(key)

    latest = video.briefs[-1]
    settings = get_all_settings()
    model = (req.model or settings.get("local_llm_model") or local_llm.DEFAULT_MODEL).strip()
    num_ctx = int(settings.get("local_llm_num_ctx") or local_llm.DEFAULT_NUM_CTX)
    temperature = float(settings.get("local_llm_temperature") or 0.3)
    max_tx_chars = int(settings.get("local_llm_max_transcript_chars") or local_llm.MAX_TRANSCRIPT_CHARS)

    ctx_mode = req.context or ("both" if req.include_transcript else "brief")
    use_brief = ctx_mode in ("brief", "both")
    use_transcript = ctx_mode in ("transcript", "both")

    sb_json = None
    if use_brief and latest.contentJson:
        try:
            sb_json = (json.loads(latest.contentJson) if isinstance(latest.contentJson, str)
                       else latest.contentJson).get("software_brief")
        except (json.JSONDecodeError, AttributeError):
            sb_json = None

    transcript_excerpt = ""
    if use_transcript and video.segments:
        transcript_excerpt = "\n".join(s.text for s in video.segments if s.text)
        local_llm.MAX_TRANSCRIPT_CHARS = max_tx_chars

    system, user = local_llm.build_expand_prompt(
        mode=req.mode, video_title=video.title or video_id,
        section_title=req.section_title, section_md=req.section_md,
        software_brief_json=sb_json,
        full_brief_md=(latest.contentMd or "") if use_brief else "",
        transcript_excerpt=transcript_excerpt,
    )

    # Mark running (preserves any previous content) BEFORE launching the thread.
    start_expansion(
        video_id=video_id, mode=req.mode, source_title=req.section_title,
        source_md=req.section_md, context_mode=ctx_mode, model=model,
        num_ctx=num_ctx, input_chars=len(user),
    )
    threading.Thread(
        target=_run_expansion_job, daemon=True,
        kwargs={"video_id": video_id, "mode": req.mode, "system": system,
                "user": user, "model": model, "num_ctx": num_ctx,
                "temperature": temperature},
    ).start()
    return {"status": "running", "mode": req.mode, "context_mode": ctx_mode}
```

Also make `section_md` optional in `ExpandSpecRequest` (source is the whole video now):
```python
    section_md: str = ""
    section_title: str = "бриф"
```

- [ ] **Step 5: Add `status`/`error` to `_expansion_to_dict`**

In `_expansion_to_dict`, add to the returned dict:
```python
        "status": getattr(e, "status", "done"),
        "error": getattr(e, "error", None),
```

- [ ] **Step 6: Run the job test + full suite**

Run: `.venv/Scripts/python.exe -m pytest tests/test_expansion_durable.py -q`
Expected: PASS (4 passed).
Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: all green (smoke skipped).

- [ ] **Step 7: Commit**

```bash
git add server.py tests/test_expansion_durable.py
git commit -m "feat(server): durable fire-and-forget expand-spec + startup sweep"
```

## Task 6: Frontend — "Артефакты" source mode + status polling

**Files:**
- Modify: `static/index.html` (source toggle adds 3rd button; new `#editor-pane-artifacts`)
- Modify: `static/editor-workspace.js` (artifacts mode logic)

- [ ] **Step 1: Add the toggle button + pane markup**

In `static/index.html`, in `.editor-source-toggle`, add a third button after `editor-src-transcripts`:

```html
    <button type="button" id="editor-src-artifacts"
            style="padding:6px 14px; border:1px solid var(--line); border-radius:8px; font-size:13px; cursor:pointer;">Артефакты</button>
```

After `#editor-pane-transcripts` closing div, add the artifacts pane:

```html
  <div class="editor-workspace" id="editor-pane-artifacts" style="display:none;">
    <aside class="editor-workspace-left">
      <div class="editor-items-meta" id="af-videos-meta">—</div>
      <div class="editor-items-list" id="af-videos-list"><em>Загружаю…</em></div>
    </aside>
    <section class="editor-workspace-right">
      <div class="editor-empty" id="af-empty">← Выбери видео слева</div>
      <div class="editor-item" id="af-item" style="display:none;">
        <div class="editor-item-header"><h2 id="af-title">—</h2></div>
        <div class="editor-actions-bar" id="af-modes"></div>
        <div style="display:flex; gap:10px; align-items:center; margin:8px 0; font-size:12px;">
          <label style="color:var(--mute);">контекст:</label>
          <select id="af-context">
            <option value="both">бриф + расшифровка</option>
            <option value="brief">бриф</option>
            <option value="transcript">расшифровка</option>
          </select>
          <label style="color:var(--mute);">модель:</label>
          <select id="af-model"></select>
          <button type="button" id="af-generate" style="cursor:pointer; padding:5px 14px;">сгенерировать</button>
        </div>
        <div id="af-status" style="font-size:12px; color:var(--mute); margin-bottom:8px;"></div>
        <pre id="af-text" class="tx-text" style="white-space:pre-wrap; font-family:inherit; font-size:13px; line-height:1.55; max-height:420px; overflow-y:auto; background:var(--panel); padding:14px; border-radius:8px;"></pre>
        <div id="af-export" style="margin-top:8px; font-size:12px;"></div>
      </div>
    </section>
  </div>
```

- [ ] **Step 2: Add artifacts state + logic in `editor-workspace.js`**

Add to `state`: `afVideos: [], afSelectedId: null, afMode: 'research', afExpansions: {}, afPoll: null`.

Add the `MODES` constant and functions:

```javascript
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
    state.afSelectedId = id;
    $('af-empty').style.display = 'none';
    $('af-item').style.display = 'block';
    const v = state.afVideos.find(x => x.id === id);
    $('af-title').textContent = v ? (v.title || id) : id;
    // model options: Ollama models (expand uses local LLM)
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
    if (e.status === 'running') box.textContent = '⏳ генерируется… (можно уйти с экрана)';
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
    state.afExpansions[mode] = { mode, status: 'running', content_md: state.afExpansions[mode]?.content_md || '' };
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
      } catch (_) {}
    }, 2000);
  }

  function stopArtifactPolling() {
    if (state.afPoll) { clearInterval(state.afPoll); state.afPoll = null; }
  }

  // Update only the view for an already-selected mode (avoids re-triggering polling).
  function selectArtifactModeView(e) {
    renderArtifactStatus(e);
    $('af-text').textContent = e.content_md || '';
    $('af-export').innerHTML = (e.status === 'done')
      ? `<a href="/videos/${state.afSelectedId}/expansions/${state.afMode}.md" target="_blank">.md</a>
         &nbsp; <a href="/videos/${state.afSelectedId}/expansions/${state.afMode}.pdf" target="_blank">.pdf</a>`
      : '';
  }
```

- [ ] **Step 3: Extend `setSource` + wire**

Update `setSource` to handle three panes:

```javascript
  function setSource(src) {
    state.source = src;
    const on = (b, active) => { if (b) { b.style.background = active ? 'var(--ink)' : 'white'; b.style.color = active ? 'white' : 'var(--ink)'; } };
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
```

In `wire()` add:
```javascript
    $('editor-src-artifacts').addEventListener('click', () => setSource('artifacts'));
    $('af-generate').addEventListener('click', () => generateArtifact());
```

Bump `editor-workspace.js?v=3` → `?v=4` in `index.html`.

- [ ] **Step 4: Verify live (durability is the key check)**

AI Editor → Артефакты → pick a video → mode "Алгоритмы" → "сгенерировать". Status shows ⏳. **Switch to the Видео tab, wait, switch back to Артефакты** → status reaches ✅ and the text is there (process survived navigation). `.md`/`.pdf` links open. Switch modes — each shows its own status.

- [ ] **Step 5: Commit**

```bash
git add static/index.html static/editor-workspace.js
git commit -m "feat(editor-ui): Артефакты mode with durable status polling"
```

## Task 7: Remove the `specExpand` modal + per-section buttons

**Files:**
- Modify: `static/index.html` (modal block ~2488-2588; per-section "🦙 расширить" button ~480)
- Modify: `static/app.js` (`specExpand` state + methods + `isSpecSection`)

- [ ] **Step 1: Remove the modal markup**

In `static/index.html` delete the entire `<!-- Spec expansion modal (local Ollama) -->` block (the `div.modal-backdrop` containing `specExpand.open`, through its closing `</div>`).

- [ ] **Step 2: Remove the per-section expand button**

In the brief preview section, delete the button:
```html
<button @click="openExpandSpec(result.video_id, sec.title, sec.body)" ...>🦙 расширить</button>
```
(and the `isSpecSection`-gated download stays as-is — only the 🦙 button goes). Keep "копировать" and the ТЗ ".md" download.

- [ ] **Step 3: Remove the JS**

In `static/app.js` delete: the `specExpand: { ... }` state object; methods `expandPlaceholder`, `preloadExpansion`, `expandSavedLabel`, `downloadExpansionPdf`, `expandPhaseLabel`, `openExpandSpec`, `closeExpandSpec`, `runExpandSpec`, `copyExpandedSpec`, `downloadExpandedSpec`, `isSpecSection`, and the `$watch('specExpand.mode', ...)` in the init block. Grep to confirm zero remaining references:

```bash
grep -n "specExpand\|openExpandSpec\|isSpecSection" static/app.js static/index.html
```
Expected: no matches.

- [ ] **Step 4: Cache-bust + verify live**

Bump `app.js?v=12` → `?v=13` in `index.html`. Reload. Видео tab: brief renders, no "🦙 расширить" button, no console errors. Expand now lives only in AI Editor → Артефакты.

- [ ] **Step 5: Commit**

```bash
git add static/app.js static/index.html
git commit -m "refactor(ui): remove specExpand modal — expand lives in Артефакты mode"
```

---

# ═══ LAYER 2 — v2 expand prompts + UI/UX mode ═══

## Task 8: Rewrite expand prompts + add `uiux`

**Files:**
- Modify: `local_llm.py` (`SYSTEM_PROMPTS` + `build_expand_prompt` instruction map)
- Modify: `server.py` (`ExpandMode` Literal)
- Test: `tests/test_expand_modes.py`

- [ ] **Step 1: Update the test for `uiux` + boundaries**

In `tests/test_expand_modes.py` change `_NEW_MODES` and add an assertion:

```python
_NEW_MODES = ["ai_skills", "ai_algorithms", "uiux"]


def test_uiux_prompt_is_interface_oriented():
    s = local_llm.SYSTEM_PROMPTS["uiux"].lower()
    assert "экран" in s and "состояни" in s
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_expand_modes.py -q`
Expected: FAIL — `uiux` missing from `SYSTEM_PROMPTS`.

- [ ] **Step 3: Rewrite `SYSTEM_PROMPTS` in `local_llm.py`**

Replace the `SYSTEM_PROMPTS` dict with the v2-aligned versions (template + «Что НЕ делает» per `docs/task-flow-v2.md`):

```python
SYSTEM_PROMPTS: dict[str, str] = {
    "research": (
        "Ты делаешь РЕСЕРЧ по теме: собрать и первично структурировать информацию.\n\n"
        "СЕКЦИИ: Задача / Контекст (где и зачем это нужно) / Варианты решения (минимум 2) /"
        " Ограничения / Риски / Источники и наблюдения / Открытые вопросы.\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не делаешь выводы и не рекомендуешь решение (это репорт); не"
        " пишешь ТЗ; не придумываешь реализацию. Если данных нет — пиши '—' / 'TBD'."
        " markdown-заголовки и списки, без преамбул."
    ),
    "report": (
        "Ты составляешь РЕПОРТ по результатам ресерча: сделать выводы и рекомендацию.\n\n"
        "СЕКЦИИ: Краткое резюме (2-3 предложения) / Что исследовали / Основные выводы /"
        " Варианты решений + плюсы-минусы каждого / Риски / Рекомендация + обоснование /"
        " Следующие действия.\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не пишешь ТЗ; не описываешь реализацию; не перечисляешь всё"
        " найденное без выводов. markdown, без преамбул."
    ),
    "spec": (
        "Ты пишешь ТЗ: зафиксировать постановку задачи на реализацию. Деловой, конкретный.\n\n"
        "СЕКЦИИ: Название задачи / Цель / Пользователь-заказчик / Пользовательские сценарии /"
        " Функциональные требования / Нефункциональные требования / Ограничения /"
        " Используемые данные / Критерии приёмки / Что не входит в скоп.\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не описываешь порядок реализации (это алгоритм); не рекомендуешь"
        " технологии (это репорт); не описываешь интерфейс детально (это UI/UX). Если"
        " данных нет — 'TBD'. markdown, без преамбул."
    ),
    "uiux": (
        "Ты описываешь UI/UX — интерфейсный сценарий взаимодействия пользователя с"
        " функцией. Нужен только если задача предполагает интерфейс.\n\n"
        "UI-СЛОЙ: какие экраны нужны; какие элементы на каждом экране; какие состояния"
        " (загрузка/успех/ошибка/пустое); какие сообщения видит пользователь.\n"
        "UX-СЛОЙ: что пользователь делает первым; что происходит после каждого действия;"
        " где нужна подсказка/подтверждение/предупреждение; где можно упростить путь.\n\n"
        "ФОРМАТ на каждый экран: Экран / Цель экрана / Элементы / Кнопки и действия /"
        " Состояния (загрузка/успех/ошибка/пусто) / Переходы (при успехе → / при ошибке →).\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не описываешь внутреннюю логику; не пишешь алгоритм обработки"
        " данных; не заменяешь ТЗ. markdown, без преамбул."
    ),
    "ai_algorithms": (
        "Ты описываешь АЛГОРИТМЫ действий — пошагово, что происходит в системе, у"
        " пользователя или в AI-процессе. Выдели 1-4 алгоритма. Для КАЖДОГО:\n\n"
        "Название алгоритма / Тип [пользовательский | системный | AI | технический] /"
        " Цель / Предусловия / Входные данные (что брать, откуда) / Шаги (нумерованно:"
        " что взять, что с чем сравнить, что проверить) / Правила и условия (если X → Y) /"
        " Критерии выхода / Результат / Граничные случаи / Ошибки и обработка.\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не пишешь ТЗ (не фиксируешь требования); не пишешь AI-скилл (не"
        " повторяемая инструкция). Не выдумывай пороги/формулы — помечай '[TBD: ...]'."
        " markdown и нумерованные списки, без преамбул."
    ),
    "ai_skills": (
        "Ты проектируешь AI-СКИЛЛЫ — повторяемые инструкции для агента (Claude Code /"
        " Cursor). Скилл оправдан, если действие повторяется 3+ раза. Выдели 2-5 скиллов."
        " Для КАЖДОГО:\n\n"
        "Название скилла / Версия / Когда использовать / Когда НЕ использовать /"
        " Входные данные (обязательные / опциональные) / Что делать (шаги) / Что не делать /"
        " Формат результата / Критерии качества / Примеры (Вход / Выход).\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не заменяешь алгоритм; не пишешь ТЗ; не создаёшь скилл из сырой"
        " идеи без предварительных этапов. Не выдумывай API/данные — 'TBD: уточнить'."
        " markdown, без преамбул."
    ),
}
```

In `build_expand_prompt`, extend the `instruction` map:
```python
        "research":      "Подготовь ресерч по структуре из системного промпта.",
        "report":        "Составь репорт по структуре из системного промпта.",
        "spec":          "Сформулируй ТЗ по структуре из системного промпта.",
        "uiux":          "Опиши UI/UX-сценарий по структуре из системного промпта.",
        "ai_algorithms": "Опиши алгоритмы действий по структуре из системного промпта.",
        "ai_skills":     "Спроектируй AI-скиллы по структуре из системного промпта.",
```
(remove the obsolete `"report": "Составь executive-report..."` etc. — replace the whole map; unknown modes still fall back to the dict's `.get(mode, ...)` default.)

- [ ] **Step 4: Add `uiux` to `ExpandMode` in `server.py`**

```python
ExpandMode = Literal["spec", "research", "report", "uiux", "ai_skills", "ai_algorithms"]
```

- [ ] **Step 5: Run tests + full suite**

Run: `.venv/Scripts/python.exe -m pytest tests/test_expand_modes.py -q`
Expected: PASS.
Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add local_llm.py server.py tests/test_expand_modes.py
git commit -m "feat(expand): v2 prompts (research/report/spec/algorithms/skills) + UI/UX mode"
```

## Task 9: Verify UI/UX mode end-to-end + full pass

**Files:** none (verification)

- [ ] **Step 1: Confirm `uiux` is offered in Артефакты**

`AF_MODES` (Task 6) already includes `['uiux', 'UI/UX']`. Reload, AI Editor → Артефакты → pick a video → "UI/UX" → "сгенерировать". Status reaches ✅, text follows the Экран/Элементы/Состояния/Переходы format.

- [ ] **Step 2: Full Python suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: all PASS (smoke skipped).

- [ ] **Step 3: Final clean state**

```bash
git status   # clean
```

---

## Notes for the implementer

- **Restart cleanly (Windows):** `uvicorn --reload` leaves an orphan worker holding port 8000 when the parent is killed. Kill by the listening PID (`Get-NetTCPConnection -LocalPort 8000`) or run without `--reload`. Python changes need a restart; static JS does not (but bump `?v=`).
- **Cache-bust:** every edit to `app.js` / `editor-workspace.js` must bump its `?v=N` in `index.html` or the browser serves stale JS.
- **Cyrillic curl payloads:** write JSON to a UTF-8 file and use `--data-binary @file`.
- **Durability is the headline requirement:** Task 6 Step 4 (generate → navigate away → come back → done) is the acceptance test for the whole feature. Don't skip it.
- **No JS test harness:** frontend tasks are verified live, not via unit tests.
