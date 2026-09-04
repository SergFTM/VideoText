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


def test_duplicate_slugs_get_unique_files():
    skills = [
        {"slug": "dup-slug", "title": "First", "description": "First one", "body": "first body"},
        {"slug": "dup-slug", "title": "Second", "description": "Second one", "body": "second body"},
    ]
    blob = skills_export.build_bundle(skills, spec_md="", algorithms_md="", video_title="T")
    zf = zipfile.ZipFile(io.BytesIO(blob))
    skill_files = sorted(n for n in zf.namelist() if n.startswith("skills/") and n.endswith("SKILL.md"))
    assert skill_files == ["skills/dup-slug-2/SKILL.md", "skills/dup-slug/SKILL.md"]

    first = zf.read("skills/dup-slug/SKILL.md").decode()
    second = zf.read("skills/dup-slug-2/SKILL.md").decode()
    assert "first body" in first and "second body" not in first
    assert "second body" in second and "first body" not in second
    assert "name: dup-slug\n" in first
    assert "name: dup-slug-2\n" in second

    server_src = zf.read("mcp_server/server.py").decode()
    assert '"slug": "dup-slug"' in server_src
    assert '"slug": "dup-slug-2"' in server_src
    compile(server_src, "server.py", "exec")


def test_heading_inside_fenced_code_block_is_ignored():
    md = (
        "## Скилл 1. Real Skill\n\n"
        "**Slug:** real-skill\n"
        "**Описание:** Настоящий скилл с примером кода.\n\n"
        "Пример вывода:\n"
        "```\n"
        "## Скилл 99. Fake Heading Inside Fence\n"
        "```\n\n"
        "Конец блока.\n"
    )
    skills = skills_export.parse_skills(md)
    assert len(skills) == 1
    assert skills[0]["slug"] == "real-skill"
    assert "Fake Heading Inside Fence" in skills[0]["body"]
    assert "Конец блока" in skills[0]["body"]


# ─── algorithms → one MCP tool stub each (spec §8) ─────────────────

ALGOS = """## Алгоритм 1. Rebalance Basket

**Тип:** системный
**Цель:** Пересобрать корзину.

**Шаги:**
1. Загрузить последний скоринг.
2. Отсечь хвост по порогу.

## Алгоритм 2. Проверка данных

**Шаги:**
1. Сверить длины рядов.
"""


def _server_src(algorithms_md, skills_md=SAMPLE):
    blob = skills_export.build_bundle(
        skills_export.parse_skills(skills_md), spec_md="",
        algorithms_md=algorithms_md, video_title="T")
    return zipfile.ZipFile(io.BytesIO(blob)).read("mcp_server/server.py").decode()


def test_parses_every_algorithm():
    algos = skills_export.parse_algorithms(ALGOS)
    assert [a["title"] for a in algos] == ["Rebalance Basket", "Проверка данных"]
    assert [a["slug"] for a in algos] == ["rebalance-basket", "proverka-dannyh"]
    assert "Отсечь хвост" in algos[0]["body"]
    assert "Проверка данных" not in algos[0]["body"]


def test_algorithm_heading_inside_a_fence_is_ignored():
    md = ("## Алгоритм 1. Real\n\nШаг.\n```\n## Алгоритм 99. Fake\n```\nКонец.\n")
    algos = skills_export.parse_algorithms(md)
    assert len(algos) == 1
    assert "Fake" in algos[0]["body"]


def test_one_tool_stub_per_algorithm_with_its_steps():
    src = _server_src(ALGOS)
    assert "def rebalance_basket(payload: dict) -> dict:" in src
    assert "def proverka_dannyh(payload: dict) -> dict:" in src
    assert "Отсечь хвост по порогу." in src
    assert "Сверить длины рядов." in src
    # honest stub: nothing is dressed up as implemented
    assert src.count("raise NotImplementedError(") == 2
    assert "def run_algorithm(" not in src
    assert "def list_skills()" in src
    compile(src, "server.py", "exec")


def test_generated_tools_are_real_module_level_functions():
    """Not just text: the tool names must exist as top-level defs in the AST."""
    import ast
    tree = ast.parse(_server_src(ALGOS))
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert "rebalance_basket" in names and "proverka_dannyh" in names


def test_duplicate_and_non_ascii_algorithm_names_stay_valid_and_unique():
    md = ("## Алгоритм 1. Сбор\n\nA.\n\n"
          "## Алгоритм 2. Сбор\n\nB.\n\n"
          "## Алгоритм 3. 日本語 ‼\n\nC.\n\n"
          "## Алгоритм 4. list_skills\n\nD.\n\n"
          "## Алгоритм 5. class\n\nE.\n\n"
          "## Алгоритм 6. 7 шагов\n\nF.\n")
    src = _server_src(md)
    compile(src, "server.py", "exec")
    import ast
    names = [n.name for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)]
    tools = [n for n in names if n not in ("_skill_text", "_make", "_prompt")]
    assert len(tools) == len(set(tools)), f"имена тулов столкнулись: {tools}"
    assert names.count("list_skills") == 1, "алгоритм не должен затирать list_skills"
    assert all(n.isidentifier() for n in names)


def test_algorithm_body_with_quotes_and_backslashes_still_compiles():
    md = ('## Алгоритм 1. Экранирование\n\n'
          'Внутри: """ и путь C:' + '\\' + 'dir' + '\\' + 'file и хвостовая кавычка "\n')
    compile(_server_src(md), "server.py", "exec")


def test_algorithm_heading_colon_form_parses():
    md = ("## Алгоритм 1: Rebalance Basket\n\n"
          "**Шаги:**\n1. Загрузить последний скоринг.\n2. Отсечь хвост по порогу.\n")
    algos = skills_export.parse_algorithms(md)
    assert len(algos) == 1
    assert algos[0]["title"] == "Rebalance Basket"
    assert algos[0]["slug"] == "rebalance-basket"
    assert "Отсечь хвост по порогу." in algos[0]["body"]


def test_algorithm_heading_h1_form_parses():
    md = ("# Алгоритм 1. Rebalance Basket\n\n"
          "**Шаги:**\n1. Загрузить последний скоринг.\n")
    algos = skills_export.parse_algorithms(md)
    assert len(algos) == 1
    assert algos[0]["title"] == "Rebalance Basket"
    assert algos[0]["slug"] == "rebalance-basket"
    assert "Загрузить последний скоринг." in algos[0]["body"]


def test_algorithm_heading_h3_closing_paren_form_parses():
    md = ("### Алгоритм 1) Rebalance Basket\n\n"
          "**Шаги:**\n1. Загрузить последний скоринг.\n")
    algos = skills_export.parse_algorithms(md)
    assert len(algos) == 1
    assert algos[0]["title"] == "Rebalance Basket"
    assert algos[0]["slug"] == "rebalance-basket"
    assert "Загрузить последний скоринг." in algos[0]["body"]


def test_bundle_without_algorithms_is_still_valid():
    for algorithms_md in ("", "Просто текст без заголовков алгоритмов"):
        src = _server_src(algorithms_md)
        compile(src, "server.py", "exec")
        assert "def list_skills()" in src
        assert "NotImplementedError" not in src
        assert "Алгоритмы не найдены" in src
