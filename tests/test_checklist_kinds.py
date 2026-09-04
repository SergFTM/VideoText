"""Checklist items now carry a kind: some are AI-assessable, some are a human's call.

The bug this prevents: an AI assessment wiping the human's "принято к разработке"
tick every time the user re-runs it.
"""
import pipeline


def test_every_item_is_a_triple_with_valid_kind():
    for stage, items in pipeline.CHECKLISTS.items():
        for item in items:
            assert len(item) == 3, f"{stage}: ожидалась тройка (key, label, kind)"
            assert item[2] in ("ai", "human"), f"{stage}/{item[0]}: неизвестный kind"


def test_report_accepted_is_a_human_decision():
    kinds = {k: kind for k, _label, kind in pipeline.CHECKLISTS["report"]}
    assert kinds["accepted"] == "human", "согласование живёт вне текста артефакта"


def test_all_stages_have_checklists():
    assert set(pipeline.CHECKLISTS) == {
        "research", "report", "spec", "uiux", "ai_algorithms", "ai_skills"}


def test_research_has_verification_item():
    keys = [k for k, _l, _kind in pipeline.CHECKLISTS["research"]]
    assert "verified" in keys


def test_report_has_reconciliation_item():
    keys = [k for k, _l, _kind in pipeline.CHECKLISTS["report"]]
    assert "reconciled" in keys


def test_algorithms_single_item_no_longer_contradicts_the_prompt():
    """The prompt asks for 1-4 algorithms; the criterion must judge each one."""
    label = next(l for k, l, _kind in pipeline.CHECKLISTS["ai_algorithms"] if k == "single")
    assert "аждый" in label


def test_assess_prompt_hides_human_items_from_the_model():
    _system, user = pipeline.build_assess_prompt("report", "текст репорта")
    assert "reconciled" in user
    assert "accepted" not in user, "человеческий пункт не отдаём модели"


def test_parse_assessment_preserves_human_checked():
    previous = [{"key": "accepted", "checked": True, "ai_note": ""}]
    items = pipeline.parse_assessment("report", {"verdict": {"checked": True, "note": "ok"}},
                                      previous=previous)
    by_key = {i["key"]: i for i in items}
    assert by_key["accepted"]["checked"] is True, "AI-оценка стёрла решение человека"
    assert by_key["verdict"]["checked"] is True


def test_parse_assessment_human_defaults_false_without_previous():
    items = pipeline.parse_assessment("report", {})
    by_key = {i["key"]: i for i in items}
    assert by_key["accepted"]["checked"] is False


def test_parse_assessment_carries_kind_out():
    items = pipeline.parse_assessment("report", {})
    assert all("kind" in i for i in items)
