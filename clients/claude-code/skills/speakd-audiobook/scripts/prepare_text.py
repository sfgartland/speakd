#!/usr/bin/env python3
"""Turn a PDF, text or Markdown file into text `speakctl render` can narrate.

Output is the format `speakctl render` reads: parts separated by form feeds,
a part whose first non-blank line is `# <title>` becomes a named chapter.
Layout is cleaned structurally (two-column reading order, running headers,
page numbers, footnotes per policy, references); abbreviations, ranges and
ellipses are left to the daemon's speech pipeline, and anything semantic is
left to the agent following SKILL.md. Stdlib only, so it runs wherever the
agent does; PDF extraction shells out to mupdf (`mutool draw -F stext`, for
geometry and font sizes, falling back to poppler's `pdftotext`, degraded).
"""

import argparse
import re
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from collections import Counter
from itertools import pairwise
from pathlib import Path
from typing import NamedTuple

CHARS_PER_SECOND = 15
HEADER_SHARE = 0.4
HEADER_MIN_PAGES = 5
# The footnote and OCR taxonomy is mirrored from the Zotero plugin's
# footnotes.ts and cleanup.ts (docs/design/2026-10-08-pdf-text-cleanup-
# proposal-glm.md §4.2): shared design and corpus, not shared code.
FOOTNOTE_POLICIES = ("classify", "drop", "defer")
SHORT_FOOTNOTE = 48
LONG_FOOTNOTE = 140
FOOTNOTE_MARKER = re.compile(
    r"^(?:[¹²³⁰-⁹]{1,3}"  # superscripts
    r"|[0-9OolIil]{0,1}\d[0-9OolIil]{0,1}[.:\s]"  # 1-2 digits, OCR-confused or not
    r"|[OolIil]{2,3}[.:\s]"  # two-plus OCR confusables, e.g. 'il' for '1 I'
    r"|n\d{1,2}|\*+|[†‡¶])"
)
CITATION_OPENER = re.compile(
    r"\bIbid\.?\b|\bCf\.?\b|\bop\. cit\.|\bSee\b|\b[A-ZÖÄÜ][^\W\d_]+[,]?\s+\d{4}\b"
)
# The provisional bottom band mirrors the plugin's (Stage 0's notes): its
# final value is conditional on the owner's failing sample. The OCR gate
# ships conservative for the same reason -- a real scan fixture has not
# arrived; both are one constant away from tuning.
BOTTOM_BAND = 0.25
SMALL_SIZE = 0.85
PARAGRAPH_GAP = 1.5
# Visual-line grouping (mutool emits word-sized `<line>` elements): a band is a
# share of the line's font size, the horizontal guard a multiple of it.
VISUAL_BAND = 0.6
VISUAL_GAP = 2.0
# A gutter: at most this share of fragments may cross it, it is this many ems
# wide, and each side holds at least this share of the fragments.
GUTTER_COVERAGE = 0.25
GUTTER_WIDTH = 0.5
GUTTER_SIDE = 0.2
# A line wider than this share of the text width is a full-width block.
WIDE_LINE = 0.6
COLUMN_SHARE = 0.25
OCR_SUSPICION_GATE = 6.0
SCAN_JUNK = re.compile("[\ufb00-\ufb04\x00-\x08\x0e-\x1f\x7f\xad\xac\xb7\u200b-\u200d\u2060\ufeff]")
SPLIT_DIGITS = re.compile(r"(?<=\d)\s+(?=\d)")
LETTERS = re.compile(r"[^\W\d_]+")
REFERENCES = re.compile(r"^(References|Bibliography|Works Cited|Literatur(verzeichnis)?)$", re.I)
# Roman numerals i..lxxxix, covering xl-xlix: front-matter page numbers. A bare character class
# would also eat body lines such as "mix", "did" or "civil".
ARABIC_OR_ROMAN = re.compile(r"^(\d+|(?=[ivxl])(xl|l?x{0,3})(ix|iv|v?i{0,3}))$", re.I)
# mutool prints "+" or "|" markers, then one tab per depth level; top level
# is exactly one tab before the quoted title.
OUTLINE_LINE = re.compile(r'^[+|-]\t"((?:[^"\\]|\\.)*)"\t#page=(\d+)')


def parse_outline(text: str) -> list[tuple[str, int]]:
    """Top-level (title, page) entries of `mutool show <pdf> outline`."""
    entries = []
    for line in text.splitlines():
        match = OUTLINE_LINE.match(line)
        if match:
            entries.append((re.sub(r"\\(.)", r"\1", match.group(1)), int(match.group(2))))
    return entries


def is_page_number(line: str) -> bool:
    return bool(ARABIC_OR_ROMAN.match(line.strip()))


# A page number as OCR leaves it: digits and the confusions of 1/0, at least
# one real digit so body words are untouched.
_PAGE_TOKEN = re.compile(r"^(?=[0-9OolIil]*\d)[0-9OolIil]+$")


def _normalise(line: str) -> str:
    """Whitespace collapsed, digits `#` -- and a page-number-like token (digits
    plus the OCR confusions of 1/0, `i l I o O`) wholly `#`, so a mangled
    running head ('i6') and its clean twin ('16') normalise equal."""
    tokens = []
    for token in line.split():
        bare = re.sub(r"[.,;:!?()\[\]{}'\"]", "", token)
        if _PAGE_TOKEN.match(bare):
            tokens.append(re.sub(r"[0-9OolIil]", "#", bare))
        else:
            tokens.append(re.sub(r"\d", "#", token))
    return " ".join(tokens)


def running_lines(pages: list[list[str]]) -> set[str]:
    """Normalised lines that open or close at least 40 % of the pages."""
    if len(pages) < HEADER_MIN_PAGES:
        return set()
    counts: Counter[str] = Counter()
    for page in pages:
        content = [_normalise(line) for line in page if line.strip() and not is_page_number(line)]
        if content:
            counts.update({content[0], content[-1]})
    return {line for line, seen in counts.items() if seen >= HEADER_SHARE * len(pages)}


def strip_pages(pages: list[list[str]]) -> list[list[str]]:
    """Drop running headers/footers and bare page-number lines."""
    running = running_lines(pages)
    return [
        [line for line in page if not is_page_number(line) and _normalise(line) not in running]
        for page in pages
    ]


def footnote_kind(text: str) -> str:
    """One footnote's shape: drop (a bare citation), defer (clear prose), or
    classify (the in-between, which the policy resolves to kept)."""
    trimmed = text.strip()
    dense = "".join(trimmed.split())
    dominated = bool(dense) and sum(1 for c in dense if not c.isalpha()) / len(dense) >= 0.5
    if len(trimmed) <= SHORT_FOOTNOTE or CITATION_OPENER.search(trimmed) or dominated:
        return "drop"
    sentences = len(re.findall(r"[.!?](?=\s|$)", trimmed))
    if len(trimmed) >= LONG_FOOTNOTE and sentences >= 2:
        return "defer"
    return "classify"


def _policy_units(units: list[list[str]], footnotes: str) -> list[list[str]]:
    """The footnote units the policy keeps, each its own paragraph later.

    `drop` keeps none; `defer` keeps all; `classify` drops the citation-shaped
    and keeps the rest -- the agent following SKILL.md is the classifier of
    last resort, so an undecided footnote is kept, never dropped.
    """
    if footnotes == "drop":
        return []
    return [
        unit for unit in units if footnotes == "defer" or footnote_kind(" ".join(unit)) != "drop"
    ]


def ocr_suspicion(text: str) -> float:
    """How much a text looks like a scan's OCR layer (the plugin's cleanup.ts,
    mirrored): stray marks, digit runs broken by one space, and one- and
    two-letter tokens. Counts, never rewrites; the gate is provisional."""
    score = len(SCAN_JUNK.findall(text)) + 2 * len(SPLIT_DIGITS.findall(text))
    tokens = LETTERS.findall(text)
    short = sum(1 for token in tokens if len(token) <= 2)
    return score + (4 * short / len(tokens) if tokens else 0.0)


# ---- the stext path: mutool's structured text, geometry and font sizes ----


class StextLine(NamedTuple):
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float


class StextPage(NamedTuple):
    height: float
    lines: list[StextLine]


def parse_stext(xml: str) -> list[StextPage]:
    """mutool stext as pages of lines: text, x-range, top, bottom, font size.

    stext's y runs top-down (0 at the page top), so the bottom band of a
    page is *high* y -- the mirror image of the plugin's bottom-up rect y.
    """
    pages = []
    for page in ElementTree.fromstring(xml).iter("page"):
        height = float(page.get("height") or 0)
        lines = []
        for line in page.iter("line"):
            bbox = (line.get("bbox") or "").split()
            text = line.get("text") or ""
            if len(bbox) < 4 or not text.strip():
                continue
            font = line.find("font")
            size = float(font.get("size") or 0) if font is not None else 0.0
            lines.append(
                StextLine(
                    text,
                    float(bbox[0]),
                    float(bbox[2]),
                    float(bbox[1]),
                    float(bbox[3]),
                    size,
                )
            )
        pages.append(StextPage(height, lines))
    return pages


def _merge_visual_line(run: list[StextLine]) -> StextLine:
    """One visual line from the word-sized `<line>` elements it holds: their
    texts in x order, the union of their boxes, the largest font size."""
    ordered = sorted(run, key=lambda line: line.x0)
    return StextLine(
        " ".join(line.text for line in ordered),
        min(line.x0 for line in ordered),
        max(line.x1 for line in ordered),
        min(line.top for line in ordered),
        max(line.bottom for line in ordered),
        max(line.size for line in ordered),
    )


def _gutter(lines: list[StextLine], median_height: float) -> float | None:
    """The x of a column gutter, or None: a stretch of the page's middle that
    almost no fragment covers (a few full-width ones may), at least half an em
    wide, with a real share of the fragments on each side of it. Word gaps in
    running text are never aligned line after line, so they cannot qualify."""
    if len(lines) < 4:
        return None
    sizes = sorted(line.size for line in lines if line.size > 0)
    em = sizes[len(sizes) // 2] if sizes else median_height
    left = min(line.x0 for line in lines)
    right = max(line.x1 for line in lines)
    width = right - left
    if width <= 0 or em <= 0:
        return None
    points = sorted({line.x0 for line in lines} | {line.x1 for line in lines})
    allowed = GUTTER_COVERAGE * len(lines)
    best: tuple[float, float] | None = None
    start: float | None = None
    for low, high in [*pairwise(points), (right, right)]:
        covered = sum(1 for line in lines if line.x0 < high and line.x1 > low)
        empty = low < high and covered <= allowed
        if empty and start is None:
            start = low
        if not empty and start is not None:
            if best is None or low - start > best[1] - best[0]:
                best = (start, low)
            start = None
    if best is None or best[1] - best[0] < GUTTER_WIDTH * em:
        return None
    middle = (best[0] + best[1]) / 2
    if not left + 0.3 * width <= middle <= left + 0.7 * width:
        return None
    on_left = sum(1 for line in lines if line.x1 <= middle)
    on_right = sum(1 for line in lines if line.x0 >= middle)
    if min(on_left, on_right) < GUTTER_SIDE * len(lines):
        return None
    return middle


def _visual_lines(lines: list[StextLine]) -> list[StextLine]:
    """mutool emits each word as its own `<line>` with a noisy top; lines whose
    vertical centres share a band (a share of the font size) are one visual
    line, read left to right -- the fix for the Köhler word-order regression.
    A horizontal guard (a few ems) keeps two columns' words on the same
    baseline apart, so column detection still sees two columns; so does a
    detected gutter, which no fragment is merged across however narrow it is
    (a LaTeX gutter of 10pt at a 10pt font is well under the guard)."""
    if not lines:
        return []
    heights = sorted(line.bottom - line.top for line in lines if line.bottom > line.top)
    median_height = heights[len(heights) // 2] if heights else 0.0
    bands: list[list[StextLine]] = []
    for line in sorted(lines, key=lambda item: ((item.top + item.bottom) / 2, item.x0)):
        centre = (line.top + line.bottom) / 2
        for band in bands:
            first = band[0]
            size = max(line.size, first.size)
            tolerance = VISUAL_BAND * (size if size > 0 else median_height)
            # Compared with the band's first centre, so the band stays bounded
            # and cannot chain one visual line into the next.
            if centre - (first.top + first.bottom) / 2 <= tolerance:
                band.append(line)
                break
        else:
            bands.append([line])
    gutter = _gutter(lines, median_height)
    visual: list[StextLine] = []
    for band in bands:
        ordered = sorted(band, key=lambda item: item.x0)
        run = [ordered[0]]
        for line in ordered[1:]:
            right = max(item.x1 for item in run)
            gap = line.x0 - right
            size = max(line.size, max(item.size for item in run))
            across_gutter = gutter is not None and right <= gutter < line.x0
            if gap > VISUAL_GAP * size or across_gutter:
                visual.append(_merge_visual_line(run))
                run = [line]
            else:
                run.append(line)
        visual.append(_merge_visual_line(run))
    return sorted(visual, key=lambda line: line.top)


def _order_columns(lines: list[StextLine]) -> list[list[StextLine]]:
    """The page's lines as reading-order groups, see `_column_segments`."""
    return [group for segment in _column_segments(lines) for group in segment]


def _column_segments(lines: list[StextLine]) -> list[list[list[StextLine]]]:
    """The page's lines as segments of reading-order groups (proposal §4.3):
    a segment is one full-width block, or the columns between two blocks. Columns are
    found from the narrow lines only, grown by x-overlap: a line wider than
    WIDE_LINE of the text width, or crossing the gutter (a centred title, an
    abstract, a wide caption), would bridge both columns and merge the page.
    Such full-width blocks stay in place by vertical position; the column
    lines between two of them read left column in full, then the next."""
    if not lines:
        return []
    left = min(line.x0 for line in lines)
    right = max(line.x1 for line in lines)
    gutter = _gutter(lines, 0.0)

    def full_width(line: StextLine) -> bool:
        crosses = gutter is not None and line.x0 < gutter < line.x1
        return crosses or line.x1 - line.x0 > WIDE_LINE * (right - left)

    columns: list[list[StextLine]] = []
    spans: list[list[float]] = []
    narrow = [line for line in lines if not full_width(line)]
    for line in sorted(narrow, key=lambda item: (item.x0, item.top)):
        for column, span in zip(columns, spans, strict=True):
            if line.x0 < span[1] and line.x1 > span[0]:
                column.append(line)
                span[0] = min(span[0], line.x0)
                span[1] = max(span[1], line.x1)
                break
        else:
            columns.append([line])
            spans.append([line.x0, line.x1])
    # Stray narrow lines (a head, a page number, a short last line) are not
    # columns: with no gutter found, a cluster must hold a real share of them.
    substantial = [c for c in columns if len(c) >= 3 and len(c) >= COLUMN_SHARE * len(narrow)]
    if len(columns) < 2 or (gutter is None and len(substantial) < 2):
        # One column (or none): no gutter to respect, read straight down.
        return [[sorted(lines, key=lambda item: item.top)]]
    column_of = {id(line): number for number, column in enumerate(columns) for line in column}
    ordered = sorted(lines, key=lambda item: item.top)
    groups: list[list[list[StextLine]]] = []
    segment: list[list[StextLine]] = [[] for _ in columns]
    block: list[StextLine] = []

    def flush_segment() -> None:
        kept = [list(column) for column in segment if column]
        if kept:
            groups.append(kept)
        for column in segment:
            column.clear()

    for line in ordered:
        number = column_of.get(id(line))
        if number is None:
            if not block:
                flush_segment()
            block.append(line)
        else:
            if block:
                groups.append([block])
                block = []
            segment[number].append(line)
    if block:
        groups.append([block])
    flush_segment()
    return groups


def _running_head_texts(pages: list[StextPage]) -> set[str]:
    """Normalised texts recurring as the first line of a column, or as a page's
    bottom content line, on enough pages (`running_lines` semantics, counted
    per column): running heads and footers, dropped wherever they appear. A
    body line that opens a page once is never a candidate. The pages hold
    visual lines, so a full-width head is one line and still the first of the
    column it swallowed -- found, dropped, and the columns regrouped without
    it."""
    if len(pages) < HEADER_MIN_PAGES:
        return set()
    counts: Counter[str] = Counter()
    for page in pages:
        # Only the page's first segment: the first line of a later column or
        # block is body, however often a paragraph opens it.
        top = _column_segments(page.lines)[:1]
        candidates = {_normalise(group[0].text) for segment in top for group in segment if group}
        content = [
            line for line in page.lines if line.text.strip() and not is_page_number(line.text)
        ]
        if content:
            bottom = max(content, key=lambda line: line.top)
            candidates.add(_normalise(bottom.text))
        counts.update(candidates)
    return {text for text, seen in counts.items() if seen >= HEADER_SHARE * len(pages)}


def _split_footnotes(
    lines: list[StextLine], height: float
) -> tuple[list[StextLine], list[list[str]]]:
    """A column's lines split into body and footnote units: a marker-led line
    in small print or in the page's bottom band opens a footnote, and the
    wrapped lines after it continue it. The band is the bottom quarter of the
    page's height; conservative otherwise: a marker-led line of body size
    mid-page is body still."""
    if not lines:
        return [], []
    if height > 0:
        band_from = height * (1 - BOTTOM_BAND)
    else:
        tops = [line.top for line in lines]
        band_from = min(tops) + (max(tops) - min(tops)) * (1 - BOTTOM_BAND)
    sizes: Counter[float] = Counter()
    for line in lines:
        if line.size > 0:
            sizes[line.size] += len(line.text)
    dominant = max(sizes, key=sizes.get) if sizes else 0.0

    body: list[StextLine] = []
    units: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        small = line.size > 0 and line.size < SMALL_SIZE * dominant
        in_band = line.top >= band_from
        if FOOTNOTE_MARKER.match(line.text.lstrip()) and (small or in_band):
            if current is not None:
                units.append(current)
            current = [line.text]
        elif current is not None and (small or in_band):
            current.append(line.text)
        else:
            if current is not None:
                units.append(current)
                current = None
            body.append(line)
    if current is not None:
        units.append(current)
    return body, units


def _paragraph_lines(columns: list[list[StextLine]]) -> list[str]:
    """The columns' lines as paragraph lines: a blank line at every paragraph
    break -- a gap past one and a half times the column's median line gap, an
    indented first line, and every column boundary, since a new column is
    always a new paragraph. The indent matters on scans where the paragraph
    break is not wider than the line spacing (the Köhler page 16 case)."""
    out: list[str] = []
    for number, column in enumerate(columns):
        if number > 0 and out and out[-1] != "":
            out.append("")  # a new column is always a new paragraph
        if not column:
            continue
        gaps = [below.top - above.top for above, below in pairwise(column)]
        sorted_gaps = sorted(gaps)
        median = sorted_gaps[len(sorted_gaps) // 2] if sorted_gaps else 0.0
        sizes = sorted(line.size for line in column if line.size > 0)
        indent = 0.6 * (sizes[len(sizes) // 2] if sizes else 0.0)
        margin = min(line.x0 for line in column)
        for index, line in enumerate(column):
            if index > 0:
                gap = gaps[index - 1]
                if gap > max(median * PARAGRAPH_GAP, median + 2) or line.x0 - margin > indent:
                    out.append("")
            out.append(line.text)
    return out


def stext_body_pages(xml: str, footnotes: str) -> tuple[list[list[str]], list[list[list[str]]]]:
    """stext XML as (body pages of lines, kept footnote units per page), in
    reading order: the word-sized `<line>` elements are first merged into
    visual lines, then columns, then the recurring top-band heads out, then
    the footnotes held aside per the policy for their page's end."""
    pages = [StextPage(page.height, _visual_lines(page.lines)) for page in parse_stext(xml)]
    heads = _running_head_texts(pages)
    bodies: list[list[str]] = []
    notes: list[list[list[str]]] = []
    for page in pages:
        # The heads and the bare page numbers go here, in one pass: strip_pages
        # itself is not run afterwards, because its first-content-line counting
        # would promote whatever survived to "first" and cascade.
        kept = [
            line
            for line in page.lines
            if not is_page_number(line.text) and _normalise(line.text) not in heads
        ]
        body_columns: list[list[StextLine]] = []
        units: list[list[str]] = []
        for column in _order_columns(kept):
            body, column_units = _split_footnotes(column, page.height)
            body_columns.append(body)
            units.extend(column_units)
        bodies.append(_paragraph_lines(body_columns))
        notes.append(_policy_units(units, footnotes))
    return bodies, notes


def marker_footnote_pages(
    pages: list[list[str]], footnotes: str
) -> tuple[list[list[str]], list[list[list[str]]]]:
    """The pdftotext fallback: no geometry, so marker-only, and only at a page's
    tail -- its consecutive marker-led lines are footnote units, each its own;
    a wrapped continuation without a marker is body, kept rather than guessed."""
    bodies: list[list[str]] = []
    notes: list[list[list[str]]] = []
    for lines in pages:
        cut = len(lines)
        while cut > 0 and FOOTNOTE_MARKER.match(lines[cut - 1].lstrip()):
            cut -= 1
        tail = [line for line in lines[cut:] if line.strip()]
        bodies.append(lines[:cut])
        notes.append(_policy_units([[line] for line in tail], footnotes))
    return bodies, notes


def join_paragraphs(lines: list[str]) -> str:
    """One line per paragraph; blank lines separate paragraphs, `# ` lines stand alone."""
    paragraphs: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            paragraphs.append(current)
            current = ""

    for raw in lines:
        line = raw.strip()
        if not line:
            flush()
        elif line.startswith("# "):
            flush()
            paragraphs.append(line)
        elif not current:
            current = line
        elif current.endswith("-") and current[-2:-1].isalpha() and line[0].islower():
            current = current[:-1] + line
        else:
            current += " " + line
    flush()
    return "\n\n".join(paragraphs)


def cut_references(lines: list[str]) -> list[str]:
    """Cut at the last references heading, if it lies in the second half."""
    for index in range(len(lines) - 1, len(lines) // 2 - 1, -1):
        if REFERENCES.match(lines[index].strip()):
            return lines[:index]
    return lines


def prepare_text_file(source: Path, keep_references: bool) -> list[str]:
    """Normalise paragraphs of a text file; existing parts and headings pass through."""
    parts = []
    chunks = source.read_text(encoding="utf-8").split("\f")
    for index, chunk in enumerate(chunks):
        lines = chunk.splitlines()
        if not keep_references and index == len(chunks) - 1:
            lines = cut_references(lines)
        text = join_paragraphs(lines)
        if text:
            parts.append(text)
    return parts


def parse_pages(spec: str) -> tuple[int, int]:
    first, _, last = spec.partition("-")
    start, end = int(first), int(last or first)
    if start < 1 or end < start:
        raise ValueError(f"bad page range: {spec}")
    return start, end


class ToolMissing(SystemExit):
    """An external tool is not installed; exits like any other failure."""


def _run(command: list[str]) -> str:
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except FileNotFoundError:
        raise ToolMissing(f"{command[0]} not found; install poppler/mupdf tools") from None
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"{command[0]} failed: {error.stderr.strip()}") from None
    return done.stdout


def _outline(pdf: Path) -> list[tuple[str, int]]:
    try:
        return parse_outline(_run(["mutool", "show", str(pdf), "outline"]))
    except ToolMissing:
        print("chapters skipped: mutool (mupdf) is not installed", file=sys.stderr)
        return []
    except SystemExit:
        return []  # no outline just means one part


def _stext_pages_or_none(
    pdf: Path, pages: tuple[int, int] | None, footnotes: str
) -> tuple[list[list[str]], list[list[list[str]]]] | None:
    """The PDF's pages through mutool's stext, or None when that cannot be had:
    mutool missing, its call failing, or its answer not XML -- the pdftotext
    path takes over, degraded, one line saying so (never fatal)."""
    command = ["mutool", "draw", "-F", "stext", "-o", "-", str(pdf)]
    if pages:
        command.append(f"{pages[0]}-{pages[1]}")
    try:
        return stext_body_pages(_run(command), footnotes)
    except (ToolMissing, SystemExit):
        print(
            "columns and footnotes degraded: mutool (mupdf) is not installed or failed",
            file=sys.stderr,
        )
    except ElementTree.ParseError:
        print("columns and footnotes degraded: mutool's stext was not XML", file=sys.stderr)
    return None


def prepare_pdf(
    pdf: Path,
    pages: tuple[int, int] | None,
    chapters: str,
    keep_references: bool,
    keep_front_matter: bool = False,
    footnotes: str = "classify",
) -> list[str]:
    extracted = _stext_pages_or_none(pdf, pages, footnotes)
    if extracted is None:
        command = ["pdftotext"]
        if pages:
            command += ["-f", str(pages[0]), "-l", str(pages[1])]
        texts = _run([*command, str(pdf), "-"]).split("\f")
        if texts and not texts[-1].strip():
            texts.pop()  # pdftotext ends every page, the last included, with a form feed
        split = [text.splitlines() for text in texts]
        body_pages, note_units = marker_footnote_pages(split, footnotes)
        # No geometry here, so the existing heuristics keep their work: running
        # lines and bare page numbers over the body, the footnotes held aside
        # meanwhile, so a recurring footnote is never mistaken for a footer.
        body_pages = strip_pages(body_pages)
    else:
        body_pages, note_units = extracted
    first = pages[0] if pages else 1
    last_page = first + len(body_pages) - 1

    def group_lines(low: int, high: int) -> list[str]:
        """A page range's body lines, then its deferred footnotes at the end:
        a footnote wedged between two pages would split a sentence that runs
        across the page break (the Köhler p.35/36 case)."""
        body = [line for page in body_pages[low:high] for line in page]
        notes = [unit for units in note_units[low:high] for unit in units]
        return body + [line for unit in notes for line in ("", *unit)]

    # Each group is (title, its pages' lines). Pages before the first outline
    # entry are front matter (title page, contents) and are dropped unless
    # asked for; chapters starting outside --pages are dropped too.
    groups: list[tuple[str | None, list[str]]] = []
    outline = _outline(pdf) if chapters == "auto" else []
    if outline:
        starts = [page for _, page in outline]
        bounds = [*starts[1:], last_page + 1]
        if keep_front_matter and starts[0] > first:
            groups.append((None, group_lines(0, starts[0] - first)))
        for (title, start), end in zip(outline, bounds, strict=True):
            # A chapter that overlaps the range keeps its in-range pages;
            # only chapters wholly outside it are dropped.
            if start > last_page or end <= first:
                continue
            groups.append(
                (title, group_lines(max(start, first) - first, min(end, last_page + 1) - first))
            )
    else:
        groups.append((None, group_lines(0, len(body_pages))))

    if not keep_references and groups:
        # A whole trailing chapter called "References" goes; otherwise the
        # list starts somewhere inside the last chapter.
        while len(groups) > 1 and REFERENCES.match((groups[-1][0] or "").strip()):
            groups.pop()
        title, lines = groups[-1]
        groups[-1] = (title, cut_references(lines))
    parts = []
    for title, lines in groups:
        body = join_paragraphs(lines)
        if body:
            parts.append(f"# {title}\n\n{body}" if title else body)
    return parts


def summary(parts: list[str]) -> str:
    characters = sum(len(part) for part in parts)
    minutes = characters / CHARS_PER_SECOND / 60
    lines = [f"{len(parts)} parts, {characters} characters, about {minutes:.0f} min of speech"]
    for number, part in enumerate(parts, start=1):
        first = part.lstrip().splitlines()[0] if part.strip() else ""
        title = first[2:] if first.startswith("# ") else f"(untitled, Part {number})"
        lines.append(f"  {number}. {title}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help=".pdf, .txt or .md file")
    parser.add_argument("--out", type=Path, required=True, help="text file to write")
    parser.add_argument("--pages", help="PDF page range, e.g. 12-48")
    parser.add_argument("--chapters", choices=["auto", "none"], default="auto")
    parser.add_argument("--keep-references", action="store_true")
    parser.add_argument(
        "--keep-front-matter",
        action="store_true",
        help="keep pages before the first outline entry as an untitled leading part",
    )
    parser.add_argument(
        "--footnotes",
        choices=list(FOOTNOTE_POLICIES),
        default="classify",
        help=(
            "how footnotes are treated: classify drops the citation-shaped and reads"
            " the rest at their page's end (default), defer keeps all at page end,"
            " drop removes all"
        ),
    )
    parser.add_argument(
        "--ocr-report",
        action="store_true",
        help="say on stderr when the text looks like an OCR layer worth a spot-check pass",
    )
    args = parser.parse_args()

    if args.source.suffix.lower() == ".pdf":
        pages = parse_pages(args.pages) if args.pages else None
        parts = prepare_pdf(
            args.source,
            pages,
            args.chapters,
            args.keep_references,
            args.keep_front_matter,
            args.footnotes,
        )
    else:
        parts = prepare_text_file(args.source, args.keep_references)
    if not parts:
        print("no text found (a scanned PDF needs OCR first)", file=sys.stderr)
        return 1
    if args.ocr_report:
        suspicion = ocr_suspicion("\n".join(parts))
        if suspicion > OCR_SUSPICION_GATE:
            print(
                f"OCR artefacts likely (suspicion {suspicion:.1f}); worth a spot-check pass",
                file=sys.stderr,
            )
    args.out.write_text("\n\f\n".join(parts) + "\n", encoding="utf-8")
    print(summary(parts), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
