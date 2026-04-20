"""Base persona contract — what every persona must declare."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class BasePersona:
    """A persona is a bundle of (prompt, tools, KB sources, DB queries).

    Core logic reads these as data — does not introspect. To add a new persona,
    subclass this and fill in the fields.

    `tool_names` — whitelist. tools_runtime rejects anything outside it.
    `kb_source_keys` — list of kind names ("platform_docs", "content_db", ...)
                       that context_builder uses to filter AssistantKB.
    `db_query_builders` — list of callables that take prisma client + question
                           and return structured context rows.
    """
    name: str = ""
    system_prompt: str = ""
    tool_names: list[str] = field(default_factory=list)
    kb_source_keys: list[str] = field(default_factory=list)
    db_query_builders: list[Callable[..., Any]] = field(default_factory=list)
    kb_priorities: dict[str, int] = field(default_factory=dict)
