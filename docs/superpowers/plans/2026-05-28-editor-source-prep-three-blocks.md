# Three-Block Source-Prep Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the AI Editor → «Расшифровки» tab into three versioned, AI-editable document blocks (transcript / brief / essence) whose current text feeds the artifacts pipeline context.

**Architecture:** Generalize the existing transcript-edit machinery with a `kind` discriminator (`transcript` | `brief` | `essence`). One DB table (`TranscriptEdit` + `kind` column), one set of store functions and HTTP endpoints (`/videos/{id}/docs/{kind}/...`) parameterized by kind, one frontend component (`renderDocBlock(kind)`) mounted three times. `expand_spec` sources its context from the current text of the three blocks instead of raw segments + raw brief.

**Tech Stack:** Python 3 · FastAPI · Prisma (prisma-client-py, SQLite) · pytest + pytest-asyncio · httpx · vanilla JS (Alpine-adjacent) frontend.

**Spec:** [docs/superpowers/specs/2026-05-28-editor-source-prep-three-blocks-design.md](../specs/2026-05-28-editor-source-prep-three-blocks-design.md)

**Conventions:**
- Dev Python is `.venv/Scripts/python.exe` (Windows). Run pytest as `.venv/Scripts/python.exe -m pytest`.
- Tests live in `tests/`, run from repo root (`pytest.ini` sets `asyncio_mode`).
- Commit after every green step. Do not skip hooks.

---

## File Structure

- `prisma/schema.prisma` — **modify**: add `kind` column to `TranscriptEdit`, change `@@unique` to `[videoId, kind, version]`.
- `store.py` — **modify**: 4 async edit functions + 4 sync wrappers gain a `kind` param; compound-key lookups become `videoId_kind_version`.
- `transcript_edit.py` — **modify**: `build_edit_prompt` gains a `kind` param (noun substitution); add `seed` system prompt + `build_seed_prompt`.
- `server.py` — **modify**: add `_original_doc_text`, `_pick_current`, `_current_doc_text` helpers; add generalized `/videos/{id}/docs/{kind}/...` endpoints; convert the five `/transcript/...` routes to thin aliases (`kind="transcript"`); refactor `expand_spec` context sourcing.
- `static/index.html` — **modify**: `#editor-pane-transcripts` hosts three block containers + a pipeline-context note.
- `static/editor-workspace.js` — **modify**: collapse the `tx-*` functions into `renderDocBlock(kind)` mounted for transcript/brief/essence; wire the essence "seed" button.
- `static/i18n.js` — **modify**: add labels for the three block titles + the pipeline note (RU/EN).
- `tests/test_doc_edits.py` — **create**: kind isolation, per-kind version numbering, original-per-kind, `_pick_current`.
- `tests/test_transcript_edit.py` — **modify**: kind-aware prompt routing + seed op.
- `tests/test_endpoints.py` — **modify**: `/docs/{kind}` routes registered + transcript aliases still present.

---

## Task 1: Schema — add `kind` discriminator

**Files:**
- Modify: `prisma/schema.prisma:92-107`

- [ ] **Step 1: Edit the model**

In `prisma/schema.prisma`, replace the `TranscriptEdit` model body (lines 92-107) with:

```prisma
model TranscriptEdit {
  id          Int      @id @default(autoincrement())
  videoId     String
  video       Video    @relation(fields: [videoId], references: [id], onDelete: Cascade)
  kind        String   @default("transcript") // "transcript" | "brief" | "essence"
  version     Int // 1..N within (video, kind)
  contentMd   String // full edited document text
  op          String // "clean"|"structure"|"improve"|"expand_idea"|"chat"|"seed"|"rollback"
  instruction String   @default("") // user's ТЗ / chat message
  fromVersion Int? // lineage: which version this grew from (null = from original)
  model       String
  inputChars  Int      @default(0)
  elapsedMs   Int      @default(0)
  createdAt   DateTime @default(now())

  @@unique([videoId, kind, version])
}
```

- [ ] **Step 2: Push schema + regenerate client**

Run:
```bash
.venv/Scripts/python.exe -m prisma db push --accept-data-loss
.venv/Scripts/python.exe -m prisma generate
```
Expected: `db push` prints "Your database is now in sync with your Prisma schema" (the table is empty, so no data loss in practice). `generate` prints "Generated Prisma Client Python".

- [ ] **Step 3: Verify the column is queryable**

Run:
```bash
.venv/Scripts/python.exe -c "from prisma import Prisma; import asyncio; \
async def m():\n d=Prisma(); await d.connect(); \
 print([f for f in d.transcriptedit.create.__doc__ or ''][:0] or 'ok'); await d.disconnect()\nasyncio.run(m())"
```
Expected: prints `ok` with no schema error (client imported against the new schema).

- [ ] **Step 4: Commit**

```bash
git add prisma/schema.prisma
git commit -m "feat(schema): add kind discriminator to TranscriptEdit"
```

---

## Task 2: store.py — generalize edit functions by `kind`

**Files:**
- Modify: `store.py:695-755` (async internals), `store.py:799-825` (sync wrappers)
- Test: `tests/test_doc_edits.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_doc_edits.py`:

```python
"""Versioned doc-edits generalized by `kind` (transcript | brief | essence)."""
import pytest
import store


@pytest.mark.asyncio
async def test_versions_are_isolated_per_kind(db):
    await db.video.create(data={"id": "vid1", "url": "u", "source": "test"})

    t1 = await store._create_transcript_edit(
        video_id="vid1", kind="transcript", content_md="T1", op="improve",
        instruction="", from_version=None, model="m", input_chars=2, elapsed_ms=1,
    )
    b1 = await store._create_transcript_edit(
        video_id="vid1", kind="brief", content_md="B1", op="improve",
        instruction="", from_version=None, model="m", input_chars=2, elapsed_ms=1,
    )
    t2 = await store._create_transcript_edit(
        video_id="vid1", kind="transcript", content_md="T2", op="clean",
        instruction="", from_version=1, model="m", input_chars=2, elapsed_ms=1,
    )

    # Each kind numbers from 1 independently.
    assert (t1.version, t2.version) == (1, 2)
    assert b1.version == 1

    tx = await store._list_transcript_edits("vid1", kind="transcript")
    assert [r.version for r in tx] == [2, 1]
    br = await store._list_transcript_edits("vid1", kind="brief")
    assert [r.contentMd for r in br] == ["B1"]


@pytest.mark.asyncio
async def test_rollback_is_per_kind(db):
    await db.video.create(data={"id": "vid2", "url": "u", "source": "test"})
    await store._create_transcript_edit(
        video_id="vid2", kind="essence", content_md="E1", op="seed",
        instruction="", from_version=None, model="m", input_chars=2, elapsed_ms=1,
    )
    await store._create_transcript_edit(
        video_id="vid2", kind="essence", content_md="E2", op="chat",
        instruction="", from_version=1, model="m", input_chars=2, elapsed_ms=1,
    )
    v3 = await store._rollback_transcript_edit("vid2", kind="essence", version=1)
    assert v3.version == 3
    assert v3.contentMd == "E1"
    assert v3.op == "rollback"
    assert v3.fromVersion == 1
    assert v3.kind == "essence"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_doc_edits.py -v`
Expected: FAIL — `_create_transcript_edit() got an unexpected keyword argument 'kind'`.

- [ ] **Step 3: Generalize the async internals**

In `store.py`, replace `_create_transcript_edit` (lines 695-713) so it takes `kind` and scopes the version counter:

```python
async def _create_transcript_edit(
    *, video_id: str, content_md: str, op: str, instruction: str,
    from_version: int | None, model: str, input_chars: int, elapsed_ms: int,
    kind: str = "transcript",
):
    db = Prisma()
    await db.connect()
    try:
        last = await db.transcriptedit.find_first(
            where={"videoId": video_id, "kind": kind}, order={"version": "desc"},
        )
        version = (last.version + 1) if last else 1
        return await db.transcriptedit.create(data={
            "videoId": video_id, "kind": kind, "version": version, "contentMd": content_md,
            "op": op, "instruction": instruction, "fromVersion": from_version,
            "model": model, "inputChars": input_chars, "elapsedMs": elapsed_ms,
        })
    finally:
        await db.disconnect()
```

Replace `_list_transcript_edits` (lines 715-723):

```python
async def _list_transcript_edits(video_id: str, kind: str = "transcript"):
    db = Prisma()
    await db.connect()
    try:
        return await db.transcriptedit.find_many(
            where={"videoId": video_id, "kind": kind}, order={"version": "desc"},
        )
    finally:
        await db.disconnect()
```

Replace `_get_transcript_edit` (lines 726-734):

```python
async def _get_transcript_edit(video_id: str, version: int, kind: str = "transcript"):
    db = Prisma()
    await db.connect()
    try:
        return await db.transcriptedit.find_unique(
            where={"videoId_kind_version": {
                "videoId": video_id, "kind": kind, "version": version}},
        )
    finally:
        await db.disconnect()
```

Replace `_rollback_transcript_edit` (lines 737-755) — scope both lookups by kind:

```python
async def _rollback_transcript_edit(video_id: str, version: int, kind: str = "transcript"):
    db = Prisma()
    await db.connect()
    try:
        src = await db.transcriptedit.find_unique(
            where={"videoId_kind_version": {
                "videoId": video_id, "kind": kind, "version": version}},
        )
        if not src:
            return None
        last = await db.transcriptedit.find_first(
            where={"videoId": video_id, "kind": kind}, order={"version": "desc"},
        )
        new_version = (last.version + 1) if last else 1
        return await db.transcriptedit.create(data={
            "videoId": video_id, "kind": kind, "version": new_version,
            "contentMd": src.contentMd, "op": "rollback", "instruction": "",
            "fromVersion": version, "model": src.model,
            "inputChars": src.inputChars, "elapsedMs": 0,
        })
    finally:
        await db.disconnect()
```

> NOTE: keep any trailing lines of the original `_rollback_transcript_edit` body in sync; the block above is the full function.

- [ ] **Step 4: Update the sync wrappers**

In `store.py`, replace the four sync wrappers (lines 815-825 for list/get/rollback; `create` at 799-800):

```python
def create_transcript_edit(**kwargs):
    return asyncio.run(_create_transcript_edit(**kwargs))
```
(unchanged — already passes `**kwargs`, so `kind=` flows through)

```python
def list_transcript_edits(video_id: str, kind: str = "transcript"):
    return asyncio.run(_list_transcript_edits(video_id, kind=kind))


def get_transcript_edit(video_id: str, version: int, kind: str = "transcript"):
    return asyncio.run(_get_transcript_edit(video_id, version, kind=kind))


def rollback_transcript_edit(video_id: str, version: int, kind: str = "transcript"):
    return asyncio.run(_rollback_transcript_edit(video_id, version, kind=kind))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_doc_edits.py tests/test_transcript_edit.py -v`
Expected: PASS (new kind tests pass; the existing `test_version_numbering_and_rollback` still passes because `kind` defaults to `"transcript"`).

- [ ] **Step 6: Commit**

```bash
git add store.py tests/test_doc_edits.py
git commit -m "feat(store): parameterize transcript-edit functions by kind"
```

---

## Task 3: transcript_edit.py — kind-aware prompts + `seed` op

**Files:**
- Modify: `transcript_edit.py:14-77`
- Test: `tests/test_transcript_edit.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_transcript_edit.py`:

```python
def test_build_edit_prompt_substitutes_noun_by_kind():
    _s, u_t = te.build_edit_prompt(op="clean", current_text="x", instruction="", kind="transcript")
    _s, u_b = te.build_edit_prompt(op="clean", current_text="x", instruction="", kind="brief")
    assert "расшифров" in u_t.lower()
    assert "бриф" in u_b.lower()


def test_seed_prompt_registered_and_builds_from_sources():
    assert "seed" in te.SYSTEM_PROMPTS
    system, user = te.build_seed_prompt(
        transcript_text="Берём цену на бирже A и B.",
        brief_md="## Суть\nАрбитраж между биржами.",
    )
    assert system == te.SYSTEM_PROMPTS["seed"]
    assert "бирже A" in user            # transcript fed in
    assert "Арбитраж между биржами" in user  # brief fed in
    assert "сут" in system.lower()      # essence-oriented
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_transcript_edit.py -k "noun or seed" -v`
Expected: FAIL — `build_edit_prompt() got an unexpected keyword argument 'kind'` and `module 'transcript_edit' has no attribute 'build_seed_prompt'`.

- [ ] **Step 3: Add the kind noun map + seed prompt**

In `transcript_edit.py`, after the `SYSTEM_PROMPTS` dict (after line 62) add:

```python
SYSTEM_PROMPTS["seed"] = (
    "Ты — редактор. Режим СУТЬ. На входе — расшифровка видео и его бриф."
    " Сформулируй ядро материала: о чём он, ключевые тезисы, главный вывод.\n\n"
    "ТРЕБОВАНИЯ: 3–5 предложений или короткий список; только то, что есть в источниках;"
    " без воды, без выдуманных фактов. Верни ТОЛЬКО текст сути в markdown, без преамбул."
)

# Document-kind → noun used inside the per-op prompts/user message.
_KIND_NOUN = {"transcript": "расшифровки", "brief": "брифа", "essence": "сути"}
```

- [ ] **Step 4: Add `kind` to build_edit_prompt + add build_seed_prompt**

Replace `build_edit_prompt` (lines 65-77) with:

```python
def build_edit_prompt(*, op: str, current_text: str, instruction: str,
                      kind: str = "transcript") -> tuple[str, str]:
    """Returns (system_prompt, user_prompt). Unknown ops fall back to 'improve'."""
    system = SYSTEM_PROMPTS.get(op, SYSTEM_PROMPTS["improve"])
    noun = _KIND_NOUN.get(kind, "расшифровки")
    instr = (instruction or "").strip()
    if op == "chat":
        tail = f"\n\n## Инструкция пользователя\n{instr}" if instr else ""
    else:
        tail = f"\n\n## Дополнительная инструкция\n{instr}" if instr else ""
    user = (
        f"Вот текущий текст {noun}.\n\n"
        f"--- ТЕКСТ ---\n{current_text}{tail}"
    )
    return system, user


def build_seed_prompt(*, transcript_text: str, brief_md: str) -> tuple[str, str]:
    """Essence v1: derive a short core from transcript + brief."""
    system = SYSTEM_PROMPTS["seed"]
    user = (
        "Источники для сути.\n\n"
        f"--- РАСШИФРОВКА ---\n{transcript_text}\n\n"
        f"--- БРИФ ---\n{brief_md}"
    )
    return system, user
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_transcript_edit.py -v`
Expected: PASS (all existing prompt tests + the two new ones).

- [ ] **Step 6: Commit**

```bash
git add transcript_edit.py tests/test_transcript_edit.py
git commit -m "feat(transcript_edit): kind-aware prompts + essence seed op"
```

---

## Task 4: server.py — original/current doc-text helpers (pure where possible)

**Files:**
- Modify: `server.py:718-737` (helpers area)
- Test: `tests/test_doc_edits.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_doc_edits.py`:

```python
import server


def _row(version, content):
    # Minimal stand-in for a TranscriptEdit row (newest-first lists).
    from types import SimpleNamespace
    return SimpleNamespace(version=version, contentMd=content)


def test_pick_current_prefers_latest_edit_else_original():
    assert server._pick_current("ORIG", []) == "ORIG"
    assert server._pick_current("ORIG", [_row(2, "V2"), _row(1, "V1")]) == "V2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_doc_edits.py::test_pick_current_prefers_latest_edit_else_original -v`
Expected: FAIL — `module 'server' has no attribute '_pick_current'`.

- [ ] **Step 3: Add the helpers**

In `server.py`, immediately after `_edit_to_dict` (after line 736) add:

```python
_DOC_KINDS = ("transcript", "brief", "essence")


def _pick_current(original_md: str, edits: list) -> str:
    """Latest edited text if any (edits are newest-first), else the original."""
    return edits[0].contentMd if edits else original_md


def _original_doc_text(video_id: str, kind: str) -> tuple[str, bool]:
    """(original text for this kind, has_original). Raises 404 for unknown video."""
    v = get_video(video_id, with_segments=True)
    if not v:
        raise HTTPException(status_code=404, detail=f"Video {video_id} not found")
    if kind == "transcript":
        text = "\n".join(s.text for s in (v.segments or []) if s.text)
        return text, bool(text)
    if kind == "brief":
        text = v.briefs[-1].contentMd if v.briefs else ""
        return text, bool(text)
    if kind == "essence":
        return "", False  # no source of truth; v1 comes from `seed`
    raise HTTPException(status_code=422, detail=f"Unknown doc kind: {kind}")


def _current_doc_text(video_id: str, kind: str) -> str:
    """Current working text for a kind = latest edit, else original."""
    original, _has = _original_doc_text(video_id, kind)
    return _pick_current(original, list_transcript_edits(video_id, kind=kind))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_doc_edits.py::test_pick_current_prefers_latest_edit_else_original -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_doc_edits.py
git commit -m "feat(server): per-kind original/current doc-text helpers"
```

---

## Task 5: server.py — generalized `/docs/{kind}/...` endpoints + transcript aliases

**Files:**
- Modify: `server.py:739-862` (transcript endpoints block)
- Test: `tests/test_endpoints.py`, `tests/test_doc_edits.py`

- [ ] **Step 1: Write the failing route + behavior tests**

Append to `tests/test_endpoints.py`:

```python
def test_docs_routes_registered():
    paths = {r.path for r in app.routes}
    assert "/videos/{video_id}/docs/{kind}" in paths
    assert "/videos/{video_id}/docs/{kind}/edits" in paths
    assert "/videos/{video_id}/docs/{kind}/edit" in paths
    assert "/videos/{video_id}/docs/{kind}/edits/apply" in paths
    # legacy transcript aliases must still exist
    assert "/videos/{video_id}/transcript" in paths
    assert "/videos/{video_id}/transcript/edits/apply" in paths
```

Append to `tests/test_doc_edits.py`:

```python
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_get_docs_reports_original_per_kind(db):
    v = await db.video.create(data={"id": "vid3", "url": "u", "source": "test"})
    await db.segment.create(data={"videoId": v.id, "index": 0, "start": 0, "end": 1, "text": "hello"})
    await db.brief.create(data={
        "videoId": v.id, "model": "m", "language": "ru", "format": "markdown",
        "contentMd": "BRIEF BODY", "inputTokens": 0, "outputTokens": 0,
    })
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        rt = (await ac.get(f"/videos/{v.id}/docs/transcript")).json()
        rb = (await ac.get(f"/videos/{v.id}/docs/brief")).json()
        re = (await ac.get(f"/videos/{v.id}/docs/essence")).json()
        bad = await ac.get(f"/videos/{v.id}/docs/bogus")
    assert rt["has_original"] and rt["original_md"] == "hello"
    assert rb["has_original"] and rb["original_md"] == "BRIEF BODY"
    assert re["has_original"] is False and re["original_md"] == ""
    assert bad.status_code == 422
```

Add the import at the top of `tests/test_doc_edits.py`:
```python
from server import app
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_endpoints.py::test_docs_routes_registered tests/test_doc_edits.py::test_get_docs_reports_original_per_kind -v`
Expected: FAIL — routes 404 / not in `app.routes`.

- [ ] **Step 3: Add generalized endpoints**

In `server.py`, **after** the existing transcript block (after line 862) add the generalized routes. Note: `.md`/`.pdf` are registered BEFORE `{version}` so the path resolver doesn't treat `5.pdf` as a version int.

```python
# ─── Generalized doc editor (transcript | brief | essence) ─────────

def _require_kind(kind: str) -> str:
    if kind not in _DOC_KINDS:
        raise HTTPException(status_code=422, detail=f"Unknown doc kind: {kind}")
    return kind


def _doc_title(kind: str) -> str:
    return {"transcript": "Расшифровка", "brief": "Бриф", "essence": "Суть"}[kind]


@app.get("/videos/{video_id}/docs/{kind}")
def read_doc(video_id: str, kind: str) -> dict:
    _require_kind(kind)
    original, has_original = _original_doc_text(video_id, kind)
    edits = list_transcript_edits(video_id, kind=kind)
    return {
        "video_id": video_id, "kind": kind,
        "original_md": original, "original_chars": len(original),
        "has_original": has_original,
        "versions": len(edits),
        "latest_version": edits[0].version if edits else 0,
    }


@app.get("/videos/{video_id}/docs/{kind}/edits")
def read_doc_edits(video_id: str, kind: str) -> list[dict]:
    _require_kind(kind)
    return [_edit_to_dict(e) for e in list_transcript_edits(video_id, kind=kind)]


@app.get("/videos/{video_id}/docs/{kind}/edits/{version}.pdf")
def export_doc_edit_pdf(video_id: str, kind: str, version: int):
    _require_kind(kind)
    from export import markdown_to_pdf
    e = get_transcript_edit(video_id, version, kind=kind)
    if not e:
        raise HTTPException(status_code=404, detail=f"No v{version} for {video_id}/{kind}")
    pdf_bytes = markdown_to_pdf(e.contentMd, title=f"{_doc_title(kind)} v{version}")
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{kind}-v{version}-{video_id}.pdf"'},
    )


@app.get("/videos/{video_id}/docs/{kind}/edits/{version}.md")
def export_doc_edit_md(video_id: str, kind: str, version: int):
    _require_kind(kind)
    e = get_transcript_edit(video_id, version, kind=kind)
    if not e:
        raise HTTPException(status_code=404, detail=f"No v{version} for {video_id}/{kind}")
    return Response(
        content=e.contentMd, media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{kind}-v{version}-{video_id}.md"'},
    )


@app.get("/videos/{video_id}/docs/{kind}/edits/{version}")
def read_doc_edit(video_id: str, kind: str, version: int) -> dict:
    _require_kind(kind)
    e = get_transcript_edit(video_id, version, kind=kind)
    if not e:
        raise HTTPException(status_code=404, detail=f"No v{version} for {video_id}/{kind}")
    return _edit_to_dict(e)


class DocEditRequest(BaseModel):
    op: Literal["improve", "structure", "clean", "chat", "expand_idea", "seed"] = "improve"
    instruction: str = ""
    model: str | None = None
    base_version: int | None = None


@app.post("/videos/{video_id}/docs/{kind}/edit")
def doc_edit_preview(video_id: str, kind: str, req: DocEditRequest):
    """Stream a proposed new full text (SSE). Does NOT persist."""
    import transcript_edit

    _require_kind(kind)
    settings = get_all_settings()
    model = (req.model or settings.get("editor_transcript_model") or "claude-sonnet-4-6").strip()
    num_ctx = int(settings.get("local_llm_num_ctx") or 32768)
    temperature = float(settings.get("local_llm_temperature") or 0.3)

    if kind == "essence" and req.op == "seed":
        tx = _current_doc_text(video_id, "transcript")
        br = _current_doc_text(video_id, "brief")
        if not (tx or br).strip():
            raise HTTPException(status_code=400, detail="Нет исходного текста для сути")
        system, user = transcript_edit.build_seed_prompt(transcript_text=tx, brief_md=br)
    else:
        if req.base_version:
            base = get_transcript_edit(video_id, req.base_version, kind=kind)
            if not base:
                raise HTTPException(status_code=404, detail=f"No v{req.base_version}")
            current = base.contentMd
        else:
            current = _current_doc_text(video_id, kind)
        system, user = transcript_edit.build_edit_prompt(
            op=req.op, current_text=current, instruction=req.instruction, kind=kind)

    def event_stream():
        import time
        started = time.monotonic()
        try:
            yield f"data: {json.dumps({'type': 'meta', 'model': model, 'op': req.op, 'kind': kind, 'current_chars': len(user)}, ensure_ascii=False)}\n\n"
            if transcript_edit._is_claude(model):
                pieces = transcript_edit._stream_claude(system, user, model)
            else:
                pieces = local_llm.stream_chat(system=system, user=user, model=model,
                                                num_ctx=num_ctx, temperature=temperature)
            for piece in pieces:
                yield f"data: {json.dumps({'type': 'delta', 'text': piece}, ensure_ascii=False)}\n\n"
            elapsed_ms = int((time.monotonic() - started) * 1000)
            yield f"data: {json.dumps({'type': 'done', 'model': model, 'op': req.op, 'elapsed_ms': elapsed_ms}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'msg': f'{type(e).__name__}: {e}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


class DocApplyRequest(BaseModel):
    op: str
    instruction: str = ""
    content_md: str
    model: str = ""
    from_version: int | None = None
    elapsed_ms: int = 0


@app.post("/videos/{video_id}/docs/{kind}/edits/apply")
def doc_edit_apply(video_id: str, kind: str, req: DocApplyRequest) -> dict:
    _require_kind(kind)
    if not (req.content_md or "").strip():
        raise HTTPException(status_code=400, detail="Пустой текст — нечего сохранять")
    row = create_transcript_edit(
        video_id=video_id, kind=kind, content_md=req.content_md, op=req.op,
        instruction=req.instruction, from_version=req.from_version,
        model=req.model or "claude-sonnet-4-6",
        input_chars=len(req.content_md), elapsed_ms=req.elapsed_ms,
    )
    return _edit_to_dict(row)


@app.post("/videos/{video_id}/docs/{kind}/edits/{version}/rollback")
def doc_edit_rollback(video_id: str, kind: str, version: int) -> dict:
    _require_kind(kind)
    row = rollback_transcript_edit(video_id, version, kind=kind)
    if not row:
        raise HTTPException(status_code=404, detail=f"No v{version} for {video_id}/{kind}")
    return _edit_to_dict(row)
```

- [ ] **Step 4: Make `_edit_to_dict` include `kind`**

In `server.py`, update `_edit_to_dict` (lines 729-736) to add the kind field (used by the frontend version list):

```python
def _edit_to_dict(e) -> dict:
    return {
        "id": e.id, "video_id": e.videoId, "kind": e.kind, "version": e.version,
        "content_md": e.contentMd, "op": e.op, "instruction": e.instruction,
        "from_version": e.fromVersion, "model": e.model,
        "input_chars": e.inputChars, "elapsed_ms": e.elapsedMs,
        "created_at": e.createdAt.isoformat(),
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_endpoints.py tests/test_doc_edits.py -v`
Expected: PASS (routes registered; per-kind original reporting correct; 422 on bogus kind). The legacy `/transcript/...` routes are untouched and still pass `test_transcript_routes_registered`.

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_endpoints.py tests/test_doc_edits.py
git commit -m "feat(server): generalized /docs/{kind} editor endpoints"
```

---

## Task 6: server.py — `expand_spec` sources context from current doc text

**Files:**
- Modify: `server.py:571-590` (context assembly inside `expand_spec`)
- Test: `tests/test_doc_edits.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_doc_edits.py`:

```python
def test_assemble_essence_section_included_when_present():
    # Pure helper: builds the optional "## Суть" block fed into expand prompts.
    assert server._essence_section("") == ""
    block = server._essence_section("ядро материала")
    assert "Суть" in block and "ядро материала" in block
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_doc_edits.py::test_assemble_essence_section_included_when_present -v`
Expected: FAIL — `module 'server' has no attribute '_essence_section'`.

- [ ] **Step 3: Add the pure helper + wire into expand_spec**

In `server.py`, near the other doc helpers (after `_current_doc_text`) add:

```python
def _essence_section(essence_md: str) -> str:
    """Optional '## Суть' block prepended to the expand user-prompt source."""
    essence_md = (essence_md or "").strip()
    return f"## Суть\n{essence_md}\n\n" if essence_md else ""
```

Then in `expand_spec`, replace the transcript/brief sourcing (lines 571-573 and the `full_brief_md=` argument at line 587). Change:

```python
    transcript_excerpt = ""
    if use_transcript and video.segments:
        transcript_excerpt = "\n".join(s.text for s in video.segments if s.text)
        local_llm.MAX_TRANSCRIPT_CHARS = max_tx_chars
```
to:
```python
    # Curated source: prefer the edited blocks over raw segments / raw brief.
    transcript_excerpt = ""
    if use_transcript:
        transcript_excerpt = _current_doc_text(video_id, "transcript")
        local_llm.MAX_TRANSCRIPT_CHARS = max_tx_chars
    curated_brief = _current_doc_text(video_id, "brief") if use_brief else ""
    essence_block = _essence_section(_current_doc_text(video_id, "essence"))
```

And change the `build_expand_prompt(...)` call (line 587) argument
`full_brief_md=(latest.contentMd or "") if use_brief else "",`
to:
```python
        full_brief_md=(essence_block + curated_brief) if use_brief else essence_block,
```

> The `software_brief_json` (`sb_json`) path is unchanged; the essence block rides in the markdown brief so it reaches the prompt even when structured JSON exists.

- [ ] **Step 4: Run test + full backend suite**

Run: `.venv/Scripts/python.exe -m pytest tests/ -v`
Expected: PASS across the suite (new helper test + all prior tests). Pre-existing tests that don't touch this path are unaffected.

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_doc_edits.py
git commit -m "feat(server): expand_spec context sourced from curated doc blocks"
```

---

## Task 7: Frontend — index.html three-block scaffold + i18n

**Files:**
- Modify: `static/index.html` (`#editor-pane-transcripts` container)
- Modify: `static/i18n.js`

> No JS unit-test harness exists in this repo; the frontend is verified live in Task 9.

- [ ] **Step 1: Locate the transcripts pane**

Run: `grep -n "editor-pane-transcripts" static/index.html`
Expected: one match — the container currently holding the single transcript workspace (`tx-*` ids).

- [ ] **Step 2: Replace the pane's right side with three block hosts**

In `static/index.html`, inside `#editor-pane-transcripts`, keep the left video list (`tx-videos-list`, `tx-videos-meta`) and replace the single right-hand work area (`tx-item` / `tx-empty`) with three mount points plus the pipeline note:

```html
<div id="tx-empty"><em>Выбери видео слева.</em></div>
<div id="tx-item" style="display:none;">
  <div id="doc-block-transcript" class="doc-block"></div>
  <div id="doc-block-brief" class="doc-block"></div>
  <div id="doc-block-essence" class="doc-block"></div>
  <div class="doc-pipeline-note" data-i18n="editor.docs.pipelineNote">
    ▸ Этот набор → вход для ТЗ / Алгоритмов / AI-скиллов
  </div>
</div>
```

- [ ] **Step 3: Add i18n labels**

In `static/i18n.js`, add to both the RU and EN dictionaries (match existing nesting/style):

```js
// RU
"editor.docs.transcript": "Расшифровка",
"editor.docs.brief": "Бриф",
"editor.docs.essence": "Суть",
"editor.docs.seed": "сгенерировать суть",
"editor.docs.pipelineNote": "▸ Этот набор → вход для ТЗ / Алгоритмов / AI-скиллов",
```
```js
// EN
"editor.docs.transcript": "Transcript",
"editor.docs.brief": "Brief",
"editor.docs.essence": "Essence",
"editor.docs.seed": "generate essence",
"editor.docs.pipelineNote": "▸ This set → input for Spec / Algorithms / AI-skills",
```

- [ ] **Step 4: Commit**

```bash
git add static/index.html static/i18n.js
git commit -m "feat(ui): three-block scaffold + i18n for docs editor"
```

---

## Task 8: Frontend — `renderDocBlock(kind)` ×3 in editor-workspace.js

**Files:**
- Modify: `static/editor-workspace.js:334-551` (the `tx-*` Transcripts-mode block)

> Verified live in Task 9.

- [ ] **Step 1: Replace the tx-* block with a kind-parameterized component**

In `static/editor-workspace.js`, replace the Transcripts-mode section (from `// ── Transcripts mode ──` at line 334 through `applyTranscriptEdit` end at line 551) with a single component instantiated per kind. Key points the implementation must hit:

- `state.docs = { transcript: {...}, brief: {...}, essence: {...} }` holding per-kind `{original, hasOriginal, versions, showing}`.
- `selectTranscriptVideo(id)` loads all three: for each kind `GET /videos/{id}/docs/{kind}` + `GET /videos/{id}/docs/{kind}/edits`, then `renderDocBlock(kind)` into `#doc-block-${kind}`.
- `renderDocBlock(kind)` renders the same controls the transcript block had — `[улучшенный|оригинал]` toggle, text area, the 4 buttons (`data-op` = `clean|structure|improve|expand_idea`), ТЗ input, model `<select>` (populated from `/local-llm/models`, Claude default first), chat input + `→`, version list with откатить / `.md` / `.pdf` — but all ids namespaced by kind (e.g. `doc-${kind}-text`, `doc-${kind}-preview`).
- For `kind === "essence"` with zero versions and empty original: show a single **«сгенерировать суть»** button (label via `t("editor.docs.seed")`) that calls the edit endpoint with `op:"seed"`; after apply, re-render shows the normal controls.
- `runDocEdit(kind, op, instruction)` → `POST /videos/{id}/docs/${kind}/edit` (SSE; identical stream parsing to the old `runTranscriptEdit`: `delta`/`error` frames) → preview with `применить / показать дифф / отмена`.
- `applyDocEdit(kind, payload)` → `POST /videos/{id}/docs/${kind}/edits/apply` → reload that kind's versions + show current.
- `rollbackDoc(kind, version)` → `POST /videos/{id}/docs/${kind}/edits/${version}/rollback`.
- Reuse the existing `lineDiff` / `renderDiffHtml` helpers unchanged.

Implementation note: this is a mechanical generalization of the existing `tx-*` functions — copy each one, add a `kind` parameter, swap the hard-coded `/transcript/` paths for `/docs/${kind}/`, and namespace the element ids. Keep `window.editorSelectVideo` working (it calls `setSource('transcripts')` then `selectTranscriptVideo(id)`).

- [ ] **Step 2: Keep the deep-link + source toggle wiring intact**

Confirm `setSource('transcripts')` still calls `loadTranscriptVideos()` and that `selectTranscriptVideo` is the entry that now hydrates all three blocks. The News and Artifacts modes are untouched.

- [ ] **Step 3: Syntax check**

Run: `node --check static/editor-workspace.js`
Expected: no output (valid JS). If `node` is unavailable, open the app (Task 9) and confirm no console parse error.

- [ ] **Step 4: Commit**

```bash
git add static/editor-workspace.js
git commit -m "feat(ui): renderDocBlock(kind) — transcript/brief/essence blocks"
```

---

## Task 9: Live verification (verify skill)

**Files:** none (runtime observation).

- [ ] **Step 1: Start the server**

Run: `.venv/Scripts/python.exe -m uvicorn server:app --port 8000` (background). Confirm `GET /health` → 200.

- [ ] **Step 2: Drive the three blocks in a browser**

Open `http://127.0.0.1:8000/#editor`, source = Расшифровки, pick the «Арбитражный» video. Verify three blocks render: transcript (has text), brief (has text), essence (shows «сгенерировать суть»). Capture a screenshot.

- [ ] **Step 3: Exercise each surface**

- Brief block → «структурировать» → preview streams → «применить» → version list shows v1; reload page → v1 persists.
- Essence block → «сгенерировать суть» → preview streams a 3–5 sentence essence → «применить» → v1.
- Transcript block → «откатить» a version → new clone version appears.
- Probe: `POST /videos/{id}/docs/bogus/edit` via curl → 422; essence seed on a video with no brief AND no transcript → 400.

- [ ] **Step 4: Verify pipeline wiring**

On the Артефакты tab for the same video, generate «ТЗ». Confirm the run completes and (spot-check the prompt via a temporary log or the resulting text) that the essence/edited brief influenced the output. Capture evidence.

- [ ] **Step 5: Final report**

Produce a PASS/FAIL verdict per the verify skill with screenshots + the probe results.

---

## Self-Review

**Spec coverage:**
- §4.2 schema `kind` → Task 1. ✅
- §4.3 prompts by kind + seed → Task 3. ✅
- §4.4 store kind param + current_doc_text → Tasks 2, 4. ✅
- §4.5 generalized `/docs/{kind}` endpoints + transcript aliases → Task 5. ✅
- §4.6 expand_spec context from curated blocks → Task 6. ✅
- §4.7 renderDocBlock ×3 + layout → Tasks 7, 8. ✅
- §5 data flow (seed → v1, edit→apply→version, rollback) → Tasks 5, 8, 9. ✅
- §6 error handling (422 kind, 400 empty seed, 404 version, no-brief) → Tasks 5, 9. ✅
- §7 testing → Tasks 2–6 (unit) + Task 9 (live). ✅
- §8 i18n → Task 7. ✅

**Placeholder scan:** No TBD/TODO; every code step shows full code. Task 8 is a mechanical generalization described by exact endpoints + id-naming rules rather than a full re-paste of ~220 lines — acceptable because each transformation is concrete (swap `/transcript/`→`/docs/${kind}/`, add `kind` param, namespace ids) and the source functions are cited by line range.

**Type consistency:** `kind` param defaults to `"transcript"` everywhere (store async + sync, prompt builder, endpoints) so legacy callers/tests keep working. Compound key `videoId_kind_version` used consistently in `_get_*` and `_rollback_*`. `_edit_to_dict` now emits `kind`; `DocEditRequest`/`DocApplyRequest` mirror the legacy `TranscriptEditRequest`/`TranscriptApplyRequest` fields. `_pick_current` / `_original_doc_text` / `_current_doc_text` / `_essence_section` names match across tasks 4–6.
