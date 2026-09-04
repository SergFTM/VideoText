"""The one machine-readable contract in an otherwise free-form markdown artifact."""
import pipeline


def test_parses_each_valid_value():
    for value in ("confirmed", "partial", "refuted"):
        assert pipeline.parse_verdict(f"# Репорт\n\n<!-- verdict: {value} -->") == value


def test_missing_marker_returns_none():
    assert pipeline.parse_verdict("# Репорт\n\nНикакого маркера тут нет.") is None


def test_empty_text_returns_none():
    assert pipeline.parse_verdict("") is None
    assert pipeline.parse_verdict(None) is None


def test_last_marker_wins():
    """A model may quote the format in its own prose before emitting the real one."""
    md = ("Формат такой: <!-- verdict: confirmed -->\n"
          "...текст репорта...\n"
          "<!-- verdict: refuted -->")
    assert pipeline.parse_verdict(md) == "refuted"


def test_case_and_spacing_tolerant():
    assert pipeline.parse_verdict("<!--verdict:REFUTED-->") == "refuted"
    assert pipeline.parse_verdict("<!--   verdict:   partial   -->") == "partial"


def test_unknown_value_is_ignored():
    assert pipeline.parse_verdict("<!-- verdict: maybe -->") is None
