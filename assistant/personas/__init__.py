"""Persona definitions — data only (prompts, tool lists, KB sources, DB filters).

All logic lives in assistant/core/. Adding a new persona means adding one file here.
"""

from .base import BasePersona
from .platform import PlatformPersona
from .editor import EditorPersona

__all__ = ["BasePersona", "PlatformPersona", "EditorPersona"]
