import pipeline


def test_stage_order_and_upstream_graph():
    assert pipeline.STAGE_ORDER == ["research", "report", "spec", "uiux", "ai_algorithms", "ai_skills"]
    assert pipeline.UPSTREAM["report"] == ["research"]
    assert pipeline.UPSTREAM["spec"] == ["report"]
    assert pipeline.UPSTREAM["uiux"] == ["spec"]
    assert pipeline.UPSTREAM["ai_algorithms"] == ["spec", "uiux"]
    assert pipeline.UPSTREAM["ai_skills"] == ["spec", "ai_algorithms"]
    assert pipeline.UPSTREAM["research"] == []


def test_gate_predecessor_chain_excludes_optional_uiux():
    assert pipeline.GATE_PREDECESSOR["report"] == "research"
    assert pipeline.GATE_PREDECESSOR["spec"] == "report"
    assert pipeline.GATE_PREDECESSOR["ai_algorithms"] == "spec"   # uiux skipped (optional)
    assert pipeline.GATE_PREDECESSOR["ai_skills"] == "ai_algorithms"
    assert "uiux" not in pipeline.GATE_PREDECESSOR


def test_checklists_present_for_gated_stages_only():
    assert set(pipeline.CHECKLISTS) == {"research", "report", "spec", "ai_algorithms"}
    assert len(pipeline.CHECKLISTS["research"]) == 5
    assert all(len(item) == 2 for item in pipeline.CHECKLISTS["research"])  # (key, label)


def test_build_assess_prompt_includes_artifact_and_keys():
    system, user = pipeline.build_assess_prompt("research", "Вот ресерч: варианты A и B.")
    assert "чеклист" in system.lower()        # system is stage-agnostic
    assert "research" in user                  # stage name appears in the user message
    assert "Вот ресерч" in user
    for key, _label in pipeline.CHECKLISTS["research"]:
        assert key in user


def test_parse_assessment_maps_to_items_with_labels():
    raw = {
        "domain": {"checked": True, "note": "есть"},
        "options": {"checked": False, "note": "только 1"},
    }
    items = pipeline.parse_assessment("research", raw)
    by_key = {i["key"]: i for i in items}
    assert by_key["domain"]["checked"] is True
    assert by_key["options"]["checked"] is False
    assert by_key["options"]["ai_note"] == "только 1"
    # every checklist item is represented, missing keys default to unchecked
    assert {i["key"] for i in items} == {k for k, _ in pipeline.CHECKLISTS["research"]}
    assert by_key["limits"]["checked"] is False
    assert by_key["domain"]["label"]  # label carried through
