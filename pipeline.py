"""Layer 3 — guided artifact pipeline: stage graph, checklists, AI assessment.

Pure data + prompt building here; the Claude call (assess_checklist) is the only
network part. Mirrors brief.py's JSON-output approach for structured assessment.
"""
from __future__ import annotations

import json
import re

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

# The single machine-readable contract inside an otherwise free-form artifact:
# the report ends with a verdict marker the server can act on (see §2 of the spec).
_VERDICT_RE = re.compile(
    r"<!--\s*verdict:\s*(confirmed|partial|refuted)\s*-->", re.IGNORECASE)

# Fenced blocks are illustration, not contract. The report prompt itself lists all
# three markers with `refuted` last, so a report that restates that legend inside a
# ``` block near the end would parse as `refuted` under a naive last-wins scan and
# wrongly hard-block ТЗ. Same fence-awareness as skills_export's heading splitter.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def parse_verdict(md: str | None) -> str | None:
    """Last verdict marker outside fenced code, lowercased. None if absent/unknown.

    Last-wins because a model may quote the format in its prose before emitting
    the real marker at the end; fenced regions are dropped first so a quoted
    legend inside ``` can never outvote the real marker.
    """
    if not md:
        return None
    matches = _VERDICT_RE.findall(_FENCE_RE.sub("", md))
    return matches[-1].lower() if matches else None

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

# (Backward-loop hints are display-only and live in the frontend — see AF_HINT in
# editor-workspace.js — so they are not duplicated here.)


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
    # Truncated JSON used to fall through to {} — every criterion then read as
    # failed, indistinguishable from a genuinely incomplete artifact.
    brief.raise_if_truncated(msg, "AI-оценка чеклиста")
    text = next((b.text for b in msg.content if b.type == "text"), "{}")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        # Best-effort: strip code fences if the model wrapped JSON.
        cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        raw = json.loads(cleaned) if cleaned.startswith("{") else {}
    return parse_assessment(stage, raw, previous=previous)
