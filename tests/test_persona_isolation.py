from assistant.core.tools_runtime import execute
from assistant.personas.platform import PlatformPersona

def test_platform_can_call_own_tool():
    p = PlatformPersona()
    result = execute("get_settings_snapshot", {}, p)
    # don't care about data here — only that it's permitted
    assert "error" not in result or "not allowed" not in result.get("error", "").lower()

def test_platform_rejects_editor_tool():
    p = PlatformPersona()
    result = execute("improve_headline", {"item_id": 1}, p)
    assert result["ok"] is False
    assert "not allowed" in result["error"].lower()

def test_unknown_tool_rejected():
    p = PlatformPersona()
    result = execute("totally_made_up_tool", {}, p)
    assert result["ok"] is False
