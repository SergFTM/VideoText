"""The research stage must verify, not retell; the report must reconcile.

These assert the prompt contract, not model behaviour — the point is that a weak
model gets the same instructions as Opus.
"""
import local_llm


def _build(mode, web_search_available=True):
    return local_llm.build_expand_prompt(
        mode=mode, video_title="Rough Volatility",
        section_title="бриф", section_md="",
        software_brief_json=None,
        full_brief_md="## Суть\nЛекция про rough volatility.",
        transcript_excerpt="наклон 0,1555 ... приращение в 1,21 раза",
        web_search_available=web_search_available,
    )


def test_research_demands_claim_verification():
    s = local_llm.SYSTEM_PROMPTS["research"].lower()
    assert "проверка утверждений" in s
    assert "арифметическ" in s
    assert "ангажирован" in s


def test_research_forbids_self_confirmation():
    s = local_llm.SYSTEM_PROMPTS["research"].lower()
    assert "сам себя" in s or "самого себя" in s


def test_research_demands_literal_source_urls():
    """A research artifact whose sources can't be followed isn't verified — the
    model must write the URL out as text, not just cite a name or a domain."""
    s = local_llm.SYSTEM_PROMPTS["research"].lower()
    assert "url" in s
    assert "https://" in s


def test_report_demands_reconciliation_and_verdict():
    s = local_llm.SYSTEM_PROMPTS["report"].lower()
    assert "сверка" in s
    assert "вердикт" in s
    assert "<!-- verdict:" in s


def test_report_prompt_lists_all_three_verdict_values():
    s = local_llm.SYSTEM_PROMPTS["report"]
    for value in ("confirmed", "partial", "refuted"):
        assert value in s


def test_research_degrades_honestly_without_search():
    system_off, _ = _build("research", web_search_available=False)
    assert "не проверено" in system_off
    system_on, _ = _build("research", web_search_available=True)
    assert system_on != system_off


def test_degradation_only_touches_research():
    for mode in ("report", "spec", "uiux", "ai_algorithms", "ai_skills"):
        on, _ = _build(mode, web_search_available=True)
        off, _ = _build(mode, web_search_available=False)
        assert on == off, f"{mode} не должен зависеть от доступности поиска"


def test_algorithms_demand_core_modules_and_ml_judgement():
    s = local_llm.SYSTEM_PROMPTS["ai_algorithms"].lower()
    assert "вычислительное ядро" in s
    assert "модульная декомпозиция" in s
    assert "ml" in s


def test_algorithms_allow_saying_no_to_ml():
    """Without an explicit out, models bolt ML onto problems that need an if."""
    s = local_llm.SYSTEM_PROMPTS["ai_algorithms"].lower()
    assert "правил достаточно" in s


def test_skills_prompt_pins_machine_parsable_shape():
    s = local_llm.SYSTEM_PROMPTS["ai_skills"]
    assert "## Скилл N." in s
    assert "**Slug:**" in s
    assert "Инструменты MCP" in s
