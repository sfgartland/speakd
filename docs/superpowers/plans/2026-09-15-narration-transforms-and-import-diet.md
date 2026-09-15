# Narration: import diet and transform fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the Claude Code hook loading numpy on every tool call, and make a long spoken answer sound like prose instead of one breathless run-on.

**Architecture:** Two unrelated fixes that share a branch because both are small, self-contained and audible. The first moves one function out of a module that drags in the synthesis pipeline. The second changes `markdown()` from "one Piece in, one Piece out, everything joined by a space" to "one Piece per block", which gives the segmenter real boundaries to breathe at, then extends the substitution tables.

**Tech Stack:** Python 3.11–3.13, pytest, ruff, mypy (strict). No new dependencies.

**Spec:** `docs/design/2026-09-15-streaming-narration-design.md` (§1, §2)

## Global Constraints

- **Never run `uv sync`, in any form, including `uv sync --group dev`.** This venv holds a hand-installed CPU-only torch (`2.14.0+cpu`) plus kokoro, sounddevice, scipy, spacy and transformers; any sync prunes them. **Always `uv run --no-sync ...`.** Verified 2026-09-14.
- Run tests with `uv run --no-sync pytest`.
- `line-length = 100` (ruff). Lint with `uv run --no-sync ruff check src tests`.
- mypy is `strict = true`, `python_version = "3.12"`. Type every new function.
- Target `py310` syntax in ruff terms; `requires-python` is `>=3.11,<3.14`.
- Branch: `feat/streaming-narration`, already created and holding the design doc.
- This project writes *why* in comments, not *what*. Match that: a comment that restates the code will be rejected in review.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/speakd/paths.py` | **new.** Where things live on disk. Imports `os` and `pathlib` and nothing else, ever. |
| `src/speakd/cli.py` | loses `default_socket_path`'s body, re-exports it |
| `src/speakd/clients/claude_code/send.py` | imports the path helper from `paths`, not `cli` |
| `src/speakd/transforms/markdown.py` | gains `_Block` and `_blocks`; `markdown()` emits one Piece per block; quote and table rules |
| `src/speakd/transforms/pronunciation.py` | empty-paren, path, em-dash rules; more acronyms |
| `tests/test_import_cost.py` | **new.** Guards the import diet against regression |
| `tests/test_markdown_transform.py` | helper joins pieces; new block-boundary and span tests |
| `tests/test_pronunciation_transform.py` | new rule tests |

---

### Task 1: Move `default_socket_path` off the pipeline

The Claude Code hook costs ~250 ms, of which ~200 ms is import. `send.py` imports `speakd.cli` to call `default_socket_path()` — a function that reads an environment variable and joins a `Path` — and `cli.py` imports `speakd.pipeline`, which imports numpy (81 ms), and `speakd.profiles`, which imports tomllib (21 ms). Every tool call pays for the whole synthesis stack to build a path.

**Files:**
- Create: `src/speakd/paths.py`
- Modify: `src/speakd/cli.py:30-34` (remove the body, re-export), `src/speakd/clients/claude_code/send.py:14`
- Test: `tests/test_import_cost.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `speakd.paths.default_socket_path() -> pathlib.Path`. `speakd.cli.default_socket_path` remains a working name for every existing caller.

- [ ] **Step 1: Write the failing test**

Create `tests/test_import_cost.py`:

```python
"""The hook's import cost, guarded.

`send.py` once reached into `cli.py` for `default_socket_path`, and `cli.py`
imports the synthesis pipeline, so every Claude Code tool call loaded numpy to
build a path -- about 200ms of a 250ms hook. Nothing visible breaks when that
comes back, which is why it is a test and not a comment.
"""

import subprocess
import sys


def _modules_after_importing(module: str) -> set[str]:
    """Import `module` in a clean interpreter and report what came with it.

    A subprocess rather than an assertion about this interpreter's
    `sys.modules`: pytest has already imported numpy by the time any test
    runs, so an in-process check can only ever pass.
    """
    source = f"import {module}, sys; print('\\n'.join(sorted(sys.modules)))"
    result = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(result.stdout.split())


def test_the_hooks_send_path_does_not_import_numpy() -> None:
    loaded = _modules_after_importing("speakd.clients.claude_code.send")
    assert "numpy" not in loaded


def test_the_hooks_send_path_does_not_import_the_pipeline() -> None:
    loaded = _modules_after_importing("speakd.clients.claude_code.send")
    assert "speakd.pipeline" not in loaded
    assert "speakd.profiles" not in loaded
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --no-sync pytest tests/test_import_cost.py -v`
Expected: both tests FAIL — numpy, `speakd.pipeline` and `speakd.profiles` are all present.

- [ ] **Step 3: Create the leaf module**

Create `src/speakd/paths.py`:

```python
"""Where things live on disk.

Deliberately a leaf: this module imports `os` and `pathlib` and must never
import anything from `speakd`. It exists because `default_socket_path` used to
live in `cli.py`, which imports the synthesis pipeline -- so the Claude Code
hook loaded numpy on every tool call to build a socket path. Anything added
here inherits that obligation.
"""

from __future__ import annotations

import os
from pathlib import Path


def default_socket_path() -> Path:
    """Where the daemon listens, following XDG with a sensible fallback."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path(os.environ.get("TMPDIR", "/tmp"))
    return base / "speakd" / "speakd.sock"
```

- [ ] **Step 4: Re-export from `cli.py` and import it in `send.py`**

In `src/speakd/cli.py`, delete the `default_socket_path` definition at lines 30-34 and add to the import block near the top:

```python
from speakd.paths import default_socket_path
```

Keep the name importable from `cli`: `speakctl`, `__main__.py` and the test suite all use `speakd.cli.default_socket_path`, and breaking them is not this task's job. Add `"default_socket_path"` to an `__all__` in `cli.py` so ruff does not flag the re-export as unused:

```python
__all__ = ["default_socket_path", "main"]
```

In `src/speakd/clients/claude_code/send.py`, change line 14 from `from speakd.cli import default_socket_path` to:

```python
from speakd.paths import default_socket_path
```

- [ ] **Step 5: Run the new tests, then the whole suite**

Run: `uv run --no-sync pytest tests/test_import_cost.py -v`
Expected: PASS.

Run: `uv run --no-sync pytest`
Expected: PASS. `test_cli.py` and `test_cc_send.py` both touch this; if either fails it is because a patch target moved — `monkeypatch.setattr("speakd.cli.default_socket_path", ...)` no longer affects `send.py`, which now reads `speakd.paths`. Repoint such a patch at `speakd.clients.claude_code.send.default_socket_path`, which is the name that module actually calls.

- [ ] **Step 6: Measure it, and record the number**

Run:

```bash
for i in 1 2 3; do
  /usr/bin/time -f "%e s" uv run --no-sync python -c \
    "import speakd.clients.claude_code.send" 2>&1 | tail -1
done
```

Expected: ~0.07 s, down from ~0.20 s. Put the measured figure in the commit message. If it has not moved, something else in `send.py`'s import graph is heavy — find it with `python -X importtime` before proceeding.

- [ ] **Step 7: Lint, type-check, commit**

```bash
uv run --no-sync ruff check src tests
uv run --no-sync mypy src
git add src/speakd/paths.py src/speakd/cli.py src/speakd/clients/claude_code/send.py tests/test_import_cost.py
git commit -m "Build the socket path without loading the synthesis pipeline"
```

---

### Task 2: One Piece per block

`markdown()` renders a Piece to a single string joining every line with `" "`. The segmenter then splits that on sentence boundaries only, so paragraphs, headings and list items run together with no pause — which is why a long answer sounds like one breathless run-on. Emitting one Piece per block gives the segmenter real boundaries, and lets each block carry the source offsets it actually came from, which the timeline needs for follow-along highlighting.

**Files:**
- Modify: `src/speakd/transforms/markdown.py:56-95`
- Test: `tests/test_markdown_transform.py`

**Interfaces:**
- Consumes: `Piece`, `Span` from `speakd.model` (already imported).
- Produces: `markdown(pieces: Sequence[Piece]) -> list[Piece]`, unchanged in signature but now returning **one Piece per block** rather than one per input Piece. `_render` is replaced by `_blocks(text: str) -> list[_Block]`, where `_Block` is a frozen dataclass with `text: str`, `start: int`, `end: int` (offsets into the input text).

- [ ] **Step 1: Update the existing test helper first**

Every current assertion in `tests/test_markdown_transform.py` reads `pieces[0].spoken`, which will only ever see the first block once this task lands. Change the helper at the top of the file to join, so the existing assertions keep testing what they were written to test:

```python
def render(text: str) -> str:
    pieces = markdown([Piece(span=Span(0, len(text)), spoken=text)])
    return " ".join(piece.spoken for piece in pieces)
```

Run: `uv run --no-sync pytest tests/test_markdown_transform.py -v`
Expected: PASS, unchanged — the helper joins exactly as `_render` did, so this edit is a no-op today and stops being one in Step 3.

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_markdown_transform.py`:

```python
def blocks(text: str) -> list[str]:
    return [piece.spoken for piece in markdown([Piece(span=Span(0, len(text)), spoken=text)])]


def test_each_paragraph_becomes_its_own_piece() -> None:
    assert blocks("First para.\n\nSecond para.") == ["First para.", "Second para."]


def test_wrapped_lines_stay_one_paragraph() -> None:
    # A blank line separates paragraphs; a newline inside one does not.
    assert blocks("One sentence\nwrapped over lines.") == ["One sentence wrapped over lines."]


def test_each_bullet_is_its_own_piece() -> None:
    assert blocks("- first\n- second") == ["first.", "second."]


def test_a_heading_is_its_own_piece() -> None:
    assert blocks("## Results\n\nThe body.") == ["Results.", "The body."]


def test_a_code_fence_is_one_piece() -> None:
    assert blocks("Before.\n```py\nx = 1\n```\nAfter.") == [
        "Before.",
        "Code block omitted.",
        "After.",
    ]


def test_a_blocks_span_points_at_the_text_it_came_from() -> None:
    source = "First para.\n\nSecond para."
    pieces = markdown([Piece(span=Span(0, len(source)), spoken=source)])
    second = pieces[1]
    assert source[second.span.start : second.span.end].strip() == "Second para."


def test_spans_are_offset_by_the_input_pieces_own_start() -> None:
    # The piece is a window into a larger job, so block offsets are relative
    # to the piece's start, not to zero.
    source = "First para.\n\nSecond para."
    pieces = markdown([Piece(span=Span(100, 100 + len(source)), spoken=source)])
    assert pieces[0].span.start == 100
    assert pieces[1].span.start > 100


def test_an_inexact_piece_gives_every_block_the_whole_span() -> None:
    # Offsets into rewritten text do not correspond to the source, so
    # sub-spans would point at the wrong characters. Better to be coarse
    # than wrong -- see Piece.exact in model.py.
    source = "First para.\n\nSecond para."
    piece = Piece(span=Span(0, 999), spoken=source, exact=False)
    assert [p.span for p in markdown([piece])] == [Span(0, 999), Span(0, 999)]
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run --no-sync pytest tests/test_markdown_transform.py -v`
Expected: the new `test_each_paragraph_becomes_its_own_piece` and its neighbours FAIL — `blocks()` returns a single joined string. The pre-existing tests still PASS.

- [ ] **Step 4: Replace `_render` with `_blocks`**

In `src/speakd/transforms/markdown.py`, add `from dataclasses import dataclass` and `from speakd.model import Piece, Span` to the imports, then replace `_render` (lines 56-85) with:

```python
@dataclass(frozen=True)
class _Block:
    """One rendered block, and the offsets into the input it came from."""

    text: str
    start: int
    end: int


def _blocks(text: str) -> list[_Block]:
    """Split markdown into blocks, rendering each.

    A block is the unit a listener hears as one breath: a paragraph, a
    heading, a list item, an omitted code fence. Consecutive prose lines join
    into one block because a hard-wrapped paragraph is still one paragraph; a
    blank line ends it.
    """
    blocks: list[_Block] = []
    prose: list[str] = []
    prose_start = 0
    prose_end = 0
    in_fence = False
    fence_start = 0
    offset = 0

    def flush() -> None:
        nonlocal prose
        if prose:
            blocks.append(_Block(" ".join(prose), prose_start, prose_end))
            prose = []

    for raw in text.splitlines(keepends=True):
        start = offset
        offset += len(raw)
        line = raw.rstrip("\n")

        if _FENCE.match(line):
            if in_fence:
                blocks.append(_Block(CODE_BLOCK_MARKER, fence_start, offset))
                in_fence = False
            else:
                flush()
                in_fence = True
                fence_start = start
            continue
        if in_fence:
            continue
        if _RULE.match(line):
            flush()
            continue

        heading = _HEADING.match(line)
        if heading:
            flush()
            rendered = _terminate(_inline(heading.group(1)))
            if rendered:
                blocks.append(_Block(rendered, start, offset))
            continue

        item = _BULLET.match(line) or _ORDERED.match(line)
        if item:
            flush()
            rendered = _terminate(_inline(item.group(1)))
            if rendered:
                blocks.append(_Block(rendered, start, offset))
            continue

        quote = _QUOTE.match(line)
        if quote:
            line = quote.group(1)

        rendered = _inline(line).strip()
        if not rendered:
            flush()
            continue
        if not prose:
            prose_start = start
        prose_end = offset
        prose.append(rendered)

    flush()
    # An unterminated fence still swallowed its content; say so rather than
    # silently dropping it.
    if in_fence:
        blocks.append(_Block(CODE_BLOCK_MARKER, fence_start, offset))
    return blocks
```

Then replace `markdown()` (lines 88-95) with:

```python
def markdown(pieces: Sequence[Piece]) -> list[Piece]:
    """Render markdown as speech, one Piece per block.

    One in, many out: the segmenter splits on sentence boundaries only, so a
    single joined Piece gave a listener no pause between paragraphs. Blocks
    are those pauses.
    """
    out: list[Piece] = []
    for piece in pieces:
        for block in _blocks(piece.spoken):
            if not block.text.strip():
                continue
            # Sub-spans are only meaningful while `spoken` is still the
            # verbatim source at `span`. An upstream transform that rewrote it
            # leaves offsets that no longer correspond, and a wrong span is
            # worse than a coarse one -- it highlights the wrong words
            # silently. See Piece.exact in model.py.
            span = (
                Span(piece.span.start + block.start, piece.span.start + block.end)
                if piece.exact
                else piece.span
            )
            out.append(Piece(span=span, spoken=block.text, exact=False))
    return out
```

Update the module docstring's first line to say what it now does: "Render markdown as speech, one block at a time."

- [ ] **Step 5: Run the tests**

Run: `uv run --no-sync pytest tests/test_markdown_transform.py -v`
Expected: PASS, new and old alike.

Run: `uv run --no-sync pytest`
Expected: PASS. `tests/test_transforms_end_to_end.py` and `tests/test_transform_chain.py` are the likely casualties — both may assert a piece count. A count that changed from 1 to N is this change working, not breaking: update the expectation. A count that changed to 0, or text that vanished, is a real bug — stop and find it.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run --no-sync ruff check src tests
uv run --no-sync mypy src
git add src/speakd/transforms/markdown.py tests/test_markdown_transform.py
git commit -m "Give each markdown block its own piece, and its own span"
```

---

### Task 3: Quote and table blocks

Today a `>` quote has its marker stripped and is spoken as though it were the speaker's own sentence, and a markdown table is read as a row of pipes. Both now become blocks of their own.

**Files:**
- Modify: `src/speakd/transforms/markdown.py`
- Test: `tests/test_markdown_transform.py`

**Interfaces:**
- Consumes: `_Block`, `_blocks` from Task 2.
- Produces: module constants `QUOTE_PREFIX = "Quote,"`, `QUOTE_SUFFIX = "End quote."`, `TABLE_MARKER = "Table omitted."`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_block_quote_is_announced_and_closed() -> None:
    assert blocks("> Nothing waits on that.") == ["Quote, Nothing waits on that. End quote."]


def test_consecutive_quote_lines_are_one_quotation() -> None:
    assert blocks("> One line.\n> And another.") == ["Quote, One line. And another. End quote."]


def test_prose_after_a_quote_is_its_own_block() -> None:
    assert blocks("> Quoted.\n\nMine.") == ["Quote, Quoted. End quote.", "Mine."]


def test_a_table_is_announced_rather_than_read() -> None:
    table = "| a | b |\n| --- | --- |\n| 1 | 2 |"
    assert blocks(table) == ["Table omitted."]


def test_a_table_between_paragraphs_keeps_them_apart() -> None:
    source = "Before.\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\nAfter."
    assert blocks(source) == ["Before.", "Table omitted.", "After."]


def test_a_pipe_in_prose_is_not_a_table() -> None:
    # A table row is delimited at both ends; prose mentioning a pipe is not.
    assert blocks("Use a | to pipe.") == ["Use a | to pipe."]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-sync pytest tests/test_markdown_transform.py -k "quote or table or pipe" -v`
Expected: FAIL — quotes come back bare, table rows come back as pipes.

- [ ] **Step 3: Implement**

Add near the other patterns in `markdown.py`:

```python
QUOTE_PREFIX = "Quote,"
QUOTE_SUFFIX = "End quote."

# A table row is delimited at both ends. Prose that merely mentions a pipe is
# not a table, and reading a six-by-three table aloud is worse than saying
# nothing -- the same trade the code fence already makes.
TABLE_MARKER = "Table omitted."
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
```

In `_blocks`, add two accumulators beside `prose`:

```python
    quote: list[str] = []
    quote_start = 0
    quote_end = 0
    table_start = 0
    table_end = 0
    in_table = False
```

and two flushes beside `flush()`:

```python
def flush_quote() -> None:
    nonlocal quote
    if quote:
        said = " ".join(quote)
        blocks.append(_Block(f"{QUOTE_PREFIX} {said} {QUOTE_SUFFIX}", quote_start, quote_end))
        quote = []


def flush_table() -> None:
    nonlocal in_table
    if in_table:
        blocks.append(_Block(TABLE_MARKER, table_start, table_end))
        in_table = False
```

Rename the existing `flush()` to `flush_prose()` and define one `flush_all()` that calls all three, then replace every existing bare `flush()` call site with `flush_all()`. This matters: a heading directly after a quote must close the quote, not just the prose.

```python
    def flush_all() -> None:
        flush_prose()
        flush_quote()
        flush_table()
```

Add the table branch **before** the quote and prose branches, and the quote branch before the prose branch:

```python
        if _TABLE_ROW.match(line):
            flush_prose()
            flush_quote()
            if not in_table:
                in_table = True
                table_start = start
            table_end = offset
            continue
        flush_table()

        quoted = _QUOTE.match(line)
        if quoted:
            flush_prose()
            rendered = _terminate(_inline(quoted.group(1)))
            if rendered:
                if not quote:
                    quote_start = start
                quote_end = offset
                quote.append(rendered)
            continue
        flush_quote()
```

Delete the old three-line `quote = _QUOTE.match(line)` block that stripped the marker into prose. Replace the final `flush()` before the unterminated-fence check with `flush_all()`, and make the fence, rule, heading and list branches call `flush_all()`.

- [ ] **Step 4: Run the tests**

Run: `uv run --no-sync pytest tests/test_markdown_transform.py -v`
Expected: PASS, including the Task 2 tests unchanged.

Run: `uv run --no-sync pytest`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run --no-sync ruff check src tests
uv run --no-sync mypy src
git add src/speakd/transforms/markdown.py tests/test_markdown_transform.py
git commit -m "Announce a quotation as a quotation, and decline to read tables"
```

---

### Task 4: Pronunciation rules

Measured against this session's own output: `~/.claude/settings.json` was spoken as "tilde slash dot claude slash settings dot jason", `speakable()` carried its parentheses into the audio, `CLI` became "C L I" while `GUI` stayed "GUI", and every em dash passed through untouched.

**Files:**
- Modify: `src/speakd/transforms/pronunciation.py`
- Test: `tests/test_pronunciation_transform.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: no new public names; `_apply` gains three rules and `_WORDS` gains thirteen entries.

- [ ] **Step 1: Write the failing tests**

Use whatever single-string helper `tests/test_pronunciation_transform.py` already defines; if it has none, add:

```python
def say(text: str) -> str:
    pieces = pronunciation([Piece(span=Span(0, len(text)), spoken=text)])
    return pieces[0].spoken if pieces else ""
```

Then:

```python
def test_an_empty_call_loses_its_parentheses() -> None:
    assert say("Call speakable() first.") == "Call speakable first."


def test_a_path_is_read_as_separated_names() -> None:
    assert say("~/.claude/settings.json") == "dot claude, settings dot jason"


def test_a_leading_dot_slash_goes_too() -> None:
    assert say("./src/speakd") == "src, speakd"


def test_a_url_is_left_alone() -> None:
    # Speaking "https, , example dot com" is worse than speaking the URL.
    assert "https://example.com/x" in say("See https://example.com/x now.")


def test_an_em_dash_becomes_a_comma() -> None:
    assert say("One thing — and another.") == "One thing, and another."


def test_gui_is_pronounced() -> None:
    assert say("the GUI") == "the gooey"


def test_the_new_acronyms_are_spelled_out() -> None:
    assert say("PDF") == "P D F"
    assert say("CPU") == "C P U"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-sync pytest tests/test_pronunciation_transform.py -v`
Expected: the seven new tests FAIL.

- [ ] **Step 3: Implement**

Add the patterns beside `_FILENAME`:

```python
# "speakable()" reaches the engine with its parentheses intact and is spoken
# as such. Only the empty pair: a call with arguments in prose is rare, and
# stripping its brackets would weld the arguments onto the name.
_EMPTY_CALL = re.compile(r"(?<=\w)\(\)")

# A path is a run of name-and-slash with no spaces. Read literally the engine
# says "slash" between every segment; as commas it reads as the list of names
# it is. A leading "~/" or "./" carries no information a listener wants.
#
# Anything containing "://" is left alone: a URL spoken as separated names is
# worse than a URL spoken as a URL, and the negative lookahead below is
# cheaper than unpicking the scheme afterwards.
_PATH = re.compile(r"(?<![\w:/])(?:~/|\./)?(?:[\w.@+-]+/)+[\w.@+-]+")

# An em dash between clauses is a prosodic break the engine does not take on
# its own. The surrounding whitespace goes with it, or the comma arrives with
# a space in front of it.
_EM_DASH = re.compile(r"\s*—\s*")
```

Add a replacement function beside `_spoken_range`:

```python
def _spoken_path(match: re.Match[str]) -> str:
    """Read a path as its names, or hand back a URL unchanged."""
    token = match.group(0)
    if "://" in token:
        return token
    for prefix in ("~/", "./"):
        if token.startswith(prefix):
            token = token[len(prefix) :]
            break
    return token.replace("/", ", ")
```

Extend `_apply`, keeping the existing order and placing the new rules where the comments say:

```python
def _apply(text: str) -> str:
    # Ranges first: the literal table rewrites "->" and "..." and there is no
    # reason to let either reach a range before this rule has seen it.
    text = _NUMERIC_RANGE.sub(_spoken_range, text)
    text = _EM_DASH.sub(", ", text)
    text = _EMPTY_CALL.sub("", text)
    for needle, replacement in _LITERAL:
        text = text.replace(needle, replacement)
    # Before _FILENAME, which would otherwise rewrite the extension inside a
    # path and leave a "dot" for this rule's comma to land beside.
    text = _PATH.sub(_spoken_path, text)
    text = _FILENAME.sub(r"\1 dot \2", text)
    text = _CHAINED.sub(r"\1 dot \2", text)
    for pattern, replacement in _WORDS:
        text = pattern.sub(replacement, text)
    return " ".join(text.split())
```

Add to `_WORDS`, after the existing `CLI` entry. Pronounceable acronyms get a phonetic spelling, as `SQL` and `TOML` already do; the rest are spelled out:

```python
((re.compile(r"\bGUI\b"), "gooey"),)
((re.compile(r"\bHTML\b"), "H T M L"),)
((re.compile(r"\bTTS\b"), "T T S"),)
((re.compile(r"\bPDF\b"), "P D F"),)
((re.compile(r"\bXDG\b"), "X D G"),)
((re.compile(r"\bRTF\b"), "R T F"),)
((re.compile(r"\bOCR\b"), "O C R"),)
((re.compile(r"\bCSV\b"), "C S V"),)
((re.compile(r"\bSSH\b"), "S S H"),)
((re.compile(r"\bCPU\b"), "C P U"),)
((re.compile(r"\bGPU\b"), "G P U"),)
((re.compile(r"\bIDE\b"), "I D E"),)
((re.compile(r"\bCI\b"), "C I"),)
```

Record the ordering constraint in the module docstring: `_PATH` must run before `_FILENAME`, and `_EM_DASH` before `_LITERAL` so that `...` and `->` cannot fragment a dash that has not been seen yet.

- [ ] **Step 4: Run the tests**

Run: `uv run --no-sync pytest tests/test_pronunciation_transform.py -v`
Expected: PASS.

Run: `uv run --no-sync pytest`
Expected: PASS. `tests/test_transforms_end_to_end.py` may carry a fixture with a path or an em dash in it; update the expectation to the new reading.

- [ ] **Step 5: Listen to it**

The point of this task is audible, so check it by ear rather than only by assertion:

```bash
uv run --no-sync speakctl say "Open ~/.claude/settings.json — the GUI reads it. Call speakable() first."
```

Expected: "Open dot claude, settings dot jason, the gooey reads it. Call speakable first."

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run --no-sync ruff check src tests
uv run --no-sync mypy src
git add src/speakd/transforms/pronunciation.py tests/test_pronunciation_transform.py
git commit -m "Read paths as names, take the em dash as a breath, and say GUI"
```

---

## Done when

- `uv run --no-sync pytest` passes.
- Importing `speakd.clients.claude_code.send` loads neither numpy nor the pipeline, measured at roughly 0.07 s.
- `speakctl say` on a multi-paragraph markdown input pauses between paragraphs, announces quotations, declines tables, and reads paths as names.
