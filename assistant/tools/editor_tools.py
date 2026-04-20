"""Editor tools — work with news item content.

All mutating tools return a preview dict: {ok, old, new, diff, tool_call_id}.
Actual DB writes happen in /chat/editor/apply after user clicks "apply".
"""

from __future__ import annotations
import uuid

from .base import ToolDef


def _stub_rewrite(item_id: int, field: str, marker: str) -> dict:
    return {
        "ok": True,
        "tool_call_id": str(uuid.uuid4()),
        "item_id": item_id,
        "field": field,
        "old": f"(stub: current {field} of item {item_id})",
        "new": f"(stub: improved {field} of item {item_id}) {marker}",
        "diff": "--- stub diff ---",
    }


def recognize_text(image_id: int | None = None, url: str | None = None) -> dict:
    return {"ok": True, "tool_call_id": str(uuid.uuid4()), "text": "(stub OCR output)"}


def improve_headline(item_id: int, style: str = "", confirm: bool = False) -> dict:
    return _stub_rewrite(item_id, "headline", f"[style={style}]")


def rewrite_quote(item_id: int, tone: str = "", confirm: bool = False) -> dict:
    return _stub_rewrite(item_id, "quote", f"[tone={tone}]")


def expand_text(item_id: int, length: str = "medium", confirm: bool = False) -> dict:
    return _stub_rewrite(item_id, "expandedText", f"[len={length}]")


def regenerate_image(item_id: int, concept: str = "", confirm: bool = False) -> dict:
    return {
        "ok": True, "tool_call_id": str(uuid.uuid4()),
        "item_id": item_id, "field": "imageId",
        "old": None, "new": f"(stub image for concept={concept})",
        "diff": "(stub)",
    }


def suggest_tags(item_id: int) -> dict:
    return {
        "ok": True, "tool_call_id": str(uuid.uuid4()),
        "item_id": item_id, "suggestions": ["stub-tag-1", "stub-tag-2"],
    }


def bulk_action(item_ids: list[int], action: str, confirm: bool = False) -> dict:
    return {
        "ok": True, "tool_call_id": str(uuid.uuid4()),
        "action": action, "preview": [{"item_id": i, "change": "stub"} for i in item_ids],
    }


TOOLS: dict[str, ToolDef] = {
    "recognize_text": ToolDef(
        name="recognize_text",
        description="OCR on an image (by image_id or url). Returns recognized text.",
        parameters={
            "type": "object",
            "properties": {
                "image_id": {"type": "integer"},
                "url": {"type": "string"},
            },
        },
        execute=recognize_text,
    ),
    "improve_headline": ToolDef(
        name="improve_headline",
        description="Rewrite a news item headline. Returns preview (old/new/diff).",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "integer"},
                "style": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["item_id"],
        },
        execute=improve_headline,
        is_write=True,
    ),
    "rewrite_quote": ToolDef(
        name="rewrite_quote",
        description="Rewrite a news item quote preserving meaning.",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "integer"},
                "tone": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["item_id"],
        },
        execute=rewrite_quote,
        is_write=True,
    ),
    "expand_text": ToolDef(
        name="expand_text",
        description="Generate or update the expanded text of a news item.",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "integer"},
                "length": {"type": "string", "enum": ["short", "medium", "long"]},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["item_id"],
        },
        execute=expand_text,
        is_write=True,
    ),
    "regenerate_image": ToolDef(
        name="regenerate_image",
        description="Generate a new illustration for a news item.",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "integer"},
                "concept": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["item_id"],
        },
        execute=regenerate_image,
        is_write=True,
    ),
    "suggest_tags": ToolDef(
        name="suggest_tags",
        description="Suggest tags for a news item. Write happens via /apply.",
        parameters={
            "type": "object",
            "properties": {"item_id": {"type": "integer"}},
            "required": ["item_id"],
        },
        execute=suggest_tags,
    ),
    "bulk_action": ToolDef(
        name="bulk_action",
        description="Apply the same action to multiple items. Returns preview list.",
        parameters={
            "type": "object",
            "properties": {
                "item_ids": {"type": "array", "items": {"type": "integer"}},
                "action": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["item_ids", "action"],
        },
        execute=bulk_action,
        is_write=True,
    ),
}
