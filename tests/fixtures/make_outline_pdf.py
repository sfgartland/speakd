"""Regenerate outline.pdf: `/usr/bin/python3 tests/fixtures/make_outline_pdf.py`.

Uses the system Python's PyMuPDF (the project venv does not carry fitz).
Six pages with a running header "Sample Journal" and a page number footer, so
the header/footer heuristic has something to remove; a two-chapter outline
(with a nested entry that must be ignored); a References section at the end.
"""

from pathlib import Path

import fitz  # type: ignore[import-not-found]

CHAPTERS = [("Introduction", 1, 3), ("Argument", 4, 6)]

doc = fitz.open()
for number in range(1, 7):
    page = doc.new_page()
    page.insert_text((72, 40), "Sample Journal", fontsize=10)
    page.insert_text((72, 90), f"Body sentence one on page {number}.", fontsize=11)
    page.insert_text((72, 110), "A second line of the same paragraph", fontsize=11)
    page.insert_text((72, 125), "continues here.", fontsize=11)
    if number == 6:
        page.insert_text((72, 170), "References", fontsize=11)
        page.insert_text((72, 190), "Doe, J. 2001. A cited work.", fontsize=11)
    page.insert_text((300, 800), str(number), fontsize=10)
doc.set_toc(
    [
        [1, "Introduction", 1],
        [2, "Nested note", 2],
        [1, "Argument", 4],
    ]
)
doc.save(Path(__file__).with_name("outline.pdf"), garbage=4, deflate=True)
