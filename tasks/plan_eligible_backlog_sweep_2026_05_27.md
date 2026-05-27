**New primitives introduced:** NONE

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| VPS Python script hygiene | none found for editing `/home/gecko-agent/run-scanner-cycle.py` in place | Use direct VPS backup, syntax check, and atomic replace; record in repo. |
| Backlog/todo state cleanup | none needed; canonical state is repo Markdown | Update repo docs only. |
| Telegram alert qualification | Hermes not checked because the tracker-promotion soak gate remains closed | Keep gated. |

Awesome-hermes-agent ecosystem check: no drop-in skill for this repo/VPS state cleanup. Verdict: custom repo docs plus direct VPS hygiene edit are justified.

## Goal

Finish backlog items that are eligible on 2026-05-27 without disturbing gated signal-quality work.

## Eligibility Snapshot

Eligible now:

- `BL-NEW-SCANNER-EXISTING-EXCEPTION-BOUNDING` - VPS-only cleanup in `/home/gecko-agent/run-scanner-cycle.py`.
- `BL-NEW-SCANNER-DATETIME-UTCNOW-DEPRECATION` - VPS-only cleanup in the same file.
- `BL-NEW-SCANNER-PRINT-TO-LOG-CONSISTENCY` - VPS-only cleanup in the same file.

Not eligible:

- `BL-NEW-TG-ALERT-QUALIFICATION-DESIGN`: tracker-promotion soak still has only two mature UTC days (`2026-05-25`, `2026-05-26`).
- `first_signal` decision: do not pre-close before 2026-05-31.
- Source/KOL ranking and pruning: still price-coverage gated.
- Actionability downstream builds: not eligible for implementation in this PR; eligible only for separate stale-PR/current-base triage.
- Broad stale `tasks/todo.md` cleanup: eligible as a separate docs-only sweep, but excluded here to keep this PR focused on scanner hygiene.

## Plan

1. Fetch current `origin/master` into an isolated worktree.
2. Write this plan and get two plan reviews:
   - scope/eligibility reviewer;
   - operational/VPS safety reviewer.
3. Fold plan feedback.
4. Write a design doc with exact scanner edit rules, backup path, validation, rollback, and repo-status updates.
5. Get two design reviews and fold feedback.
6. Build a staged candidate, not a prod edit:
   - fetch the current VPS scanner script;
   - apply the three hygiene edits to a staged local copy;
   - save the current script, proposed script, and unified diff under `artifacts/scanner_hygiene_2026_05_27/`;
   - include that diff and exact deploy commands in the design/deploy record;
   - do not replace the VPS file until the PR review stage approves the staged diff;
   - after approval, syntax-check the staged temp file on the VPS with the same `python3` interpreter, back up the current VPS file with checksum/stat evidence, atomically replace while preserving ownership/mode, and syntax-check the final target;
   - verify no `datetime.utcnow`, targeted unbounded exception interpolation, or final-report raw `print` sites remain in the scoped areas;
   - update `backlog.md` and `tasks/todo.md` with deployed status.
7. Verify:
   - `git diff --check`;
   - scanner syntax check evidence;
   - Markdown-only repo diff plus out-of-repo deploy record.
8. Commit, push, create PR, and get two PR reviews.

## Anti-Scope

- No signal policy, alert qualification, ranking, source-quality, live dispatch, or paper-trade behavior.
- No gecko-alpha Python runtime files besides repo documentation.
- No Hermes cron schedule changes.
- No restart unless syntax/deploy validation proves the scanner process requires it.

## Risk Controls

- Use the required Windows SSH two-step for all SSH output.
- Command shape: `ssh root@srilu-vps 'command' > .ssh_out.txt 2>&1`, then read `.ssh_out.txt` separately. Never chain inline readback.
- Keep a timestamped backup on srilu before replacing `/home/gecko-agent/run-scanner-cycle.py`, for example `/home/gecko-agent/backups/run-scanner-cycle.py.YYYYMMDDTHHMMSSZ`.
- Capture `stat -c '%U %G %a %s'` and `sha256sum` before backup, after backup, and after replace.
- Replace using same-filesystem temp path and preserve ownership/mode with `install -o <owner> -g <group> -m <mode>` or equivalent.
- Rollback command must be documented before deploy: restore the backup to `/home/gecko-agent/run-scanner-cycle.py`, restore owner/mode, and rerun `python3 -m py_compile`.
- Do not edit wrapper scripts or Hermes job config.
- If the current VPS script has drifted beyond the backlog assumptions, stop and convert the item into a findings/status PR rather than forcing a patch.
