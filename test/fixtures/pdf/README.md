# Shared PDF fixture corpus

The three document classes the cleanup surfaces are built and validated
against (plan Task 13):

| File | Class | What it exercises |
|---|---|---|
| `twocolumn.pdf` | two-column academic | reading order: a sentence planted to cross page 1's column break |
| `footnotes.pdf` | footnote-heavy | bottom-band detection, citation vs prose footnotes, the year-led body-line guard |
| `ocr-sample.pdf` | scan-like | OCR confusions, split digits, stray marks, a broken ligature |

All three carry a running header and a page number.

Regenerate with PyMuPDF (not a project dependency):

```bash
uv run --with pymupdf python test/fixtures/pdf/make_fixtures.py
```

The two-column and footnote PDFs are byte-for-byte the ones the harness
dumps in `clients/zotero/test/fixtures/` were captured from. The plugin
consumes those dumps; the skill consumes the stext XML and pdftotext text in
`tests/fixtures/corpus/`.
