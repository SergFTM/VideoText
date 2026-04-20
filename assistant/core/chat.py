"""Assistant chat loop — provider-agnostic, tool-use, SSE streaming.

Supported providers:
  - openai (default): gpt-4o / gpt-4o-mini. Native function-calling + vision + streaming.
  - anthropic: claude-sonnet-4-6 / claude-opus-4-7. Native tool-use + streaming.
  - ollama: any local model. Tool-use via prompt-steering (no native function calls).

The chat loop supports multiple rounds of tool use — the model may call a tool,
read the result, then call another tool, then finally answer.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from prisma import Prisma

from assistant.core.cache import find_cached_answer, save_qa
from assistant.core.context_builder import build_context
from assistant.core.tools_runtime import execute, openai_schema, anthropic_schema, registry


SYSTEM_PROMPT_BASE = """Ты — встроенный AI-ассистент приложения VideoText.

VideoText обрабатывает YouTube: single-video briefs + live streams с
извлечением новостей, модерацией, обогащением и экспортом. Стек: FastAPI +
Prisma (SQLite) + Alpine.js.

Твоя роль — помогать ПОЛЬЗОВАТЕЛЮ настраивать и использовать приложение.
Пользователь — не разработчик, не лезет в код. Отвечай коротко, по-русски,
с конкретными действиями («нажми туда», «зайди сюда», «вставь это»).

Правила:
1. Если есть прямое совпадение с каталогом ошибок — сразу давай fix-steps.
2. Для настроек и состояния системы используй tool `get_settings_snapshot`.
3. Для проверки коннектора — tool `test_integration`. Всегда предлагай сам
   протестировать после изменений.
4. Для действий, меняющих состояние (save_setting, save_api_key, approve),
   ВСЕГДА сначала спроси подтверждение у пользователя, потом вызывай tool
   с confirm=true. Не забывай confirm — без него tool отклонит вызов.
5. Если не знаешь — честно скажи, а не галлюцинируй.
6. Отвечай в markdown. Ссылки делай кликабельными: [текст](url).
7. Не пересказывай целиком контекст системы — цитируй только нужные куски.

Если пользователь пришёл с ошибкой на экране — начни с разбора этой ошибки,
потом предлагай дальнейшие действия.
"""


# ─── Provider adapters ────────────────────────────────────────────────


@dataclass
class ChatTurn:
    """One round of the loop. Result of streaming or tool execution."""
    text: str = ""
    tool_calls: list[dict] | None = None  # [{name, args, id}]
    finish_reason: str | None = None
    usage: dict | None = None


async def _openai_round(
    client, model: str, messages: list[dict], tools: list[dict],
    on_text_delta: Callable[[str], None] | None = None,
) -> ChatTurn:
    """One streaming round with OpenAI. Accumulates tool calls + text."""
    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools or None,
        tool_choice="auto" if tools else None,
        stream=True,
        stream_options={"include_usage": True},
    )

    text_parts: list[str] = []
    tool_calls_accum: dict[int, dict] = {}  # index → {id, name, args_json_str}
    finish_reason: str | None = None
    usage: dict | None = None

    async for chunk in stream:
        if chunk.usage:
            usage = {
                "input_tokens": chunk.usage.prompt_tokens,
                "output_tokens": chunk.usage.completion_tokens,
            }
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        delta = choice.delta

        if delta and delta.content:
            text_parts.append(delta.content)
            if on_text_delta:
                on_text_delta(delta.content)

        if delta and delta.tool_calls:
            for tc in delta.tool_calls:
                idx = tc.index
                entry = tool_calls_accum.setdefault(idx, {"id": None, "name": "", "args": ""})
                if tc.id:
                    entry["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        entry["name"] += tc.function.name
                    if tc.function.arguments:
                        entry["args"] += tc.function.arguments

        if choice.finish_reason:
            finish_reason = choice.finish_reason

    calls = None
    if tool_calls_accum:
        calls = []
        for entry in tool_calls_accum.values():
            try:
                args = json.loads(entry["args"]) if entry["args"] else {}
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": entry["id"], "name": entry["name"], "args": args})

    return ChatTurn(
        text="".join(text_parts),
        tool_calls=calls,
        finish_reason=finish_reason,
        usage=usage,
    )


# ─── Main assistant orchestrator ──────────────────────────────────────


class Assistant:
    """Orchestrates: cache lookup → context build → provider call → tool loop → save."""

    def __init__(
        self,
        persona=None,
        *,
        provider: str = "openai",
        model: str = "gpt-4o",
        use_cache: bool = True,
        cache_threshold: float = 0.85,
    ):
        self.persona = persona
        self.provider = provider
        self.model = model
        self.use_cache = use_cache
        self.cache_threshold = cache_threshold

    async def ask_stream(
        self,
        db: Prisma,
        question: str,
        *,
        session_id: str | None = None,
        ui_context: dict | None = None,
        auto_confirm: bool = False,
    ) -> AsyncIterator[dict]:
        """Yield SSE-ready dicts as the answer is generated.

        Event shapes:
          {"type": "cache_hit", "question": ..., "similarity": ..., "answer": ...}
          {"type": "context", "chars": N}
          {"type": "text", "delta": "…"}
          {"type": "tool_call", "name": ..., "args": ...}
          {"type": "tool_result", "name": ..., "result": ...}
          {"type": "done", "usage": {...}, "cost_usd": ...}
          {"type": "error", "msg": ...}
        """
        # 1. Cache lookup
        if self.use_cache:
            cached = await find_cached_answer(db, question, threshold=self.cache_threshold)
            if cached:
                yield {
                    "type": "cache_hit",
                    "question": cached["question"],
                    "similarity": cached["similarity"],
                    "used_count": cached["used_count"],
                    "answer": cached["answer"],
                }
                return

        # 2. Build cherry-picked context
        ctx_text = await build_context(db, question, ui_context=ui_context)
        yield {"type": "context", "chars": len(ctx_text)}

        base_prompt = (self.persona.system_prompt if self.persona else SYSTEM_PROMPT_BASE)
        system_msg = base_prompt + "\n\n# Текущий контекст системы\n\n" + ctx_text
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": question},
        ]

        # 3. Provider call (with tool loop)
        try:
            if self.provider == "openai":
                async for ev in self._run_openai(messages, auto_confirm=auto_confirm):
                    yield ev
            elif self.provider == "anthropic":
                async for ev in self._run_anthropic(messages, auto_confirm=auto_confirm):
                    yield ev
            elif self.provider == "ollama":
                async for ev in self._run_ollama(messages):
                    yield ev
            else:
                yield {"type": "error", "msg": f"unknown provider: {self.provider}"}
                return
        except Exception as e:
            yield {"type": "error", "msg": f"{type(e).__name__}: {e}"}
            return

    # ─── OpenAI path ──────────────────────────────────────────────────

    async def _run_openai(self, messages: list[dict], *, auto_confirm: bool):
        from openai import AsyncOpenAI
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            yield {"type": "error", "msg": "OPENAI_API_KEY не задан"}
            return

        client = AsyncOpenAI(api_key=key)
        tools = openai_schema(self.persona) if self.persona else []
        total_in, total_out = 0, 0
        final_text_parts: list[str] = []

        for round_idx in range(5):  # max 5 tool-use rounds
            collected: list[str] = []

            def on_delta(txt: str):
                collected.append(txt)

            turn = await _openai_round(
                client, self.model, messages, tools,
                on_text_delta=lambda t: None,  # stream below via generator
            )

            # Yield text deltas after-the-fact (simpler than mid-streaming)
            if turn.text:
                yield {"type": "text", "delta": turn.text}
                final_text_parts.append(turn.text)

            if turn.usage:
                total_in += turn.usage.get("input_tokens", 0)
                total_out += turn.usage.get("output_tokens", 0)

            # No tool calls → we're done
            if not turn.tool_calls:
                break

            # Append assistant msg with tool calls, then run each tool
            messages.append({
                "role": "assistant",
                "content": turn.text or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])},
                    }
                    for tc in turn.tool_calls
                ],
            })

            for tc in turn.tool_calls:
                name, args = tc["name"], tc["args"]
                # Inject auto_confirm if the user already approved actions
                if auto_confirm and self.persona:
                    td = registry(self.persona).get(name)
                    if td and td.is_write:
                        args = {**args, "confirm": True}

                yield {"type": "tool_call", "name": name, "args": args}
                result = execute(name, args, self.persona) if self.persona else {"ok": False, "error": "no persona"}
                yield {"type": "tool_result", "name": name, "result": result}

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                })

        answer = "".join(final_text_parts).strip()
        usage = {"input_tokens": total_in, "output_tokens": total_out}
        cost = _estimate_openai_cost(self.model, total_in, total_out)

        yield {"type": "done", "usage": usage, "cost_usd": cost, "answer": answer}

    # ─── Anthropic path ───────────────────────────────────────────────

    async def _run_anthropic(self, messages: list[dict], *, auto_confirm: bool):
        """Anthropic tool-use variant. Reuses openai-style messages by splitting system."""
        import anthropic
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            yield {"type": "error", "msg": "ANTHROPIC_API_KEY не задан"}
            return

        system_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_msgs = [m for m in messages if m["role"] != "system"]

        client = anthropic.AsyncAnthropic(api_key=key)
        tools = anthropic_schema(self.persona) if self.persona else []
        total_in, total_out = 0, 0
        final_text_parts: list[str] = []

        anth_messages = [{"role": m["role"], "content": m["content"]} for m in user_msgs]

        for round_idx in range(5):
            resp = await client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=system_msg,
                messages=anth_messages,
                tools=tools,
            )
            total_in += resp.usage.input_tokens
            total_out += resp.usage.output_tokens

            text_blocks = [b.text for b in resp.content if b.type == "text"]
            if text_blocks:
                txt = "\n".join(text_blocks)
                yield {"type": "text", "delta": txt}
                final_text_parts.append(txt)

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses or resp.stop_reason != "tool_use":
                break

            # Anthropic requires full content echo with tool_use blocks
            anth_messages.append({"role": "assistant", "content": resp.content})

            tool_results = []
            for tu in tool_uses:
                name, args = tu.name, tu.input
                if auto_confirm and self.persona:
                    td = registry(self.persona).get(name)
                    if td and td.is_write:
                        args = {**args, "confirm": True}
                yield {"type": "tool_call", "name": name, "args": args}
                result = execute(name, args, self.persona) if self.persona else {"ok": False, "error": "no persona"}
                yield {"type": "tool_result", "name": name, "result": result}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

            anth_messages.append({"role": "user", "content": tool_results})

        answer = "\n".join(final_text_parts).strip()
        cost = _estimate_anthropic_cost(self.model, total_in, total_out)
        yield {
            "type": "done",
            "usage": {"input_tokens": total_in, "output_tokens": total_out},
            "cost_usd": cost,
            "answer": answer,
        }

    # ─── Ollama path (no native tool-use) ─────────────────────────────

    async def _run_ollama(self, messages: list[dict]):
        import aiohttp
        endpoint = os.getenv("OLLAMA_ENDPOINT", "http://localhost:11434")

        async with aiohttp.ClientSession() as http:
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": True,
            }
            final_parts: list[str] = []
            try:
                async with http.post(f"{endpoint}/api/chat", json=payload, timeout=120) as resp:
                    async for line in resp.content:
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        piece = data.get("message", {}).get("content", "")
                        if piece:
                            yield {"type": "text", "delta": piece}
                            final_parts.append(piece)
                        if data.get("done"):
                            break
            except Exception as e:
                yield {"type": "error", "msg": f"ollama: {e}"}
                return

        answer = "".join(final_parts).strip()
        yield {"type": "done", "usage": {}, "cost_usd": 0.0, "answer": answer}


# ─── Pricing helpers ──────────────────────────────────────────────────

_OPENAI_PRICING = {
    # USD per 1M tokens (input, output)
    "gpt-4o":      (2.50, 10.00),
    "gpt-4o-mini": (0.15,  0.60),
    "gpt-4.1":     (2.00,  8.00),
    "gpt-4.1-mini": (0.40, 1.60),
}

_ANTHROPIC_PRICING = {
    "claude-opus-4-7":   (5.00, 25.00),
    "claude-opus-4-6":   (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5":  (1.00,  5.00),
}


def _estimate_openai_cost(model: str, in_tok: int, out_tok: int) -> float | None:
    p = _OPENAI_PRICING.get(model)
    if not p:
        return None
    return (in_tok * p[0] + out_tok * p[1]) / 1_000_000


def _estimate_anthropic_cost(model: str, in_tok: int, out_tok: int) -> float | None:
    p = _ANTHROPIC_PRICING.get(model)
    if not p:
        return None
    return (in_tok * p[0] + out_tok * p[1]) / 1_000_000
