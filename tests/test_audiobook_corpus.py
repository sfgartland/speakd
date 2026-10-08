"""The shared PDF fixture corpus, consumed by the skill's extractor (plan Task 13).

`test/fixtures/pdf/` holds the three document classes (regenerate with
`uv run --with pymupdf python test/fixtures/pdf/make_fixtures.py`).
`tests/fixtures/corpus/` holds their mutool stext XML and pdftotext page text.
The plugin consumes the same corpus through the captured segment dumps in
`clients/zotero/test/fixtures/` (`clients/zotero/test/corpus.test.ts`).

The stext tests run `mutool` over the checked-in PDFs (the full XML is
multi-megabyte); the OCR sample's XML is checked in whole so its suspicion
test needs no external tool. Everything skips cleanly when a tool is absent,
so the repo stays reproducible without them.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from test_audiobook_prepare import prep

CORPUS = Path(__file__).parent / "fixtures" / "corpus"
PDF = Path(__file__).parent.parent / "test" / "fixtures" / "pdf"

HEADER = "A Tale of Two Columns"
CROSS_FIRST = "The cross column sentence begins"
CROSS_SECOND = "and ends in the rightmost column."


def _stext(pdf: Path, page: int | None = None) -> str:
    command = ["mutool", "draw", "-F", "stext", "-o", "-", str(pdf)]
    if page is not None:
        command.append(str(page))
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def _body_text(pages: list[list[str]]) -> str:
    return "\n".join(line for page in pages for line in page)


@pytest.mark.skipif(shutil.which("mutool") is None, reason="needs mupdf tools")
def test_two_column_corpus_reads_in_column_order() -> None:
    bodies, notes = prep.stext_body_pages(_stext(PDF / "twocolumn.pdf"), "classify")
    text = _body_text(bodies)
    assert HEADER not in text
    first = text.index(CROSS_FIRST)
    second = text.index(CROSS_SECOND)
    assert second > first
    assert second - first <= len(CROSS_FIRST) + 2


@pytest.mark.skipif(shutil.which("mutool") is None, reason="needs mupdf tools")
def test_footnote_corpus_by_policy() -> None:
    xml = _stext(PDF / "footnotes.pdf", 1)
    bodies, classify = prep.stext_body_pages(xml, "classify")
    assert "Kant, KrV, B25." not in _body_text(bodies)
    # Page 1 is citation-shaped from top to bottom: classify drops them all.
    assert classify == [[]]
    _, defer = prep.stext_body_pages(xml, "defer")
    assert [unit for unit in defer[0] if unit][0] == ["1 Kant, KrV, B25."]
    _, drop = prep.stext_body_pages(xml, "drop")
    assert drop == [[]]


def test_ocr_sample_corpus_looks_like_a_scan() -> None:
    bodies, _ = prep.stext_body_pages((CORPUS / "stext" / "ocr-sample.xml").read_text(), "classify")
    text = _body_text(bodies)
    assert "tbe" in text.lower() or "Tbe" in text
    assert prep.ocr_suspicion(text) > prep.OCR_SUSPICION_GATE


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="needs poppler tools")
def test_pdftotext_corpus_characterises_poppler_and_strips_heads() -> None:
    raw = subprocess.run(
        ["pdftotext", str(PDF / "twocolumn.pdf"), "-"], check=True, capture_output=True, text=True
    ).stdout
    # Characterisation, not endorsement: poppler's default mode interleaves the
    # columns, so the cross sentence's right half is read before its left.
    assert raw.index(CROSS_SECOND) < raw.index(CROSS_FIRST)

    footnote_pages = [
        page.splitlines()
        for page in subprocess.run(
            ["pdftotext", str(PDF / "footnotes.pdf"), "-"], check=True, capture_output=True, text=True
        ).stdout.split("\f")
        if page.strip()
    ]
    stripped = prep.strip_pages(footnote_pages)
    assert all("Footnotes and the Reader" not in line for page in stripped for line in page)
