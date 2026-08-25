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
# editor-workspace.js — so they are not duplicated here.)


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
