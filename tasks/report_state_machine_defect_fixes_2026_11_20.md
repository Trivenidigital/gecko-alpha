**New primitives introduced:** `KillSwitch._release_dead_pointer_locked` and `KillSwitch._clear(only_event_id=...)` (`scout/live/kill_switch.py`); `update_live_trade_if_status(..., and_where=...)` with the constants `_NO_EXIT_RECORDED` and `_NO_ENTRY_FILL_RECORDED` (`scout/live/kraken_pilot.py`); structured log events `live_kill_dead_pointer_released`, `live_kill_clear_skipped_superseded`, `live_kill_clear_skipped_pointer_moved`, `kraken_pilot_cancel_ledger_unchanged`, `paper_close_reason_unmapped`; evidence step `cancel_ledger_unchanged`; result key `kill_reason` in `scripts/clear_expired_kills.py`. No schema change, no migration, no new setting.

# Report — defect fixes following the state-machine survey (2026-11-20)

Follows `tasks/report_state_machine_registry_2026_11_19.md`. That report
listed six defects touching money or safety controls and asked which to fix.
The operator answered "Proceed". Five were fixed; the sixth needs a design
decision and was left.

Branch `claude/main-session-last-discussion-b8hsn7`, head `bea91036` plus this
report. No pull request, no merge, no deploy.

## 1. What changed

| Defect | Behaviour now | Commits |
|---|---|---|
| Kill switch: an expired kill that had not been cleared, or a pointer left on an already-cleared event, blocked the next kill from engaging | `trigger()` releases a pointer that no longer stands for a kill in force, then claims. A kill still in force wins as before. Automatic clears apply only to the event they judged expired. | `bbd3af58`, `cbb215bf`, `b9166536`, `bea91036` |
| Kraken cancel rewrote settled ledger rows | The venue cancel is still always sent. The ledger write applies only to a row that is open or under manual review, has no exit recorded and, for `rejected`, no entry fill recorded. Otherwise the row is left alone and the evidence says why. | `1424ba68`, `b8d6ed86`, `b9166536`, `bea91036` |
| Contract-to-coin map: a NULL write erased a resolved mapping | A NULL write leaves a resolved row untouched. | `27c39f68` |
| Paper close: an unmapped reason silently became `closed_manual` | Still closes (refusing would strand the position) and logs `paper_close_reason_unmapped` at ERROR. `TradeStatus` gained three missing members. | `524fbbf7`, `e326fcca` |
| Solana forward swap adopted after a confirmation timeout raised instead of settling | The landing is recorded first, as the reverse path already did. An adoption that is not finalized stops at `landed` for review, like a finalize timeout. | `e7cecd0f` |

Operator-visible changes worth knowing before this is ever deployed:

- **`python -m scout.live.cli_kill --off`** now prints `cleared
  kill_event_id=N`, or `nothing to clear — …`, or `NOT cleared — …` with exit
  code 1 if a kill is still in force. It used to print `cleared` in every case.
- **Kraken `cancel` can now exit 4 (review)**: when Kraken reports a fill on a
  row the ledger holds as `rejected`, or the ledger records a fill and Kraken
  shows none. The row is left unchanged in both cases. Documented in
  `docs/runbooks/kraken-pilot.md`.
- **`scripts/clear_expired_kills.py`** can answer `"reason": "kill_superseded"`
  and carries the kill's own text under `kill_reason`.
- **A Solana forward run can now end at `landed` with a review exit** where it
  used to crash at `submission_attempted`. See item 1 under "Left open".

## 2. Left open

1. **A forward Solana run that stops at `landed` has no command to move it
   on.** True before these fixes for a finalize timeout; now also the outcome
   of an unrooted adoption (which previously crashed and was equally blocked).
   The forward `resolve` command cannot advance `landed`; the reverse one can.
   Queued as a suggested follow-up task; findings item 26.
2. **Surface-alert `sent` row** can be committed by another writer on the
   shared connection before the send finishes. Not fixed: it follows from how
   the one shared database connection is used, and needs a design decision.
   Findings item 25.
3. **A Kraken partial exit recorded with neither an order id nor a price**
   leaves no exit marker, so a later cancel could write the entry fill back
   over the reduced held quantity. Needs a marker from the exit path itself.
   Findings item 28.
4. **Kraken "no entry fill" test** reads `.`, `..` and `0.0.0` as "no fill".
   No code path can write those values (`_fmt` writes fixed-point decimals).
   Recorded, not changed.
5. **`trigger()` has no rollback** if something after its INSERT raises; the
   speculative row would ride out on a later commit as an orphan
   `kill_events` row with no pointer. Low; recorded.
6. **A kill released by a new trigger skips the "latched over an hour"
   alert.** Findings item 27.

## 3. Verification

Each fix was written test-first; the new tests were run against the parent
commit by the reviewers and fail there.

| Check | Result |
|---|---|
| Solana defect reproduced before the fix | `IllegalStateTransition: submission_attempted -> reconciled`, from a real run through `LaneRunner.place` |
| Affected suites at the final head (kill switch, kill CLI, clear script, Kraken pilot, Solana lane and reverse swap, `tests/live`, paper trader, resolver, registry, audit) | all passing |
| Full suite on untouched master (baseline, this container) | 41 failed, 8978 passed, 28 skipped |
| Full suite at `bea91036`, first run | 47 failed, 9108 passed, 28 skipped — the 41 baseline failures plus 6 in `tests/test_receipt_archive.py` |
| Those 6, investigated | Every one asserted an archive outcome and got `disk_pressure`: 27 GB of pytest temp directories from this session's repeated suite runs had filled the disk past the tests' own threshold. After deleting `/tmp/pytest-of-root`, `tests/test_receipt_archive.py`: 12 passed. Not related to the change. |
| Full suite at `42c1a667` (`bea91036` plus this report), after clearing the disk | **41 failed, 9114 passed, 28 skipped — the failing set is identical, test for test, to the master baseline** |

The 41 baseline failures are environment-dependent script tests
(`test_revival_verdict_watchdog.py` 17, `test_backup_create_script.py` 9,
`test_revival_criteria.py` 8, `test_round20_health_offhost_backup.py` 3,
`test_round20_offhost_backup_script.py` 2, `test_held_position_prices.py` 2)
and fail identically on master. CI has not run on this branch.

## 4. Independent review record

Every round was a separate reviewer agent given the commits, not my
conclusions. Reviewer findings were verified by test before being fixed.

| Round | Scope | Outcome |
|---|---|---|
| A | Kill switch, Kraken cancel, Solana lane (first fix commits) | **Kill switch: defect found** — the fix opened a race in which a concurrent auto-clear tick cleared a freshly engaged kill. Kraken: held quantity overwritten after a partial exit; misleading output. Solana: sound; noted the `landed` dead end. |
| B | Coin map, paper close, registry doc commit | Sound with nits; one registry caveat was wrong. |
| C | The fixes for A | Earlier findings resolved. New: a clear that matched no row still announced "cleared"; `rejected` could be written over a row whose ledger recorded a fill; script JSON key overwritten. |
| D | The fixes for C | Mostly resolved. New: a write transaction left open on the moved-pointer path; kill CLI printed `cleared` when nothing was; CAST-based "no fill" test read junk as zero; three registry inaccuracies. |
| E | The fixes for D (`bea91036`) | **No blocking defects.** Lock release, return value, CLI messages and script output confirmed correct. One low item recorded (section 2, item 4). |

All findings from rounds A–D are fixed except those listed in section 2.

**REVIEW BARRIER status: not cleared for merge.** Independent reviewers are
terminal on the final code head and their actionable findings are fixed or
recorded here, but exact-head CI has not run and no `.reviewers/<PR>.toml`
exists because there is no PR. As CLAUDE.md notes, these reviewers are agents
whose verdicts live in this session; that is not a record outside the
author's reach.

## 5. Approvals log

| Action | Class | Approval record | Timestamp (UTC) |
|---|---|---|---|
| Fix the defects listed in the previous report, each as its own change | implementation | Operator message "Proceed", in reply to a report that listed six defects and said "Say which of these you want fixed and I'll take them as separate changes". The operator named none; I read it as all the narrow ones. **Interpretation is mine — say so if it was meant more narrowly.** | 2026-11-19 |
| `CLAUDE.md` "State machines" section (from the previous report) | implementation (project instructions) | Same "Proceed", in reply to "please confirm you want it". Interpretation is mine. | 2026-11-19 |
| Review-driven follow-up fixes inside the same five defects | implementation | Same approval; CLAUDE.md: reviewer findings are internal events, loop until closed | 2026-11-19 / 20 |
| Commits and pushes to `claude/main-session-last-discussion-b8hsn7` | push to session branch | Session branch instruction | through 2026-11-20 |
| Deleting `/tmp/pytest-of-root` (27 GB of this session's own pytest temp directories) | container housekeeping | None needed: temp output of this session's test runs, outside the repository | 2026-11-20T00:06Z |
| Pull request, merge to master, deploy, any flag or production-state change | — | None requested; **none performed** | — |

Two actions went beyond the letter of "fix the defect" and are called out
rather than flagged:

- **Solana:** an adoption that is not finalized is now a reconciliation
  mismatch. Without it the fix would still have crashed for that case
  (`landed -> reconciled` is not a legal transition either).
- **Kill CLI and Kraken exit codes** changed as listed in section 1.

Nothing is flagged `executed-without-recorded-approval`.

## 6. Final full-suite run

Run at `42c1a667` — the final code head `bea91036` plus the commit that added
this report — after clearing the disk:

    41 failed, 9114 passed, 28 skipped in 1324.42s

The 41 failing tests are the same 41, by name, that fail on untouched master
in this container (section 3). No test fails on the branch that passes on
master. Against master's 8978 passed, the branch adds 136 passing tests.

An earlier rerun was interrupted at 81% by a container restart with no
failure printed up to that point; this run replaces it.
