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
from pathlib import Path
from typing import Any

import pytest
from test_audiobook_prepare import prep

fitz: Any = pytest.importorskip(
    "fitz", reason="generates the two-column fixture; PyMuPDF is optional"
)

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


def make_two_column_pdf(path: Path) -> None:
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
    pdf = tmp_path_factory.mktemp("stage0") / "twocolumn.pdf"
    make_two_column_pdf(pdf)
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
def test_prepare_pdf_today_inherits_popplers_interleaved_order(two_column_pdf: Path) -> None:
    """Characterisation of today's pipeline: prepare_pdf has no column logic
    of its own, so its part carries poppler's interleaved order -- the cross
    sentence's halves separated by most of the paper, its second half read
    before its first. Superseded when Stage 0's recorded branch (the stext
    extractor) lands, at which point this is re-pinned to the `pdftotext`
    fallback path it characterises. The running header and page numbers are
    still gone, which is the existing heuristics' doing, not column logic."""
    parts = prep.prepare_pdf(two_column_pdf, None, "none", True)
    text = "\n".join(parts)
    assert HEADER not in text
    first = text.index(CROSS_FIRST)
    second = text.index(CROSS_SECOND)
    assert second < first
    assert first - second > 4000
