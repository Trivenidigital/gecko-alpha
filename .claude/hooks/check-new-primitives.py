#!/usr/bin/env python3
"""Pre-write hook for plan/design/spec markdown files.

Blocks `Write|Edit|MultiEdit|NotebookEdit` on `tasks/(plan|design|spec)_*.md`
that lack the mandatory `**New primitives introduced:** [list or NONE]` line.

Reads inputs from environment variables (matching existing hooks in
.claude/settings.json):
  - CLAUDE_TOOL_NAME    — "Write" | "Edit" | "MultiEdit" | "NotebookEdit"
  - CLAUDE_FILE_PATH    — target file path (when applicable)
  - CLAUDE_TOOL_INPUT   — JSON-encoded tool input (full args)

Exit codes:
  0  = allow
  2  = block; stderr returned to Claude as feedback

Bypass: include `<!-- new-primitives-check: bypass -->` in the file content.
Each bypass appends an audit row to `.claude/hooks/bypass.log` (NDJSON).

The marker check strips ```code fences``` first so an alignment doc that
documents the marker format in an example does not satisfy itself.

Limitation by design: this hook checks the marker EXISTS, not that the
listed primitives are TRUTHFUL or COMPLETE. Validating accuracy is the
responsibility of human PR review. See docs/gecko-alpha-alignment.md.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Tolerant marker regex: case-insensitive, optional bold/whitespace, colon
# mandatory. Colon is the strongest disambiguator; without it the match is
# too loose (would match prose like "introducing new primitives").
MARKER_RE = re.compile(
    r"^\s*\**\s*new\s+primitives\s+introduced\s*\**\s*:",
    re.IGNORECASE | re.MULTILINE,
)
BYPASS_RE = re.compile(
    r"<!--\s*new-primitives-check:\s*bypass\s*-->", re.IGNORECASE
)
GATED_PATH_RE = re.compile(
    r"(?:^|/)tasks/(plan|design|spec)_[^/]*\.md$", re.IGNORECASE
)
# Strip triple-backtick code blocks: the marker must appear in real prose,
# not inside an example fence within an alignment doc or test plan.
CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def _strip_code_fences(text: str) -> str:
    return CODE_FENCE_RE.sub("", text)


def _has_marker(content: str) -> bool:
    stripped = _strip_code_fences(content)
    return bool(MARKER_RE.search(stripped))


def _has_bypass(content: str) -> bool:
    return bool(BYPASS_RE.search(content))


def _normalize_path(p: str) -> str:
    return p.replace("\\", "/") if p else ""


def _load_tool_input() -> dict:
    raw = os.environ.get("CLAUDE_TOOL_INPUT", "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Malformed input → fail-closed with clear stderr.
        print(
            "[check-new-primitives] CLAUDE_TOOL_INPUT was not valid JSON; "
            "blocking write to fail closed.",
            file=sys.stderr,
        )
        sys.exit(2)


def _resulting_content_after_edit(
    existing: str, edits: list[tuple[str, str]]
) -> str:
    """Apply Edit / MultiEdit substitutions to existing content. Returns the
    POST-edit content for marker checking. We never short-circuit on
    'existing has marker' — an edit can delete the marker, and the resulting
    file would silently bypass the check otherwise."""
    out = existing
    for old, new in edits:
        if old:
            # Edit semantics: replace first occurrence. MultiEdit chains.
            out = out.replace(old, new, 1)
        else:
            # Empty old_string in Edit means "create file with new_string"
            out = new
    return out


def _log_bypass(path: str, tool: str) -> None:
    log_path = Path(".claude/hooks/bypass.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "path": path,
        "tool": tool,
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def main() -> int:
    tool = os.environ.get("CLAUDE_TOOL_NAME", "")
    file_path = _normalize_path(os.environ.get("CLAUDE_FILE_PATH", ""))
    inp = _load_tool_input()

    # Path fallback when env var absent — some hook contexts only populate
    # CLAUDE_TOOL_INPUT.
    if not file_path:
        file_path = _normalize_path(
            inp.get("file_path") or inp.get("notebook_path") or ""
        )

    if not GATED_PATH_RE.search(file_path):
        return 0  # not a plan/design/spec file

    # Compute the resulting file content after the operation
    if tool == "Write":
        new_content = inp.get("content", "")
    elif tool == "Edit":
        existing = (
            Path(file_path).read_text(encoding="utf-8")
            if Path(file_path).exists()
            else ""
        )
        new_content = _resulting_content_after_edit(
            existing, [(inp.get("old_string", ""), inp.get("new_string", ""))]
        )
    elif tool == "MultiEdit":
        existing = (
            Path(file_path).read_text(encoding="utf-8")
            if Path(file_path).exists()
            else ""
        )
        edits = [
            (e.get("old_string", ""), e.get("new_string", ""))
            for e in inp.get("edits", [])
        ]
        new_content = _resulting_content_after_edit(existing, edits)
    elif tool == "NotebookEdit":
        # Notebook plan/spec files shouldn't exist; defensively block.
        print(
            f"[check-new-primitives] BLOCKED: NotebookEdit on plan/spec "
            f"path {file_path} is unsupported. Use Write/Edit on .md files.",
            file=sys.stderr,
        )
        return 2
    else:
        return 0  # unknown tool — fail open (matcher should prevent reaching)

    if _has_marker(new_content):
        return 0

    if _has_bypass(new_content):
        _log_bypass(file_path, tool)
        return 0

    print(
        f"[check-new-primitives] BLOCKED: {file_path} is a plan/design/spec "
        f"file but is missing the mandatory `**New primitives introduced:**` "
        f"line. Add `**New primitives introduced:** [list or NONE]` near the "
        f"top of the file, OR include `<!-- new-primitives-check: bypass -->` "
        f"if this file is not actually a plan. "
        f"Note: matches inside ```code fences``` do not count. "
        f"See docs/gecko-alpha-alignment.md.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        # Any unexpected exception → fail-closed with traceback in stderr
        print(
            f"[check-new-primitives] hook crashed: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        sys.exit(2)
