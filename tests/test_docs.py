"""The docs: no unfilled placeholders, every link and every named file of the repository exists, the README (also the
PyPI page, where relative links break) links with absolute URLs only, and every piece of interpreter output they show
is real: a `text` block is a run of lines of the golden file of the recording its section names, and a sentence quoted
in the docs, the docstrings or the examples is a line of a golden file.

`docs/BEHAVIOUR-20.20.md` is a shared page, copied as it is from the maintainers' upstream: a placeholder there fails
too, so a page copied before its numbers were filled in cannot be released.
"""

from __future__ import annotations

import ast
import re
from functools import cache
from pathlib import Path

import pytest

from .support import GOLDEN, ROOT

REPO = "https://github.com/eprojac/pdigger-factory-sdk-python/"
RAW = "https://raw.githubusercontent.com/eprojac/pdigger-factory-sdk-python/main/"
HUB_PAGES = {"docs/BEHAVIOUR-20.20.md"}
"""Shared pages, copied as they are from the maintainers' upstream (which they may name as "the hub's")."""

_FENCE = re.compile(r"^```.*?^```[ \t]*$", re.S | re.M)
_INLINE = re.compile(r"`([^`\n]+)`")
_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_LINK = re.compile(r"!?\[[^\]\n]*\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*$", re.M)
_OUTPUT = re.compile(r"^```text\n(.*?)^```", re.S | re.M)
_SENTENCE = re.compile(r"(?:`|#[ \t]+)(\d{1,2}(?::\d\d)?:\d\d\.\d [^`\n]+?)[ \t]*(?:`|$)", re.M)
"""Interpreter output quoted inline (`21:17.8 Right hand lifts stone 2660`) or in a code comment (`# 5:02.8 ...`)."""
_TOP = ("src/", "tests/", "tools/", "docs/", "examples/", "fixtures/", "spec/", ".github/")


def markdown_files() -> list[Path]:
    """The repository's own Markdown (`spec/` is the shared contract, copied as it is)."""
    return sorted([*ROOT.glob("*.md"), *(ROOT / "docs").glob("*.md"), *(ROOT / "docs" / "tutorials").glob("*.md")])


def name(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def without_code(text: str) -> str:
    """The text with code blocks and inline code blanked out (line breaks kept)."""
    text = _FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    return _INLINE.sub(" ", text)


def slug(heading: str) -> str:
    """GitHub's anchor of a heading."""
    text = re.sub(r"[`*]", "", heading).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    text = _FENCE.sub("", path.read_text(encoding="utf-8"))
    return {slug(h) for h in _HEADING.findall(text)}


def exists(path: str) -> bool:
    """A path (or a pattern with `*` or `<name>` parts) names something in the repository."""
    pattern = re.sub(r"<[^>]+>", "*", path.strip("/"))
    return any(ROOT.glob(pattern)) if "*" in pattern else (ROOT / pattern).exists()


DOCS = [pytest.param(p, id=name(p)) for p in markdown_files()]


def test_the_docs_are_found() -> None:
    assert {"README.md", "docs/streams.md", "docs/BEHAVIOUR-20.20.md", "docs/tutorials/README.md"} <= {
        name(p) for p in markdown_files()
    }


@pytest.mark.parametrize("path", DOCS)
def test_no_placeholders(path: Path) -> None:
    tokens = sorted(set(_TOKEN.findall(without_code(path.read_text(encoding="utf-8")))))
    hint = " (a shared page, maintained upstream: do not edit it here)" if name(path) in HUB_PAGES else ""
    assert tokens == [], f"{name(path)}: unfilled placeholders {tokens}{hint}"


@cache
def golden_lines() -> dict[str, tuple[str, ...]]:
    """The lines of every golden text file, by recording name and part (`dry1-0000-0130.summary.txt`)."""
    return {p.name: tuple(p.read_text(encoding="utf-8").splitlines()) for p in sorted(GOLDEN.glob("*.txt"))}


def output_blocks() -> list[tuple[str, list[str], str]]:
    """Every `text` block of the repository's own docs: where, its lines, and its section's text before it."""
    out = []
    for path in markdown_files():
        if name(path) in HUB_PAGES:
            continue
        text = path.read_text(encoding="utf-8")
        blank = _FENCE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)  # headings outside code, same offsets
        for n, m in enumerate(_OUTPUT.finditer(text), 1):
            section = max((h.start() for h in _HEADING.finditer(blank, 0, m.start())), default=0)
            out.append((f"{name(path)}#{n}", m.group(1).splitlines(), text[section : m.start()]))
    return out


def is_run(lines: list[str], of: tuple[str, ...]) -> bool:
    return any(list(of[i : i + len(lines)]) == lines for i in range(len(of) - len(lines) + 1))


OUTPUTS = output_blocks()


def test_the_docs_show_output() -> None:
    assert len(OUTPUTS) >= 1


@pytest.mark.parametrize(("where", "lines", "before"), OUTPUTS, ids=[w for w, _, _ in OUTPUTS])
def test_output_blocks_are_golden(where: str, lines: list[str], before: str) -> None:
    """A `text` block shows real output: lines in a row of a golden file of the recording named before it."""
    named = [f for f in golden_lines() if f.rsplit(".", 2)[0] in before]
    assert named, f"{where}: the section does not name the recording this output comes from"
    assert lines, f"{where} is empty"
    shown = "\n".join(lines)
    assert any(is_run(lines, golden_lines()[f]) for f in named), f"{where} is not output of {named}:\n{shown}"


def quoted_sentences() -> list[tuple[str, str]]:
    """The interpreter sentences quoted in the docs, the docstrings and comments of the code, and the examples."""
    files = [p for p in markdown_files() if name(p) not in HUB_PAGES]
    files += sorted([*ROOT.glob("src/pdigger_factory_sdk/*.py"), *ROOT.glob("examples/*.py"), *ROOT.glob("tools/*.py")])
    return [(name(p), m.group(1)) for p in files for m in _SENTENCE.finditer(p.read_text(encoding="utf-8"))]


def test_quoted_sentences_are_real_output() -> None:
    real = {part for lines in golden_lines().values() for line in lines for part in (line, line.strip())}
    quoted = quoted_sentences()
    assert len(quoted) >= 10
    assert [(where, s) for where, s in quoted if s not in real] == []


@pytest.mark.parametrize("path", DOCS)
def test_links_resolve(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for target in _LINK.findall(without_code(text)):
        where, _, anchor = target.partition("#")
        if where.startswith(("http://", "https://")):
            for base in (REPO + "blob/main/", REPO + "tree/main/", RAW):
                if where.startswith(base):
                    assert exists(where[len(base) :]), f"{name(path)}: {target} names no file of this repository"
            continue
        assert name(path) != "README.md" or not where, f"README.md (the PyPI page) has a relative link: {target}"
        file = (path.parent / where).resolve() if where else path
        assert file.exists(), f"{name(path)}: {target} does not exist"
        if anchor and file.suffix == ".md":
            assert anchor in anchors(file), f"{name(path)}: {target} has no heading #{anchor}"


def named_paths(text: str) -> list[tuple[str, str]]:
    """The repository paths named in inline code, each with the text of its section before it."""
    out = []
    for section in re.split(r"^(?=#)", _FENCE.sub("", text), flags=re.M):
        for m in _INLINE.finditer(section):
            span = m.group(1).split()[0].removeprefix("./")  # a command's first word: `tools/x.py --check`
            if span.startswith(_TOP):
                out.append((span, section[: m.start()]))
    return out


@pytest.mark.parametrize("path", DOCS)
def test_named_files_exist(path: Path) -> None:
    """A file the docs name is in the repository, or is said to be the maintainers' upstream's ("the hub's
    `tools/sync.sh`"; a shared page names those files in a section that says so)."""
    for span, before in named_paths(path.read_text(encoding="utf-8")):
        if exists(span):
            continue
        attributed = before.rstrip().endswith("hub's") or (name(path) in HUB_PAGES and "hub" in before)
        assert attributed, f"{name(path)}: `{span}` is not in this repository"


@pytest.mark.parametrize(
    "path", [pytest.param(p, id=name(p)) for p in sorted([*ROOT.glob("tools/*.py"), *ROOT.glob("examples/*.py")])]
)
def test_tool_and_example_docstrings_name_real_files(path: Path) -> None:
    docstring = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
    for span, _ in named_paths(docstring):
        assert exists(span), f"{name(path)}: `{span}` is not in this repository"


def test_the_checks_see_what_they_check() -> None:
    # a path in inline code, a command's first word, and nothing of a fenced block (a shared page names its tools
    # only in one)
    page = (
        "# A\n\nThe hub's `tools/sync.sh` copies it; `./tools/gen_models.py --check` checks.\n```\n`tools/x.py`\n```\n"
    )
    assert [span for span, _ in named_paths(page)] == ["tools/sync.sh", "tools/gen_models.py"]
    assert named_paths(page)[0][1].rstrip().endswith("hub's")
    assert slug("7. The live camera") == "7-the-live-camera"
    assert slug("Late layers (`backfill`)") == "late-layers-backfill"
    assert without_code("a `B_C` d\n```\nE_F\n```\nG_H") == "a   d\n\n\n\nG_H"
    assert exists("fixtures/golden/<name>.events.txt")
    assert not exists("tests/test_pictures.py")
    assert is_run(["b", "c"], ("a", "b", "c"))
    assert not is_run(["a", "c"], ("a", "b", "c"))
    sample = "`21:17.8 Right hand lifts stone 2660` and\n    print(e)  # 5:02.8 Right hand places stone 643\n`0:05.2`"
    assert [m.group(1) for m in _SENTENCE.finditer(sample)] == [
        "21:17.8 Right hand lifts stone 2660",
        "5:02.8 Right hand places stone 643",
    ]
