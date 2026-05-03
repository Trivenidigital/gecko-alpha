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
    file would silently bypass the check otherwise.

    Limitation (acceptable for plan/spec files): if MultiEdit edit N's
    new_string contains text matching edit N+1's old_string, the chained
    apply finds a match in edit N's output that doesn't exist in the
    original file — producing post-edit content that diverges from what
    Claude Code actually applies (Claude Code validates non-overlap on
    the source file). For structured markdown plan/spec files this overlap
    is rare in practice; if it bites, the symptom is a false-positive
    block which the operator can clear by re-issuing the edit or adding
    the bypass comment.
    """
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
    """Append an audit row to .claude/hooks/bypass.log. Best-effort: if
    the log can't be written (filesystem permission, dir-as-file, etc.),
    print a warning to stderr but do NOT raise — the bypass policy check
    already passed, an audit-trail failure should not block a legitimate
    bypass."""
    try:
        log_path = Path(".claude/hooks/bypass.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "path": path,
            "tool": tool,
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(
            f"[check-new-primitives] WARN: bypass-log write failed "
            f"({type(e).__name__}: {e}); bypass still allowed.",
            file=sys.stderr,
        )


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

    # Tool unknown/empty: matcher SHOULD have gated, but if we got here on
    # a gated path with no tool name, the env contract was violated. Fail
    # CLOSED rather than silently allow — the gated-path is the load-bearing
    # signal here, not the tool name.
    if not tool:
        print(
            f"[check-new-primitives] BLOCKED: gated path {file_path} but "
            f"CLAUDE_TOOL_NAME env var was empty. Hook contract violated; "
            f"failing closed. If this is unexpected, verify the hook "
            f"matcher in .claude/settings.json fires only for "
            f"Write/Edit/MultiEdit/NotebookEdit tools.",
            file=sys.stderr,
        )
        return 2

    # Helper: tolerant file read. Plan/spec files SHOULD be UTF-8, but a
    # decode failure shouldn't cascade into a fail-closed-with-traceback.
    # `errors="replace"` lossy-reads non-UTF-8 bytes as U+FFFD; we only
    # need to find the ASCII marker text, so this is safe.
    def _read(p: str) -> str:
        if not Path(p).exists():
            return ""
        try:
            return Path(p).read_text(encoding="utf-8", errors="replace")
        except (IsADirectoryError, PermissionError, OSError) as e:
            print(
                f"[check-new-primitives] BLOCKED: cannot read {p} "
                f"({type(e).__name__}: {e}).",
                file=sys.stderr,
            )
            sys.exit(2)

    # Compute the resulting file content after the operation
    if tool == "Write":
        new_content = inp.get("content", "")
    elif tool == "Edit":
        existing = _read(file_path)
        new_content = _resulting_content_after_edit(
            existing, [(inp.get("old_string", ""), inp.get("new_string", ""))]
        )
    elif tool == "MultiEdit":
        existing = _read(file_path)
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
        # Unknown tool name on a GATED path. Same logic as empty-tool
        # case above — fail closed.
        print(
            f"[check-new-primitives] BLOCKED: unknown CLAUDE_TOOL_NAME "
            f"'{tool}' on gated path {file_path}; failing closed.",
            file=sys.stderr,
        )
        return 2

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
