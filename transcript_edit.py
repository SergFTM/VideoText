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
