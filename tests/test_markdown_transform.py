"""Tests for rendering markdown as something worth hearing."""

from speakd.model import Piece, Span
from speakd.transforms.markdown import markdown


def render(text: str) -> str:
    pieces = markdown([Piece(span=Span(0, len(text)), spoken=text)])
    return pieces[0].spoken if pieces else ""


def test_headings_become_sentences() -> None:
    assert render("## Results") == "Results."


def test_a_heading_that_already_ends_in_punctuation_is_left_alone() -> None:
    assert render("## Really?") == "Really?"


def test_bullets_become_sentences() -> None:
    assert render("- first\n- second") == "first. second."


def test_ordered_items_become_sentences() -> None:
    assert render("1. first\n2. second") == "first. second."


def test_fenced_code_is_replaced_by_a_marker_in_place() -> None:
    assert render("Before.\n```python\nx = 1\n```\nAfter.") == "Before. Code block omitted. After."


def test_a_loose_list_items_indented_continuation_paragraph_is_read_as_prose() -> None:
    # Indented (four-space) code blocks are deliberately not detected: a
    # correct detector needs list-context tracking to avoid mistaking a loose
    # list item's continuation paragraph for one, and that tradeoff was
    # judged not worth it (see the module docstring). This test pins the
    # accepted behaviour so nobody re-adds detection without noticing the
    # regression it previously caused here.
    assert render("- Do X.\n\n    This paragraph explains why X matters.\n\n- Do Y.") == (
        "Do X. This paragraph explains why X matters. Do Y."
    )


def test_inline_code_keeps_its_text() -> None:
    assert render("Run `pytest` now.") == "Run pytest now."


def test_links_keep_their_text_and_drop_the_url() -> None:
    assert render("See [the spec](https://example.com/a).") == "See the spec."


def test_emphasis_markers_are_removed() -> None:
    assert render("This is **bold** and _italic_ and *starred*.") == (
        "This is bold and italic and starred."
    )


def test_blockquote_markers_are_removed() -> None:
    assert render("> quoted text.") == "quoted text."


def test_horizontal_rules_are_dropped() -> None:
    assert render("Before.\n---\nAfter.") == "Before. After."


def test_output_is_marked_inexact() -> None:
    pieces = markdown([Piece(span=Span(0, 9), spoken="## Results")])
    assert pieces[0].exact is False
    assert pieces[0].span == Span(0, 9)


def test_a_piece_that_renders_to_nothing_is_dropped() -> None:
    assert markdown([Piece(span=Span(0, 3), spoken="---")]) == []
