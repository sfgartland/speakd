#!/usr/bin/env python3
"""Turn a PDF, text or Markdown file into text `speakctl render` can narrate.

Output is the format `speakctl render` reads: parts separated by form feeds,
a part whose first non-blank line is `# <title>` becomes a named chapter.
Only layout is cleaned (running headers, page numbers, line wrapping,
references); abbreviations, ranges and ellipses are left to the daemon's
speech pipeline. Stdlib only, so it runs wherever the agent does; PDF
extraction shells out to poppler (`pdftotext`) and mupdf (`mutool`).
"""

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

CHARS_PER_SECOND = 15
HEADER_SHARE = 0.4
HEADER_MIN_PAGES = 5
REFERENCES = re.compile(r"^(References|Bibliography|Works Cited|Literatur(verzeichnis)?)$", re.I)
ARABIC_OR_ROMAN = re.compile(r"^(\d+|[ivxlcdm]+)$", re.I)
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


def _normalise(line: str) -> str:
    return re.sub(r"\d", "#", " ".join(line.split()))


def running_lines(pages: list[list[str]]) -> set[str]:
    """Normalised lines that open or close at least 40 % of the pages."""
    if len(pages) < HEADER_MIN_PAGES:
        return set()
    counts: Counter[str] = Counter()
    for page in pages:
        content = [_normalise(line) for line in page if line.strip()]
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
    for chunk in source.read_text(encoding="utf-8").split("\f"):
        lines = chunk.splitlines()
        text = join_paragraphs(lines if keep_references else cut_references(lines))
        if text:
            parts.append(text)
    return parts


def parse_pages(spec: str) -> tuple[int, int]:
    first, _, last = spec.partition("-")
    start, end = int(first), int(last or first)
    if start < 1 or end < start:
        raise ValueError(f"bad page range: {spec}")
    return start, end


def _run(command: list[str]) -> str:
    try:
        done = subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        raise SystemExit(f"{command[0]} not found; install poppler/mupdf tools") from None
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"{command[0]} failed: {error.stderr.strip()}") from None
    return done.stdout


def _outline(pdf: Path) -> list[tuple[str, int]]:
    try:
        return parse_outline(_run(["mutool", "show", str(pdf), "outline"]))
    except SystemExit:
        return []  # no outline (or no mutool) just means one part


def prepare_pdf(
    pdf: Path, pages: tuple[int, int] | None, chapters: str, keep_references: bool
) -> list[str]:
    command = ["pdftotext"]
    if pages:
        command += ["-f", str(pages[0]), "-l", str(pages[1])]
    texts = _run([*command, str(pdf), "-"]).split("\f")
    if texts and not texts[-1].strip():
        texts.pop()  # pdftotext ends every page, the last included, with a form feed
    first = pages[0] if pages else 1
    cleaned = strip_pages([text.splitlines() for text in texts])

    # Each group is (title, its pages' lines); pages before the first outline
    # entry stay as an untitled part rather than being lost.
    groups: list[tuple[str | None, list[str]]] = []
    outline = _outline(pdf) if chapters == "auto" else []
    if outline:
        starts = [page for _, page in outline]
        last_page = first + len(cleaned) - 1
        bounds = [*starts[1:], last_page + 1]
        if starts[0] > first:
            groups.append((None, [ln for p in cleaned[: starts[0] - first] for ln in p]))
        for (title, start), end in zip(outline, bounds, strict=True):
            chosen = cleaned[max(start, first) - first : max(end, first) - first]
            groups.append((title, [line for page in chosen for line in page]))
    else:
        groups.append((None, [line for page in cleaned for line in page]))

    if not keep_references and groups:
        title, lines = groups[-1]
        groups[-1] = (title, cut_references(lines))
    parts = []
    for title, lines in groups:
        body = join_paragraphs(lines)
        if body or title:
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
    args = parser.parse_args()

    if args.source.suffix.lower() == ".pdf":
        pages = parse_pages(args.pages) if args.pages else None
        parts = prepare_pdf(args.source, pages, args.chapters, args.keep_references)
    else:
        parts = prepare_text_file(args.source, args.keep_references)
    if not parts:
        print("no text found (a scanned PDF needs OCR first)", file=sys.stderr)
        return 1
    args.out.write_text("\n\f\n".join(parts) + "\n", encoding="utf-8")
    print(summary(parts), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
