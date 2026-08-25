"""Tool definition contract."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict   # JSON Schema
    execute: Callable[..., dict]
    is_write: bool = False  # write-tools return preview, actual write via /apply
