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
   claude -p --tools 'Read,Grep,Glob' --permission-mode plan --output-format json 'Review the pinned change read-only. Return concrete defects with file and line evidence. No edits, deployment, activation, funding or trades.' > claude-review-response.json 2> claude-review-stderr.txt
   ```

5. Read the JSON, check `is_error`, and capture `session_id`. Exit code alone
   does not prove useful review. Independently verify material conclusions.
6. Send findings back to that same session and read its response:

   ```powershell
   claude -p --resume SESSION_ID --tools 'Read,Grep,Glob' --permission-mode plan --output-format json 'Review reconciliation: ... Acknowledge and prioritize next work; remain read-only.' > claude-review-followup.json 2> claude-review-followup-stderr.txt
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
