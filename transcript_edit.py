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

SYSTEM_PROMPTS["seed"] = (
    "Ты — редактор. Режим СУТЬ. На входе — расшифровка видео и его бриф."
    " Сформулируй ядро материала: о чём он, ключевые тезисы, главный вывод.\n\n"
    "ТРЕБОВАНИЯ: 3–5 предложений или короткий список; только то, что есть в источниках;"
    " без воды, без выдуманных фактов. Верни ТОЛЬКО текст сути в markdown, без преамбул."
)

# Document-kind → noun used inside the per-op prompts/user message.
_KIND_NOUN = {"transcript": "расшифровки", "brief": "брифа", "essence": "сути"}


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
