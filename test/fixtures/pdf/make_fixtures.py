#!/usr/bin/env python3
"""The shared PDF fixture corpus for the two cleanup surfaces (plan Task 13).

Three document classes, each with a running header and page numbers:

- `twocolumn.pdf` — eight pages of two equal columns, with one sentence
  planted to start at the bottom of page 1's left column and end at the top
  of its right. Reading order is the point.
- `footnotes.pdf` — six single-column pages, six body paragraphs a page and
  four or five footnotes in the bottom band, citation-shaped and prose by
  page, plus one body line opening with a year (the marker guard).
- `ocr-sample.pdf` — three pages whose text layer is a mangled scan layer:
  OCR confusion pairs, split digits, a stray mark, a broken ligature.

Reproduce the PDFs (PyMuPDF is not a project dependency) with:

    uv run --with pymupdf python test/fixtures/pdf/make_fixtures.py

The captured segment dumps (`clients/zotero/test/fixtures/*.jsonl`) come from
the live harness; the stext XML and pdftotext page text
(`tests/fixtures/corpus/`) come from `mutool`/`pdftotext` over these PDFs
(see the README beside each).
"""

from pathlib import Path

import fitz  # type: ignore[import-not-found]

OUT = Path(__file__).resolve().parent
WORDS = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima".split()


def lines_of(paragraph: int, n: int) -> list[str]:
    out = []
    for i in range(n):
        words = [WORDS[(paragraph * 31 + i * 7 + j) % len(WORDS)] for j in range(6)]
        out.append(f"P{paragraph} L{i} {' '.join(words)}.")
    return out


def running_header(page: fitz.Page, title: str, number: int) -> None:
    page.insert_text((72, 30), title, fontsize=9)
    page.insert_text((285, 820), str(number), fontsize=9)


COL_X = (54, 311)


def twocolumn() -> None:
    doc = fitz.open()
    cross_first = "The cross column sentence begins"
    cross_second = "and ends in the rightmost column."
    paragraph = 0
    for page_index in range(8):
        page = doc.new_page()
        running_header(page, "A Tale of Two Columns", page_index + 1)
        for col in range(2):
            for line in range(46):
                y = 60 + line * 16
                if page_index == 0 and col == 0 and line == 45:
                    text = cross_first
                elif page_index == 0 and col == 1 and line == 0:
                    text = cross_second
                else:
                    text = lines_of(paragraph, 3)[line % 3]
                    if line % 3 == 2:
                        paragraph += 1
                page.insert_text((COL_X[col], y), text, fontsize=10)
    doc.save(OUT / "twocolumn.pdf", garbage=4, deflate=True)


def footnotes() -> None:
    doc = fitz.open()
    citations = [
        "1 Kant, KrV, B25.",
        "2 Heidegger 1962, p. 65.",
        "3 Ibid.",
        "4 Cf. KrV, A51/B75.",
        "5 See op. cit., pp. 12-20.",
    ]
    prose = [
        "1 The inaugurating gesture of the lecture is reversed here, and what that",
        "2 reversal means for the order of asking is worked out across pages.",
        "3 A third prose footnote wraps onto this line and keeps its argument.",
        "4 The fourth footnote carries the last remark of the page's margin.",
    ]
    for page_index in range(6):
        page = doc.new_page()
        running_header(page, "Footnotes and the Reader", page_index + 1)
        for p in range(6):
            for line, text in enumerate(lines_of(page_index * 6 + p, 3)):
                page.insert_text((72, 70 + p * 20 + line * 14), text, fontsize=11)
        if page_index == 2:
            page.insert_text(
                (72, 190), "1994 was the year the reading began in earnest.", fontsize=11
            )
        source = citations if page_index % 2 == 0 else prose
        for n, text in enumerate(source):
            page.insert_text((72, 700 + n * 14), text, fontsize=8)
    doc.save(OUT / "footnotes.pdf", garbage=4, deflate=True)


def ocr_sample() -> None:
    """A scan's text layer: recognisable words with OCR confusions, split
    digits, a stray mark and a broken ligature, under a running head."""
    doc = fitz.open()
    pages = [
        [
            "Tbe modem reader begins with a de\ufb01nition.",
            "In 1 994 the edition was set, and in 2 001 it was reset.",
            "A stray \u00b7 mark and a \u00ac negation sit here.",
        ],
        [
            "Tbe second page repeats tbe confusion of the first.",
            "The rn and cl pairs are tbe scanner's to spoil.",
            "An argument \ufb01lls the page and runs on.",
        ],
        [
            "Tbe closing page keeps tbe same faults.",
            "A final 1 902 reference closes the sample.",
            "Tbe scanner left one more \u00ad soft hyphen.",
        ],
    ]
    for page_index, lines in enumerate(pages):
        page = doc.new_page()
        running_header(page, "A Scanned Chapter", page_index + 1)
        for line, text in enumerate(lines):
            page.insert_text((72, 90 + line * 20), text, fontsize=11)
        page.insert_text(
            (72, 700), f"{page_index + 1} A scan footnote, tbe same faults.", fontsize=8
        )
    doc.save(OUT / "ocr-sample.pdf", garbage=4, deflate=True)


if __name__ == "__main__":
    twocolumn()
    footnotes()
    ocr_sample()
    print(f"wrote the three PDFs to {OUT}")
