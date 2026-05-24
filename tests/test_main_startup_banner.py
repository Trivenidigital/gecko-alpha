"""Regression test for the startup-banner addition to scout/main.py.

Operators need a version + git SHA marker on every restart so journalctl
forensics can correlate behavior to the deployed commit. Before this
addition, restart left only "Scanner stopped" on graceful exit; abnormal
exits left no marker at all.
"""

from __future__ import annotations

import inspect

from scout import main as scout_main


def test_runtime_version_returns_string():
    v = scout_main._runtime_version()
    assert isinstance(v, str)
    assert v  # non-empty


def test_runtime_git_sha_returns_string():
    sha = scout_main._runtime_git_sha()
    assert isinstance(sha, str)
    assert sha  # non-empty


def test_runtime_version_handles_missing_metadata(monkeypatch):
    """If gecko-alpha isn't installed (raw source checkout), returns 'unknown'."""
    import importlib.metadata as _md

    def _raise(*_a, **_kw):
        raise _md.PackageNotFoundError("gecko-alpha")

    monkeypatch.setattr(_md, "version", _raise)
    assert scout_main._runtime_version() == "unknown"


def test_runtime_git_sha_handles_no_git(monkeypatch):
    """Pathological subprocess failure must NOT crash startup."""
    import subprocess

    def _raise(*_a, **_kw):
        raise FileNotFoundError("git not installed")

    monkeypatch.setattr(subprocess, "run", _raise)
    assert scout_main._runtime_git_sha() == "unknown"


def test_scanner_starting_log_emitted_from_main():
    """The main() function must contain a `scanner_starting` log call.

    Static check (we can't run main() in a unit test — it spawns workers
    and connects to external services), but presence of the event in the
    source guarantees future refactors preserve the banner.
    """
    src = inspect.getsource(scout_main.main)
    assert "scanner_starting" in src, (
        "scout/main.py main() must emit a 'scanner_starting' log event "
        "with version+git_sha so journalctl forensics can identify the "
        "running commit on every restart"
    )
    assert "_runtime_version" in src
    assert "_runtime_git_sha" in src
