"""Tool dispatch with persona-based whitelist.

Merges per-persona tool modules and exposes:
  - `registry(persona)` → merged dict of tools this persona is allowed to call
  - `execute(name, args, persona)` → runs tool if whitelisted, else {ok:false}
  - `openai_schema(persona)` / `anthropic_schema(persona)` — schemas for the
    whitelisted subset, ready to hand to each provider's SDK.
"""

from __future__ import annotations
from typing import Any

from assistant.personas.base import BasePersona
from assistant.tools.base import ToolDef
from assistant.tools import platform_tools


def _all_tools() -> dict[str, ToolDef]:
    """Union of all tool modules. Editor tools will be added here later."""
    merged = {}
    merged.update(platform_tools.TOOLS)
    try:
        from assistant.tools import editor_tools  # added in Phase 1
        merged.update(editor_tools.TOOLS)
    except ImportError:
        pass  # editor_tools not yet created
    return merged


def registry(persona: BasePersona) -> dict[str, ToolDef]:
    """Subset of tools this persona is allowed to call."""
    all_t = _all_tools()
    return {name: all_t[name] for name in persona.tool_names if name in all_t}


def execute(name: str, args: dict, persona: BasePersona) -> dict:
    """Run a tool — returns {ok, data, ...} or {ok:false, error}."""
    allowed = registry(persona)
    if name not in allowed:
        return {"ok": False, "error": f"tool {name!r} not allowed for persona {persona.name!r}"}
    td = allowed[name]
    try:
        return td.execute(**args)
    except TypeError as e:
        return {"ok": False, "error": f"bad arguments: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def openai_schema(persona: BasePersona) -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": td.name, "description": td.description, "parameters": td.parameters,
        }}
        for td in registry(persona).values()
    ]


def anthropic_schema(persona: BasePersona) -> list[dict]:
    return [
        {"name": td.name, "description": td.description, "input_schema": td.parameters}
        for td in registry(persona).values()
    ]
