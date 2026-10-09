# Captured segment dumps (Zotero harness)

Real Stage 0 dumps from the live harness (`ReaderHandle.dumpSegments`,
`src/export/debugdump.ts`), one JSON object per line, plus a per-page summary
line at the end of each page group. Consumed by `test/corpus.test.ts`.

| File | Source fixture | Shape |
|---|---|---|
| `twocolumn.jsonl` | `test/fixtures/pdf/twocolumn.pdf` | 736 segments; page 1's segments run left column then right |
| `footnotes.jsonl` | `test/fixtures/pdf/footnotes.pdf` | 136 segments; footnotes at `firstRectY` ≤ 140, body ≥ 641 on an 842-pt page |

Geometry convention (Stage 0 notes, "Rect coverage: complete, and y runs
bottom-up"): rect y is PDF user space, so larger y is higher on the page and
the bottom band is low y.

## Ground-truth fixtures

`pdf/<slug>/` holds the labeler's exported fixtures (`capture.jsonl`,
`labels.json`, `expected.json`, `README.md`), consumed by `corpus.test.ts`.
`labels.json` is the ground truth; `expected.json` is a regression snapshot.
The source PDF is never checked in; the README carries its sha256 and the
copyright bound. See `pdf/synthetic/` (generated) and `pdf/kohler/` (the
first real fixture, pages 34–38).

Reproduce (harness, not in this repo's default test run):

```bash
cd clients/zotero && npm run build && test-live/setup-profile.sh && test-live/run-zotero.sh
ZEVAL_TIMEOUT=300 test-live/zeval.sh '<open the reader, then:
  Zotero.SpeakdReader.handles().find(...).dumpSegments("/path/dump.jsonl")>'
```
