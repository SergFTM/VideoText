"""Unit tests for the local-LLM expand-mode routing.

`build_expand_prompt` is a pure function (no DB, no network), so we can assert
its mode → (system_prompt, instruction) wiring directly. These guard the new
AI-oriented deliverables — `ai_skills` and `ai_algorithms` — against silently
falling back to the generic `spec` prompt if a key is dropped or misspelled.
"""
import local_llm


_NEW_MODES = ["ai_skills", "ai_algorithms"]


def _build(mode: str):
    return local_llm.build_expand_prompt(
        mode=mode,
        video_title="Арбитражный робот на крипте",
        section_title="ТЗ",
        section_md="Робот строит линии по правилам и сравнивает цены на биржах.",
        software_brief_json=None,
        full_brief_md="## Суть\nАрбитраж между биржами.",
        transcript_excerpt="Берём цену на бирже A и цену на бирже B...",
    )


def test_new_modes_are_registered():
    for mode in _NEW_MODES:
        assert mode in local_llm.SYSTEM_PROMPTS, f"{mode} missing from SYSTEM_PROMPTS"


def test_new_modes_select_their_own_system_prompt():
    """A new mode must NOT silently fall back to the generic spec prompt."""
    spec_system = local_llm.SYSTEM_PROMPTS["spec"]
    for mode in _NEW_MODES:
        system, _user = _build(mode)
        assert system == local_llm.SYSTEM_PROMPTS[mode]
        assert system != spec_system, f"{mode} fell back to the spec prompt"


def test_new_modes_have_distinct_instructions():
    """The user-message instruction line is mode-specific, not the spec default."""
    _, spec_user = _build("spec")
    for mode in _NEW_MODES:
        _system, user = _build(mode)
        assert user != spec_user, f"{mode} reused the spec instruction verbatim"


def test_ai_skills_prompt_is_skill_oriented():
    system = local_llm.SYSTEM_PROMPTS["ai_skills"]
    assert "скилл" in system.lower()


def test_ai_algorithms_prompt_is_algorithm_oriented():
    system = local_llm.SYSTEM_PROMPTS["ai_algorithms"]
    assert "алгоритм" in system.lower()
