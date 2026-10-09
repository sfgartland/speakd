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

# The real failing document (Köhler 1939, PDF pages 35-39 = printed pp. 15-19),
# mutool stext with the `<char>` quads stripped: word-level `<line>` elements
# with noisy baselines, an OCR'd 'i6' running head, an 'il' footnote marker,
# and a sentence split across the PDF p.35/36 break.
KOHLER = CORPUS / "stext" / "kohler-p36.xml"
KOHLER_EXPECTED = CORPUS / "kohler-p36.expected.txt"


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
            ["pdftotext", str(PDF / "footnotes.pdf"), "-"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split("\f")
        if page.strip()
    ]
    stripped = prep.strip_pages(footnote_pages)
    assert all("Footnotes and the Reader" not in line for page in stripped for line in page)


def test_kohler_page_16_regression(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real failing scan, end to end through the stext extractor: the
    running head drops, the sentence split across the p.35/36 break rejoins,
    a paragraph break lands before the indented '"It almost…', and the OCR'd
    footnote moves out of the body to the part's end with its text intact."""
    xml = KOHLER.read_text(encoding="utf-8")
    monkeypatch.setattr(prep, "_outline", lambda pdf: [])
    monkeypatch.setattr(
        prep,
        "_stext_pages_or_none",
        lambda pdf, pages, footnotes: prep.stext_body_pages(xml, footnotes),
    )
    text = "\n".join(prep.prepare_pdf(Path("kohler.pdf"), None, "auto", True))

    # The running heads recur across the five pages and are dropped.
    assert "THE PLACE OF VALUE IN A WORLD OF FACTS" not in text
    assert "THE CASE AGAINST SCIENCE" not in text
    # The sentence split across the page break is rejoined.
    assert "we cannot transfer the rules of spatial and of temporal experience." in text
    # The indented line opens a paragraph where the vertical gap does not.
    assert '\n\n"It almost goes without saying' in text
    # The OCR'd footnote is out of the body, at the part's end, its text
    # preserved ('il' and 'aie' need not be repaired).
    assert "il found recently that these words aie quoted from A. N. Whitehead" in text
    assert text.index("A. N. Whitehead") > text.index("this curious")
    assert text.index("A. N. Whitehead") > text.index("of spatial and of temporal experience.")

    assert text + "\n" == KOHLER_EXPECTED.read_text(encoding="utf-8")
