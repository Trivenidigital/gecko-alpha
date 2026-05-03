# BL-072 Design v2 — file-by-file specifics (post-2-reviewer fixes)

**Status:** DESIGN v2 — applied 2 design-reviewer findings; ready for build
**Plan reference:** `tasks/plan_bl072_hermes_alignment.md` (v2)
**New primitives introduced:** `docs/gecko-alpha-alignment.md`, `.claude/hooks/check-new-primitives.py`, `.claude/hooks/bypass.log` (created on first bypass), 4-entry PreToolUse hook block in `.claude/settings.json`

---

## What changed from design v1 (per reviewer fixes)

| v1 issue | v2 fix |
|---|---|
| **B1** MultiEdit/NotebookEdit bypass the gate | Hook matcher now `Write\|Edit\|MultiEdit\|NotebookEdit` |
| **B2** Edit-deletes-marker fail-open ("if existing has marker, allow any edit" shortcut) | Drop the shortcut. For Edit, actually apply `old_string→new_string` substitution to existing content, then check the resulting full content |
| **B3** Code-fenced markers count as compliance | Strip triple-backtick code blocks before regex match |
| **B4** `.claude/settings.json` already has 7+ hooks; naive write clobbers | Design now shows the EXACT merged JSON (preserves all existing PreToolUse Bash/Write hooks, PostToolUse Write hooks, Stop hook). Implementation reads existing file, appends to `hooks.PreToolUse[]`, writes back |
| **H1** Marker regex too strict ("New Primitives Introduced", missing bold all block) | Tolerant regex: case-insensitive, optional bold, optional whitespace |
| **H2** Bypass has zero audit trail | Hook appends to `.claude/hooks/bypass.log` (NDJSON: `{ts, path, tool, reason}`); PR reviewers can `git diff` it |
| **H3** Windows `python` PATH issue | Hook command uses `uv run python` matching existing `.claude/settings.json` convention |
| **EXPLORER-3** Hook command wrong (used bare `python`) | Same fix — `uv run python` |
| **EXPLORER-4** Hook contract wrong (used stdin instead of `$CLAUDE_TOOL_INPUT`/`$CLAUDE_FILE_PATH` env vars) | Hook now reads env vars matching existing hooks' contract |
| **EXPLORER-1** Part 1 item 5 says "NEVER executescript" but `_create_tables` uses it | Add qualifier: "NEVER `executescript` in MIGRATION methods; `_create_tables` is exempt (initial schema, no rows to lose)" |
| **EXPLORER-5** BL-071a/b reference 2026-05-15 checkpoint with phantom scope commitment | Change to "deferred-research; revisit when data volume warrants" |
| **EXPLORER-6** Hook test plan insufficient | Add 6 more cases, including code-fence false-positive, MultiEdit, NotebookEdit, Edit deletes marker, empty Write, missing file_path |
| **H4** Rubber-stamp risk (`NONE` always satisfies) | Section 4.5 explicitly names the limitation; assigns to human PR review (not the hook) |

---

## File 1 — `.claude/hooks/check-new-primitives.py`

Full implementation. ~110 lines. Reads env vars per existing-hook convention.

```python
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
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Tolerant marker regex per H1: case-insensitive, optional bold/whitespace,
# colon mandatory (the colon is the strongest disambiguator; without it the
# match is too loose).
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
# Strip triple-backtick code blocks (B3): the marker must appear in real
# prose, not inside an example fence within an alignment doc or test plan.
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
        # Malformed input → fail-closed with clear stderr (per M3).
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
    POST-edit content for marker checking (per B2 — never short-circuit on
    'existing has marker'; an edit can delete the marker)."""
    out = existing
    for old, new in edits:
        # Edit semantics: replace first occurrence. MultiEdit chains these.
        if old:
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

    # Tool input fallback for path (some hooks may only get env vars)
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
        # Notebook edits are JSON cell mutations — too structured to check
        # marker against. Defensive default: block notebook plan/spec files
        # entirely; they should not exist anyway.
        print(
            f"[check-new-primitives] BLOCKED: NotebookEdit on plan/spec "
            f"path {file_path} is unsupported. Use Write/Edit on .md files.",
            file=sys.stderr,
        )
        return 2
    else:
        # Unknown tool — fail open (we shouldn't be here per matcher)
        return 0

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
    except Exception as e:
        # Any unexpected exception → fail-closed with traceback in stderr
        print(
            f"[check-new-primitives] hook crashed: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        sys.exit(2)
```

**Test plan (manual smoke, recorded in PR description, expanded per EXPLORER-6):**

| # | Setup | Expected |
|---|---|---|
| 1 | `Write tasks/plan_dummy.md` with `**New primitives introduced:** NONE` | exit 0 |
| 2 | `Write tasks/plan_dummy2.md` lacking the line | exit 2, stderr = block message |
| 3 | `Write tasks/plan_dummy3.md` with `<!-- new-primitives-check: bypass -->` | exit 0; `.claude/hooks/bypass.log` gains a row |
| 4 | `Write tasks/notes_dummy.md` lacking the line | exit 0 (out of scope) |
| 5 | `Edit tasks/plan_existing.md` (which HAS marker) replacing the marker line with empty string | exit 2 (B2 fix — actually checks resulting content) |
| 6 | `MultiEdit tasks/plan_existing.md` chaining 3 edits, none affecting the marker | exit 0 |
| 7 | `Write tasks/plan_inception.md` whose ONLY occurrence of `**New primitives introduced:**` is inside a triple-backtick code fence | exit 2 (B3 fix — code fences stripped) |
| 8 | `Write tasks/plan_typo.md` with `**New Primitives Introduced :**` (capitalized, extra space) | exit 0 (H1 fix — tolerant regex) |
| 9 | `Write tasks/plan_empty.md` with empty content `""` | exit 2 |
| 10 | `Write` to a path that doesn't match the gated regex (e.g., `tasks/todo.md`) | exit 0 |
| 11 | `NotebookEdit tasks/plan_x.md` (impossible in practice, but defensive) | exit 2 |
| 12 | `CLAUDE_TOOL_INPUT` env var malformed JSON | exit 2 + clear stderr |
| 13 | Hook crashes mid-execution | exit 2 + traceback in stderr |

---

## File 2 — `.claude/settings.json` (full merged content, no merge spec)

The existing file has 7 hook blocks. v2 design specifies the COMPLETE post-merge content rather than a merge instruction. The new entry is the LAST PreToolUse block. All other hooks preserved verbatim.

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "echo $CLAUDE_FILE_PATH | grep -q '\\.py$' && uv run python -m py_compile $CLAUDE_FILE_PATH"
          },
          {
            "type": "command",
            "command": "echo $CLAUDE_FILE_PATH | grep -q '\\.py$' && uv run black --check --quiet $CLAUDE_FILE_PATH"
          }
        ]
      },
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "echo $CLAUDE_FILE_PATH | grep -q '^tests/.*\\.py$' && uv run pytest $CLAUDE_FILE_PATH --tb=short -q"
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "echo \"$CLAUDE_TOOL_INPUT\" | grep -q 'rm -rf' && exit 2 || exit 0"
          }
        ]
      },
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "echo \"$CLAUDE_TOOL_INPUT\" | grep -q 'git push --force' && exit 2 || exit 0"
          }
        ]
      },
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "echo \"$CLAUDE_FILE_PATH\" | grep -q '^\\.env$' && exit 2 || exit 0"
          }
        ]
      },
      {
        "matcher": "Write|Edit|MultiEdit|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": "uv run python .claude/hooks/check-new-primitives.py"
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "uv run pytest --tb=short -q 2>&1 | tail -5"
          }
        ]
      }
    ]
  }
}
```

**Implementation note:** The build phase will use `Read` then `Write` with this exact content (rather than Edit) to avoid any merge-conflict ambiguity. The diff in the PR will be a clean before/after.

**One open question for build phase:** does Claude Code support pipe-regex matchers (`Write|Edit|MultiEdit|NotebookEdit`)? Per existing `Bash` matcher precedent and the docs, yes. Fallback if it doesn't work: 4 separate matcher entries. The PR's manual smoke test #1 will catch this immediately.

---

## File 3 — `docs/gecko-alpha-alignment.md`

Same 4-part structure as v1, with the EXPLORER-1 qualifier on Part 1 item 5, and the EXPLORER-5 fix on Part 2 BL-071a/b entries.

### Part 1 deployed-pattern updates from v1

Item 5 now reads:

> **Migration pattern** — `BEGIN EXCLUSIVE` + per-statement `await conn.execute(stmt)` + explicit `await conn.commit()` / `ROLLBACK`. NEVER `executescript` **in migration methods** (implicit COMMIT defeats rollback). The single exception is `_create_tables` (`scout/db.py:89`) which uses `executescript` legitimately for initial schema creation — no prior rows exist to lose, and the implicit COMMIT is acceptable for the bootstrap path. Indexes for added columns live in the migration step, NOT `_create_tables` (per `feedback_ddl_before_alter.md`).

### Part 2 entries (gated)

Per EXPLORER-5, BL-071a/b lose the phantom 2026-05-15 commitment:

| Item | Failure mode | State | Owner / Target |
|---|---|---|---|
| chain_patterns auto-retire on stale outcome telemetry | Patterns silently disabled when ALL show 0% hit rate; chain_matches stops being written | Guard live (BL-071, PR #61) | done 2026-05-03 |
| memecoin `outcomes` table is empty (BL-071a) | Memecoin chain_matches can never be hydrated | Known root cause; investigation deferred | deferred-research; revisit when data volume warrants OR if guard is loosened |
| narrative chain_matches start at `outcome_class='EXPIRED'` (BL-071b) | Hydrator's `WHERE outcome_class IS NULL` skips them | Known root cause; investigation deferred | deferred-research; revisit when data volume warrants OR if guard is loosened |
| `TELEGRAM_BOT_TOKEN` placeholder in prod `.env` | Every alert path silently 404s | Known | deferred-explicitly per operator instruction |
| narrative_prediction token_id divergence | 32 of 56 stale-young open trades have empty/synthetic token_ids | Known | deferred-pending-evidence; revisit when more open trades accumulate |
| BL-064 listener requires pipeline restart for new channels | New curator additions need full restart | Known operational gap | deferred-pending-priority |

### Part 4 addition (per H4)

Add explicit limitation:

> **The hook checks the marker exists. It does NOT validate that the listed primitives are TRUTHFUL or COMPLETE.** A plan that writes `**New primitives introduced:** NONE` while introducing a new table satisfies the hook but is wrong. Validating accuracy of the list is the responsibility of human PR review. The hook surfaces the obligation; reviewers verify the answer.

---

## File 4 — `CLAUDE.md` updates (revised insertion)

Insert after line 47 of existing `CLAUDE.md` (end of "Coding Conventions" section). Adds a sub-heading, no new top-level.

```markdown
### Plan/Design Document Conventions

Every plan, design, or spec document under `tasks/` matching `plan_*.md`,
`design_*.md`, or `spec_*.md` MUST begin with:

`**New primitives introduced:** [list, or NONE]`

This is mechanically enforced by `.claude/hooks/check-new-primitives.py` —
the hook blocks any `Write` / `Edit` / `MultiEdit` / `NotebookEdit` to a
gated file that lacks the line. The marker is matched case-insensitively,
ignoring formatting variations (`**New Primitives Introduced:**`, missing
bold, etc.) — so typos don't block, but the colon is required.

If a file matches the gated pattern but isn't a real plan (e.g., scratch
notes accidentally named `plan_x.md`), include the bypass comment:
`<!-- new-primitives-check: bypass -->`. Bypasses are logged to
`.claude/hooks/bypass.log` for PR-time review.

Markers inside ```code fences``` do NOT count — the marker must appear
in real prose to satisfy the hook.

For deployed-pattern reference (so you don't reinvent existing primitives),
see `docs/gecko-alpha-alignment.md`.

**Important limitation:** the hook checks the marker EXISTS. It does not
validate that the list is truthful. Human PR review verifies accuracy.
```

---

## File 5 — `backlog.md` BL-072 + BL-073 entries (unchanged from v1)

Same as plan v2, no design changes.

---

## File 6 — `tasks/notes_agentskills_browse_2026_05_03.md` (unchanged from v1)

Same. Phase 0 sharpened acceptance criterion.

---

## Build phase test commands

After implementation, before PR:

```bash
# Syntax check the hook
uv run python -m py_compile .claude/hooks/check-new-primitives.py

# Smoke-test cases 1, 2, 3, 5, 7, 8 manually via shell
CLAUDE_TOOL_NAME=Write \
  CLAUDE_FILE_PATH=tasks/plan_smoke.md \
  CLAUDE_TOOL_INPUT='{"file_path":"tasks/plan_smoke.md","content":"**New primitives introduced:** NONE\n# Foo"}' \
  uv run python .claude/hooks/check-new-primitives.py
echo "exit=$?"  # expect 0

CLAUDE_TOOL_NAME=Write \
  CLAUDE_FILE_PATH=tasks/plan_smoke2.md \
  CLAUDE_TOOL_INPUT='{"file_path":"tasks/plan_smoke2.md","content":"# Foo"}' \
  uv run python .claude/hooks/check-new-primitives.py
echo "exit=$?"  # expect 2

# Verify settings.json valid JSON
python -c "import json; json.load(open('.claude/settings.json'))"

# Verify existing pytest still passes (no regression)
uv run pytest tests/ -q --tb=short
```

---

## Verdict

All B1-B4, H1-H4, and EXPLORER-1/3/4/5/6 are addressed in this v2 design. Ready to build.

Open question for build phase only (not blocking design):
- Does Claude Code's matcher accept the regex `Write|Edit|MultiEdit|NotebookEdit`? If not, fall back to 4 separate matcher entries. Manual smoke #1 will catch this immediately.
