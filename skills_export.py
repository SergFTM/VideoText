"""Turn the ai_skills artifact into a runnable bundle.

The ai_skills prompt (local_llm.SYSTEM_PROMPTS) pins a strict shape — `## Скилл N.`
headings with a `**Slug:**` line — precisely so this parser can be simple and the
failure mode obvious: no headings, no bundle, explicit error.

What ships is honest about what it is: the MCP prompts are complete (they carry the
skill text), the MCP tools are stubs with the algorithm's steps in the docstring.
Generating a working implementation from prose is not something we can do, so we
don't pretend to.
"""
from __future__ import annotations

import io
import json
import re
import zipfile

_SKILL_RE = re.compile(r"^##\s+Скилл\s+\d+\.\s*(.+?)\s*$", re.MULTILINE)
_SLUG_RE = re.compile(r"^\*\*Slug:\*\*\s*`?([a-z0-9][a-z0-9-]*)`?\s*$", re.MULTILINE)
_DESC_RE = re.compile(r"^\*\*Описание:\*\*\s*(.+?)\s*$", re.MULTILINE)
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
}


def slugify(title: str) -> str:
    """kebab-case ASCII slug. Used only when the model omitted the Slug line."""
    out = "".join(_TRANSLIT.get(ch, ch) for ch in (title or "").lower())
    out = re.sub(r"[^a-z0-9]+", "-", out).strip("-")
    return out or "skill"


def parse_skills(md: str | None) -> list[dict]:
    """Split the artifact into skills. Returns [] when nothing matches the shape.

    Headings that fall inside fenced ``` code blocks are ignored — a skill's body
    may show example output containing the literal `## Скилл N.` pattern, and that
    must not be mistaken for a real heading or split the real skill's body in two.
    """
    if not md:
        return []
    fences = [(m.start(), m.end()) for m in _FENCE_RE.finditer(md)]

    def _fenced(pos: int) -> bool:
        return any(s <= pos < e for s, e in fences)

    heads = [m for m in _SKILL_RE.finditer(md) if not _fenced(m.start())]
    skills = []
    for i, m in enumerate(heads):
        start = m.end()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(md)
        body = md[start:end].strip()
        title = m.group(1).strip()
        slug_m = _SLUG_RE.search(body)
        desc_m = _DESC_RE.search(body)
        skills.append({
            "slug": slug_m.group(1) if slug_m else slugify(title),
            "title": title,
            "description": desc_m.group(1) if desc_m else title,
            "body": body,
        })
    return skills


def _dedupe_slugs(skills: list[dict]) -> list[dict]:
    """Make slugs unique across the bundle: `dup-slug`, `dup-slug-2`, `dup-slug-3`, ...

    Two skills can land on the same slug either because the model emitted the same
    explicit `**Slug:**` twice, or because two punctuation-only titles both fall
    through `slugify()` to its `"skill"` default. Without this, `zipfile.writestr`
    silently overwrites the first skill's file with the second's — a bundle quietly
    missing a skill, which is the worst failure mode for an export feature. Runs
    once, before anything else touches `skills`, so the directory path, the
    frontmatter `name:`, and the `SKILLS` list embedded in the generated
    `mcp_server/server.py` can never drift apart.
    """
    used: set[str] = set()
    out = []
    for s in skills:
        base = s["slug"]
        slug = base
        n = 2
        while slug in used:
            slug = f"{base}-{n}"
            n += 1
        used.add(slug)
        out.append(s if slug == base else {**s, "slug": slug})
    return out


def _skill_md(skill: dict) -> str:
    """SKILL.md with YAML frontmatter Claude Code / Cursor can load directly."""
    desc = skill["description"].replace('"', "'")
    return (
        "---\n"
        f"name: {skill['slug']}\n"
        f"description: \"{desc}\"\n"
        "---\n\n"
        f"# {skill['title']}\n\n"
        f"{skill['body']}\n"
    )


def _mcp_server_py(skills: list[dict]) -> str:
    """stdio MCP server: prompts are complete, tools are honest stubs."""
    entries = json.dumps(
        [{"slug": s["slug"], "title": s["title"], "description": s["description"]}
         for s in skills],
        ensure_ascii=False, indent=4,
    )
    return f'''"""Generated MCP server — skills as prompts, algorithms as tool stubs.

Run:  python server.py     (stdio transport)

The prompts below are complete: each one serves the full SKILL.md text.
The tools are stubs on purpose — the algorithms artifact describes WHAT to do,
not the code. Fill in each TODO, or hand this file to an agent to implement.
"""
import pathlib

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("videotext-skills")

SKILLS = {entries}

_HERE = pathlib.Path(__file__).resolve().parent.parent


def _skill_text(slug: str) -> str:
    path = _HERE / "skills" / slug / "SKILL.md"
    return path.read_text(encoding="utf-8") if path.exists() else f"Скилл {{slug}} не найден"


for _s in SKILLS:
    def _make(slug=_s["slug"], description=_s["description"]):
        @mcp.prompt(name=slug, description=description)
        def _prompt() -> str:
            return _skill_text(slug)
        return _prompt
    _make()


@mcp.tool()
def list_skills() -> list[dict]:
    """List the skills bundled with this server."""
    return SKILLS


@mcp.tool()
def run_algorithm(name: str, payload: dict) -> dict:
    """Execute one of the algorithms from the source artifact.

    TODO: implement. See README.md for the algorithm definitions — each one lists
    its inputs, steps, exit criteria and edge cases. Dispatch on `name`.
    """
    raise NotImplementedError("Алгоритмы ещё не реализованы — см. README.md")


if __name__ == "__main__":
    mcp.run()
'''


def _readme(video_title: str, skills: list[dict], spec_md: str, algorithms_md: str) -> str:
    listing = "\n".join(f"- `{s['slug']}` — {s['description']}" for s in skills)
    return (
        f"# {video_title} — бандл скиллов\n\n"
        "Собрано автоматически из артефактов VideoText (стадии ТЗ, алгоритмы, AI-скиллы).\n\n"
        "## Состав\n\n"
        "- `skills/<slug>/SKILL.md` — готовые скиллы с frontmatter.\n"
        "- `mcp_server/` — MCP-сервер: промпты рабочие, тулы — заглушки с TODO.\n\n"
        "## Скиллы\n\n"
        f"{listing}\n\n"
        "## Установка\n\n"
        "```bash\n"
        "pip install -r mcp_server/requirements.txt\n"
        "python mcp_server/server.py\n"
        "```\n\n"
        "Скиллы: скопируй каталог `skills/` в `.claude/skills/` своего проекта.\n\n"
        "---\n\n"
        "## ТЗ\n\n"
        f"{spec_md or '_ТЗ не сгенерировано_'}\n\n"
        "---\n\n"
        "## Алгоритмы\n\n"
        f"{algorithms_md or '_Алгоритмы не сгенерированы_'}\n"
    )


def build_bundle(skills: list[dict], *, spec_md: str = "", algorithms_md: str = "",
                 video_title: str = "") -> bytes:
    """Zip with skills tree, MCP scaffold and a README carrying the upstream context."""
    skills = _dedupe_slugs(skills)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for s in skills:
            zf.writestr(f"skills/{s['slug']}/SKILL.md", _skill_md(s))
        zf.writestr("mcp_server/server.py", _mcp_server_py(skills))
        zf.writestr("mcp_server/requirements.txt", "mcp>=1.2.0\n")
        zf.writestr("README.md", _readme(video_title, skills, spec_md, algorithms_md))
    return buf.getvalue()
