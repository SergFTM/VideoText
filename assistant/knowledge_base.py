"""Knowledge base builder.

Sources merged into the AssistantKB table:
  1. assistant/errors.yaml   → kind=error_pattern
  2. assistant/kb_static.md  → kind=setup_guide (one entry per "## section")
  3. prisma/schema.prisma    → kind=schema (one entry per model)
  4. *.py at repo root       → kind=code_module (AST docstrings, top-level only)

Idempotent: re-running replaces existing entries by (kind, key).
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

import yaml
from prisma import Prisma

from assistant.analyzer import tokenize

ROOT = Path(__file__).resolve().parent.parent
ASSISTANT_DIR = Path(__file__).resolve().parent


# ─── Parsers ──────────────────────────────────────────────────────────


def _parse_errors_yaml() -> list[dict]:
    path = ASSISTANT_DIR / "errors.yaml"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    entries = []
    for err in data:
        body_parts = [
            f"Pattern: {err.get('pattern', '')}",
            f"Category: {err.get('category', '')}",
            "Шаги:",
            *(f"- {s}" for s in err.get("fix_steps", [])),
        ]
        if err.get("auto_fix"):
            body_parts.append(f"Auto-fix: {err['auto_fix']}")
        if err.get("docs_url"):
            body_parts.append(f"Docs: {err['docs_url']}")
        body = "\n".join(body_parts)
        entries.append({
            "kind": "error_pattern",
            "key": err["id"],
            "title": err.get("title_ru") or err["id"],
            "body": body,
            "tokens": tokenize(f"{err.get('title_ru', '')} {err.get('pattern', '')} {' '.join(err.get('fix_steps', []))}"),
        })
    return entries


def _parse_kb_static() -> list[dict]:
    """Split kb_static.md by `## ` headers. Each section becomes one KB entry."""
    path = ASSISTANT_DIR / "kb_static.md"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")

    # Split on `## ` headers (but not `### `)
    sections = re.split(r"^## (?!#)", text, flags=re.MULTILINE)
    entries = []
    for sec in sections[1:]:  # skip intro before first `## `
        lines = sec.splitlines()
        if not lines:
            continue
        title = lines[0].strip()
        body = "\n".join(lines[1:]).strip()
        if not body:
            continue
        # key = slugified title, first 60 chars
        key = re.sub(r"[^a-zа-яё0-9]+", "-", title.lower(), flags=re.IGNORECASE)[:60].strip("-")
        entries.append({
            "kind": "setup_guide",
            "key": key,
            "title": title,
            "body": body,
            "tokens": tokenize(title + " " + body),
        })
    return entries


def _parse_prisma_schema() -> list[dict]:
    """One KB entry per Prisma model — name + field list + comments."""
    schema_path = ROOT / "prisma" / "schema.prisma"
    if not schema_path.exists():
        return []
    text = schema_path.read_text(encoding="utf-8")
    entries = []
    for match in re.finditer(r"model\s+(\w+)\s*\{([^}]+)\}", text):
        name, body = match.group(1), match.group(2)
        # Extract fields (skip relations / @@ directives)
        field_lines = []
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("//") or line.startswith("@@"):
                continue
            field_lines.append(line)
        if not field_lines:
            continue
        readable = f"Модель {name}\nПоля:\n" + "\n".join(f"  - {ln}" for ln in field_lines[:30])
        entries.append({
            "kind": "schema",
            "key": name,
            "title": f"Prisma model: {name}",
            "body": readable,
            "tokens": tokenize(f"{name} " + " ".join(field_lines)),
        })
    return entries


def _parse_code_modules() -> list[dict]:
    """AST-walk of *.py at repo root. Captures top-level docstring + public functions."""
    entries = []
    for py_file in sorted(ROOT.glob("*.py")):
        if py_file.name.startswith("_") or py_file.name in {"main.py"}:
            # main.py is CLI entry — low value for the assistant
            pass
        try:
            src = py_file.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(src)
        except (OSError, SyntaxError):
            continue

        module_doc = ast.get_docstring(tree) or ""
        parts = [module_doc] if module_doc else []

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                doc = ast.get_docstring(node) or ""
                args = [a.arg for a in node.args.args]
                parts.append(f"{node.name}({', '.join(args)}) — {doc[:200]}")
            elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                cls_doc = ast.get_docstring(node) or ""
                parts.append(f"class {node.name} — {cls_doc[:150]}")

        body = "\n".join(p for p in parts if p)
        if not body:
            continue
        entries.append({
            "kind": "code_module",
            "key": py_file.stem,
            "title": f"Module: {py_file.name}",
            "body": body[:4000],  # cap to avoid huge embeddings
            "tokens": tokenize(py_file.stem + " " + body),
        })
    return entries


# ─── DB persistence ───────────────────────────────────────────────────


async def rebuild_kb(db: Prisma | None = None) -> dict:
    """Drop + re-insert all KB entries. Returns counts by kind."""
    owns_db = db is None
    if owns_db:
        db = Prisma()
        await db.connect()

    try:
        await db.assistantkb.delete_many()

        all_entries = (
            _parse_errors_yaml()
            + _parse_kb_static()
            + _parse_prisma_schema()
            + _parse_code_modules()
        )

        counts: dict[str, int] = {}
        for e in all_entries:
            await db.assistantkb.create(
                data={
                    "kind": e["kind"],
                    "key": e["key"],
                    "title": e["title"],
                    "body": e["body"],
                    "tokens": json.dumps(e["tokens"], ensure_ascii=False),
                }
            )
            counts[e["kind"]] = counts.get(e["kind"], 0) + 1

        return counts
    finally:
        if owns_db:
            await db.disconnect()


async def load_kb(db: Prisma) -> list[dict]:
    """Load all KB entries with pre-parsed tokens."""
    rows = await db.assistantkb.find_many()
    out = []
    for r in rows:
        try:
            toks = json.loads(r.tokens) if r.tokens else []
        except json.JSONDecodeError:
            toks = []
        out.append({
            "id": r.id,
            "kind": r.kind,
            "key": r.key,
            "title": r.title,
            "body": r.body,
            "tokens": toks,
        })
    return out
