"""Unit tests for transcript-edit prompt routing.

`build_edit_prompt` is pure (no DB/network), so we assert op → system-prompt
wiring directly. Guards against an op silently falling back to the wrong prompt.
"""
import transcript_edit as te

_OPS = ["improve", "structure", "clean", "chat"]


def _build(op):
    return te.build_edit_prompt(
        op=op,
        current_text="Приветствую, уважаемые трейдеры. Робот строит спред.",
        instruction="разбей по темам",
    )


def test_all_ops_registered():
    for op in _OPS:
        assert op in te.SYSTEM_PROMPTS, f"{op} missing from SYSTEM_PROMPTS"


def test_ops_select_their_own_system_prompt():
    for op in _OPS:
        system, _user = _build(op)
        assert system == te.SYSTEM_PROMPTS[op]


def test_user_message_carries_text_and_instruction():
    _system, user = _build("structure")
    assert "Приветствую, уважаемые трейдеры" in user
    assert "разбей по темам" in user


def test_unknown_op_falls_back_to_improve():
    system, _ = te.build_edit_prompt(op="bogus", current_text="x", instruction="")
    assert system == te.SYSTEM_PROMPTS["improve"]


def test_is_claude_recognises_claude_ids_and_aliases():
    assert te._is_claude("claude-sonnet-4-6")
    assert te._is_claude("sonnet")        # alias from brief._MODEL_ALIASES
    assert te._is_claude("")              # empty -> default Claude
    assert not te._is_claude("qwen2.5:7b")
    assert not te._is_claude("nomic-embed-text:latest")
