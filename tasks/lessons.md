# Lessons

## Signal-quality proposals must quantify attribution before adding truth models

- 2026-05-26 correction: I proposed a detector-credit / resurfaced-runner
  architecture after observing screenshot examples where later runners had bad
  or unknown paper outcomes. The operator correctly pointed out that this
  bundled several unverified causes under "paper PnL is polluted" and risked
  stacking truth models before tracing the data path.
- Rule: before proposing a parallel truth model for trading signals, run a
  post-hoc attribution audit that separates at least: stop exits with better
  continuation, stale-price-cache exits/holds, known fixed bug windows, and
  holding-window-vs-peak-time mismatch. If one fixable execution cause explains
  the majority, fix that mechanism instead of adding a dashboard overlay.
- Rule: future-runner labels are valid for offline evaluation and credit
  attribution only. Do not let lookahead labels drive live ranking. Live ranking
  must use only features available at decision time.
- Rule: pin the universe before writing retention gates such as "retain 95% of
  runners"; otherwise the gate can be vacuous or overbinding.
- Follow-on pattern from the same exchange: after attribution identifies a
  plausible mechanism, verify whether a recent fix already closed that hole
  before scoping the next policy change. If a prior fix moved the metric to
  noise floor, the right output is a mature-cohort re-audit gate with a
  calendar backstop, not another feature or threshold change.

## Hermes cron diagnosis — read jobs.json before journal grep, and bound query windows

- 2026-05-20 calibration: I diagnosed `gecko-x-narrative-scanner` failure as
  "prompt-injection scanner blocking" because `journalctl -u hermes-gateway`
  showed 8+ matching log lines. I pushed back on the operator's analyst who
  reported "120s timeout." **The analyst was right; I was wrong.**
- Root cause of the misdiagnosis: the prompt-injection events were from
  2026-05-15 14:00 UTC — the day the failure mode existed. The operator
  refactored the cron to `no_agent: true` shell-script mode at ~15:00 UTC
  the same day, resolving the issue. The journal events I grepped were
  HISTORICAL, not current.
- The canonical CURRENT state is in `/home/gecko-agent/.hermes/cron/jobs.json`
  — specifically the `last_status` + `last_error` fields per job. That file
  showed `"Script timed out after 120s"` and `last_run_at` recent. The
  journal events were from days earlier.
- **Rule (Hermes cron diagnosis):**
  1. ALWAYS read `~/.hermes/cron/jobs.json` for the job's `last_status` +
     `last_error` + `last_run_at` BEFORE grepping `journalctl`.
  2. When greping journal, ALWAYS bound the query window with
     `--since "<date>"`. If the most recent matching log event is older
     than the failure-mode change date, the pattern is HISTORICAL.
  3. Compare timestamp ranges before declaring a log pattern current.
     "Historical causal logs can survive in journal and look current if we
     don't bound the query window" — operator framing 2026-05-20.

## Memory updates must target the operator's active memory store

- 2026-05-18 correction: I claimed "memory files were updated" after writing
  notes under `~/.Codex/projects/C--projects-gecko-alpha/memory`, but the
  reviewer checked the operator's active Claude memory store at
  `~/.claude/projects/C--projects-gecko-alpha/memory` and found no new files.
- Rule: when reporting that "memory" was updated, verify which memory store the
  project/session actually uses. For gecko-alpha handoffs that may be resumed
  by Claude, write or copy the note to
  `~/.claude/projects/C--projects-gecko-alpha/memory` as well, then verify with
  a directory listing before claiming memory persistence.

## Before proposing a new trader surface, audit the existing surface on motivating tokens

- 2026-05-26 correction: I proposed a `Trade Now / Watch / Research` surface for noisy signal compression without first drift-checking the already-shipped trader cockpit, Action Queue, actionability drilldowns, N-gate verdicts, and Top Gainers source/outcome columns. That risks rebuilding primitives that already exist while missing the real residual gap.
- Rule: before scoping a new trader-facing signal surface, build a gate-vs-existing-primitive matrix with file:line or PR evidence. Classify each gate as `tighten-existing`, `build-new`, or `not-needed`.
- Rule: verify the diagnosis on the motivating tokens first. For TOES/BSB/BILL/UB/TROLL-style examples, query the cockpit/actionability history at the actionable window and record bucket, filter, block reason, and transition timing. If the token was visible but not acted on, the gap is urgency/action copy; if it was hidden, the gap is gate/ranking; if the system lacked history, the gap is observability.
- Rule: treat urgency-state classification as its own scoped design, not a bullet. Break out breakout-level computation, pullback policy, too-late definition, and alert hysteresis. Offline runner labels may evaluate the classifier only after pinning the universe and enforcing `runner_board_ts > snapshot_ts`.
- Rule: "no paper-trade/cockpit row" is not a root cause. Path-trace each motivating token through detector hit, scorer-corpus eligibility, conviction gate, dispatch/live-slot decision, paper-trade insert, and cockpit verdict. Separate corpus mismatch, gate block, slot-full, missing dispatch path, and race/pipeline gaps before scoping promotion, urgency, or ranking work.

## Rebase PR branches after adjacent backlog PRs land

- 2026-05-17 correction: I left PR #146 based on `63aa13b` after #147 and
  #148 merged to `master`. The PR then appeared to delete the newly merged
  systemd drift-watchdog files and first-signal findings, and to revert the
  shipped backlog status for `BL-NEW-SYSTEMD-DRIFT-PRECOMMIT-HOOK`.
- Rule: before final PR-ready status on an active backlog branch, run
  `git fetch origin`, verify `git merge-base HEAD origin/master` is current,
  and inspect `git diff --name-status origin/master..HEAD` for accidental
  deletions/reverts of newly merged work. If the branch is stale, rebase before
  asking for merge review.

## PR review must compare against current base, not only merge-base intent

- 2026-05-17 correction: I reviewed PR #138's intended diff from its merge-base
  and missed that current `origin/master` already contained the same squashed
  feature plus later cycles. The PR was obsolete and conflict-prone even though
  the implementation itself looked sound.
- Rule: for every PR review, fetch current base and check both
  `git log --left-right --cherry-pick origin/master...origin/pr/<n>` and
  `git merge-tree origin/master origin/pr/<n>` before judging mergeability.
  If the feature primitives already exist on current master, treat the PR as
  stale/superseded and recommend close/rebase instead of approving the
  merge-base diff.

## X Alerts asset links must cover unresolved cashtags

- 2026-05-15 correction: I treated "clickable Asset column" as only safe for
  confidently resolved assets. That left the common V1 X-alert case
  (unresolved cashtag-only rows like `$GIGA`, `$PRIMIS`, `$NOTHING`) unlinked
  even though the user expected every visible asset chip to be clickable.
- Rule: when adding dashboard links for signal assets, provide a conservative
  search fallback for unresolved display identifiers. Use exact market pages
  when the backend has a confident `resolved_coin_id` or contract+chain; use a
  search page when confidence is lower. Do not silently render the primary
  visible asset as inert unless there is no identifier at all.

## Hermes-first applies to existing custom-code debt, not only future diffs

- When the operator says the project is bearing too much custom code despite Hermes-first pressure, do not only strengthen future plan templates. Run a backlog/shipped-module comparison against current installed VPS skills/plugins and the public Hermes/agent-skills ecosystem. Classify existing items as `KEEP_CUSTOM`, `USE_SKILL_AS_REFERENCE`, `REPLACE_WITH_HERMES`, `BRIDGE_TO_HERMES`, or `DELETE_OR_DEFER`, then update the backlog so stale "none found" conclusions do not keep driving new custom work.
- The debt audit should preserve project-owned runtime boundaries. A skill can replace workflow intelligence or serve as API-reference without replacing gecko-alpha's durable DB writes, scoring, watchdogs, dashboards, or operator audit trail.

## Hermes-first review scope

- When reviewing proposed custom code, "Hermes capabilities" means more than the public Hermes skill hub. Check the deployed VPS Hermes surface first: installed skills, plugins, and relevant `.hermes` artifacts on the VPS. Also include upstream ecosystem checks such as `NousResearch/hermes-agent-self-evolution` and `0xNyk/awesome-hermes-agent` before accepting a custom implementation as justified.

## 2026-05-13 -- Parse-mode hygiene Class-3 fixes (BL-NEW-PARSE-MODE-AUDIT)

**Lesson:** Default `parse_mode="Markdown"` in `scout.alerter.send_telegram_message`
is a footgun for any caller whose body interpolates user-data fields with
underscores. The trending_catch auto-suspend bug (CLAUDE.md §2.9, fixed in PR #106)
was not unique -- 7 HIGH ACTUAL sites had been silently emitting mangled
alerts for the codebase's lifetime (6 from audit + 1 plan-review discovery).

**Audit-methodology lesson:** the original audit grepped `send_telegram_message`
only and missed `send_alert` at `scout/alerter.py:189` -- a sibling function that
does its own `session.post(.../sendMessage)` call. Future Telegram-send audits
must grep both patterns: `(send_telegram_message|sendMessage)`. The AST-based
structural test added in this PR (`test_all_dispatch_sites_pin_parse_mode`)
closes the recurrence gap for the `send_telegram_message` arm.

**Rule (extension of CLAUDE.md §12b parse_mode addendum):** Any Telegram-send
call whose body interpolates `signal_type`, `symbol`, `ticker`, LLM-generated
text, or any other field that may contain `_ * [ ] \`` must either:
1. Pass `parse_mode=None` (preferred for system-health / digest / plain-text
   alerts), OR
2. Wrap each user-data field with `_escape_md()` AND keep `parse_mode="Markdown"`
   (only when the message intentionally uses Markdown formatters like `*bold*`
   or `[link](url)`).

**Default:** when in doubt, choose `parse_mode=None`. Markdown is only worth
keeping when the formatting is operator-visible and load-bearing (e.g., velocity
alerts with clickable chart links, primary token alerts with bold token names).

**Windows line-ending tooling lesson:** the Edit tool converts LF to CRLF when
rewriting Python source files on Windows. A line-ending-normalization pass plus
`Path.write_bytes` via a helper script preserved LF cleanly. For future
multi-file edits on Windows, prefer byte-level Python helpers over the Edit tool
when the file is being modified (not created fresh).
