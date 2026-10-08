---
name: speakd-audiobook
description: Use when the user wants to make an audiobook or audio version of a PDF, paper, Markdown or text file, to read a PDF into an mp3/m4b, or to listen to a paper or Zotero item later. Renders through the speakd daemon with `speakctl render`.
---

# Make an audiobook with speakd

1. **Check rendering works.** Run `speakctl status`; `render.available` must be true (it needs ffmpeg). If there is no daemon, tell the user to open speakd and stop.
2. **Find the source.** For a Zotero item, PDFs live in `~/Zotero/storage/<KEY>/`; use the `find-source` skill if you have it. Zotero is read-only for you: never modify its files or database.
3. **Prepare the text** into a working file under the scratch or tmp directory:
   `python3 <skill dir>/scripts/prepare_text.py <input.pdf|.txt|.md> --out <work>.txt [--pages 12-48] [--chapters auto|none] [--keep-references] [--footnotes classify|drop|defer] [--ocr-report]`
   It reads the PDF's own geometry (columns, font sizes) so two-column papers come out in reading order; it strips running headers and page numbers, joins wrapped lines, cuts the references list, and turns the PDF outline into chapters. Footnote policy was applied along the way -- the default (`classify`) drops the citation-shaped footnotes and defers the rest to the end of their page, so they are spoken after the body, never inline; `--footnotes drop` removes every footnote, `--footnotes defer` keeps all at page end. If the spot-check shows footnote trouble, look at the end of each part's pages for the deferred ones (a wrongly dropped or wrongly kept footnote is a policy or shape-rule miss: change the `--footnotes` flag, or move a footnote by hand). Read only its stderr summary (parts, titles, characters, estimated duration), then spot-check a few hundred characters at 2-3 places in the file. Never load the whole text into your context. Pages before the first outline entry (title page, contents) are dropped; pass `--keep-front-matter` to keep them. A scanned PDF with no text needs OCR first.
4. **Fix what the spot-check shows** with targeted edits, for example a stray header the heuristic missed. An `OCR artefacts likely` line on stderr (from `--ocr-report`) means the file's text layer probably looks scanned: give it a targeted-edit pass for the usual confusions -- `tbe`/`the`, `modem`/`modern`, `rn`/`m`, `cl`/`d`, broken ligatures, numbers split by a space (`1 994`). Keep `\f` between parts and `# Title` as a part's first line.
5. **Render** with `speakctl render <work>.txt --out <path> --title "…" --artist "…" --date <year> [--album "…"] [--lang xx] --no-wait`. Use `.m4b` when there is more than one part (chapters), else `.mp3`. Unless the user named a place, write to `~/Audiobooks/<Author> - <Title>.<ext>`.
6. **Report** the job id and the duration estimate. Progress is the `render.jobs` entry in `speakctl status`. Renders yield to live speech and survive a daemon restart, so there is no need to wait.
7. **Attaching the result to a Zotero item** is not yours to do: point the user to the speakd Zotero plugin's right-click **Export audiobook…**.
