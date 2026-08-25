# Layer 3 — Guided Artifact Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Артефакты mode's 6 independent expand modes into a guided pipeline: auto-передача (output N → input N+1 by dependency graph), hybrid readiness checklists (Claude assessment + manual override), soft out-of-order warnings, and static backward-loop hints.

**Architecture:** A new pure module `pipeline.py` holds the stage order, the передача dependency graph, the per-stage checklists, and the Claude-based assessment (prompt + JSON parsing). `local_llm.build_expand_prompt` gains an `upstream` param so the durable `expand_spec` endpoint can feed predecessor artifacts into the prompt. A new `StageGate` table persists checklist state (AI-assessed + user overrides). The frontend adds a stepper + checklist panel + warning banner to the Артефакты pane.

**Tech Stack:** Python 3.13, FastAPI, Prisma (SQLite), Anthropic (JSON output), Alpine.js + vanilla JS, pytest.

**Spec:** `docs/superpowers/specs/2026-05-27-pipeline-gates-layer3-design.md`

---

## File Structure

- **Create** `pipeline.py` — `STAGE_ORDER`, `UPSTREAM`, `GATE_PREDECESSOR`, `CHECKLISTS`, `build_assess_prompt`, `parse_assessment`, `assess_checklist`.
- **Modify** `local_llm.py` — `_format_context` + `build_expand_prompt` gain `upstream`.
- **Modify** `prisma/schema.prisma` — `StageGate` model + `Video.stageGates`.
- **Modify** `store.py` — `upsert_stage_gate` / `list_stage_gates` / `get_stage_gate`.
- **Modify** `server.py` — передача in `expand_spec`; `GET /stage-gates`, `PUT /stage-gates/{stage}`, `POST /stage-assess/{stage}`.
- **Modify** `static/index.html` + `static/editor-workspace.js` — stepper, checklist panel, warning, hints.
- **Create** `tests/test_pipeline.py`; **Modify** `tests/test_endpoints.py`.

---

## Task 1: `pipeline.py` — graph, checklists, assessment

**Files:**
- Create: `pipeline.py`
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_pipeline.py`:

```python
import pipeline


def test_stage_order_and_upstream_graph():
    assert pipeline.STAGE_ORDER == ["research", "report", "spec", "uiux", "ai_algorithms", "ai_skills"]
    assert pipeline.UPSTREAM["report"] == ["research"]
    assert pipeline.UPSTREAM["spec"] == ["report"]
    assert pipeline.UPSTREAM["uiux"] == ["spec"]
    assert pipeline.UPSTREAM["ai_algorithms"] == ["spec", "uiux"]
    assert pipeline.UPSTREAM["ai_skills"] == ["spec", "ai_algorithms"]
    assert pipeline.UPSTREAM["research"] == []


def test_gate_predecessor_chain_excludes_optional_uiux():
    assert pipeline.GATE_PREDECESSOR["report"] == "research"
    assert pipeline.GATE_PREDECESSOR["spec"] == "report"
    assert pipeline.GATE_PREDECESSOR["ai_algorithms"] == "spec"   # uiux skipped (optional)
    assert pipeline.GATE_PREDECESSOR["ai_skills"] == "ai_algorithms"
    assert "uiux" not in pipeline.GATE_PREDECESSOR


def test_checklists_present_for_gated_stages_only():
    assert set(pipeline.CHECKLISTS) == {"research", "report", "spec", "ai_algorithms"}
    assert len(pipeline.CHECKLISTS["research"]) == 5
    assert all(len(item) == 2 for item in pipeline.CHECKLISTS["research"])  # (key, label)


def test_build_assess_prompt_includes_artifact_and_keys():
    system, user = pipeline.build_assess_prompt("research", "Вот ресерч: варианты A и B.")
    assert "ресерч" in system.lower() or "research" in system.lower()
    assert "Вот ресерч" in user
    for key, _label in pipeline.CHECKLISTS["research"]:
        assert key in user


def test_parse_assessment_maps_to_items_with_labels():
    raw = {
        "domain": {"checked": True, "note": "есть"},
        "options": {"checked": False, "note": "только 1"},
    }
    items = pipeline.parse_assessment("research", raw)
    by_key = {i["key"]: i for i in items}
    assert by_key["domain"]["checked"] is True
    assert by_key["options"]["checked"] is False
    assert by_key["options"]["note"] == "только 1"
    # every checklist item is represented, missing keys default to unchecked
    assert {i["key"] for i in items} == {k for k, _ in pipeline.CHECKLISTS["research"]}
    assert by_key["limits"]["checked"] is False
    assert by_key["domain"]["label"]  # label carried through
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pipeline.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline'`.

- [ ] **Step 3: Write `pipeline.py`**

Create `pipeline.py`:

```python
"""Layer 3 — guided artifact pipeline: stage graph, checklists, AI assessment.

Pure data + prompt building here; the Claude call (assess_checklist) is the only
network part. Mirrors brief.py's JSON-output approach for structured assessment.
"""
from __future__ import annotations

import json

import brief  # resolve_model

# Canonical pipeline order (matches docs/task-flow-v2.md).
STAGE_ORDER = ["research", "report", "spec", "uiux", "ai_algorithms", "ai_skills"]

# Передача: which predecessors' outputs feed a stage's generation as context.
# Doc cycle (UI/UX<->algorithms) resolved as: uiux<-spec, ai_algorithms<-spec+uiux.
UPSTREAM: dict[str, list[str]] = {
    "research": [],
    "report": ["research"],
    "spec": ["report"],
    "uiux": ["spec"],
    "ai_algorithms": ["spec", "uiux"],
    "ai_skills": ["spec", "ai_algorithms"],
}

# Soft-gate chain (required stages only; uiux is an optional side branch).
# Generating a stage warns if its GATE_PREDECESSOR's checklist isn't fully checked.
GATE_PREDECESSOR: dict[str, str] = {
    "report": "research",
    "spec": "report",
    "ai_algorithms": "spec",
    "ai_skills": "ai_algorithms",
}

# Readiness checklists (docs/task-flow-v2.md §3), keyed by the stage being assessed.
CHECKLISTS: dict[str, list[tuple[str, str]]] = {
    "research": [
        ("domain", "Описана предметная область задачи"),
        ("options", "Перечислены варианты решения (минимум 2)"),
        ("limits", "Указаны ограничения и риски"),
        ("open_q", "Сформулированы открытые вопросы"),
        ("sources", "Указаны источники или наблюдения"),
    ],
    "report": [
        ("verdict", "Сформулирован чёткий вывод (что рекомендуется)"),
        ("justified", "Обоснован выбор варианта"),
        ("next", "Указаны следующие действия"),
        ("accepted", "Решение согласовано / принято к разработке"),
    ],
    "spec": [
        ("goal_user", "Определены цель и пользователь"),
        ("func", "Перечислены все функциональные требования"),
        ("scenarios", "Указаны сценарии использования"),
        ("acceptance", "Определены критерии приёмки"),
    ],
    "ai_algorithms": [
        ("single", "Алгоритм описывает одно конкретное действие (не смешанное)"),
        ("repeat", "Действие будет повторяться 3+ раза"),
        ("inputs", "Входные данные чётко определены"),
        ("output", "Формат результата зафиксирован"),
    ],
}

# (Backward-loop hints are display-only and live in the frontend — see AF_HINT in
# editor-workspace.js, Task 6 — so they are not duplicated here.)


def build_assess_prompt(stage: str, artifact_md: str) -> tuple[str, str]:
    """(system, user) for assessing one stage's artifact against its checklist."""
    items = CHECKLISTS.get(stage, [])
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


def parse_assessment(stage: str, raw: dict) -> list[dict]:
    """Map a raw {key:{checked,note}} dict to the full ordered checklist item list.
    Missing keys default to unchecked; labels are carried from CHECKLISTS."""
    out = []
    for key, label in CHECKLISTS.get(stage, []):
        entry = raw.get(key) or {}
        out.append({
            "key": key,
            "label": label,
            "checked": bool(entry.get("checked", False)),
            "ai_note": str(entry.get("note", "")),
        })
    return out


def assess_checklist(stage: str, artifact_md: str, model: str | None = None) -> list[dict]:
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
    return parse_assessment(stage, raw)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pipeline.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add pipeline.py tests/test_pipeline.py
git commit -m "feat(pipeline): stage graph, checklists, Claude assessment (layer 3)"
```

## Task 2: `local_llm` — передача (`upstream`) in the expand prompt

**Files:**
- Modify: `local_llm.py` (`_format_context` ~163, `build_expand_prompt` ~208)
- Test: `tests/test_expand_modes.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_expand_modes.py`:

```python
def test_build_expand_prompt_includes_upstream_outputs():
    system, user = local_llm.build_expand_prompt(
        mode="spec", video_title="t", section_title="ТЗ", section_md="",
        software_brief_json=None, full_brief_md="", transcript_excerpt="",
        upstream={"report": "ВЫВОД РЕПОРТА: делаем X."},
    )
    assert "report" in user
    assert "ВЫВОД РЕПОРТА" in user


def test_build_expand_prompt_upstream_optional():
    # No upstream arg → still works (backwards compatible).
    system, user = local_llm.build_expand_prompt(
        mode="research", video_title="t", section_title="", section_md="",
        software_brief_json=None, full_brief_md="бриф", transcript_excerpt="",
    )
    assert "бриф" in user
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_expand_modes.py::test_build_expand_prompt_includes_upstream_outputs -q`
Expected: FAIL — `TypeError: build_expand_prompt() got an unexpected keyword argument 'upstream'`.

- [ ] **Step 3: Add `upstream` to `_format_context` + `build_expand_prompt`**

In `local_llm.py`, change `_format_context` signature and body. Replace the signature line and add an upstream block before the `body =` assignment:

Signature — replace:
```python
def _format_context(
    *,
    section_title: str,
    section_md: str,
    software_brief_json: dict | None,
    full_brief_md: str,
    transcript_excerpt: str,
) -> tuple[str, list[str]]:
```
with:
```python
def _format_context(
    *,
    section_title: str,
    section_md: str,
    software_brief_json: dict | None,
    full_brief_md: str,
    transcript_excerpt: str,
    upstream: dict[str, str] | None = None,
) -> tuple[str, list[str]]:
```

Before `sources = []`, add the upstream block:
```python
    upstream_block = ""
    for dep_mode, dep_text in (upstream or {}).items():
        if dep_text and dep_text.strip():
            upstream_block += (
                f"\n\n## Выход предыдущего этапа: {dep_mode} "
                "(используй как основной вход — конвейер)\n" + dep_text.strip()
            )
```

Add to `sources` (after the transcript source line):
```python
    if upstream_block: sources.append("предыдущих этапов")
```

Append `upstream_block` to `body`:
```python
    body = (
        f"## Исходная секция: {section_title}\n{section_md}"
        f"{brief_block}{sb_block}{upstream_block}{transcript_block}"
    )
```

In `build_expand_prompt`, add `upstream: dict[str, str] | None = None` to the signature (after `transcript_excerpt`) and pass it through:
```python
    body, sources = _format_context(
        section_title=section_title,
        section_md=section_md,
        software_brief_json=software_brief_json,
        full_brief_md=full_brief_md,
        transcript_excerpt=transcript_excerpt,
        upstream=upstream,
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_expand_modes.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add local_llm.py tests/test_expand_modes.py
git commit -m "feat(expand): передача — build_expand_prompt accepts upstream stage outputs"
```

## Task 3: Schema — `StageGate`

**Files:**
- Modify: `prisma/schema.prisma`

- [ ] **Step 1: Add the relation + model**

In `model Video`, add to the relations block:
```prisma
  stageGates      StageGate[]
```
Add a new model (after `TranscriptEdit`):
```prisma
// Layer 3 — readiness-checklist state per (video, stage). AI-assessed + user overrides.
model StageGate {
  id         Int       @id @default(autoincrement())
  videoId    String
  video      Video     @relation(fields: [videoId], references: [id], onDelete: Cascade)
  stage      String // "research" | "report" | "spec" | "ai_algorithms"
  items      String // JSON: [{key,label,checked,ai_note}]
  assessedAt DateTime?
  updatedAt  DateTime  @updatedAt

  @@unique([videoId, stage])
}
```

- [ ] **Step 2: Regenerate + push**

Run:
```bash
.venv/Scripts/python.exe -m prisma generate
.venv/Scripts/python.exe -m prisma db push
```
Expected: client regenerated; DB in sync.

- [ ] **Step 3: Verify**

Run:
```bash
.venv/Scripts/python.exe -c "import sqlite3; c=sqlite3.connect('prisma/videotext.db'); print('StageGate' in [r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")])"
```
Expected: `True`

- [ ] **Step 4: Commit**

```bash
git add prisma/schema.prisma
git commit -m "feat(db): StageGate model for pipeline checklist state"
```

## Task 4: Store — StageGate CRUD

**Files:**
- Modify: `store.py`
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pipeline.py`:

```python
import pytest
import store


@pytest.mark.asyncio
async def test_stage_gate_upsert_list_get(db):
    await db.video.create(data={"id": "vidG", "url": "u", "source": "test"})
    items = [{"key": "domain", "label": "L", "checked": True, "ai_note": "ok"}]
    r = await store._upsert_stage_gate(video_id="vidG", stage="research", items=items, assessed=True)
    assert r.stage == "research"
    got = await store._get_stage_gate("vidG", "research")
    import json as _j
    assert _j.loads(got.items)[0]["checked"] is True
    # upsert again (override) — replaces items
    items2 = [{"key": "domain", "label": "L", "checked": False, "ai_note": ""}]
    await store._upsert_stage_gate(video_id="vidG", stage="research", items=items2, assessed=False)
    rows = await store._list_stage_gates("vidG")
    assert len(rows) == 1
    assert _j.loads(rows[0].items)[0]["checked"] is False
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pipeline.py::test_stage_gate_upsert_list_get -q`
Expected: FAIL — `AttributeError: module 'store' has no attribute '_upsert_stage_gate'`.

- [ ] **Step 3: Add to `store.py`** (after the transcript-edit functions, before sync wrappers)

```python
async def _upsert_stage_gate(*, video_id: str, stage: str, items: list, assessed: bool):
    import json as _json
    from datetime import datetime, timezone
    db = Prisma()
    await db.connect()
    try:
        payload = _json.dumps(items, ensure_ascii=False)
        assessed_at = datetime.now(timezone.utc) if assessed else None
        return await db.stagegate.upsert(
            where={"videoId_stage": {"videoId": video_id, "stage": stage}},
            data={
                "create": {"videoId": video_id, "stage": stage, "items": payload,
                           "assessedAt": assessed_at},
                "update": {"items": payload, **({"assessedAt": assessed_at} if assessed else {})},
            },
        )
    finally:
        await db.disconnect()


async def _get_stage_gate(video_id: str, stage: str):
    db = Prisma()
    await db.connect()
    try:
        return await db.stagegate.find_unique(
            where={"videoId_stage": {"videoId": video_id, "stage": stage}})
    finally:
        await db.disconnect()


async def _list_stage_gates(video_id: str):
    db = Prisma()
    await db.connect()
    try:
        return await db.stagegate.find_many(where={"videoId": video_id})
    finally:
        await db.disconnect()
```

Sync wrappers (end of sync area):
```python
def upsert_stage_gate(**kwargs):
    return asyncio.run(_upsert_stage_gate(**kwargs))


def get_stage_gate(video_id: str, stage: str):
    return asyncio.run(_get_stage_gate(video_id, stage))


def list_stage_gates(video_id: str):
    return asyncio.run(_list_stage_gates(video_id))
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pipeline.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add store.py tests/test_pipeline.py
git commit -m "feat(store): StageGate CRUD (upsert/get/list)"
```

## Task 5: Server — передача + stage-gate endpoints

**Files:**
- Modify: `server.py` (imports; `expand_spec`; new endpoints near the expansion routes)
- Test: `tests/test_endpoints.py`

- [ ] **Step 1: Add store imports + `import pipeline`**

Extend `from store import (...)` with:
```python
    get_stage_gate, list_stage_gates, upsert_stage_gate,
```
Add module-level `import pipeline  # noqa: E402` near `import local_llm`.

- [ ] **Step 2: Wire передача into `expand_spec`**

In `expand_spec`, right before the `system, user = local_llm.build_expand_prompt(...)` call, gather upstream:
```python
    upstream: dict[str, str] = {}
    for dep in pipeline.UPSTREAM.get(req.mode, []):
        de = get_expansion(video_id, dep)
        if de and getattr(de, "status", "done") == "done" and de.contentMd:
            upstream[dep] = de.contentMd
```
And pass `upstream=upstream` into the `build_expand_prompt(...)` call.

- [ ] **Step 3: Write the failing registration test**

Append to `tests/test_endpoints.py`:
```python
def test_stage_gate_routes_registered():
    paths = {r.path for r in app.routes}
    assert "/videos/{video_id}/stage-gates" in paths
    assert "/videos/{video_id}/stage-gates/{stage}" in paths
    assert "/videos/{video_id}/stage-assess/{stage}" in paths
```

- [ ] **Step 4: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_endpoints.py::test_stage_gate_routes_registered -q`
Expected: FAIL.

- [ ] **Step 5: Add the endpoints to `server.py`** (after `read_expansions`)

```python
def _stage_gate_to_dict(g) -> dict:
    import json as _json
    return {
        "stage": g.stage,
        "items": _json.loads(g.items),
        "assessed_at": g.assessedAt.isoformat() if g.assessedAt else None,
        "updated_at": g.updatedAt.isoformat(),
    }


class StageGatePutRequest(BaseModel):
    items: list[dict]


@app.get("/videos/{video_id}/stage-gates")
def read_stage_gates(video_id: str) -> dict:
    rows = list_stage_gates(video_id)
    return {g.stage: _stage_gate_to_dict(g) for g in rows}


@app.put("/videos/{video_id}/stage-gates/{stage}")
def put_stage_gate(video_id: str, stage: str, req: StageGatePutRequest) -> dict:
    if stage not in pipeline.CHECKLISTS:
        raise HTTPException(status_code=400, detail=f"Этап {stage} без чеклиста")
    row = upsert_stage_gate(video_id=video_id, stage=stage, items=req.items, assessed=False)
    return _stage_gate_to_dict(row)


@app.post("/videos/{video_id}/stage-assess/{stage}")
def assess_stage(video_id: str, stage: str) -> dict:
    if stage not in pipeline.CHECKLISTS:
        raise HTTPException(status_code=400, detail=f"Этап {stage} без чеклиста")
    e = get_expansion(video_id, stage)
    if not e or not (e.contentMd or "").strip():
        raise HTTPException(status_code=400, detail="Нет артефакта этапа для оценки")
    try:
        items = pipeline.assess_checklist(stage, e.contentMd)
    except Exception as ex:
        raise HTTPException(status_code=502, detail=f"AI-оценка не удалась: {ex}")
    row = upsert_stage_gate(video_id=video_id, stage=stage, items=items, assessed=True)
    return _stage_gate_to_dict(row)
```

- [ ] **Step 6: Run tests + full suite**

Run: `.venv/Scripts/python.exe -m pytest tests/test_endpoints.py::test_stage_gate_routes_registered tests/test_pipeline.py -q`
Expected: PASS.
Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: all green.

- [ ] **Step 7: Live smoke — передача (server running)**

Generate `report` (which has `research` upstream) on a video that already has a `research` artifact and confirm the prompt picks it up. With a clean server:
```bash
printf '%s' '{"mode":"report","model":"qwen2.5:3b","context":"brief"}' > _test/r.json
curl -s -X POST http://127.0.0.1:8000/videos/ZkoOGPLwTBE/expand-spec -H "Content-Type: application/json" --data-binary @_test/r.json
rm -f _test/r.json
```
Expected: `{"status":"running","mode":"report",...}`. (If a `research` artifact exists, its text is fed in as upstream — verified end-to-end in Task 6.)

- [ ] **Step 8: Commit**

```bash
git add server.py tests/test_endpoints.py
git commit -m "feat(server): передача in expand-spec + stage-gate read/put/assess endpoints"
```

## Task 6: Frontend — stepper, checklist panel, warnings, hints

**Files:**
- Modify: `static/index.html` (artifacts pane: stepper + checklist + warning + hint containers; cache-bust)
- Modify: `static/editor-workspace.js` (pipeline state + rendering)

> No JS unit-test harness — verified live in the browser.

- [ ] **Step 1: Add markup to the artifacts pane**

In `#editor-pane-artifacts` `.editor-workspace-right`, above `#af-modes`, add:
```html
<div id="af-stepper" style="display:flex; gap:6px; flex-wrap:wrap; margin-bottom:10px; font-size:12px;"></div>
<div id="af-warning" style="display:none; padding:8px 12px; margin-bottom:8px; border-radius:6px; background:#fffbeb; color:#92400e; font-size:12px;"></div>
```
After `#af-export`, add:
```html
<div id="af-checklist" style="margin-top:14px;"></div>
<div id="af-hint" style="margin-top:10px; font-size:12px; color:var(--mute); font-style:italic;"></div>
```

- [ ] **Step 2: Add pipeline state + constants in `editor-workspace.js`**

Add to `state`: `afGates: {}`. Add constants:
```javascript
  const AF_ORDER = ['research', 'report', 'spec', 'uiux', 'ai_algorithms', 'ai_skills'];
  const AF_LABEL = { research:'Ресерч', report:'Репорт', spec:'ТЗ', uiux:'UI/UX', ai_algorithms:'Алгоритмы', ai_skills:'AI-скиллы' };
  const AF_GATE_PRED = { report:'research', spec:'report', ai_algorithms:'spec', ai_skills:'ai_algorithms' };
  const AF_HINT = {
    research:'Если ресерч показал, что идея нежизнеспособна → вернись к брифу, переформулируй.',
    report:'Если репорт без однозначной рекомендации → доп. ресерч по открытым вопросам.',
    spec:'Если ТЗ противоречиво → назад к репорту, уточни решение.',
    uiux:'Если сценарий не сходится с ТЗ → уточни ТЗ.',
    ai_algorithms:'Если алгоритм не покрывает граничные случаи → доработай, не иди в AI-скилл.',
    ai_skills:'Если AI-скилл нестабилен → пересмотри алгоритм и формат входных.',
  };
  function gateClosed(stage) {
    const g = state.afGates[stage];
    return !!(g && g.items && g.items.length && g.items.every(i => i.checked));
  }
```

- [ ] **Step 3: Load gates + render stepper/checklist/warning/hint**

In `selectArtifactVideo`, after `await loadArtifactExpansions(id);` add `await loadArtifactGates(id);`. Add:
```javascript
  async function loadArtifactGates(id) {
    try { state.afGates = await fetchJSON(`/videos/${id}/stage-gates`); }
    catch (_) { state.afGates = {}; }
  }

  function renderStepper() {
    $('af-stepper').innerHTML = AF_ORDER.map(st => {
      const e = state.afExpansions[st];
      const done = e && e.status === 'done';
      const gated = AF_GATE_PRED[st] && !gateClosed(AF_GATE_PRED[st]);
      const mark = !e ? '○' : e.status === 'running' ? '⏳' : e.status === 'error' ? '⚠️' : '●';
      const cur = st === state.afMode;
      return `<span data-step="${st}" style="cursor:pointer; padding:3px 8px; border-radius:6px;
        border:1px solid ${cur ? 'var(--ink)' : 'var(--line)'};
        ${gated ? 'opacity:.6;' : ''} ${done ? 'color:#166534;' : ''}">${mark} ${AF_LABEL[st]}</span>`;
    }).join('<span style="color:var(--mute-2);">→</span>');
    $('af-stepper').querySelectorAll('[data-step]').forEach(el =>
      el.addEventListener('click', () => selectArtifactMode(el.dataset.step)));
  }

  function renderChecklist() {
    const box = $('af-checklist');
    const stage = state.afMode;
    const hasChecklist = ['research', 'report', 'spec', 'ai_algorithms'].includes(stage);
    if (!hasChecklist) { box.innerHTML = ''; return; }
    const g = state.afGates[stage];
    const items = g ? g.items : [];
    box.innerHTML = `
      <div class="editor-items-meta">чеклист готовности: ${AF_LABEL[stage]}</div>
      <button type="button" id="af-assess" style="cursor:pointer; font-size:12px; margin:4px 0;">AI-оценка</button>
      ${items.length ? items.map((it, i) => `
        <label style="display:flex; gap:8px; align-items:flex-start; font-size:12px; margin:3px 0;">
          <input type="checkbox" data-ci="${i}" ${it.checked ? 'checked' : ''}>
          <span>${escapeHtml(it.label)}${it.ai_note ? ` <em style="color:var(--mute-2);">— ${escapeHtml(it.ai_note)}</em>` : ''}</span>
        </label>`).join('') : '<em style="font-size:12px; color:var(--mute);">нет оценки — нажми «AI-оценка»</em>'}`;
    $('af-assess').addEventListener('click', () => assessStage(stage));
    box.querySelectorAll('[data-ci]').forEach(cb =>
      cb.addEventListener('change', () => toggleChecklistItem(stage, Number(cb.dataset.ci), cb.checked)));
  }

  function renderWarningAndHint() {
    const stage = state.afMode;
    const pred = AF_GATE_PRED[stage];
    const warn = pred && !gateClosed(pred);
    const w = $('af-warning');
    if (warn) { w.style.display = 'block'; w.textContent = `⚠ Рекомендуется сначала закрыть этап «${AF_LABEL[pred]}» (чеклист не пройден) — но сгенерировать можно.`; }
    else { w.style.display = 'none'; }
    $('af-hint').textContent = AF_HINT[stage] || '';
  }

  async function assessStage(stage) {
    $('af-assess').textContent = 'оцениваю…';
    try {
      const g = await fetchJSON(`/videos/${state.afSelectedId}/stage-assess/${stage}`, { method: 'POST' });
      state.afGates[stage] = g;
      renderChecklist(); renderStepper(); renderWarningAndHint();
    } catch (e) {
      $('af-assess').textContent = 'AI-оценка'; alert('Не удалось оценить: ' + e.message);
    }
  }

  async function toggleChecklistItem(stage, idx, checked) {
    const g = state.afGates[stage]; if (!g) return;
    g.items[idx].checked = checked;
    await fetchJSON(`/videos/${state.afSelectedId}/stage-gates/${stage}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: g.items }),
    });
    renderStepper(); renderWarningAndHint();
  }
```

- [ ] **Step 4: Hook the renders into `selectArtifactMode`**

At the end of `selectArtifactMode(mode)`, add:
```javascript
    renderStepper();
    renderChecklist();
    renderWarningAndHint();
```

- [ ] **Step 5: Cache-bust + verify live**

Bump `editor-workspace.js?v=4` → `?v=5` in `index.html`. Reload, AI Editor → Артефакты → pick the arbitrage video. Confirm: stepper shows 6 stages with status marks; selecting a gated stage shows the warning banner; checklist panel shows for research/report/spec/ai_algorithms; "AI-оценка" fills checkmarks + notes; manual toggle persists; backward-loop hint shows under each stage.

- [ ] **Step 6: Commit**

```bash
git add static/index.html static/editor-workspace.js
git commit -m "feat(editor-ui): pipeline stepper, checklists, soft warnings, backward hints"
```

## Task 7: Full verification

**Files:** none

- [ ] **Step 1: Full Python suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: all PASS (smoke skipped).

- [ ] **Step 2: Live e2e on the arbitrage video**

With a clean server: AI Editor → Артефакты → ZkoOGPLwTBE.
1. Generate `research` → done.
2. "AI-оценка" on research → checklist fills (e.g., domain ✓, options depends).
3. Generate `report` → confirm (via the saved report text or server log) it composed on the research output (передача).
4. Select `ai_skills` while `ai_algorithms` is empty → warning banner appears; generation still works.
5. Manually toggle a research checklist item → stepper opacity for `report` updates.

- [ ] **Step 3: Clean state**

```bash
rm -f _test/r.json
git status   # clean
```

---

## Notes for the implementer

- **Restart cleanly (Windows):** kill the listening PID on 8000 or run without `--reload`; Python changes need a restart, static JS just needs a `?v=` bump.
- **Cyrillic curl payloads:** write JSON to a UTF-8 file, `--data-binary @file`.
- **Assessment model:** `assess_checklist` defaults to `claude-haiku-4-5` (fast/cheap structured judgment) — distinct from the Ollama expand generation.
- **No JS test harness:** frontend verified live.
