# Transcript AI-Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an AI editor for video transcripts inside the AI Editor tab — read the full transcript, transform it via preset buttons (improve/structure/clean) or freeform chat, preview before applying, and keep a rollback-able version history.

**Architecture:** Original transcript (`Segment[]`) stays immutable as the source of truth. Edited versions live in a new append-only `TranscriptEdit` table. A new `transcript_edit.py` module builds per-op prompts and streams the new full text from Claude (default) or local Ollama. The frontend reuses the existing 2-column AI Editor chrome with a `Новости | Расшифровки` source toggle; a "подробнее" button on the Видео tab deep-links into it.

**Tech Stack:** Python 3.13, FastAPI, Prisma (SQLite), Anthropic SDK (streaming), Ollama (local), Alpine.js + vanilla JS frontend, pytest.

**Spec:** `docs/superpowers/specs/2026-05-27-transcript-ai-editor-design.md`

---

## File Structure

- **Create** `transcript_edit.py` — `SYSTEM_PROMPTS`, `build_edit_prompt`, `stream_edit` (Claude/Ollama dispatch). Mirrors `local_llm.py`.
- **Modify** `prisma/schema.prisma` — add `TranscriptEdit` model + relation on `Video`.
- **Modify** `store.py` — async + sync CRUD for `TranscriptEdit`.
- **Modify** `server.py` — request models + 7 transcript endpoints.
- **Modify** `static/index.html` — source toggle markup, transcripts workspace, "подробнее" buttons.
- **Modify** `static/editor-workspace.js` — source toggle + transcripts mode logic.
- **Modify** `static/app.js` — "подробнее" deep-link handler.
- **Modify** `static/i18n.js` — RU/EN labels.
- **Create** `tests/test_transcript_edit.py` — prompt routing + store version/rollback logic.

---

## Task 1: DB schema — `TranscriptEdit` model

**Files:**
- Modify: `prisma/schema.prisma` (Video model ~line 18-32; append new model after `Brief`)

- [ ] **Step 1: Add the relation field to `Video`**

In `prisma/schema.prisma`, inside `model Video { ... }`, add `transcriptEdits` to the relations block (after `expansions Expansion[]`):

```prisma
  segments       Segment[]
  briefs         Brief[]
  runs           Run[]
  expansions     Expansion[]
  transcriptEdits TranscriptEdit[]
```

- [ ] **Step 2: Add the `TranscriptEdit` model**

Add after the `Brief` model (after its closing `}`):

```prisma
// AI-edited versions of a video's transcript. The original Segment[] is the
// immutable source of truth; each edit is a new append-only version. Rollback
// to vK creates a new version cloned from vK (history stays linear).
model TranscriptEdit {
  id          Int      @id @default(autoincrement())
  videoId     String
  video       Video    @relation(fields: [videoId], references: [id], onDelete: Cascade)
  version     Int      // 1..N within a video
  contentMd   String   // full edited transcript text
  op          String   // "improve" | "structure" | "clean" | "chat" | "rollback"
  instruction String   @default("") // user's ТЗ / chat message
  fromVersion Int?     // lineage: which version this grew from (null = from original)
  model       String
  inputChars  Int      @default(0)
  elapsedMs   Int      @default(0)
  createdAt   DateTime @default(now())

  @@unique([videoId, version])
}
```

- [ ] **Step 3: Regenerate the Prisma client and push the schema**

Run:
```bash
.venv/Scripts/python.exe -m prisma generate
.venv/Scripts/python.exe -m prisma db push
```
Expected: `generated Prisma Client` then `Your database is now in sync with your Prisma schema.`

- [ ] **Step 4: Verify the model is queryable**

Run:
```bash
.venv/Scripts/python.exe -c "from prisma import Prisma; import asyncio; \
asyncio.run((lambda: (lambda db: (db.connect(), print('transcriptedit' in dir(db)), db.disconnect()))(Prisma()))()) if False else print('ok')"
.venv/Scripts/python.exe -c "import prisma.models as m; print('TranscriptEdit' in dir(m))"
```
Expected: `True`

- [ ] **Step 5: Commit**

```bash
git add prisma/schema.prisma
git commit -m "feat(db): add TranscriptEdit model for transcript version history"
```

---

## Task 2: `transcript_edit.py` — prompts + builder

**Files:**
- Create: `transcript_edit.py`
- Test: `tests/test_transcript_edit.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_transcript_edit.py`:

```python
"""Unit tests for transcript-edit prompt routing.

`build_edit_prompt` is pure (no DB/network), so we assert op → system-prompt
wiring directly. Guards against an op silently falling back to the wrong prompt.
"""
import transcript_edit as te

_OPS = ["improve", "structure", "clean", "chat"]


def _build(op):
    return te.build_edit_prompt(
        op=op,
        current_text="Приветствую, уважаемые трейдеры. Робот строит спред.",
        instruction="разбей по темам",
    )


def test_all_ops_registered():
    for op in _OPS:
        assert op in te.SYSTEM_PROMPTS, f"{op} missing from SYSTEM_PROMPTS"


def test_ops_select_their_own_system_prompt():
    for op in _OPS:
        system, _user = _build(op)
        assert system == te.SYSTEM_PROMPTS[op]


def test_user_message_carries_text_and_instruction():
    _system, user = _build("structure")
    assert "Приветствую, уважаемые трейдеры" in user
    assert "разбей по темам" in user


def test_unknown_op_falls_back_to_improve():
    system, _ = te.build_edit_prompt(op="bogus", current_text="x", instruction="")
    assert system == te.SYSTEM_PROMPTS["improve"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_transcript_edit.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'transcript_edit'`

- [ ] **Step 3: Write `transcript_edit.py` (prompts + builder)**

Create `transcript_edit.py`:

```python
"""Transcript AI-editor — prompt building + streaming dispatch.

Mirrors local_llm.py: pure "current text + instruction -> new full text".
Default backend is Claude (quality matters for RU editing); any model not
recognised as a Claude id is routed to local Ollama via local_llm.stream_chat.
"""
from __future__ import annotations

from collections.abc import Iterator

import brief  # resolve_model + _MODEL_ALIASES
import local_llm

# ─── Per-op system prompts ─────────────────────────────────────────
# Each op transforms the WHOLE transcript and returns the full new text.
SYSTEM_PROMPTS: dict[str, str] = {
    "improve": (
        "Ты — редактор расшифровок видео. Тебе дан полный текст расшифровки"
        " (часто из автосубтитров с ошибками). Задача: улучшить интерпретацию —"
        " исправить ошибки распознавания, термины, имена собственные, названия"
        " продуктов и технологий; восстановить пунктуацию и регистр.\n\n"
        "ЗАПРЕТЫ: не менять смысл и факты, не сокращать содержание, не добавлять"
        " то, чего не было. Верни ТОЛЬКО полный исправленный текст в markdown,"
        " без преамбул и комментариев."
    ),
    "structure": (
        "Ты — редактор расшифровок видео. Тебе дан полный текст расшифровки."
        " Задача: структурировать — разбить на логические разделы с markdown-"
        "заголовками (##) и абзацами, при необходимости списками.\n\n"
        "ЗАПРЕТЫ: ничего не выбрасывать и не сокращать — только переразбить и"
        " оформить существующий текст. Не выдумывать заголовки, которых тема не"
        " подразумевает. Верни ТОЛЬКО полный структурированный текст в markdown,"
        " без преамбул и комментариев."
    ),
    "clean": (
        "Ты — редактор расшифровок видео. Тебе дан полный текст расшифровки."
        " Задача: вычитать — убрать слова-паразиты, повторы, оговорки, обрывы"
        " фраз и разговорный шум; сделать текст связным и читаемым.\n\n"
        "ЗАПРЕТЫ: сохранить все факты, цифры, имена и выводы; не пересказывать,"
        " не сокращать содержательную часть. Верни ТОЛЬКО полный вычитанный текст"
        " в markdown, без преамбул и комментариев."
    ),
    "chat": (
        "Ты — редактор расшифровок видео. Тебе дан полный текст расшифровки и"
        " инструкция пользователя, что с ним сделать. Выполни инструкцию над всем"
        " текстом.\n\n"
        "ЗАПРЕТЫ: если инструкция не требует сокращения — ничего не выбрасывай."
        " Не добавляй фактов, которых нет в тексте. Верни ТОЛЬКО полный новый"
        " текст в markdown, без преамбул и комментариев."
    ),
}


def build_edit_prompt(*, op: str, current_text: str, instruction: str) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt). Unknown ops fall back to 'improve'."""
    system = SYSTEM_PROMPTS.get(op, SYSTEM_PROMPTS["improve"])
    instr = (instruction or "").strip()
    if op == "chat":
        tail = f"\n\n## Инструкция пользователя\n{instr}" if instr else ""
    else:
        tail = f"\n\n## Дополнительная инструкция\n{instr}" if instr else ""
    user = (
        "Вот текущий текст расшифровки.\n\n"
        f"--- ТЕКСТ ---\n{current_text}{tail}"
    )
    return system, user
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_transcript_edit.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add transcript_edit.py tests/test_transcript_edit.py
git commit -m "feat(transcript): add edit-prompt builder with per-op system prompts"
```

---

## Task 3: `transcript_edit.py` — streaming dispatch

**Files:**
- Modify: `transcript_edit.py`
- Test: `tests/test_transcript_edit.py`

- [ ] **Step 1: Write the failing test (backend selection logic)**

Append to `tests/test_transcript_edit.py`:

```python
def test_is_claude_recognises_claude_ids_and_aliases():
    assert te._is_claude("claude-sonnet-4-6")
    assert te._is_claude("sonnet")        # alias from brief._MODEL_ALIASES
    assert te._is_claude("")              # empty -> default Claude
    assert not te._is_claude("qwen2.5:7b")
    assert not te._is_claude("nomic-embed-text:latest")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_transcript_edit.py::test_is_claude_recognises_claude_ids_and_aliases -q`
Expected: FAIL with `AttributeError: module 'transcript_edit' has no attribute '_is_claude'`

- [ ] **Step 3: Add dispatch + streaming to `transcript_edit.py`**

Append to `transcript_edit.py`:

```python
# ─── Backend dispatch ──────────────────────────────────────────────

def _is_claude(model: str) -> bool:
    """True if the model id should be served by Anthropic (vs local Ollama).
    Empty string means 'use the Claude default'."""
    m = (model or "").strip()
    if not m:
        return True
    return m.startswith("claude") or m in brief._MODEL_ALIASES


def _stream_claude(system: str, user: str, model: str) -> Iterator[str]:
    """Stream text deltas from Claude. System prompt is prompt-cached."""
    import anthropic
    client = anthropic.Anthropic()
    resolved = brief.resolve_model(model or None)
    with client.messages.stream(
        model=resolved,
        max_tokens=16000,  # full-text rewrites of ~20K-char transcripts
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    ) as stream:
        for text in stream.text_stream:
            yield text


def stream_edit(
    *, op: str, current_text: str, instruction: str, model: str,
    num_ctx: int = local_llm.DEFAULT_NUM_CTX, temperature: float = 0.3,
) -> Iterator[str]:
    """Build the prompt and stream the new full text from the chosen backend."""
    system, user = build_edit_prompt(op=op, current_text=current_text, instruction=instruction)
    if _is_claude(model):
        yield from _stream_claude(system, user, model)
    else:
        yield from local_llm.stream_chat(
            system=system, user=user, model=model,
            num_ctx=num_ctx, temperature=temperature,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_transcript_edit.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add transcript_edit.py tests/test_transcript_edit.py
git commit -m "feat(transcript): add Claude/Ollama streaming dispatch for edits"
```

---

## Task 4: `store.py` — TranscriptEdit CRUD + versioning + rollback

**Files:**
- Modify: `store.py` (add async fns near `_list_expansions` ~line 596; sync wrappers near line 607)
- Test: `tests/test_transcript_edit.py`

- [ ] **Step 1: Write the failing test (uses the `db` fixture)**

Append to `tests/test_transcript_edit.py`:

```python
import pytest
import store


@pytest.mark.asyncio
async def test_version_numbering_and_rollback(db):
    # Seed a video (FK target for TranscriptEdit).
    await db.video.create(data={"id": "vid1", "url": "u", "source": "test"})

    v1 = await store._create_transcript_edit(
        video_id="vid1", content_md="text v1", op="improve",
        instruction="", from_version=None, model="claude-sonnet-4-6",
        input_chars=10, elapsed_ms=5,
    )
    assert v1.version == 1

    v2 = await store._create_transcript_edit(
        video_id="vid1", content_md="text v2", op="structure",
        instruction="разбей", from_version=1, model="claude-sonnet-4-6",
        input_chars=12, elapsed_ms=6,
    )
    assert v2.version == 2

    versions = await store._list_transcript_edits("vid1")
    assert [r.version for r in versions] == [2, 1]  # newest first

    # Rollback to v1 -> new v3 cloned from v1.
    v3 = await store._rollback_transcript_edit("vid1", 1)
    assert v3.version == 3
    assert v3.contentMd == "text v1"
    assert v3.op == "rollback"
    assert v3.fromVersion == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_transcript_edit.py::test_version_numbering_and_rollback -q`
Expected: FAIL with `AttributeError: module 'store' has no attribute '_create_transcript_edit'`

- [ ] **Step 3: Add async fns + sync wrappers to `store.py`**

Add after `_list_expansions` (after line 595, before the sync wrappers block):

```python
# ─── Transcript edits (version history) ────────────────────────────

async def _create_transcript_edit(
    *, video_id: str, content_md: str, op: str, instruction: str,
    from_version: int | None, model: str, input_chars: int, elapsed_ms: int,
):
    db = Prisma()
    await db.connect()
    try:
        last = await db.transcriptedit.find_first(
            where={"videoId": video_id}, order={"version": "desc"},
        )
        version = (last.version + 1) if last else 1
        return await db.transcriptedit.create(data={
            "videoId": video_id, "version": version, "contentMd": content_md,
            "op": op, "instruction": instruction, "fromVersion": from_version,
            "model": model, "inputChars": input_chars, "elapsedMs": elapsed_ms,
        })
    finally:
        await db.disconnect()


async def _list_transcript_edits(video_id: str):
    db = Prisma()
    await db.connect()
    try:
        return await db.transcriptedit.find_many(
            where={"videoId": video_id}, order={"version": "desc"},
        )
    finally:
        await db.disconnect()


async def _get_transcript_edit(video_id: str, version: int):
    db = Prisma()
    await db.connect()
    try:
        return await db.transcriptedit.find_unique(
            where={"videoId_version": {"videoId": video_id, "version": version}},
        )
    finally:
        await db.disconnect()


async def _rollback_transcript_edit(video_id: str, version: int):
    db = Prisma()
    await db.connect()
    try:
        src = await db.transcriptedit.find_unique(
            where={"videoId_version": {"videoId": video_id, "version": version}},
        )
        if not src:
            return None
        last = await db.transcriptedit.find_first(
            where={"videoId": video_id}, order={"version": "desc"},
        )
        new_version = (last.version + 1) if last else 1
        return await db.transcriptedit.create(data={
            "videoId": video_id, "version": new_version, "contentMd": src.contentMd,
            "op": "rollback", "instruction": f"откат к v{version}",
            "fromVersion": version, "model": src.model,
            "inputChars": 0, "elapsedMs": 0,
        })
    finally:
        await db.disconnect()
```

- [ ] **Step 4: Add sync wrappers to `store.py`**

Add at the end of the sync-wrappers area (after `list_expansions` ~line 607):

```python
def create_transcript_edit(**kwargs):
    return asyncio.run(_create_transcript_edit(**kwargs))


def list_transcript_edits(video_id: str):
    return asyncio.run(_list_transcript_edits(video_id))


def get_transcript_edit(video_id: str, version: int):
    return asyncio.run(_get_transcript_edit(video_id, version))


def rollback_transcript_edit(video_id: str, version: int):
    return asyncio.run(_rollback_transcript_edit(video_id, version))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_transcript_edit.py -q`
Expected: PASS (6 passed)

- [ ] **Step 6: Commit**

```bash
git add store.py tests/test_transcript_edit.py
git commit -m "feat(store): TranscriptEdit CRUD with versioning and append-only rollback"
```

---

## Task 5: `server.py` — read endpoints (original + versions + export)

**Files:**
- Modify: `server.py` (import block ~line 44-48; add routes after the expansions block ~line 660)
- Test: `tests/test_endpoints.py`

- [ ] **Step 1: Add store imports**

In `server.py`, extend the `from store import (...)` block (around line 44-48) to include the new functions:

```python
    create_transcript_edit, get_transcript_edit, list_transcript_edits,
    rollback_transcript_edit,
```
(append these names to the existing import list)

- [ ] **Step 2: Write the failing test (route registration)**

Append to `tests/test_endpoints.py`:

```python
def test_transcript_routes_registered():
    paths = {r.path for r in app.routes}
    assert "/videos/{video_id}/transcript" in paths
    assert "/videos/{video_id}/transcript/edits" in paths
    assert "/videos/{video_id}/transcript/edits/{version}" in paths
    assert "/videos/{video_id}/transcript/edit" in paths
    assert "/videos/{video_id}/transcript/edits/apply" in paths
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_endpoints.py::test_transcript_routes_registered -q`
Expected: FAIL (paths not in set)

- [ ] **Step 4: Add read endpoints + helper to `server.py`**

Add after the expansion routes (after `read_expansion`, ~line 660). The `.md`/`.pdf` and `apply` routes are declared BEFORE the `{version}` route so the param doesn't shadow them (same rule as expansions):

```python
# ─── Transcript AI-editor ──────────────────────────────────────────

def _original_transcript_text(video_id: str) -> tuple[str, Any]:
    """(joined original transcript text, video). Raises 404/400 as needed."""
    v = get_video(video_id, with_segments=True)
    if not v:
        raise HTTPException(status_code=404, detail=f"Video {video_id} not found")
    if not v.segments:
        raise HTTPException(status_code=400, detail="У видео нет расшифровки")
    text = "\n".join(s.text for s in v.segments if s.text)
    return text, v


def _edit_to_dict(e) -> dict:
    return {
        "id": e.id, "video_id": e.videoId, "version": e.version,
        "content_md": e.contentMd, "op": e.op, "instruction": e.instruction,
        "from_version": e.fromVersion, "model": e.model,
        "input_chars": e.inputChars, "elapsed_ms": e.elapsedMs,
        "created_at": e.createdAt.isoformat(),
    }


@app.get("/videos/{video_id}/transcript")
def read_transcript(video_id: str) -> dict:
    text, v = _original_transcript_text(video_id)
    edits = list_transcript_edits(video_id)
    return {
        "video_id": video_id,
        "title": v.title,
        "original_md": text,
        "original_chars": len(text),
        "versions": len(edits),
        "latest_version": edits[0].version if edits else 0,
    }


@app.get("/videos/{video_id}/transcript/edits")
def read_transcript_edits(video_id: str) -> list[dict]:
    return [_edit_to_dict(e) for e in list_transcript_edits(video_id)]


@app.get("/videos/{video_id}/transcript/edits/{version}.pdf")
def export_transcript_edit_pdf(video_id: str, version: int):
    from export import markdown_to_pdf
    e = get_transcript_edit(video_id, version)
    if not e:
        raise HTTPException(status_code=404, detail=f"No v{version} for {video_id}")
    pdf_bytes = markdown_to_pdf(e.contentMd, title=f"Расшифровка v{version}")
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="transcript-v{version}-{video_id}.pdf"'},
    )


@app.get("/videos/{video_id}/transcript/edits/{version}.md")
def export_transcript_edit_md(video_id: str, version: int):
    e = get_transcript_edit(video_id, version)
    if not e:
        raise HTTPException(status_code=404, detail=f"No v{version} for {video_id}")
    return Response(
        content=e.contentMd, media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="transcript-v{version}-{video_id}.md"'},
    )


@app.get("/videos/{video_id}/transcript/edits/{version}")
def read_transcript_edit(video_id: str, version: int) -> dict:
    e = get_transcript_edit(video_id, version)
    if not e:
        raise HTTPException(status_code=404, detail=f"No v{version} for {video_id}")
    return _edit_to_dict(e)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_endpoints.py::test_transcript_routes_registered -q`
Expected: still FAIL (edit/apply routes added in Task 6) — confirm the three read paths now exist by running:
`.venv/Scripts/python.exe -c "from server import app; print([r.path for r in app.routes if 'transcript' in r.path])"`
Expected: lists `/videos/{video_id}/transcript`, `/.../edits`, `/.../edits/{version}`, `/.../edits/{version}.md`, `/.../edits/{version}.pdf`

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_endpoints.py
git commit -m "feat(server): transcript read + version + pdf endpoints"
```

---

## Task 6: `server.py` — edit (SSE preview), apply, rollback

**Files:**
- Modify: `server.py` (after the read endpoints from Task 5)
- Test: `tests/test_endpoints.py` (the `test_transcript_routes_registered` from Task 5 goes green here)

- [ ] **Step 1: Add request models + write/preview endpoints**

Add after `read_transcript_edit` in `server.py`:

```python
class TranscriptEditRequest(BaseModel):
    op: Literal["improve", "structure", "clean", "chat"] = "improve"
    instruction: str = ""
    model: str | None = None        # None/empty -> Claude default
    base_version: int | None = None # which version we edit from (None = original)


class TranscriptApplyRequest(BaseModel):
    op: str
    instruction: str = ""
    content_md: str
    model: str = ""
    from_version: int | None = None
    elapsed_ms: int = 0


@app.post("/videos/{video_id}/transcript/edit")
def transcript_edit_preview(video_id: str, req: TranscriptEditRequest):
    """Stream a proposed new full text (SSE). Does NOT persist."""
    import transcript_edit

    original, _v = _original_transcript_text(video_id)
    # Edit from the requested base version if given, else the original text.
    current = original
    if req.base_version:
        base = get_transcript_edit(video_id, req.base_version)
        if not base:
            raise HTTPException(status_code=404, detail=f"No v{req.base_version}")
        current = base.contentMd

    settings = get_all_settings()
    model = (req.model or settings.get("editor_transcript_model") or "claude-sonnet-4-6").strip()
    num_ctx = int(settings.get("local_llm_num_ctx") or 32768)
    temperature = float(settings.get("local_llm_temperature") or 0.3)

    def event_stream():
        import time
        started = time.monotonic()
        try:
            yield f"data: {json.dumps({'type': 'meta', 'model': model, 'op': req.op, 'base_version': req.base_version, 'current_chars': len(current)}, ensure_ascii=False)}\n\n"
            for piece in transcript_edit.stream_edit(
                op=req.op, current_text=current, instruction=req.instruction,
                model=model, num_ctx=num_ctx, temperature=temperature,
            ):
                yield f"data: {json.dumps({'type': 'delta', 'text': piece}, ensure_ascii=False)}\n\n"
            elapsed_ms = int((time.monotonic() - started) * 1000)
            yield f"data: {json.dumps({'type': 'done', 'model': model, 'op': req.op, 'elapsed_ms': elapsed_ms}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'msg': f'{type(e).__name__}: {e}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/videos/{video_id}/transcript/edits/apply")
def transcript_edit_apply(video_id: str, req: TranscriptApplyRequest) -> dict:
    """Persist a new version from the previewed text."""
    if not (req.content_md or "").strip():
        raise HTTPException(status_code=400, detail="Пустой текст — нечего сохранять")
    row = create_transcript_edit(
        video_id=video_id, content_md=req.content_md, op=req.op,
        instruction=req.instruction, from_version=req.from_version,
        model=req.model or "claude-sonnet-4-6",
        input_chars=len(req.content_md), elapsed_ms=req.elapsed_ms,
    )
    return _edit_to_dict(row)


@app.post("/videos/{video_id}/transcript/edits/{version}/rollback")
def transcript_edit_rollback(video_id: str, version: int) -> dict:
    row = rollback_transcript_edit(video_id, version)
    if not row:
        raise HTTPException(status_code=404, detail=f"No v{version} for {video_id}")
    return _edit_to_dict(row)
```

- [ ] **Step 2: Run the registration test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_endpoints.py::test_transcript_routes_registered -q`
Expected: PASS

- [ ] **Step 3: Run the full Python suite (no regressions)**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: all prior tests + new ones PASS (skips for smoke remain)

- [ ] **Step 4: Live smoke — preview a real edit (server must be running)**

Start a clean server (kill any stale uvicorn first, then):
```bash
.venv/Scripts/python.exe -m uvicorn server:app --port 8000 --log-level warning &
```
Then preview a `structure` edit on the arbitrage transcript (write payload to a UTF-8 file to avoid shell quoting):
```bash
printf '%s' '{"op":"structure","model":"qwen2.5:3b"}' > _test/tx.json
curl -s --max-time 300 -N -X POST http://127.0.0.1:8000/videos/ZkoOGPLwTBE/transcript/edit \
  -H "Content-Type: application/json" --data-binary @_test/tx.json | grep -o '"type": "done"[^}]*}' | head -c 120
```
Expected: a `"type": "done"` frame (and many `delta` frames before it).

- [ ] **Step 5: Commit**

```bash
git add server.py
git commit -m "feat(server): transcript edit (SSE preview), apply, rollback endpoints"
```

---

## Task 7: Frontend — source toggle (Новости | Расшифровки)

**Files:**
- Modify: `static/index.html` (AI Editor section `#editor-section` header)
- Modify: `static/editor-workspace.js` (add `state.source`, toggle wiring, show/hide panes)

> No JS unit-test harness exists in this repo (all tests are pytest). Frontend tasks are implemented, then verified live in the browser via the running server.

- [ ] **Step 1: Add the toggle markup**

In `static/index.html`, at the top of the AI Editor section (inside `#editor-section`, before the existing 2-column layout), add:

```html
<div class="editor-source-toggle" style="display:flex; gap:8px; margin-bottom:12px;">
  <button type="button" id="editor-src-news" class="is-active">Новости</button>
  <button type="button" id="editor-src-transcripts">Расшифровки</button>
</div>
```

Wrap the existing news 2-column markup in `<div id="editor-pane-news"> ... </div>` and add an empty sibling `<div id="editor-pane-transcripts" style="display:none;"></div>` (its inner markup is built in Task 8).

- [ ] **Step 2: Wire the toggle in `editor-workspace.js`**

Add to `state` (line ~11): `source: 'news',`. Add a function and call it from `wire()`:

```javascript
  function setSource(src) {
    state.source = src;
    $('editor-src-news').classList.toggle('is-active', src === 'news');
    $('editor-src-transcripts').classList.toggle('is-active', src === 'transcripts');
    $('editor-pane-news').style.display = src === 'news' ? '' : 'none';
    $('editor-pane-transcripts').style.display = src === 'transcripts' ? '' : 'none';
    if (src === 'transcripts') loadTranscriptVideos();  // defined in Task 8
  }
```
In `wire()` add:
```javascript
    $('editor-src-news').addEventListener('click', () => setSource('news'));
    $('editor-src-transcripts').addEventListener('click', () => setSource('transcripts'));
```
Expose for deep-link (end of IIFE, before the closing `})();`):
```javascript
  window.editorSetSource = setSource;
  window.editorSelectVideo = (id) => { setSource('transcripts'); selectTranscriptVideo(id); };
```

- [ ] **Step 3: Verify live**

Reload `http://127.0.0.1:8000/`, open AI Editor tab, click both toggle buttons.
Expected: clicking "Расшифровки" hides the news panes and shows the (empty for now) transcripts pane; "Новости" restores the original.

- [ ] **Step 4: Commit**

```bash
git add static/index.html static/editor-workspace.js
git commit -m "feat(editor-ui): News|Transcripts source toggle in AI Editor"
```

---

## Task 8: Frontend — transcripts list + load original/versions

**Files:**
- Modify: `static/index.html` (`#editor-pane-transcripts` inner markup)
- Modify: `static/editor-workspace.js` (transcript state + loaders + render)

- [ ] **Step 1: Add the transcripts pane markup**

Inside `#editor-pane-transcripts` in `static/index.html`:

```html
<div class="editor-grid">
  <aside class="editor-list-col">
    <div class="editor-list-meta" id="tx-videos-meta">—</div>
    <div id="tx-videos-list"><em>Загружаю…</em></div>
  </aside>
  <section class="editor-main-col">
    <div id="tx-empty"><em>Выберите видео слева.</em></div>
    <div id="tx-item" style="display:none;">
      <h2 id="tx-title">—</h2>
      <div id="tx-meta" class="editor-item-row-meta">—</div>
      <div class="tx-view-toggle" style="margin:8px 0;">
        <button type="button" id="tx-show-current" class="is-active">улучшенный</button>
        <button type="button" id="tx-show-original">оригинал</button>
      </div>
      <pre id="tx-text" class="tx-text"></pre>
      <div id="tx-actions"></div>           <!-- filled in Task 9 -->
      <div id="tx-preview"></div>           <!-- filled in Task 9 -->
      <div id="tx-versions"></div>          <!-- versions list -->
    </div>
  </section>
</div>
```

- [ ] **Step 2: Add transcript state + loaders to `editor-workspace.js`**

Add to `state`: `txVideos: [], txSelectedId: null, txOriginal: '', txVersions: [], txShowing: 'current'`. Add:

```javascript
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
    try {
      const t = await fetchJSON(`/videos/${id}/transcript`);
      state.txOriginal = t.original_md;
      $('tx-title').textContent = t.title || id;
      $('tx-meta').textContent = `оригинал ${t.original_chars} симв · версий ${t.versions}`;
      await loadTranscriptVersions(id);
      showTranscript('current');
      renderTranscriptActions();   // defined in Task 9
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
    $('tx-show-current').classList.toggle('is-active', which === 'current');
    $('tx-show-original').classList.toggle('is-active', which === 'original');
    $('tx-text').textContent = which === 'original' ? state.txOriginal : currentTranscriptText();
  }

  function renderTranscriptVersions() {
    const box = $('tx-versions');
    if (!state.txVersions.length) { box.innerHTML = '<em>Версий пока нет — это оригинал.</em>'; return; }
    box.innerHTML = '<div class="editor-list-meta">версии</div>' + state.txVersions.map(v => `
      <div class="tx-version-row">
        <span>v${v.version} · ${escapeHtml(v.op)} · ${v.input_chars} симв</span>
        <button type="button" data-roll="${v.version}">откатить</button>
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
```

Wire the view toggle in `wire()`:
```javascript
    $('tx-show-current').addEventListener('click', () => showTranscript('current'));
    $('tx-show-original').addEventListener('click', () => showTranscript('original'));
```

- [ ] **Step 3: Verify live**

Reload, AI Editor → Расшифровки. Click a video.
Expected: title + meta load, full text renders, оригинал/улучшенный toggle works, versions list shows (empty message if none), rollback button on existing versions creates a new version.

- [ ] **Step 4: Commit**

```bash
git add static/index.html static/editor-workspace.js
git commit -m "feat(editor-ui): transcripts list, full-text view, versions + rollback"
```

---

## Task 9: Frontend — action buttons + chat + SSE preview + apply

**Files:**
- Modify: `static/editor-workspace.js`

- [ ] **Step 1: Render the action bar + chat**

Add `renderTranscriptActions()`:

```javascript
  function renderTranscriptActions() {
    $('tx-actions').innerHTML = `
      <div class="tx-actions-bar">
        <button type="button" data-op="improve">улучшить интерпретацию</button>
        <button type="button" data-op="structure">структурировать</button>
        <button type="button" data-op="clean">почистить</button>
      </div>
      <input id="tx-instruction" type="text" placeholder="ТЗ / инструкция (необязательно)">
      <select id="tx-model"></select>
      <div class="tx-chat">
        <input id="tx-chat-input" type="text" placeholder="чат: что сделать с текстом…">
        <button type="button" id="tx-chat-send">→</button>
      </div>`;
    // model options: Claude default + local Ollama models
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
```

- [ ] **Step 2: Stream a preview (SSE) and render apply controls**

Add `runTranscriptEdit` + `applyTranscriptEdit` (mirrors `runTool`/`applyPreview` SSE parsing already in this file):

```javascript
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
      box.innerHTML = '<pre class="tx-text" id="tx-proposed"></pre>';
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
      // apply / cancel controls
      const bar = document.createElement('div');
      bar.className = 'editor-preview-actions';
      bar.innerHTML = `<button type="button" data-role="apply">✓ применить (новая версия)</button>
                       <button type="button" data-role="cancel">✕ отмена</button>`;
      box.appendChild(bar);
      bar.querySelector('[data-role="cancel"]').onclick = () => { box.innerHTML = ''; };
      bar.querySelector('[data-role="apply"]').onclick = () =>
        applyTranscriptEdit({ op, instruction, content_md: proposed, model, from_version: baseVersion });
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
```

- [ ] **Step 3: Add a "показать дифф" toggle to the preview**

Add a minimal line-level diff (LCS) and wire a toggle into the preview bar. First add the helpers (top-level inside the IIFE):

```javascript
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
      const cls = d.t === '+' ? 'tx-diff-add' : d.t === '-' ? 'tx-diff-del' : 'tx-diff-eq';
      return `<div class="${cls}">${escapeHtml(d.t + ' ' + d.line)}</div>`;
    }).join('');
  }
```

Then, in `runTranscriptEdit` (Step 2), extend the controls bar to include a diff toggle. Replace the `bar.innerHTML = ...` line with:

```javascript
      bar.innerHTML = `<button type="button" data-role="apply">✓ применить (новая версия)</button>
                       <button type="button" data-role="diff">показать дифф</button>
                       <button type="button" data-role="cancel">✕ отмена</button>`;
```

And after the existing `cancel`/`apply` handlers in that function, add the diff toggle handler:

```javascript
      let showingDiff = false;
      const out2 = $('tx-proposed');
      bar.querySelector('[data-role="diff"]').onclick = (ev) => {
        showingDiff = !showingDiff;
        ev.target.textContent = showingDiff ? 'показать текст' : 'показать дифф';
        if (showingDiff) out2.innerHTML = renderDiffHtml(currentTranscriptText(), proposed);
        else out2.textContent = proposed;
      };
```

Add the diff line colors to `static/index.html` `<style>` (or styles.css):

```css
.tx-diff-add { background:#f0fdf4; color:#166534; }
.tx-diff-del { background:#fef2f2; color:#b91c1c; }
.tx-diff-eq  { color: var(--mute); }
```

- [ ] **Step 4: Verify live (full loop)**

Reload, AI Editor → Расшифровки → pick the arbitrage video → click "структурировать".
Expected: proposed text streams into the preview pane; "показать дифф" toggles a line-level add/del view against the current text; "✓ применить" creates v1 (appears in versions list, becomes the "улучшенный" current); a chat instruction then produces v2 from v1; "откатить v1" produces v3 == v1's text.

- [ ] **Step 5: Commit**

```bash
git add static/editor-workspace.js static/index.html
git commit -m "feat(editor-ui): transcript edit actions, chat, SSE preview, diff, apply"
```

---

## Task 10: Frontend — "подробнее" deep-link from Видео tab

**Files:**
- Modify: `static/index.html` (brief preview section ~line 471-483; history rows ~line 514)
- Modify: `static/app.js` (add `openTranscriptEditor` method)

- [ ] **Step 1: Add the method in `app.js`**

Add to the Alpine component (near `loadVideo`, ~line 819):

```javascript
    openTranscriptEditor(videoId) {
      if (!videoId) return;
      this.setView('editor');
      // editor-workspace.js exposes this once its IIFE has run
      if (window.editorSelectVideo) window.editorSelectVideo(videoId);
    },
```

- [ ] **Step 2: Add the "подробнее" button to the brief preview**

In `static/index.html`, in the per-section action row (~line 471-483, next to "🦙 расширить"), add once per result (place it in the result header, not per-section — put near line 466 before the `<template x-for>`):

```html
<div class="flex justify-end mb-2">
  <button x-show="result.video_id" @click="openTranscriptEditor(result.video_id)"
          class="text-[11px] text-ink-500 hover:text-ink"
          title="Открыть полную расшифровку в AI-редакторе">подробнее →</button>
</div>
```

- [ ] **Step 3: Add "подробнее" to history rows**

In the history `<template x-for="(h, i) in historyVideos">` button (~line 514), the row is a `<button @click="loadVideo(h.id)">`. Add a nested control that stops propagation:

```html
<span @click.stop="openTranscriptEditor(h.id)"
      class="text-[11px] text-ink-500 hover:text-ink cursor-pointer"
      title="Открыть расшифровку в AI-редакторе">подробнее</span>
```
Place it inside the row's grid (add a column) so it doesn't trigger `loadVideo`.

- [ ] **Step 4: Verify live**

Reload. On the Видео tab, process or open a video, click "подробнее →".
Expected: jumps to AI Editor, Расшифровки source active, that video selected with its transcript loaded.

- [ ] **Step 5: Commit**

```bash
git add static/index.html static/app.js
git commit -m "feat(video-ui): 'подробнее' deep-link into transcript AI-editor"
```

---

## Task 11: i18n labels + styles polish

**Files:**
- Modify: `static/i18n.js` (RU + EN dictionaries)
- Modify: `static/index.html` (optional: swap hardcoded RU labels for `t('...')` where the surrounding code uses i18n)

- [ ] **Step 1: Add keys to both dictionaries**

In `static/i18n.js`, add to the RU dictionary:
```javascript
  'editor.src.news': 'Новости',
  'editor.src.transcripts': 'Расшифровки',
  'tx.op.improve': 'улучшить интерпретацию',
  'tx.op.structure': 'структурировать',
  'tx.op.clean': 'почистить',
  'tx.detail': 'подробнее',
```
And the EN equivalents:
```javascript
  'editor.src.news': 'News',
  'editor.src.transcripts': 'Transcripts',
  'tx.op.improve': 'improve interpretation',
  'tx.op.structure': 'structure',
  'tx.op.clean': 'clean up',
  'tx.detail': 'details',
```

- [ ] **Step 2: Verify live**

Reload, toggle the language switch.
Expected: toggle labels and op buttons follow the selected language (where wired through `t()`).

- [ ] **Step 3: Commit**

```bash
git add static/i18n.js static/index.html
git commit -m "feat(i18n): labels for transcript AI-editor"
```

---

## Task 12: Full verification pass

**Files:** none (verification only)

- [ ] **Step 1: Run the entire Python suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: all PASS (smoke skipped).

- [ ] **Step 2: End-to-end live drive on the arbitrage transcript**

With a clean server running:
1. Видео tab → open video `ZkoOGPLwTBE` → "подробнее →".
2. AI Editor → Расшифровки → video selected, original text visible.
3. "структурировать" → preview streams → ✓ применить → v1 appears, becomes current.
4. Chat: "сожми вступление" → preview → применить → v2.
5. "откатить v1" → v3 created, current == v1 text.
6. Download v2 `.pdf` → opens a real PDF.

Expected: every step behaves as described; original (оригинал toggle) is unchanged throughout.

- [ ] **Step 3: Clean up scratch + final commit if needed**

```bash
rm -f _test/tx.json
git status   # should be clean
```

---

## Notes for the implementer

- **Server reload caveat (Windows):** `--reload` spawns a worker via multiprocessing; killing the parent leaves the worker holding port 8000. To restart cleanly: kill by the listening PID (`Get-NetTCPConnection -LocalPort 8000`), or just run without `--reload` and restart manually after Python changes.
- **Cyrillic in curl payloads:** always write the JSON body to a UTF-8 file and use `--data-binary @file`; inline `-d '{...}'` with Cyrillic gets mangled by the shell and returns "error parsing the body".
- **Route ordering:** the `.pdf` route is declared before `{version}` on purpose (path params match dots) — do not reorder.
- **No JS test harness:** frontend tasks are verified live in the browser, not via unit tests. Don't fabricate a JS test framework.
