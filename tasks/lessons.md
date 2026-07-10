# Lessons

## Calibrate review depth to actual PR scope

- 2026-05-26 correction: the actionability gate revalidation started as a
  potentially policy-relevant branch decision, but the final PR was a
  Markdown-only status/finding update. Running plan, design, and PR reviews
  with two reviewers each was defensible while scope was still ambiguous, but
  would be excessive if applied mechanically to routine docs-only status flips.
- Rule: if a revalidation or stale-status cleanup is known upfront to be
  Markdown-only and cannot change runtime behavior, default to 0-1 focused
  review unless the finding is money/exits/schema-critical or the operator
  explicitly asks for the full review chain.
- Rule: if the work begins with broader policy/build implications, start with
  the heavier review posture; once evidence collapses the output to docs-only,
  do not carry the full machinery forward to every follow-up by habit. This is
  CLAUDE.md section 10 in practice: discipline sections are heuristics, not
  rituals.

## Encode anti-scope as runtime contracts when possible

- 2026-05-26 correction: the Trade Inbox plan said "no urgency tiers, no alert
  qualification, no ranking, no cross-id resolver," but prose alone decays.
  The operator called out the stronger pattern: turn anti-scope into runtime or
  CI assertions when it is technically possible.
- Rule: when a plan/design commits to anti-scope, ask whether that boundary can
  be enforced as a contract checker, lint rule, schema test, CI gate, or
  post-deploy smoke command. If yes, encode it. The plan explains why; the
  executable check enforces it.
- Example: `/api/trade_inbox` forbids urgency, alert, ranking, and resolver
  semantics through the contract firewall, and this branch adds a named CI
  dashboard-contract gate plus one aggregate post-deploy smoke command.
- Future alert qualification should either use a separate endpoint such as
  `/api/trade_alert_intent`, or deliberately relax the Trade Inbox firewall in
  its own contract PR with new invariants.

## Complete trader surfaces before ranking or urgency tiers

- 2026-05-26 correction: when the operator asked for fewer, better trading
  signals, I twice reached for ranking/tiering before the motivating tokens
  were guaranteed to reach the trader surface. First this appeared as
  detector-credit / resurfaced-runner queues; later it appeared as Telegram
  `TRADE_NOW` / `WATCH_BREAKOUT` tiers.
- Rule: operator frustration with trader-surface actionability should first
  trigger surface-completeness and path-trace checks. Complete the promotion
  surface, measure queue volume, then add ranking or urgency tiers only if the
  measured queue volume justifies them.
- Rule: alert qualification should run over the complete decision-support
  universe. If watcher/tracker wins are not yet promoted into the cockpit, a
  TG alert gate built on paper-trade-backed rows will miss the exact tokens
  that motivated the work.

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
- Rule: instrument decision-support promotion/blocks before expanding the promotion surface. If promotion ships before structured decision events, future "why did X surface or not surface?" audits fall back to journal archaeology and repeat the same failure mode.

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

### SSH alias multiplexing can route every alias to the first-connected host (2026-05-30)

When probing multiple VPS hosts in one session via SSH aliases, connection
multiplexing (`ControlMaster` / `ControlPath`) can cause later alias calls
(e.g. `main-vps`) to reuse the first host's already-open socket
(`srilu-vps`), so `hostname` returns the wrong host and per-host output is
silently attributed to the wrong machine.

**Rule:** always verify `hostname` (or another host-identity probe) matches
the intended target before trusting per-host output. Run each host in a
separate call and confirm identity; do not assume the alias resolved to a
fresh connection. If host identity cannot be confirmed, treat that host as
NOT verified rather than reporting its (possibly mis-attributed) output.

**Worked example:** 2026-05-30 Kraken MCP probe — the `main-vps` alias
returned `hostname` `srilu-vps` (multiplexed socket reuse). Only srilu-vps
was definitively verified; main-vps / vpin-vps were reported as
not-independently-verified this session.

### Recorded approval or it didn't happen (2026-07-02, cross-project standing rule)

Every implementation, merge, deploy, and flag/prod-state action requires a
recorded operator approval — an operator message quotable in the session's
approvals log. Sessions maintain an explicit approvals-log table in their
deliverable report (action, class, approval record, execution timestamp);
anything without a recorded approval is marked
**executed-without-recorded-approval**, never silently normalized. No
standing merge approvals: each merge is approved per-PR (migration-bearing
PRs get a two-vector review brief — fresh install / upgrade-with-data /
rollback — before the approval ask). Sessions never attest to or "close"
actions another session performed.

**Worked example:** 2026-07-02 fable-review — a subagent ran the test suite
on srilu (isolated /tmp worktree, prod untouched) without approval; it was
halted, fully disclosed, and logged as the session's only
executed-without-recorded-approval entry
(`tasks/gecko-alpha-fable-review_2026_07.md`, approvals log row 13).
Origin: operator directive in the SMB-Agents/Flyer-Studio review, propagated
here per instruction.

### Detection that doesn't reach a human doesn't count as detection (2026-07-02, cross-project invariant)

A detector whose output terminates in a log line, a dashboard tile, or a DB
row that no alert path consumes has NOT detected anything, operationally.
Design every detector with its delivery path: who is notified, on what
channel, with what dedup/cooldown — or explicitly record it as
observe-only-by-design.

**gecko-alpha evidence:** (1) frozen positions 2610/2613 sat 5-7 days
flagged by the dashboard Trader Action Queue ("NO CURRENT PRICE — stop/TP
cannot trigger") with 90 INFO-level `trade_eval_no_price` skips per 2h — no
Telegram, nobody acted (fixed by #404's write-time alert + #408 stale-onset
alerts). (2) GA-19: the ingest-starvation watchdog reset its own counter on
every restart, so the alarm for silent ingestion death could never fire
(fixed by #402). Cross-project evidence: 6,118 unheard flyer
source-edit-SLA rows in SMB-Agents.

**Rule:** §12a/§12b reviews must ask not only "is there a watchdog?" but
"does its output REACH a human?" — a scheduled script, a live dashboard,
and structured logs all fail this test unless something pushes.

### Worktree-first, one worktree per session (2026-07-02)

Claim an isolated `git worktree` before any git state change; never operate
on the shared root checkout. Evidence: 2026-07-02, a parallel session moved
the shared checkout's detached HEAD onto a PR-#400 review line while another
session had uncommitted report files there — the exact divergence class in
[[feedback_parallel_session_branch_coordination]]. The root checkout is
master-pinned and read-only by convention.

### Sibling-branch schema simulations must survive the sibling landing (2026-07-02)

A test that simulates a parallel branch's schema change must either be written
to survive that branch's merge (guarded ALTER / IF-NOT-EXISTS / PRAGMA check)
or carry a tombstone naming the PR whose landing deletes it. #407's unguarded
`ALTER TABLE paper_trades ADD COLUMN exit_provenance` broke #408's CI the
moment the real migration landed — the duplicate-column failure itself proving
the migration worked. With three-plus parallel worktrees this class is
recurrent, not exotic.

### Watchdog verification must exercise the CRON path, not bash-invocation (2026-07-03)

A cron entry `* * * * * /path/script.sh >> log 2>&1` invokes the script
DIRECTLY — it requires the executable bit. Verifying with `bash script.sh`
bypasses that bit and passes even when the script is mode 0644 and cron
cannot run it. Result: a "PASS" that certifies the logic while the real
scheduled invocation fails `Permission denied` every tick, silently (cron
logs it; nobody reads cron logs — the whole reason the watchdog exists).

**Worked example (2026-07-03 deploy):** the #399 held-position + revival
watchdogs and the pre-existing acceleration-heartbeat watchdog were all git
mode 100644; cron failed every run with `Permission denied` (acceleration
~31 days / 2,959 consecutive failures). The deploy's §A-5 smoke used
`bash scripts/X.sh` → false PASS. Rule: watchdog/cron verification runs the
script the way cron does (`./script.sh` or the exact cron command), and
scripts destined for direct cron invocation are committed +x
(`git update-index --chmod=+x`) so the bit survives every checkout.

### Routing headers on cross-session payloads; verify-against-tree before delivering (2026-07-03)

Multi-session relay can MISROUTE, not just lose/duplicate: a foreign-project
payload can arrive in the wrong session, plausibly (in-format IDs, adjacent PR
numbers, overlapping vocabulary). Two rules:

1. **Routing header, both directions:** every cross-hop payload opens with
   `TO: <session> | FROM: <origin> | RE: <topic>`. A payload whose header names
   another session is returned UNPROCESSED. (Generalizes the per-part ID-line
   that got a 6-part relay through a hop that ate 4 messages.)
2. **Verify against THIS tree before producing any deliverable:** if a request
   names projects/paths/PRs, grep the tree first. Zero matches on independent
   markers → refuse and surface the refusal (route home), never fabricate a
   plausible-looking answer. Confabulation-refusal at a project boundary is
   evidence-first.

**Worked example (2026-07-03, gecko-alpha session):** a Flyer Studio payload
(WS2b / F0201 / projects #40–#43 / final_asset_ids / letterbox) requested a
reachability table for projects absent from gecko-alpha. Grep returned 0/5
marker matches; the deliverable was refused, the refusal surfaced and routed
to the owning session. A pattern-matching agent would have produced a
confident table for projects that don't exist.

### String-compared datetimes must share a format — off-by-one #4 (2026-07-04)

A comparison operator does exactly what it's told, on representations nobody
checked were comparable. `refresh_all` used
`opened_at >= datetime('now','-30 days')` intending a strict timestamp `>=`.
But SQLite compares these as TEXT, and the two sides use MISMATCHED formats:
the stored column is ISO with a `T` separator + `+00:00` timezone
(`"2026-06-04T02:31:52.497424+00:00"`); `datetime('now',...)` yields a
space-separated, tz-less string (`"2026-06-04 03:04:08"`). At character 10,
`'T'` (0x54) > `' '` (0x20), so on the boundary DAY the comparison keeps the
row in-window regardless of time-of-day — the predicate behaves as `DATE() >=`
by accident, an off-by-one of one whole day.

This is off-by-one #4 in this engagement, and the purest of the species: the
first three were design-level (a threshold above a documented ceiling; two
mcap bands that never intersected; a refresh window that excludes its own
parolees); this one is SUB-SEMANTIC — correct operator, incomparable operands.

**Rule (cousin to the exec-bit CI guard — enforced, not remembered):**
datetimes that will be string-compared must share a serialization format,
normalized WHERE THEY ARE WRITTEN, not remembered where they are compared.
Prefer comparing on `julianday()`/epoch, or normalize both sides to identical
`strftime` output. Any `datetime('now',...)` compared against a stored ISO
column is suspect until audited — tracked as BL-DATETIME-NORMALIZATION.

### Date-boundary jobs summarize the CLOSED period, and quiet periods must speak — off-by-one #5 (2026-07-10)

The daily paper-trading digest fired at the ~01:00 UTC daily-learn tick and
computed `today = datetime.now(timezone.utc).strftime("%Y-%m-%d")`, then
`build_paper_digest(db, today)`. At 1 AM, "today" is one hour old: the digest
summarized only the first hour of the current day — and `digest.py` then
`return None` whenever that hour had zero opens and closes, so no Telegram
went out and no `paper_daily_summary` row was written. Prod effect: trades
open/close nearly every day, yet the latest summary row was stuck at
2026-06-26, and every digest ever sent had been a partial-hour summary. The
ORIGINAL design (`docs/superpowers/plans/2026-04-19-paper-trading-engine.md`)
passed **yesterday** (`now - timedelta(days=1)`); the implementation quietly
diverged to `now`.

This is off-by-one #5, and it braids two of the earlier species together: a
date-boundary WINDOW bug (like #1–#3, summarizing the wrong period) AND a
silent no-op (the quiet-day `return None` meant the failure produced neither
a message nor a table row — nothing to notice).

**Two rules, both cheap, both enforced by the tests in this PR:**

1. **A job that runs at time T to summarize a period must summarize the
   CLOSED period, not the one still in progress.** A daily digest at 01:00
   summarizes YESTERDAY. Compute the boundary as `now - timedelta(days=1)`
   (or the closed window's explicit bounds), never `now`. Pin it with a
   date-semantics test: a row on the last second of the target day is IN, a
   row one second into the next day is OUT.

2. **Quiet periods must emit explicitly — never `return None`.** A zero-
   activity period is a fact worth recording, not an absence to swallow. Write
   the summary row (zeros) so a freshness watchdog can see a heartbeat (§12a; wired by PR #431),
   and return an explicit one-liner so silence is never ambiguous between "no
   activity" and "the job didn't run" (cousin to §12b's silent-success
   problem).
