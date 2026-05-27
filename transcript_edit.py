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
