# Skill-path corpus fixtures

Extractor output over `test/fixtures/pdf/`, checked in so the skill's tests
do not need PyMuPDF and, for the OCR sample, not even mutool. Consumed by
`tests/test_audiobook_corpus.py`.

- `stext/ocr-sample.xml` — `mutool draw -F stext` of the scan-like sample.
  Small enough to check in whole; the suspicion test reads it directly.
- `stext/kohler-p36.xml` — the real failing document (Köhler 1939, PDF pages
  35–39 = printed pp. 15–19) with the `<char>` quads stripped: mutool's
  word-sized `<line>` elements with noisy baselines, an OCR'd `i6` running
  head, an `il` footnote marker, and a sentence split across the p.35/36
  break. `kohler-p36.expected.txt` is the cleaned output the regression test
  pins (the running head dropped, the sentence rejoined, the indented `"It
  almost…` paragraph split, the footnote deferred to the part's end).
- `pdftotext/*.txt` — poppler's default-mode output of all three PDFs, page
  breaks as form feeds. The two-column file records poppler's interleaved
  order (a characterisation of the fallback path, not an endorsement).

The two-column and footnote stext XML is multi-megabyte, so
`test_audiobook_corpus.py` runs `mutool` over the checked-in PDFs for those
instead of checking the XML in. Regenerate the text fixtures with:

```bash
for f in twocolumn footnotes ocr-sample; do
  mutool draw -F stext -o tests/fixtures/corpus/stext/$f.xml test/fixtures/pdf/$f.pdf
  pdftotext test/fixtures/pdf/$f.pdf tests/fixtures/corpus/pdftotext/$f.txt
done
```
