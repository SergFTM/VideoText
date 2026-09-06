"""Turn the ai_skills (and ai_algorithms) artifacts into a runnable bundle.

The ai_skills prompt (local_llm.SYSTEM_PROMPTS) pins a strict shape — `## Скилл N.`
headings with a `**Slug:**` line — precisely so this parser can be simple and the
failure mode obvious: no headings, no bundle, explicit error. The algorithms
artifact is split the same way on `## Алгоритм N.` headings, by the same splitter.

What ships is honest about what it is: the MCP prompts are complete (they carry the
skill text), the MCP tools are one stub per algorithm with that algorithm's steps in
the docstring and a body that raises. Generating a working implementation from prose
is not something we can do, so we don't pretend to.
"""
from __future__ import annotations

import io
import json
import keyword
import re
import zipfile

_SKILL_RE = re.compile(r"^#{1,3}\s+Скилл\s+\d+[.:)]\s*(.+?)\s*$", re.MULTILINE)
_ALGO_RE = re.compile(r"^#{1,3}\s+Алгоритм\s+\d+[.:)]\s*(.+?)\s*$", re.MULTILINE)
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


def slugify(title: str, default: str = "skill") -> str:
    """kebab-case ASCII slug. Used when the model omitted the Slug line."""
    out = "".join(_TRANSLIT.get(ch, ch) for ch in (title or "").lower())
    out = re.sub(r"[^a-z0-9]+", "-", out).strip("-")
    return out or default


def _split_sections(md: str | None, heading_re: re.Pattern) -> list[tuple[str, str]]:
    """[(title, body)] for every heading matching `heading_re`, outside code fences.

    The one splitter for both artifacts: skills (`## Скилл N.`) and algorithms
    (`## Алгоритм N.`) have the same pinned shape and the same hazard — a section's
    body may show example output containing the literal heading pattern, and that
    must not be mistaken for a real heading or split the real body in two.
    """
    if not md:
        return []
    fences = [(m.start(), m.end()) for m in _FENCE_RE.finditer(md)]

    def _fenced(pos: int) -> bool:
        return any(s <= pos < e for s, e in fences)

    heads = [m for m in heading_re.finditer(md) if not _fenced(m.start())]
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(md)
        out.append((m.group(1).strip(), md[m.end():end].strip()))
    return out


def parse_skills(md: str | None) -> list[dict]:
    """Split the artifact into skills. Returns [] when nothing matches the shape."""
    skills = []
    for title, body in _split_sections(md, _SKILL_RE):
        slug_m = _SLUG_RE.search(body)
        desc_m = _DESC_RE.search(body)
        skills.append({
            "slug": slug_m.group(1) if slug_m else slugify(title),
            "title": title,
            "description": desc_m.group(1) if desc_m else title,
            "body": body,
        })
    return skills


def parse_algorithms(md: str | None) -> list[dict]:
    """Split the algorithms artifact into algorithms. [] when nothing matches.

    The algorithms prompt has no `**Slug:**` line, so the slug is always derived
    from the title — which is exactly what the generated tool name is built on.
    """
    return [{"slug": slugify(title, default="algorithm"), "title": title, "body": body}
            for title, body in _split_sections(md, _ALGO_RE)]


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


# Names the generated module already binds at module level. A tool named after an
# algorithm must never shadow one of these — that would rebind `mcp` or clobber
# `list_skills`, a breakage no syntax check would catch.
_RESERVED_NAMES = {
    "mcp", "pathlib", "FastMCP", "SKILLS", "_HERE", "_skill_text",
    "_make", "_prompt", "_s", "list_skills",
}


def _py_name(slug: str) -> str:
    """Python identifier from a slug. ASCII by construction — `slugify` already
    transliterated Cyrillic and dropped everything outside [a-z0-9-]."""
    name = re.sub(r"[^a-z0-9_]+", "_", (slug or "").lower().replace("-", "_")).strip("_")
    if not name:
        return "algorithm"
    if name[0].isdigit():
        name = f"algo_{name}"
    if keyword.iskeyword(name) or keyword.issoftkeyword(name):
        name = f"{name}_"
    return name


def _tool_names(algorithms: list[dict]) -> list[str]:
    """One valid, unique Python function name per algorithm.

    Two algorithms collide the same way two skills do — identical titles, or titles
    differing only in punctuation/non-ASCII that `slugify` strips. A collision here
    would silently redefine the first tool, so later duplicates get a `_2` suffix.
    """
    used = set(_RESERVED_NAMES)
    names = []
    for a in algorithms:
        base = _py_name(a.get("slug") or "")
        name, n = base, 2
        while name in used:
            name, n = f"{base}_{n}", n + 1
        used.add(name)
        names.append(name)
    return names


def _docstring(text: str, indent: str = "    ") -> str:
    """Render arbitrary artifact prose as a safe triple-quoted docstring block."""
    safe = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    safe = safe.replace("\\", "\\\\").replace('"""', '\\"' * 3).strip()
    if safe.endswith('"'):
        safe += " "
    body = "\n".join((indent + ln).rstrip() for ln in safe.split("\n"))
    return f'{indent}"""\n{body}\n{indent}"""\n'


def _algo_tools_src(algorithms: list[dict]) -> str:
    """One stub tool per algorithm: real name, real steps, a body that refuses.

    Deliberately NOT a generic `run_algorithm(name, payload)` dispatcher: §8 of the
    design asks for one tool per algorithm so whoever opens the server sees the real
    inventory. Every body raises — nothing here is dressed up as implemented.
    """
    if not algorithms:
        return (
            "# Алгоритмы не найдены в артефакте ai_algorithms — тулов-заглушек нет.\n"
            "# Сгенерируй стадию «Алгоритмы» и пересобери бандл.\n"
        )
    out = []
    for name, a in zip(_tool_names(algorithms), algorithms):
        doc = _docstring(
            f"{a['title']}\n\n"
            "ЗАГЛУШКА: тело не реализовано. Ниже — шаги алгоритма из артефакта.\n\n"
            f"{a['body']}"
        )
        msg = json.dumps(
            f"Алгоритм «{a['title']}» не реализован — это заглушка из бандла"
            " VideoText. Шаги алгоритма — в докстринге.", ensure_ascii=False)
        out.append(f"@mcp.tool()\ndef {name}(payload: dict) -> dict:\n{doc}"
                   f"    raise NotImplementedError({msg})\n")
    return "\n\n".join(out)


def _mcp_server_py(skills: list[dict], algorithms: list[dict] | None = None) -> str:
    """stdio MCP server: prompts are complete, one honest tool stub per algorithm."""
    entries = json.dumps(
        [{"slug": s["slug"], "title": s["title"], "description": s["description"]}
         for s in skills],
        ensure_ascii=False, indent=4,
    )
    algo_tools = _algo_tools_src(algorithms or [])
    return f'''"""Generated MCP server — skills as prompts, algorithms as tool stubs.

Run:  python server.py     (stdio transport)

The prompts below are complete: each one serves the full SKILL.md text.
The tools are stubs on purpose — one per algorithm from the source artifact, each
carrying that algorithm's steps in its docstring. The artifact describes WHAT to do,
not the code. Fill in each body, or hand this file to an agent to implement.
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


{algo_tools}

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
        "- `mcp_server/` — MCP-сервер: промпты рабочие, на каждый алгоритм —\n"
        "  тул-заглушка с его шагами в докстринге.\n\n"
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
        zf.writestr("mcp_server/server.py",
                    _mcp_server_py(skills, parse_algorithms(algorithms_md)))
        # Upper bound is load-bearing: mcp 2.x renamed FastMCP, so an unpinned
        # install makes the README's own start command fail with ImportError.
        zf.writestr("mcp_server/requirements.txt", "mcp>=1.2.0,<2\n")
        zf.writestr("README.md", _readme(video_title, skills, spec_md, algorithms_md))
    return buf.getvalue()
