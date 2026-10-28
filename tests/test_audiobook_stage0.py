"""Stage 0 characterisation of the skill path's PDF extraction (Task 2).

A real two-column academic layout, generated with PyMuPDF, and the order the
current extraction actually produces on it. The assertions are a
**characterisation, not an endorsement**: they record which order poppler's
default mode really produces, so the decision they drive (whether
prepare_text.py needs the `mutool stext` extractor,
docs/design/2026-10-08-pdf-text-cleanup-stage0.md) is reproducible. PyMuPDF
is not a project dependency; without it these tests skip, and the recorded
outcome in the Stage 0 notes is the decision record. Making them always-run
is a dependency decision for the owner.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_audiobook_prepare import prep

WORDS = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima".split()
# The sentence planted to cross page 1's column break: any reading-order fix
# must keep it intact and in order.
CROSS_FIRST = "The cross column sentence begins"
CROSS_SECOND = "and ends in the rightmost column."
PAGES = 8
LINES = 46  # lines per column, ~720 pt tall columns on an A4 page
COL_X = (54, 311)  # left and right column origins
HEADER = "A Tale of Two Columns"


def _lines(paragraph: int, n: int) -> list[str]:
    """`n` short body lines for paragraph `paragraph`, distinct per paragraph."""
    out = []
    for i in range(n):
        words = [WORDS[(paragraph * 31 + i * 7 + j) % len(WORDS)] for j in range(6)]
        out.append(f"P{paragraph} L{i} {' '.join(words)}.")
    return out


def make_two_column_pdf(fitz: Any, path: Path) -> None:
    """The layout a two-column academic paper has: two equal columns, a
    running header, a page number per page, and one sentence planted to start
    at the bottom of page 1's left column and end at the top of its right."""
    doc = fitz.open()
    paragraph = 0
    for page_index in range(PAGES):
        page = doc.new_page()
        page.insert_text((72, 30), HEADER, fontsize=9)
        page.insert_text((285, 820), str(page_index + 1), fontsize=9)
        for col in range(2):
            for line in range(LINES):
                if page_index == 0 and col == 0 and line == LINES - 1:
                    text = CROSS_FIRST
                elif page_index == 0 and col == 1 and line == 0:
                    text = CROSS_SECOND
                else:
                    text = _lines(paragraph, 3)[line % 3]
                    if line % 3 == 2:
                        paragraph += 1
                page.insert_text((COL_X[col], 60 + line * 16), text, fontsize=10)
    doc.save(path, garbage=4, deflate=True)


@pytest.fixture(scope="module")
def two_column_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The two-column fixture, generated with PyMuPDF: not a project
    dependency, so without it the real-PDF tests skip -- the recorded outcome
    in the Stage 0 notes is the decision record, and making them always-run
    needs a dependency decision from the owner."""
    fitz: Any = pytest.importorskip(
        "fitz", reason="generates the two-column fixture; PyMuPDF is optional"
    )
    pdf = tmp_path_factory.mktemp("stage0") / "twocolumn.pdf"
    make_two_column_pdf(fitz, pdf)
    return pdf


def _page1_lines(pdf: Path) -> list[str]:
    """Poppler's default-mode output for page 1, one entry per non-blank line."""
    raw = subprocess.run(
        ["pdftotext", "-f", "1", "-l", "1", str(pdf), "-"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in raw.splitlines() if line.strip()]


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="needs poppler tools")
def test_pdftotext_default_mode_interleaves_two_columns(two_column_pdf: Path) -> None:
    """Characterisation: poppler's default mode reads the two-column layout
    line-band by line-band -- the right column's top line right after the left
    column's first -- not column by column."""
    lines = _page1_lines(two_column_pdf)
    assert lines[0] == HEADER
    assert lines[1].startswith("P0 L0")
    # The right column's top, interleaved with the left column's first line.
    assert lines[2] == CROSS_SECOND
    assert lines[3].startswith("P0 L1")


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="needs poppler tools")
def test_pdftotext_splits_the_cross_column_sentence(two_column_pdf: Path) -> None:
    """Characterisation: the sentence that crosses the column break comes out
    split across the whole page -- its left half near the page's end, its
    right half near its top."""
    lines = _page1_lines(two_column_pdf)
    first = lines.index(CROSS_FIRST)
    second = lines.index(CROSS_SECOND)
    assert second < 5
    assert first > len(lines) - 8
    assert first - second > 40


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="needs poppler tools")
def test_prepare_pdf_fallback_inherits_popplers_interleaved_order(
    two_column_pdf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Characterisation of the `pdftotext` fallback (the stext extractor has
    landed, per Stage 0's recorded branch): mutool absent, the pipeline is
    poppler's own order -- line-band interleaving, the cross sentence's
    halves separated by most of the paper, its second half read before its
    first. A characterisation, not an endorsement: it records which order
    poppler's default mode produces, and the degraded fallback inherits it."""
    monkeypatch.setattr(prep, "_stext_pages_or_none", lambda pdf, pages, footnotes: None)
    parts = prep.prepare_pdf(two_column_pdf, None, "none", True)
    text = "\n".join(parts)
    assert HEADER not in text
    first = text.index(CROSS_FIRST)
    second = text.index(CROSS_SECOND)
    assert second < first
    assert first - second > 4000


@pytest.mark.skipif(
    shutil.which("mutool") is None or shutil.which("pdftotext") is None,
    reason="needs poppler and mupdf tools",
)
def test_prepare_pdf_reads_two_columns_in_order(two_column_pdf: Path) -> None:
    """The stext extractor's payoff (Stage 0's recorded branch): the columns
    read in order, and the sentence planted across the column break comes
    out intact and adjacent -- what poppler's default mode cannot do."""
    parts = prep.prepare_pdf(two_column_pdf, None, "none", True)
    text = "\n".join(parts)
    assert HEADER not in text
    first = text.index(CROSS_FIRST)
    second = text.index(CROSS_SECOND)
    # The left column's last line straight into the right column's first.
    assert second > first
    assert second - first <= len(CROSS_FIRST) + 2
    # Left column in full before the right one begins.
    assert text.index("P0 L0") < text.index("P14 L2") < text.index("P15 L1")


# ---- Task 8: the stext extractor, the fallback, the flags ----


def stext_line(text: str, x0: float, top: float, x1: float, size: float) -> str:
    """One line's worth of stext: a block, a line carrying its text, a font."""
    bottom = top + size
    return (
        f'<block bbox="{x0} {top} {x1} {bottom}">'
        f'<line bbox="{x0} {top} {x1} {bottom}" text="{text}">'
        f'<font name="Test" size="{size}"/></line></block>'
    )


def stext_doc(pages: list[str]) -> str:
    return '<?xml version="1.0"?><document>' + "".join(pages) + "</document>"


def stext_page(lines: list[str], height: int = 842) -> str:
    return f'<page width="595" height="{height}">' + "".join(lines) + "</page>"


def paper_pages() -> list[str]:
    """Six pages: a recurring top-band head, a body paragraph, two footnotes."""
    return [
        stext_page(
            [
                stext_line("Sample Journal", 72, 24, 176, 9),
                stext_line(f"The body of page {n} opens.", 72, 62, 400, 11),
                stext_line("1 Kant, KrV, B25.", 72, 700, 300, 8),
                stext_line(
                    f"2 A footnote of page {'abcdef'[n]} with prose that runs on and on "
                    "well past any bare citation, and it argues at length.",
                    72,
                    715,
                    500,
                    8,
                ),
            ]
        )
        for n in range(6)
    ]


def test_stext_orders_columns_before_rows() -> None:
    """Column by column, each top to bottom: the reading order poppler cannot do."""
    doc = stext_doc(
        [
            stext_page(
                [
                    stext_line("Left one.", 54, 60, 250, 11),
                    stext_line("Right one.", 311, 60, 507, 11),
                    stext_line("Left two.", 54, 76, 250, 11),
                    stext_line("Right two.", 311, 76, 507, 11),
                ]
            )
        ]
    )
    bodies, notes = prep.stext_body_pages(doc, "classify")
    assert bodies == [["Left one.", "Left two.", "", "Right one.", "Right two."]]
    assert notes == [[]]


def test_stext_keeps_columns_apart_across_a_ten_point_gutter() -> None:
    """A normal LaTeX gutter (10pt at a 10pt font) is wider than a word space:
    same-baseline fragments across it are two columns, not one visual line."""
    doc = stext_doc(
        [
            stext_page(
                [
                    stext_line("Left one.", 54, 60, 250, 10),
                    stext_line("Right one.", 260, 60, 456, 10),
                    stext_line("Left two.", 54, 72, 250, 10),
                    stext_line("Right two.", 260, 72, 456, 10),
                ]
            )
        ]
    )
    bodies, _ = prep.stext_body_pages(doc, "classify")
    assert bodies == [["Left one.", "Left two.", "", "Right one.", "Right two."]]


def test_stext_full_width_blocks_do_not_merge_the_columns() -> None:
    """A centred title and a full-width abstract above two columns read first,
    in place; the columns below read left in full, then right."""
    lines = [
        stext_line("A Centred Title", 200, 40, 400, 14),
        stext_line("The abstract runs the full width of the page,", 54, 70, 507, 10),
        stext_line("and it takes a second line to finish.", 54, 82, 507, 10),
    ]
    for row in range(5):
        top = 110 + 12 * row
        lines.append(stext_line(f"Left {row}.", 54, top, 250, 10))
        lines.append(stext_line(f"Right {row}.", 311, top, 507, 10))
    bodies, _ = prep.stext_body_pages(stext_doc([stext_page(lines)]), "classify")
    assert [line for line in bodies[0] if line] == [
        "A Centred Title",
        "The abstract runs the full width of the page,",
        "and it takes a second line to finish.",
        *(f"Left {row}." for row in range(5)),
        *(f"Right {row}." for row in range(5)),
    ]


def test_stext_columns_between_full_width_blocks_read_left_then_right() -> None:
    lines = [
        stext_line("Left above.", 54, 40, 250, 10),
        stext_line("Right above.", 311, 40, 507, 10),
        stext_line("Left above two.", 54, 52, 250, 10),
        stext_line("Right above two.", 311, 52, 507, 10),
        stext_line("A wide caption set across both columns of the page.", 54, 90, 507, 9),
        stext_line("Left below.", 54, 120, 250, 10),
        stext_line("Right below.", 311, 120, 507, 10),
        stext_line("Left below two.", 54, 132, 250, 10),
        stext_line("Right below two.", 311, 132, 507, 10),
        stext_line("Left below three.", 54, 144, 250, 10),
        stext_line("Right below three.", 311, 144, 507, 10),
    ]
    bodies, _ = prep.stext_body_pages(stext_doc([stext_page(lines)]), "classify")
    assert [line for line in bodies[0] if line] == [
        "Left above.",
        "Left above two.",
        "Right above.",
        "Right above two.",
        "A wide caption set across both columns of the page.",
        "Left below.",
        "Left below two.",
        "Left below three.",
        "Right below.",
        "Right below two.",
        "Right below three.",
    ]


def test_stext_drops_recurring_top_band_heads() -> None:
    bodies, _ = prep.stext_body_pages(stext_doc(paper_pages()), "classify")
    assert all("Sample Journal" not in lines for lines in bodies)
    # Under five pages nothing is a running head, as ever.
    four, _ = prep.stext_body_pages(stext_doc(paper_pages()[:4]), "classify")
    assert four[0][0] == "Sample Journal"


def test_stext_footnotes_by_policy() -> None:
    doc = stext_doc(paper_pages())
    bodies, notes = prep.stext_body_pages(doc, "classify")
    assert bodies[0] == ["The body of page 0 opens."]
    # The citation is dropped on shape; the prose is kept, for the page's end.
    assert notes[0] == [
        [
            "2 A footnote of page a with prose that runs on and on well past "
            "any bare citation, and it argues at length."
        ]
    ]
    defer = prep.stext_body_pages(doc, "defer")
    assert defer[1][0][0] == ["1 Kant, KrV, B25."]
    drop = prep.stext_body_pages(doc, "drop")
    assert drop[1] == [[] for _ in paper_pages()]


def test_stext_detects_footnotes_by_size_or_band() -> None:
    # A small-print marker-led line mid-page is a footnote without the band:
    # the body's flow runs past it, so the band is nowhere near.
    mid_small = stext_page(
        [
            stext_line("The body opens.", 72, 62, 400, 11),
            stext_line("The body goes on early.", 72, 96, 400, 11),
            stext_line("1 A small-print note set mid-page.", 72, 300, 400, 8),
        ]
    )
    bodies, notes = prep.stext_body_pages(stext_doc([mid_small]), "defer")
    assert bodies[0] == ["The body opens.", "The body goes on early."]
    assert notes[0] == [["1 A small-print note set mid-page."]]
    # A body-size marker-led line in the bottom band is a footnote without the size.
    band = stext_page(
        [
            stext_line("The body opens.", 72, 62, 400, 11),
            stext_line("More body sits here.", 72, 96, 400, 11),
            stext_line("1 A note in the bottom band, body size.", 72, 700, 400, 11),
        ]
    )
    bodies, notes = prep.stext_body_pages(stext_doc([band]), "defer")
    assert bodies[0] == ["The body opens.", "More body sits here."]
    assert notes[0] == [["1 A note in the bottom band, body size."]]
    # A marker-led body line mid-page, body size, is body still.
    body = stext_page(
        [
            stext_line("12 of them were counted mid-page.", 72, 300, 400, 11),
            stext_line("The body goes on.", 72, 330, 400, 11),
        ]
    )
    bodies, notes = prep.stext_body_pages(stext_doc([body]), "defer")
    assert bodies[0] == ["12 of them were counted mid-page.", "The body goes on."]
    assert notes == [[]]


# ---- the pdftotext fallback: marker deferral over page-text fixtures ----


def test_marker_tail_notes_split_and_policy() -> None:
    lines = [
        "Body one.",
        "Body two.",
        "",
        "1 Kant, KrV, B25.",
        "2 A prose footnote arguing at length across the closing sentences here.",
    ]
    bodies, notes = prep.marker_footnote_pages([lines], "classify")
    assert bodies == [["Body one.", "Body two.", ""]]
    assert notes == [[["2 A prose footnote arguing at length across the closing sentences here."]]]
    defer = prep.marker_footnote_pages([lines], "defer")
    assert defer[1][0][0] == ["1 Kant, KrV, B25."]
    assert defer[1][0][1] == [
        "2 A prose footnote arguing at length across the closing sentences here."
    ]
    assert prep.marker_footnote_pages([lines], "drop")[1] == [[]]


def test_marker_tail_notes_need_the_tail() -> None:
    # A marker-led line mid-page is body; a year-led tail line is body too.
    lines = ["1 A first line that opens with a count.", "1994 was the year the reading began."]
    bodies, notes = prep.marker_footnote_pages([lines], "defer")
    assert bodies == [lines]
    assert notes == [[]]


def test_prepare_pdf_falls_back_degraded_when_mutool_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pages = [
        [
            "Sample Journal",
            "Body one.",
            "",
            "1 Kant, KrV, B25.",
            "2 A prose footnote that argues its case at length across the page.",
        ]
        for _ in range(2)
    ]

    def fake(command: list[str]) -> str:
        if command[0] == "mutool":
            raise prep.ToolMissing("mutool not found")
        return "".join("\n".join(page) + "\f" for page in pages)

    monkeypatch.setattr(prep, "_run", fake)
    parts = prep.prepare_pdf(Path("x.pdf"), None, "none", True)
    assert "degraded" in capsys.readouterr().err
    text = "\n".join(parts)
    assert "Kant, KrV, B25." not in text
    assert "2 A prose footnote that argues its case at length across the page." in text
    assert text.index("prose footnote") > text.index("Body one.")


def test_cli_footnotes_flag_and_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    pages = [
        [
            "Body one.",
            "",
            "1 Kant, KrV, B25.",
            "2 A prose footnote that argues its case at length across the page.",
        ]
    ]

    def fake(command: list[str]) -> str:
        if command[0] == "mutool":
            raise prep.ToolMissing("mutool not found")
        return "".join("\n".join(page) + "\f" for page in pages)

    monkeypatch.setattr(prep, "_run", fake)
    out = tmp_path / "out.txt"
    for policy, keeps_citation in (("classify", False), ("defer", True), ("drop", False)):
        monkeypatch.setattr(
            sys, "argv", ["prepare_text.py", "x.pdf", "--out", str(out), "--footnotes", policy]
        )
        assert prep.main() == 0
        text = out.read_text(encoding="utf-8")
        assert ("Kant, KrV, B25." in text) is keeps_citation
        # The prose footnote stays under classify and defer; under drop it goes.
        assert ("2 A prose footnote that argues its case at length across the page." in text) is (
            policy != "drop"
        )
    # The default is classify: the citation goes, the prose stays.
    monkeypatch.setattr(sys, "argv", ["prepare_text.py", "x.pdf", "--out", str(out)])
    assert prep.main() == 0
    text = out.read_text(encoding="utf-8")
    assert "Kant, KrV, B25." not in text
    assert "2 A prose footnote that argues its case at length across the page." in text


def test_ocr_report_fires_on_mangled_text_and_not_on_clean(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    mangled = [
        ["Tbe mo\u00adde rn scan says 1 994 and 2 001 and 3 30 was \u00b7 tbe year \u00ac of tbe."]
    ]
    clean = [["The modern scan says that 1994 was the year of the edition, and it holds."]]

    def fake(pages: list[list[str]]) -> Any:
        def run(command: list[str]) -> str:
            if command[0] == "mutool":
                raise prep.ToolMissing("mutool not found")
            return "".join("\n".join(page) + "\f" for page in pages)

        return run

    out = tmp_path / "out.txt"
    for pages, fires in ((mangled, True), (clean, False)):
        monkeypatch.setattr(prep, "_run", fake(pages))
        monkeypatch.setattr(
            sys, "argv", ["prepare_text.py", "x.pdf", "--out", str(out), "--ocr-report"]
        )
        assert prep.main() == 0
        err = capsys.readouterr().err
        assert ("OCR artefacts likely" in err) is fires
        if fires:
            assert "spot-check" in err


# ---- the real failing document: word-level lines, OCR markers, indents ----


def test_stext_groups_word_level_lines_into_one_visual_line() -> None:
    """mutool emits each word as its own `<line>` with a noisy top; those are
    one visual line, read left to right, not a stack sorted by noisy baseline
    (the Köhler word-order regression)."""
    page = stext_page(
        [
            stext_line("qualia", 38, 75.4, 76, 8.0),
            stext_line("such", 76, 72.7, 106, 8.4),
            stext_line("experience", 106, 75.4, 166, 8.4),
            stext_line("is", 166, 74.0, 183, 5.9),
            stext_line("nothing", 183, 75.0, 229, 8.9),
            stext_line("but", 229, 72.5, 254, 9.0),
            stext_line("a", 254, 74.7, 269, 7.8),
            stext_line("subjective", 269, 75.8, 310, 7.7),
        ]
    )
    bodies, _ = prep.stext_body_pages(stext_doc([page]), "classify")
    assert bodies == [["qualia such experience is nothing but a subjective"]]


def test_running_head_recurrence_normalises_ocr_page_numbers() -> None:
    """'i6' and '16' are the same running head: the leading confusable
    normalises with the digit it OCR'd, so a mangled head still recurs across
    the pages and drops."""
    pages = []
    for number in range(5):
        if number % 2 == 0:
            head = "THE CASE AGAINST SCIENCE"
        elif number == 1:
            head = "i6 THE PLACE OF VALUE IN A WORLD OF FACTS"
        else:
            head = "18 THE PLACE OF VALUE IN A WORLD OF FACTS"
        pages.append([head, f"body of page {number}"])
    running = prep.running_lines(pages)
    assert "## THE PLACE OF VALUE IN A WORLD OF FACTS" in running
    assert "THE CASE AGAINST SCIENCE" in running


def test_stext_reads_an_ocr_mangled_footnote_marker() -> None:
    """The footnote marker OCR'd as 'il' (for '1 I') still opens a footnote:
    the geometry (bottom band, small print) is the guard, never the marker
    alone."""
    page = stext_page(
        [
            stext_line("The body of the page.", 72, 62, 400, 11),
            stext_line(
                "il found recently that these words aie quoted from A. N. Whitehead,",
                44,
                480,
                330,
                6.8,
            ),
            stext_line(
                "who does not of course share the opinion which they express.",
                36,
                490,
                320,
                8.4,
            ),
        ],
        height=533,
    )
    bodies, notes = prep.stext_body_pages(stext_doc([page]), "defer")
    assert bodies == [["The body of the page."]]
    assert notes == [
        [
            [
                "il found recently that these words aie quoted from A. N. Whitehead,",
                "who does not of course share the opinion which they express.",
            ]
        ]
    ]


def test_stext_keeps_a_small_body_line_opening_with_a_roman_i() -> None:
    """A body paragraph opening 'I ' is small print but not a marker: a lone
    confusable is not a footnote number, so geometry must not move it."""
    page = stext_page(
        [
            stext_line("The body of the page.", 72, 62, 400, 11),
            stext_line("I looked at the man speechless.", 48, 259, 400, 6.0),
            stext_line("who does not of course share the opinion.", 36, 490, 320, 8.4),
        ]
    )
    bodies, notes = prep.stext_body_pages(stext_doc([page]), "defer")
    assert notes == [[]]
    assert "I looked at the man speechless." in bodies[0]


def test_stext_starts_a_paragraph_at_an_indented_line() -> None:
    """An indented line opens a paragraph even where the vertical gap is no
    wider than the line spacing, as on the Köhler scan."""
    page = stext_page(
        [
            stext_line("the end of one paragraph.", 37, 60, 300, 11),
            stext_line("A new paragraph begins indented.", 49, 76, 300, 11),
            stext_line("and it continues unindented here.", 37, 92, 300, 11),
        ]
    )
    bodies, _ = prep.stext_body_pages(stext_doc([page]), "classify")
    assert bodies == [
        [
            "the end of one paragraph.",
            "",
            "A new paragraph begins indented.",
            "and it continues unindented here.",
        ]
    ]


def test_prepare_pdf_rejoins_across_pages_and_defers_footnotes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sentence split by a page break is rejoined because the previous
    page's footnote is deferred to the group's end, not wedged between the
    pages."""
    xml = stext_doc(
        [
            stext_page(
                [
                    stext_line(
                        "enigmatic scheme to which we cannot transfer the rules",
                        37,
                        460,
                        340,
                        8.5,
                    ),
                    stext_line(
                        "il found recently that these words aie quoted from A. N. Whitehead,",
                        44,
                        480,
                        330,
                        6.8,
                    ),
                    stext_line(
                        "who does not of course share the opinion which they express.",
                        36,
                        490,
                        320,
                        8.4,
                    ),
                ],
                height=533,
            ),
            stext_page(
                [
                    stext_line(
                        "of spatial and of temporal experience. Like the sensory",
                        37,
                        58,
                        320,
                        8.5,
                    )
                ]
            ),
        ]
    )
    monkeypatch.setattr(
        prep,
        "_stext_pages_or_none",
        lambda pdf, pages, footnotes: prep.stext_body_pages(xml, footnotes),
    )
    monkeypatch.setattr(prep, "_outline", lambda pdf: [])
    text = "\n".join(prep.prepare_pdf(Path("x.pdf"), None, "auto", True))
    assert "we cannot transfer the rules of spatial and of temporal experience." in text
    assert text.index("A. N. Whitehead") > text.index("of spatial and of temporal experience.")
