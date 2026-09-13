# Codex and Claude Code coordination on this PC

The operator authorized direct coordination for gecko-alpha on 2026-09-13.
Use the installed, authenticated Claude Code CLI; no additional plugin is
required. This is an on-demand workflow, not an unattended monitor.

## Read-only review workflow

1. Read the target branch and handoff from Git. Pin its exact SHA in the prompt.
2. Use an isolated worktree; do not switch or clean the shared root checkout.
3. Inspect `claude agents --json` if an existing session is relevant. Never
   assume an advertised session ID is resumable or that a local session contains
   cloud author history. Do not interrupt another active session.
4. Request a bounded review with the CLI. Example, from the review worktree:

   ```powershell
   claude -p --safe-mode --tools 'Read,Grep,Glob' --permission-mode plan --output-format json 'Review the pinned change read-only. Return concrete defects with file and line evidence. No edits, deployment, activation, funding or trades.' > claude-review-response.json 2> claude-review-stderr.txt
   ```

5. Read the JSON, check `is_error`, and capture `session_id`. Exit code alone
   does not prove useful review. Independently verify material conclusions.
6. Send findings back to that same session and read its response:

   ```powershell
   claude -p --safe-mode --resume SESSION_ID --tools 'Read,Grep,Glob' --permission-mode plan --output-format json 'Review reconciliation: ... Acknowledge and prioritize next work; remain read-only.' > claude-review-followup.json 2> claude-review-followup-stderr.txt
   ```

The 2026-09-13 review session ID is
`3b61201c-6b71-4273-ba91-31c0be4a7b1d`, in
`C:\projects\gecko-alpha-review-20260913`. Use that directory to resume.
If unavailable later, start a fresh session with the checked-in review report
and explicitly disclose that original conversation memory is unavailable.

Allow normal permissions; never add permission-bypass flags. Read-only review
needs no shell, editing, or trading tools. Future implementation coordination
should assign separate worktrees and explicit file ownership, with Codex
reviewing the result before integration. Do not give two agents the same branch.

Retain raw session JSON locally; commit only the relevant reviewed findings.
Do not send secrets, wallet material, environment files, or unrelated project
context in review prompts. No periodic automation is installed by this workflow.

## Writer lifecycle and handoff

An installed episodic-memory helper spawned a resumed SDK process after its
parent coding session was interrupted during this task. It continued writing
the old task's files. Stopping only the visible Claude PID was insufficient.
Before transferring file ownership, inspect `claude agents --json` and the
parent/child process tree for the task-specific worktree. Terminate the entire
owned CLI process tree when canceling its work; never stop unrelated interactive
sessions. Verify the worktree stops changing, preserve competing changes, and
integrate only explicitly reviewed commits. Do not assume a helper is read-only
because its name includes memory or summarization.

This helper restarted again during a later read-only review. The primary
review remained read-only, but the plugin's separate sync process resumed
the old coding worktree. Inspect background process ownership after each CLI
start, not just after cancellation. An unattended writer handoff is not yet
safe with that helper enabled. No global plugin setting was changed here.

The verified remedy for coordinated CLI jobs is **`--safe-mode`** (supported
by installed Claude Code 2.1.270). It disables custom hooks/plugins/background
customizations for this invocation while retaining normal auth and permissions.
Unlike `--bare`, it does not require replacing OAuth with an API key. A read-only
followup succeeded in safe mode, session `c3f2403d-bb72-42f1-beb7-edca301c9200`,
without resuming the unwanted helper. Use it for future coordinated jobs.
Safe mode also disables automatic project instructions: implementation prompts
must explicitly request reading AGENTS.md and the applicable plan before edits.

Concurrent writers also share Git's index within a worktree. Prefer separate
worktrees, and use `git commit --only <owned paths>` when committing disjoint
work in a shared integration checkout. This prevents one worker's staged files
from accidentally entering another worker's commit.
