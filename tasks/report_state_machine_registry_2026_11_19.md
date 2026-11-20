**New primitives introduced:** `scout/statemachines/` package (descriptive state-machine registry: `model.py`, `registry.py`, `render.py`, `audit.py`, CLI in `__main__.py`); `scripts/audit_state_machines.py` (read-only database auditor); `docs/state_machines.md` (generated reference); `tests/test_statemachine_registry.py`, `tests/test_statemachine_audit.py` (drift guards). No schema change, no migration, no new setting, no runtime import.

# Report — state-machine registry (2026-11-19)

Request: "Find all of the implicit state machines in my app. Make the best
decision that will elevate all three: the user experience (UX), developer
experience (DX), and agent experience (AX) always, without breaking anything."

Surveyed at master `0f7fa3af` (PR #600). Branch:
`claude/main-session-last-discussion-b8hsn7`. No pull request was opened.

## 1. What was found

About 70 state machines; two were already explicit (the Solana execution run
and the Pons lifecycle ordering). The inventory, with where each one lives and
why it was implicit, is in `tasks/findings_implicit_state_machines_2026_11_19.md`.

## 2. The decision

Declare the main lifecycles in one **descriptive** registry that no runtime
module imports, and build the three audiences' tools on top of it.

Why not wire guards into the write sites: that is the only option that could
break something. Most write sites are unconditional UPDATEs whose legal moves
were never written down; a guard built from a first-pass reading would refuse
moves the code legitimately makes (the reviews below found several such
moves that the first draft had missed). A registry that only describes
changes no behavior, and gives a later, reviewed guard something to be
checked against.

| Audience | What it gets |
|---|---|
| Operator (UX) | `docs/state_machines.md`: every registered status in plain language, what it means, what it can become, which ones need action. `scripts/audit_state_machines.py`: read-only check of a database for rows in undeclared or contradictory states. |
| Developer (DX) | One place per lifecycle instead of string literals across write sites. Tests fail when a status value is added to a CHECK constraint, Enum, Literal, the paper close literals or the dashboard copies without the registry following, when the Solana transition table changes, or when a writer function is renamed. |
| Agent (AX) | `python -m scout.statemachines list / show / next / json`, a JSON form of the whole registry, and a `CLAUDE.md` section saying where to look and how far to trust it. |

22 machines are registered; about 47 more are catalogued but not declared.

## 3. Limits — read before relying on it

- **It is a map, not a proof.** Tests hold the state *lists* to the code for
  13 of the 22 machines (8 by CHECK constraint, 5 more by Enum / Literal /
  literal scan / dashboard copies). Transitions are held to the code for one
  machine, `solana_execution`. For the rest, a test only checks that the
  named function still exists. Meanings, guard labels and hazard notes are
  reviewed prose. Each machine's "Checked by tests" line in the reference
  says which applies.
- **Nine machines have no test on their state list**: `exit_ladder`,
  `kill_switch`, `combo_suppression`, `tg_channel` (derived from column
  combinations), `source_call_resolution`, `prediction_outcome`,
  `chain_match_outcome`, `narrative_resolution` (columns with no CHECK) and
  `ingest_source` (in memory).
- **The auditor has not been run against a real `scout.db`**, only against
  freshly migrated and hand-built test databases. Each machine costs one or
  two full table scans.
- `tg_listener` counts mix the listener's own row with per-channel and
  shadow-scan heartbeat rows that share the column.

## 4. Verification

| Check | Result |
|---|---|
| Focused tests (`tests/test_statemachine_registry.py`, `tests/test_statemachine_audit.py`) at the final tree | 89 passed |
| Registry self-check (`python -m scout.statemachines check`) | 22 machines, 0 problems |
| Six mutation checks (new close literal; registry state not in CHECK; `via` naming a missing function; script opening read-write; a runtime module importing the registry; stale doc) | each failed a test; each reverted |
| Full suite on untouched master in this container | 41 failed, 8978 passed, 28 skipped |
| Full suite on the branch | 42 failed, 9065 passed, 28 skipped |

The 41 master failures are environment-dependent script tests
(`test_revival_verdict_watchdog.py` 17, `test_backup_create_script.py` 9,
`test_revival_criteria.py` 8, `test_round20_health_offhost_backup.py` 3,
`test_round20_offhost_backup_script.py` 2, `test_held_position_prices.py` 2).
The branch run failed the same 41 plus
`test_statemachine_registry.py::test_generated_reference_is_current`. That
one failed because the reference was being regenerated in the same worktree
while the suite ran; it passes at the final tree (part of the 89 above).

**Not done:** the full suite was not rerun after the last two rounds of
review fixes. Those fixes touched only `scout/statemachines/`, the audit
script, the two new test files and documentation, and a test pins that
nothing under `scout/`, `dashboard/` or `scripts/` imports the registry
(other than the audit script). CI has not run on this branch.

## 5. Independent review record

| Round | Scope | Outcome |
|---|---|---|
| 1 | Two reviewers: registry accuracy against the code; tooling and tests | Many inaccuracies in the first draft (missed transitions, wrong guard labels, wrong meanings, one entry that was not a lifecycle, a vacuous read-only test). All applied; `order_confirmation` removed from the registry. |
| 2 | One reviewer: every corrected entry re-read against the code, plus the audit code | No missing or mispointed transition. 10 wording / guard-label corrections and one real gap (the audit script exited 0 on a database with none of the tables). All applied. `OPERATOR_ACTION` and `TG_RESOLUTION` were not re-read in this round. |
| 3 | One reviewer: the 11 round-2 fixes only | 9 confirmed, 2 partly (surface lane does not insert `dispatch_failed`; time-death cannot fire after leg 1), one message wart in the script. All three applied. |

**REVIEW BARRIER status: not cleared for merge.** The three round-3 edits
have not themselves been re-reviewed, exact-head CI has not run, and no
`.reviewers/<PR>.toml` exists because there is no PR. Nothing here has been
merged.

## 6. Approvals log

| Action | Class | Approval record | Timestamp (UTC) |
|---|---|---|---|
| Survey, registry, tests, generated doc, auditor (additive, no behavior change) | implementation | Operator message quoted at the top of this report | 2026-11-19 |
| `CLAUDE.md`: new "State machines" subsection | implementation (project instructions) | Same message (the agent-experience part). It changes standing instructions for every future session, so it is called out for explicit confirmation at review. | 2026-11-19 |
| Session worktree `/home/user/gecko-alpha-wt`; branch brought up to master `0f7fa3af` by a normal merge (no force push) | branch maintenance | Session branch instruction; operator message "Making sure you have access to latest code" | 2026-11-19 |
| Commit and push to `claude/main-session-last-discussion-b8hsn7` | push to session branch | Session branch instruction (develop, commit and push on this branch only) | 2026-11-19T16:07Z |
| Pull request, merge to master, deploy, any flag or production-state change | — | None requested; **none performed** | — |

No action is flagged `executed-without-recorded-approval`.

## 7. Defects noticed (not fixed here)

> **Update 2026-11-20:** items 1–5 below were fixed afterwards on the same
> branch. See `tasks/report_state_machine_defect_fixes_2026_11_20.md` for what
> changed, the review record and what is still open. Item 6 is not fixed.

25 items are listed with file and line in section 4a of the findings
document; this change alters no behavior, so none is fixed. The ones that
touch money or safety controls:

1. **Solana forward swap adopted after a confirmation timeout cannot reach
   `reconciled`** — the lane's own guard raises
   (`scout/live/solana_lane.py:4335-4357`, `:4421-4427`). Read from the code
   by me and by two reviewers; not reproduced by a test.
2. **`PilotRunner.cancel` (Kraken pilot) writes `rejected` without checking the row's
   status**, so it can relabel a closed row
   (`scout/live/kraken_pilot.py:5024-5118`).
3. **An expired kill that has not been cleared blocks the next kill from
   engaging** until the automatic clear runs
   (`scout/live/kill_switch.py:197-209`).
4. **`record_contract_coin_map` overwrites `coin_id` on conflict**, so a
   resolved mapping can be reset to NULL (`scout/db.py:1771-1774`).
5. **An unrecognised paper close reason silently becomes `closed_manual`**,
   which the combo statistics exclude (`scout/trading/paper.py:748-759`).
6. **The surface-alert lane's provisional `sent` row can be committed by
   another writer before the send finishes**
   (`scout/trading/trade_surface_alerts.py:266-333`). Read from the code; the
   race was not reproduced.

Section 4b of the findings document holds further leads from the survey that
nobody has confirmed.
