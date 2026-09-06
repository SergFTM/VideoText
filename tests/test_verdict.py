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


def test_marker_inside_a_fenced_block_is_ignored():
    """The report prompt lists all three markers with `refuted` last, so a report
    that restates the legend in a code block would otherwise hard-block ТЗ."""
    md = (
        "# Репорт\n\n"
        "<!-- verdict: confirmed -->\n\n"
        "Приложение: формат маркера.\n"
        "```\n"
        "<!-- verdict: confirmed -->  — если проблематика подтверждена\n"
        "<!-- verdict: partial -->    — если подтверждена частично\n"
        "<!-- verdict: refuted -->    — если не подтверждена\n"
        "```\n"
    )
    assert pipeline.parse_verdict(md) == "confirmed"


def test_only_fenced_markers_means_no_verdict():
    md = "```\n<!-- verdict: refuted -->\n```\n"
    assert pipeline.parse_verdict(md) is None


def test_real_marker_after_a_fenced_block_still_wins():
    md = (
        "```\n<!-- verdict: confirmed -->\n```\n\n"
        "Вывод по существу.\n\n"
        "<!-- verdict: refuted -->\n"
    )
    assert pipeline.parse_verdict(md) == "refuted"


def test_valid_marker_followed_by_a_bogus_one():
    """A bogus trailing value must not erase the real verdict — the regex only
    matches the three known values, so last-wins picks the last VALID marker."""
    md = "<!-- verdict: partial -->\n\n<!-- verdict: maybe -->\n"
    assert pipeline.parse_verdict(md) == "partial"
