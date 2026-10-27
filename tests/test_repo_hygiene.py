"""Repository hygiene checks.

These are not tests of behaviour; they guard properties that are invisible in a diff and expensive
to notice later.

The encoding check exists because of a real incident in this repository: a bulk edit performed
through PowerShell's `Get-Content`/`Set-Content` pair read UTF-8 bytes as the system code page and
wrote them back re-encoded, turning every em dash in `state.py` into a CJK codepoint followed by a
stray `?`. Python still parsed the file — the damage was confined to comments and docstrings, which
is exactly why it survived a test run and a code review. A one-line assertion that the sources
contain no CJK characters catches the whole class of failure, since nothing in this codebase
legitimately contains one. (The documentation does: `docs/*.zh.md` is Chinese by design, and is
excluded.)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
TESTS = REPO_ROOT / "tests"
SCRIPTS = REPO_ROOT / "scripts"


def _python_files() -> list:
    files = []
    for root in (SRC, TESTS, SCRIPTS):
        if root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    return files


def test_python_sources_exist() -> None:
    assert len(_python_files()) > 15


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_cjk_characters_in_python_sources(path: Path) -> None:
    """No Python source may contain a CJK codepoint or a replacement character.

    Documents the rule rather than the incident: this codebase's identifiers, comments and docstrings
    are English, so any CJK character is evidence of a mis-encoded write, and U+FFFD is evidence of a
    lost byte. Both are silent in every other check.
    """
    text = path.read_text(encoding="utf-8")
    offenders = sorted(
        {
            character
            for character in text
            if "\u4e00" <= character <= "\u9fff" or character in ("\ufffd", "\u9225", "\u922b")
        }
    )
    assert not offenders, (
        "%s contains %r — a mis-encoded write, not intended content"
        % (path.relative_to(REPO_ROOT), offenders)
    )


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_every_python_source_parses(path: Path) -> None:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_no_merge_conflict_markers() -> None:
    """Catch conflict markers, matched as whole lines.

    Matched anchored at the start of a line rather than as bare substrings: a docstring separator
    such as `=======` (or a comment rule) is ordinary content here, and a substring check flags it.
    """
    import re

    patterns = (
        re.compile(r"^<{7}( |$)", re.MULTILINE),
        re.compile(r"^>{7}( |$)", re.MULTILINE),
        re.compile(r"^={7}$", re.MULTILINE),
    )
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            match = pattern.search(text)
            assert match is None, "%s contains a conflict marker %r" % (
                path.relative_to(REPO_ROOT), match.group(0) if match else ""
            )


def test_core_engine_imports_without_optional_dependencies() -> None:
    """The core must import with nothing installed but the standard library.

    This is a design commitment, not a preference: it is what lets `brian replay`, the offline
    evaluation harness and the test suite run on a bare interpreter, with no Kafka, no PyFlink, no
    torch and no LLM SDK. If an import from the core modules ever reaches for one of those at module
    scope, replay stops being usable exactly when it is needed most — during an incident.
    """
    import importlib
    import sys

    forbidden = ("kafka", "pyflink", "torch", "transformers", "numpy", "pandas", "requests", "fastapi")
    saved = {name: sys.modules.get(name) for name in forbidden}

    def guard(name: str, *args, **kwargs):
        if name.split(".")[0] in forbidden:
            raise AssertionError(
                "the core engine tried to import %r at module scope; it must stay optional" % name
            )
        return original_import(name, *args, **kwargs)

    original_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__
    try:
        for name in forbidden:
            sys.modules[name] = None  # type: ignore[assignment]
        if isinstance(__builtins__, dict):
            __builtins__["__import__"] = guard
        else:
            __builtins__.__import__ = guard  # type: ignore[attr-defined]
        for module in (
            "brian",
            "brian.contracts",
            "brian.topics",
            "brian.bus",
            "brian.config",
            "brian.state",
            "brian.calibration",
            "brian.laya.qcatalog",
            "brian.laya.gate",
            "brian.laya.client",
            "brian.llm.rca",
            "brian.pipeline",
            "brian.eval",
            "brian.demo",
            "brian.export",
            "brian.outcomes",
            "brian.cli",
        ):
            importlib.import_module(module)
    finally:
        if isinstance(__builtins__, dict):
            __builtins__["__import__"] = original_import
        else:
            __builtins__.__import__ = original_import  # type: ignore[attr-defined]
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
