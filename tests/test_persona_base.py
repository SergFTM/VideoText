from assistant.personas.platform import PlatformPersona

def test_platform_persona_name():
    p = PlatformPersona()
    assert p.name == "platform"

def test_platform_persona_has_system_prompt():
    p = PlatformPersona()
    assert len(p.system_prompt) > 200  # non-trivial prompt

def test_platform_persona_tool_names():
    p = PlatformPersona()
    # The platform persona owns integration + settings + error tools
    assert "test_integration" in p.tool_names
    assert "save_setting" in p.tool_names
    assert "save_api_key" in p.tool_names
    assert "get_recent_errors" in p.tool_names

def test_platform_persona_forbids_editor_tools():
    p = PlatformPersona()
    assert "improve_headline" not in p.tool_names
    assert "recognize_text" not in p.tool_names
