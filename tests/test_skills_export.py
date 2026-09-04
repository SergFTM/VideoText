"""Turning the ai_skills artifact into a directory tree someone can actually run."""
import io
import zipfile

import skills_export

SAMPLE = """## Скилл 1. Rolling Universe Scan

**Slug:** rolling-universe-scan
**Описание:** Формирует корзину торгуемых пар без look-ahead bias.

**Когда использовать:**
- Нужна корзина на каждый месяц.

## Скилл 2. Momentum Score

**Slug:** momentum-score
**Описание:** Считает score по нескольким горизонтам.

**Когда использовать:**
- Корзина уже собрана.
"""


def test_parses_every_skill():
    skills = skills_export.parse_skills(SAMPLE)
    assert [s["slug"] for s in skills] == ["rolling-universe-scan", "momentum-score"]
    assert skills[0]["title"] == "Rolling Universe Scan"
    assert "look-ahead" in skills[0]["description"]


def test_body_excludes_the_next_skill():
    skills = skills_export.parse_skills(SAMPLE)
    assert "Momentum Score" not in skills[0]["body"]


def test_slug_falls_back_to_transliterated_title():
    md = "## Скилл 1. Проверка данных\n\nБез слага.\n"
    assert skills_export.parse_skills(md)[0]["slug"] == "proverka-dannyh"


def test_empty_input_yields_nothing():
    assert skills_export.parse_skills("") == []
    assert skills_export.parse_skills("Просто текст без заголовков") == []


def test_bundle_layout_and_frontmatter():
    skills = skills_export.parse_skills(SAMPLE)
    blob = skills_export.build_bundle(
        skills, spec_md="# ТЗ", algorithms_md="# Алгоритмы", video_title="Rough Volatility")
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = set(zf.namelist())
    assert "skills/rolling-universe-scan/SKILL.md" in names
    assert "skills/momentum-score/SKILL.md" in names
    assert "mcp_server/server.py" in names
    assert "mcp_server/requirements.txt" in names
    assert "README.md" in names

    skill_md = zf.read("skills/rolling-universe-scan/SKILL.md").decode()
    assert skill_md.startswith("---\n")
    assert "name: rolling-universe-scan" in skill_md
    assert "description:" in skill_md


def test_readme_carries_spec_and_algorithms():
    blob = skills_export.build_bundle(
        skills_export.parse_skills(SAMPLE),
        spec_md="# ТЗ\nсодержимое тз", algorithms_md="# Алгоритмы\nсодержимое алгоритмов",
        video_title="T")
    readme = zipfile.ZipFile(io.BytesIO(blob)).read("README.md").decode()
    assert "содержимое тз" in readme
    assert "содержимое алгоритмов" in readme


def test_generated_mcp_server_is_valid_python():
    blob = skills_export.build_bundle(
        skills_export.parse_skills(SAMPLE), spec_md="", algorithms_md="", video_title="T")
    src = zipfile.ZipFile(io.BytesIO(blob)).read("mcp_server/server.py").decode()
    compile(src, "server.py", "exec")  # syntax must be valid, not just plausible
