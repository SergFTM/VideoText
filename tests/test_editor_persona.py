from assistant.personas.editor import EditorPersona


def test_editor_persona_name():
    assert EditorPersona().name == "editor"


def test_editor_persona_tool_names():
    p = EditorPersona()
    expected = {
        "recognize_text", "improve_headline", "rewrite_quote",
        "expand_text", "regenerate_image", "suggest_tags", "bulk_action",
    }
    assert expected.issubset(set(p.tool_names))


def test_editor_persona_forbids_platform_tools():
    p = EditorPersona()
    assert "save_setting" not in p.tool_names
    assert "save_api_key" not in p.tool_names
    assert "test_integration" not in p.tool_names


def test_editor_persona_has_system_prompt():
    p = EditorPersona()
    # Editor prompt must mention news items and forbid settings
    s = p.system_prompt
    assert len(s) > 200
    assert "news" in s.lower() or "новост" in s.lower()
