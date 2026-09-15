"""Tests for rendering markdown as something worth hearing."""

from speakd.model import Piece, Span
from speakd.transforms.markdown import markdown


def render(text: str) -> str:
    pieces = markdown([Piece(span=Span(0, len(text)), spoken=text)])
    return " ".join(piece.spoken for piece in pieces)


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


def test_horizontal_rules_are_dropped() -> None:
    assert render("Before.\n---\nAfter.") == "Before. After."


def test_output_is_marked_inexact() -> None:
    source = "## Results"
    pieces = markdown([Piece(span=Span(0, len(source)), spoken=source)])
    assert pieces[0].exact is False
    assert pieces[0].span == Span(0, len(source))


def test_a_piece_that_renders_to_nothing_is_dropped() -> None:
    assert markdown([Piece(span=Span(0, 3), spoken="---")]) == []


def blocks(text: str) -> list[str]:
    return [piece.spoken for piece in markdown([Piece(span=Span(0, len(text)), spoken=text)])]


def test_each_paragraph_becomes_its_own_piece() -> None:
    assert blocks("First para.\n\nSecond para.") == ["First para.", "Second para."]


def test_wrapped_lines_stay_one_paragraph() -> None:
    # A blank line separates paragraphs; a newline inside one does not.
    assert blocks("One sentence\nwrapped over lines.") == ["One sentence wrapped over lines."]


def test_each_bullet_is_its_own_piece() -> None:
    assert blocks("- first\n- second") == ["first.", "second."]


def test_a_heading_is_its_own_piece() -> None:
    assert blocks("## Results\n\nThe body.") == ["Results.", "The body."]


def test_a_code_fence_is_one_piece() -> None:
    assert blocks("Before.\n```py\nx = 1\n```\nAfter.") == [
        "Before.",
        "Code block omitted.",
        "After.",
    ]


def test_a_blocks_span_points_at_the_text_it_came_from() -> None:
    source = "First para.\n\nSecond para."
    pieces = markdown([Piece(span=Span(0, len(source)), spoken=source)])
    second = pieces[1]
    assert source[second.span.start : second.span.end].strip() == "Second para."


def test_spans_are_offset_by_the_input_pieces_own_start() -> None:
    # The piece is a window into a larger job, so block offsets are relative
    # to the piece's start, not to zero.
    source = "First para.\n\nSecond para."
    pieces = markdown([Piece(span=Span(100, 100 + len(source)), spoken=source)])
    assert pieces[0].span.start == 100
    assert pieces[1].span.start > 100


def test_an_inexact_piece_gives_every_block_the_whole_span() -> None:
    # Offsets into rewritten text do not correspond to the source, so
    # sub-spans would point at the wrong characters. Better to be coarse
    # than wrong -- see Piece.exact in model.py.
    source = "First para.\n\nSecond para."
    piece = Piece(span=Span(0, 999), spoken=source, exact=False)
    assert [p.span for p in markdown([piece])] == [Span(0, 999), Span(0, 999)]


def test_a_block_quote_is_announced_and_closed() -> None:
    assert blocks("> Nothing waits on that.") == ["Quote, Nothing waits on that. End quote."]


def test_consecutive_quote_lines_are_one_quotation() -> None:
    assert blocks("> One line.\n> And another.") == ["Quote, One line. And another. End quote."]


def test_prose_after_a_quote_is_its_own_block() -> None:
    assert blocks("> Quoted.\n\nMine.") == ["Quote, Quoted. End quote.", "Mine."]


def test_a_table_is_announced_rather_than_read() -> None:
    table = "| a | b |\n| --- | --- |\n| 1 | 2 |"
    assert blocks(table) == ["Table omitted."]


def test_a_table_between_paragraphs_keeps_them_apart() -> None:
    source = "Before.\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\nAfter."
    assert blocks(source) == ["Before.", "Table omitted.", "After."]


def test_a_pipe_in_prose_is_not_a_table() -> None:
    # A table row is delimited at both ends; prose mentioning a pipe is not.
    assert blocks("Use a | to pipe.") == ["Use a | to pipe."]
