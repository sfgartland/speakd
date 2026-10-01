"""The speakd-audiobook skill's text preparation script."""

import importlib.util
import os
import shutil
import subprocess as sp
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "clients" / "claude-code" / "skills" / "speakd-audiobook"
SCRIPT = SKILL / "scripts" / "prepare_text.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "outline.pdf"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prepare_text", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prep = _load()

# Captured from `mutool show outline.pdf outline`: "|" marks nesting, and the
# number of tabs after it gives the depth.
OUTLINE = (
    '+\t"Introduction"\t#page=1&zoom=100,72,36\n'
    '|\t\t"Nested note"\t#page=2&zoom=100,72,36\n'
    '|\t"Argument: a \\"turn\\""\t#page=4&zoom=100,72,36\n'
)


def test_outline_keeps_top_level_entries_only() -> None:
    assert prep.parse_outline(OUTLINE) == [("Introduction", 1), ('Argument: a "turn"', 4)]


def test_outline_of_nothing_is_empty() -> None:
    assert prep.parse_outline("") == []


def test_page_number_lines() -> None:
    for line in ("12", "  7 ", "iv", "XIV", "lxxx", "xl", "xlv"):
        assert prep.is_page_number(line)
    for line in ("", "Chapter 1", "12 apples", "word", "mix", "did", "civil"):
        assert not prep.is_page_number(line)


def test_running_lines_need_five_pages_and_forty_percent() -> None:
    def page(n: int) -> list[str]:
        return ["Sample Journal", "", f"body {n}", f"Page {n}"]

    found = prep.running_lines([page(n) for n in range(1, 7)])
    assert found == {"Sample Journal", "Page #"}
    assert prep.running_lines([page(n) for n in range(1, 5)]) == set()
    # Header on 2 of 6 pages (33%) is body text, not a running header.
    mixed = [["Odd one"] + page(n) if n < 3 else page(n) for n in range(1, 7)]
    assert "Odd one" not in prep.running_lines(mixed)


def test_strip_pages_removes_running_lines_and_page_numbers() -> None:
    pages = [["Sample Journal", f"text {n}", f"w{chr(96 + n)}x", "", str(n)] for n in range(1, 6)]
    out = prep.strip_pages(pages)
    assert out == [[f"text {n}", f"w{chr(96 + n)}x", ""] for n in range(1, 6)]


def test_join_paragraphs_dehyphenates_and_keeps_breaks() -> None:
    lines = ["an exam-", "ple of it", "and more", "", "", "next par-", "Agraph", "- item"]
    assert prep.join_paragraphs(lines) == "an example of it and more\n\nnext par- Agraph - item"


def test_join_paragraphs_leaves_headings_alone() -> None:
    assert prep.join_paragraphs(["# Title", "body", "more"]) == "# Title\n\nbody more"


def test_references_cut_at_last_heading_in_second_half() -> None:
    lines = ["a", "b", "c", "d", "References", "x", "y"]
    assert prep.cut_references(lines) == ["a", "b", "c", "d"]
    lines = ["a", "b", "c", "d", "bibliography", "x", "y", "Works Cited", "z"]
    assert prep.cut_references(lines) == ["a", "b", "c", "d", "bibliography", "x", "y"]


def test_references_in_first_half_are_ignored() -> None:
    lines = ["References", "a", "b", "c", "d", "e"]
    assert prep.cut_references(lines) == lines


def test_references_pattern_covers_german() -> None:
    lines = ["a", "b", "c", "Literaturverzeichnis", "x"]
    assert prep.cut_references(lines) == ["a", "b", "c"]


def test_text_input_passes_through_parts_and_headings(tmp_path: Path) -> None:
    source = tmp_path / "in.md"
    source.write_text("# One\nline a\nline b\n\f# Two\nhello\n\n\nworld\n", encoding="utf-8")
    parts = prep.prepare_text_file(source, keep_references=True)
    assert parts == ["# One\n\nline a line b", "# Two\n\nhello\n\nworld"]


def test_parse_pages() -> None:
    assert prep.parse_pages("12-48") == (12, 48)
    assert prep.parse_pages("5") == (5, 5)
    with pytest.raises(ValueError):
        prep.parse_pages("9-2")


def test_cli_text_input_writes_parts_and_summary(tmp_path: Path) -> None:
    source = tmp_path / "in.txt"
    source.write_text("one\ntwo\n\f# Named\nthree\n", encoding="utf-8")
    out = tmp_path / "out.txt"
    done = sp.run(
        [sys.executable, str(SCRIPT), str(source), "--out", str(out)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    chunks = out.read_text(encoding="utf-8").split("\f")
    assert [c.strip() for c in chunks] == ["one two", "# Named\n\nthree"]
    assert "2 parts" in done.stderr
    assert "Named" in done.stderr
    assert "characters" in done.stderr


@pytest.mark.skipif(
    not (shutil.which("pdftotext") and shutil.which("mutool")),
    reason="needs poppler and mupdf tools",
)
def test_pdf_end_to_end_with_outline(tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    done = sp.run(
        [sys.executable, str(SCRIPT), str(FIXTURE), "--out", str(out)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    parts = out.read_text(encoding="utf-8").split("\f")
    assert [p.strip().splitlines()[0] for p in parts] == ["# Introduction", "# Argument"]
    text = out.read_text(encoding="utf-8")
    assert "Sample Journal" not in text
    assert "References" not in text and "Doe, J." not in text
    assert "Body sentence one on page 3." in text
    assert "A second line of the same paragraph continues with charlie." in text
    assert "Introduction" in done.stderr and "Argument" in done.stderr

    single = tmp_path / "single.txt"
    sp.run(
        [
            sys.executable,
            str(SCRIPT),
            str(FIXTURE),
            "--out",
            str(single),
            "--chapters",
            "none",
            "--keep-references",
            "--pages",
            "5-6",
        ],
        check=True,
        capture_output=True,
    )
    body = single.read_text(encoding="utf-8")
    assert "\f" not in body and "# " not in body
    assert "Doe, J." in body and "page 4" not in body


def test_running_lines_look_past_page_numbers() -> None:
    above = [
        ["(c) 2020 Publisher", f"body {n}", f"more {chr(96 + n)}x", str(n)] for n in range(1, 7)
    ]
    below = [[str(n), "Running Title", f"body {n}", f"more {chr(96 + n)}x"] for n in range(1, 7)]
    assert prep.running_lines(above) == {"(c) #### Publisher"}
    assert prep.running_lines(below) == {"Running Title"}


def test_references_cut_only_in_last_text_chunk(tmp_path: Path) -> None:
    source = tmp_path / "in.txt"
    source.write_text(
        "a\nb\nc\nd\nReferences\nx\n\f# Two\n1\n2\n3\n4\nBibliography\ny\n",
        encoding="utf-8",
    )
    parts = prep.prepare_text_file(source, keep_references=False)
    assert parts[0].endswith("References x")
    assert "Bibliography" not in parts[1]


def _mock_pdf(monkeypatch: pytest.MonkeyPatch, pages: list[str]) -> None:
    def fake(command: list[str]) -> str:
        if command[0] == "mutool":
            return OUTLINE
        # Honour -f/-l like pdftotext does.
        first = int(command[command.index("-f") + 1]) if "-f" in command else 1
        last = int(command[command.index("-l") + 1]) if "-l" in command else len(pages)
        return "".join(page + "\f" for page in pages[first - 1 : last])

    monkeypatch.setattr(prep, "_run", fake)


def test_scanned_pdf_with_outline_yields_no_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_pdf(monkeypatch, ["", "", "", "", ""])
    assert prep.prepare_pdf(Path("x.pdf"), None, "auto", False) == []
    monkeypatch.setattr(sys, "argv", ["prepare_text.py", "x.pdf", "--out", "unused.txt"])
    assert prep.main() == 1


def test_pages_range_drops_chapters_wholly_outside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_pdf(monkeypatch, [f"alpha{chr(96 + n)} beta{chr(96 + n)}" for n in range(1, 7)])
    parts = prep.prepare_pdf(Path("x.pdf"), (4, 6), "auto", False)
    assert [p.splitlines()[0] for p in parts] == ['# Argument: a "turn"']
    parts = prep.prepare_pdf(Path("x.pdf"), (2, 3), "auto", False)
    assert [p.splitlines()[0] for p in parts] == ["# Introduction"]


def test_front_matter_dropped_unless_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = ["cover", "contents", "intro text", "more", "arg text", "end"]
    monkeypatch.setattr(
        prep,
        "_run",
        lambda c: (
            '+\t"One"\t#page=3&zoom=1\n+\t"Two"\t#page=5&zoom=1\n'
            if c[0] == "mutool"
            else "".join(p + "\f" for p in pages)
        ),
    )
    dropped = prep.prepare_pdf(Path("x.pdf"), None, "auto", False)
    assert [p.splitlines()[0] for p in dropped] == ["# One", "# Two"]
    assert "cover" not in "".join(dropped)
    kept = prep.prepare_pdf(Path("x.pdf"), None, "auto", False, keep_front_matter=True)
    assert [p.splitlines()[0] for p in kept] == ["cover contents", "# One", "# Two"]


def test_skill_install_excludes_pycache(tmp_path: Path) -> None:
    cache = SKILL / "scripts" / "__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "junk.pyc").write_bytes(b"x")
    try:
        for installer, target in (
            ("codex", tmp_path / "codex-home" / "skills"),
            ("opencode", tmp_path / "opencode" / "skills"),
        ):
            stub = tmp_path / "bin"
            stub.mkdir(exist_ok=True)
            (stub / "codex").write_text("#!/bin/sh\n", encoding="utf-8")
            (stub / "codex").chmod(0o755)
            env = {
                **os.environ,
                "CODEX_HOME": str(tmp_path / "codex-home"),
                "XDG_CONFIG_HOME": str(tmp_path),
                "PATH": f"{stub}:/usr/bin:/bin",
            }
            done = sp.run(
                ["bash", str(ROOT / "clients" / installer / "install.sh")],
                env=env,
                capture_output=True,
                text=True,
            )
            assert done.returncode == 0, done.stderr
            assert (target / "speakd-audiobook" / "scripts" / "prepare_text.py").is_file()
            assert not (target / "speakd-audiobook" / "scripts" / "__pycache__").exists()
    finally:
        shutil.rmtree(cache, ignore_errors=True)


def test_range_starting_mid_chapter_keeps_its_in_range_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_pdf(monkeypatch, [f"alpha{chr(96 + n)} beta{chr(96 + n)}" for n in range(1, 7)])
    # Pages 2-3 lie inside "Introduction" (pages 1-3), which started before the range.
    parts = prep.prepare_pdf(Path("x.pdf"), (2, 5), "auto", False)
    assert [p.splitlines()[0] for p in parts] == ["# Introduction", '# Argument: a "turn"']
    assert "alphab" in parts[0] and "alphaa" not in parts[0]
    assert "alphad" in parts[1] and "alphae" in parts[1] and "alphaf" not in parts[1]
