"""Tests for the BL-072 pre-write hook (`.claude/hooks/check-new-primitives.py`).

Mechanical verification that the hook fires correctly. Without this test
the hook could silently never-fire (e.g. matcher misconfiguration in
settings.json) and the entire convention is theatre with zero failure
evidence — exactly the silent-failure mode the BL-071 chain_patterns
incident taught us to fear.

Tests shell out to the script with controlled env vars (matching the
contract used by the existing PreToolUse hooks in .claude/settings.json:
`$CLAUDE_TOOL_NAME`, `$CLAUDE_FILE_PATH`, `$CLAUDE_TOOL_INPUT`).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

HOOK_SCRIPT = Path(__file__).parent.parent / ".claude" / "hooks" / "check-new-primitives.py"


def _run(
    tool_name: str,
    file_path: str,
    tool_input: dict,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    """Invoke the hook with the given env contract. Returns CompletedProcess."""
    env = os.environ.copy()
    env["CLAUDE_TOOL_NAME"] = tool_name
    env["CLAUDE_FILE_PATH"] = file_path
    env["CLAUDE_TOOL_INPUT"] = json.dumps(tool_input)
    return subprocess.run(
        ["python", str(HOOK_SCRIPT)],
        env=env,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )


def test_hook_script_exists():
    assert HOOK_SCRIPT.exists(), f"hook script missing at {HOOK_SCRIPT}"


def test_write_with_marker_passes():
    r = _run(
        "Write",
        "tasks/plan_smoke.md",
        {
            "file_path": "tasks/plan_smoke.md",
            "content": "**New primitives introduced:** NONE\n# Plan",
        },
    )
    assert r.returncode == 0, f"expected exit 0, got {r.returncode}; stderr={r.stderr}"


def test_write_without_marker_blocks():
    r = _run(
        "Write",
        "tasks/plan_smoke2.md",
        {"file_path": "tasks/plan_smoke2.md", "content": "# Plan, no marker"},
    )
    assert r.returncode == 2, f"expected exit 2, got {r.returncode}"
    assert "BLOCKED" in r.stderr
    assert "**New primitives introduced:**" in r.stderr


def test_bypass_comment_passes(tmp_path):
    # Use tmp_path as cwd so bypass.log is sandboxed
    r = _run(
        "Write",
        "tasks/plan_bypass.md",
        {
            "file_path": "tasks/plan_bypass.md",
            "content": "<!-- new-primitives-check: bypass -->\n# Plan",
        },
        cwd=tmp_path,
    )
    assert r.returncode == 0, f"expected exit 0, got {r.returncode}; stderr={r.stderr}"
    log = tmp_path / ".claude" / "hooks" / "bypass.log"
    assert log.exists(), "bypass.log not created"
    entry = json.loads(log.read_text(encoding="utf-8").strip())
    assert entry["path"] == "tasks/plan_bypass.md"
    assert entry["tool"] == "Write"


def test_notes_file_out_of_scope():
    r = _run(
        "Write",
        "tasks/notes_dummy.md",
        {"file_path": "tasks/notes_dummy.md", "content": "# notes, no marker"},
    )
    assert r.returncode == 0


def test_marker_only_in_code_fence_blocks():
    """B3 from design review: code-fenced markers must NOT count."""
    content = (
        "# Plan\n\n"
        "Example marker format:\n\n"
        "```\n"
        "**New primitives introduced:** NONE\n"
        "```\n"
    )
    r = _run(
        "Write",
        "tasks/plan_inception.md",
        {"file_path": "tasks/plan_inception.md", "content": content},
    )
    assert r.returncode == 2
    assert "BLOCKED" in r.stderr


def test_typo_tolerant_marker_passes():
    """H1 from design review: case-insensitive + bold-tolerant."""
    r = _run(
        "Write",
        "tasks/plan_typo.md",
        {
            "file_path": "tasks/plan_typo.md",
            "content": "**New Primitives Introduced :** NONE\n# Plan",
        },
    )
    assert r.returncode == 0


def test_out_of_gate_path_passes():
    r = _run(
        "Write",
        "tasks/todo.md",
        {"file_path": "tasks/todo.md", "content": "# todo, no marker"},
    )
    assert r.returncode == 0


def test_malformed_tool_input_fails_closed():
    """Malformed CLAUDE_TOOL_INPUT must fail closed, not silently allow."""
    env = os.environ.copy()
    env["CLAUDE_TOOL_NAME"] = "Write"
    env["CLAUDE_FILE_PATH"] = "tasks/plan_bad.md"
    env["CLAUDE_TOOL_INPUT"] = "{not valid json"
    r = subprocess.run(
        ["python", str(HOOK_SCRIPT)], env=env, capture_output=True, text=True
    )
    assert r.returncode == 2
    assert "not valid JSON" in r.stderr


def test_empty_tool_name_on_gated_path_fails_closed():
    """H2 from PR review: env contract violation must fail closed."""
    env = os.environ.copy()
    env["CLAUDE_TOOL_NAME"] = ""  # violated
    env["CLAUDE_FILE_PATH"] = "tasks/plan_x.md"
    env["CLAUDE_TOOL_INPUT"] = json.dumps(
        {"file_path": "tasks/plan_x.md", "content": "# foo"}
    )
    r = subprocess.run(
        ["python", str(HOOK_SCRIPT)], env=env, capture_output=True, text=True
    )
    assert r.returncode == 2
    assert "CLAUDE_TOOL_NAME env var was empty" in r.stderr


def test_unknown_tool_name_on_gated_path_fails_closed():
    """Symmetric: unknown tool name on gated path must fail closed."""
    r = _run(
        "WhateverTool",
        "tasks/plan_x.md",
        {"file_path": "tasks/plan_x.md", "content": "# foo"},
    )
    assert r.returncode == 2
    assert "unknown CLAUDE_TOOL_NAME" in r.stderr


def test_edit_with_marker_kept_passes(tmp_path):
    """Edit on existing file that retains marker passes."""
    target = tmp_path / "tasks" / "plan_existing.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "**New primitives introduced:** foo\n# Plan body\n", encoding="utf-8"
    )
    r = _run(
        "Edit",
        str(target).replace("\\", "/"),
        {
            "file_path": str(target).replace("\\", "/"),
            "old_string": "# Plan body",
            "new_string": "# Plan body — extended",
        },
    )
    assert r.returncode == 0


def test_edit_that_deletes_marker_blocks(tmp_path):
    """B2 from design review: edit-deletes-marker must block. The simpler
    'if existing has marker, allow' shortcut would pass this incorrectly."""
    target = tmp_path / "tasks" / "plan_existing.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "**New primitives introduced:** foo\n# Plan body\n", encoding="utf-8"
    )
    r = _run(
        "Edit",
        str(target).replace("\\", "/"),
        {
            "file_path": str(target).replace("\\", "/"),
            "old_string": "**New primitives introduced:** foo\n",
            "new_string": "",
        },
    )
    assert r.returncode == 2, "edit-deletes-marker should fail closed"


def test_multiedit_chained_with_marker_preserved(tmp_path):
    """B1 from design review: MultiEdit must trigger the hook (not bypass it)."""
    target = tmp_path / "tasks" / "plan_existing.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "**New primitives introduced:** foo\n# Plan\n## Section A\n## Section B\n",
        encoding="utf-8",
    )
    r = _run(
        "MultiEdit",
        str(target).replace("\\", "/"),
        {
            "file_path": str(target).replace("\\", "/"),
            "edits": [
                {"old_string": "## Section A", "new_string": "## Section A — done"},
                {"old_string": "## Section B", "new_string": "## Section B — done"},
            ],
        },
    )
    assert r.returncode == 0, f"expected exit 0; stderr={r.stderr}"


def test_notebookedit_on_gated_path_blocks():
    """NotebookEdit on a plan/spec path must block (no plan should be a notebook)."""
    r = _run(
        "NotebookEdit",
        "tasks/plan_x.md",
        {"notebook_path": "tasks/plan_x.md"},
    )
    assert r.returncode == 2
    assert "NotebookEdit" in r.stderr
