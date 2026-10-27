"""Shared test fixtures.

`tmp_path` is overridden deliberately. pytest's default temporary root is outside the workspace, and
under a sandboxed run that directory is not writable — the failure surfaces as a `PermissionError`
from inside pytest's own fixture machinery, which looks like a test bug rather than an environment
one. Pointing temporary files at a workspace-local directory keeps the tests runnable in a sandbox and
in CI alike, and they are cleaned up per test as usual.
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

_TEMP_ROOT = Path(__file__).resolve().parent.parent / "var" / "pytest-tmp"


@pytest.fixture
def tmp_path() -> Path:
    """A unique, writable directory inside the workspace, removed after the test."""
    _TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    directory = _TEMP_ROOT / ("t-" + uuid.uuid4().hex[:12])
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def tmp_dir(tmp_path: Path) -> str:
    return str(tmp_path)
