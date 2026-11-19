# Report — RH/Pons sustained early capture (2026-09-13)

Branch `feat/rh-sustained-capture`. Plan:
`tasks/plan_rh_pons_sustained_capture_20260913.md`. Capture only; nothing is
activated. `RH_PONS_COLLECTOR_ENABLED` still defaults off. No production
config, DB, service, alert or trade state was touched.

## What changed

- **Dedicated worker.** `run_rh_pons_loop` is spawned by `main()` only when
  enabled; `run_cycle` makes no RH calls. The loop never exits while enabled
  (a returning worker would stop the service) and is cancelled with the other
  workers at shutdown.
- **Loop control.** In-memory window from `RH_PONS_MIN_SCAN_SPAN_BLOCKS`
  (capped at the max) to `RH_PONS_BACKFILL_BLOCK_SPAN`. No sleep while a pass
  stays behind its starting head; idle sleep only after reaching it. Timeouts,
  failures and 429 halve the window and back off exponentially to
  `RH_PONS_FAILURE_BACKOFF_MAX_SEC`, honouring a bounded Retry-After.
  Cold-start coverage is pinned in memory, so a failed or shrunken first pass
  never skips launches. A provider head below the checkpoint never regresses it.
- **Headers.** Strict id-keyed JSON-RPC batches (`RH_PONS_HEADER_BATCH_SIZE`,
  default 50, two POSTs in flight). Malformed replies fail the pass closed and
  keep batching on; only explicit refusal falls back to bounded single calls.
  Hashes are normalized to lowercase. Reorg anchor, per-log and final-block
  checks are unchanged.
- **Trades.** One topic-only query per pass (`RH_PONS_TOPIC_ONLY_TRADE_QUERY`,
  default true). Emitter membership (indexed lookup of returned emitters, plus
  curves launched in the pass and curves referenced by overlap evidence) is
  checked before decoding; foreign and mimic emitters are excluded and counted.
  A member log that fails decoding still fails the pass. The address-batched
  mode remains and still grows with every known curve.
- **DB scope.** Projection repair is scoped to the log identities a pass
  processed. Affected tokens come from launch/graduation rows of those
  identities (old and new token on replacement); NULL-token markers are reached
  through identity; one atomic publish. No startup rebuild: the checkpoint only
  advances after scoped repair, and replay reprocesses every uncheckpointed log.
  The full rebuild stays available without `identities`. New index
  `idx_curve_launch_ev_block` bounds overlap-range reads; no table or migration.
- **Compatibility.** `poll_once` keeps its signature, cadence gate and legacy
  transports; existing collector tests are unchanged.

## Commits

- `c3ea686c` — bounded projection repair, emitter membership, block index.
- `aeb643e9` — dedicated loop, strict batch headers, topic-only trades,
  capacity probe runner, runbook.

## Verification

| Evidence | Result |
|---|---|
| Local (Git Bash, `--noconftest`): `test_rh_pons_capture_scope.py` + `test_rh_pons_db_hardening.py` | 21 passed |
| Local DB guard mutations (publish scope, token/ids event filters, NULL-marker join) | 4/4 killed, file restored |
| Codex native REQUEST 1 (baseline, six RH suites) | 91 passed |
| Codex native REQUEST 2 (RH suites + new loop/scope/deadline + main/tg wiring) | 165 passed, 2 existing marker warnings |
| Codex native REQUEST 3 (formatted HEAD rerun + collector guard mutations) | pending |

Git Bash here cannot import aiohttp (`OPENSSL_Uplink ... no OPENSSL_Applink`),
so aiohttp-backed suites ran natively via Codex. The real `main()` wiring test
drives startup, spawns the worker behind the flag, and asserts it is cancelled
at shutdown.

## Round 2 — throttled live smoke, pacing and header budget

Inputs: Codex native REQUEST 3 (227 passed; mutations 16/18 killed) and
`investigation/rh_capacity_smoke_throttled_20260913.json` (isolated DB,
public RPC): 7 passes, 4 completed and 3 failed `headers_unavailable` on 429.
The 3000-block backlog did not drain; checkpoints never regressed.

What the smoke shows:

- Header reads scale with active blocks: 442/812, 205/412, 261/412 and 88/212
  per window, about 0.44–0.54 per block, plus ~13 overlap per pass.
- Passes needing 13, 65 and 99 headers succeeded. Every failure had requested
  204 logical calls, meaning the second round of two 50-call batches. The
  window had just doubled and there was no pause between passes.
- Every pass took ~7.5–9.5 s whatever its size, even 100 blocks with 19 calls.
  The smoke did not record where that time went.
- The chain advanced ~9.8 blocks/s over the run.

Changes (smallest correction that removes the failure mechanism):

1. Two batch cases now reach the guards that survived mutation. A same-length
   reply whose duplicate id leaves an expected id unanswered is refused by the
   duplicate-id guard alone, since each item's height check passes. An item
   carrying both a valid result and an error is refused by the per-item error
   guard alone. Neither guard is equivalent to another.
2. Loop-owned token bucket over logical calls (a batch of N costs N). A 429
   empties it, halves the rate to a floor and holds for a capped Retry-After;
   completed passes restore it additively. Waits are serialized across
   concurrent POSTs. `poll_once` stays unpaced.
3. Per-pass header budget: min(`RH_PONS_MAX_HEADERS_PER_PASS`, what the pacer
   can supply in half the remaining deadline, minus the two tail calls). If
   the window needs more, the pass covers the largest block prefix that fits
   (exact sweep, checked against brute force). It writes and checkpoints only
   that prefix, and the next window is sized to twice the verified progress.
   A throttled pacer therefore shortens passes instead of timing them out.
4. Instrumentation: `rpc_s`, `pacing_wait_s`, `truncated`, `header_budget`,
   `rpc_rate` per pass.
5. Runner: the real-time cutoff is the catch-up head, not the scanned block.
   Missing, unparseable and negative clocks are counted and fail
   `event_clocks_valid`. `--db-output` saves an integrity-checked copy of the
   isolated DB after the worker is cancelled, refusing to overwrite (checked
   before any network call). Pacing arguments are passed through.

## Actual remaining capacity limits

- **Header verification is the binding cost.** At ~9.8 blocks/s and
  ~0.5 headers/block, steady state needs about 5 header calls/s plus roughly
  6 calls per pass. With ~8 s passes that is about 5.5–6 logical calls/s.
- **The provisional pace has thin headroom.** The default 8 calls/s leaves
  about 2 calls/s spare, roughly 4 extra blocks/s. A 3000-block backlog would
  take on the order of 12+ minutes, and only if the endpoint sustains 8/s.
- **Quota is unknown.** One smoke accepted ~195 calls over ~27 s and one
  100-call burst, then throttled a second burst. If the sustainable rate is
  below ~6/s, the halved rate cannot keep pace and lag grows without bound.
  Pacing keeps passes completing and coverage correct; it cannot create
  provider capacity.
- **Steady-state lag likely fails the runbook target regardless.** A ~8 s
  pass at ~9.8 blocks/s means ~80 blocks of completion lag, against the
  p50 <= 50 target, and event delay tracks pass latency. The cause of the
  constant pass time is unmeasured; the new `rpc_s` and `pacing_wait_s` fields
  are there to locate it.
- **Options not implemented (each needs a decision or review):**
  - a higher-quota endpoint (operator choice; no purchased provider here);
  - trusting log `blockTimestamp`/`blockHash` with range-endpoint verification
    instead of a header per event block (a semantics change);
  - a bounded header cache for retries (reorg review);
  - fewer round trips per pass (cached chain id, concurrent log queries).

## Round 3 — independent reviews and fixes

Inputs:
- Codex native REQUEST 4(a): **254 passed**, 2 existing marker warnings.
- Two terminal independent reviews of `d4694a06`, the pre-pacing code:
  `claude-review-concurrency-logic.json` and `claude-review-ops-silent.json`
  in the review worktree.
- Codex's check of Robinhood's documentation: the public RPC is rate-limited
  and not meant for production high-throughput or latency-sensitive use, and
  it publishes no numeric quota.
- Codex runs the 31-mutant check separately at `59dfc74c`. It does not cover
  this head.

| Finding | Disposition |
|---|---|
| **Concurrency blocker**: collector commits on the shared connection could make the chains tracker's half-finished transaction durable, or lose collector rows to its rollback | **Fixed.** The loop opens its own connection (`_open_collector_db`), closed at shutdown, with the write-lock wait bounded to half the pass deadline. Uncommitted work is rolled back before each pass and after any unsuccessful one. New tests use a real loop: a sibling transaction on the shared connection stays uncommitted and the collector's later rows are durable; a pass cancelled after an INSERT but before its commit leaves nothing durable, and the replay matches an uninterrupted run (events, discoveries, membership, checkpoint). |
| Transient batch errors disabled batching | **Fixed.** Only HTTP 400/404/405/415/501 or JSON-RPC -32600/-32601 disable batching. Other top-level errors fail the pass and keep batching; HTTP 413 halves the batch size. |
| Topic query has no fallback under provider result caps | **Residual, scoped.** The pass fails closed and the new failure-streak watchdog pages; nothing is silently skipped. |
| **F1** window/backoff bounced back to the throttled size | **Fixed.** After a throttle, a span ceiling and a batch ceiling hold both below the throttled size and rise additively on clean passes; the failure count decays instead of resetting. The pacer from round 2 empties and halves on 429. Tests cover the sequence and a synthetic provider that refuses batches above 20 calls: it converges with at most 3 throttles and no gaps. |
| **F2** throttling not classified | **Fixed.** HTTP-200 JSON-RPC throttle errors (single call and per batch item) set rate limiting and honour Retry-After, including HTTP-date form. A failed or throttled `eth_chainId` is `failed/chain_id_unavailable`; only a real mismatch is `refused`. |
| **F3** probe report untruthful | **Fixed.** Explicit `stop_reason` (`rate_limited`, `max_rpc_calls`, `steady_target`, `max_passes`, `max_seconds`); overall `verdict` of pass, fail or inconclusive, with missing catch-up or a rate-limit stop counting as fail. Steady-state failed and throttled passes are criteria. Checkpoint regressions come from the committed DB checkpoint read after each pass. Drain chain growth starts from the first observed head. The hardcoded `production_mutated` claim is replaced by explicit isolation facts. The throttled smoke JSON now evaluates to `fail/rate_limited`. |
| **F4** probe settings leaked from environment | **Fixed.** Settings are init-only (no `.env`, no environment), every effective RH setting is reported, and `--max-rpc-calls` (default 10000) is added. |
| **F5** monitoring coarse; frozen head hides lag | **Fixed.** A per-attempt streak row (`rh_pons_attempt`) and attempt heads raise the checkpoint head without touching the success clock. The watchdog gains `--staleness-minutes` (wrapper default 10) and `attempt_failures_exceeded`, and the generic ingestion watchdog excludes the new row. |
| **F6a** watchdog disarms if the flag is not in `.env` | **Fixed.** Recent collector activity with a false gate raises `enabled_gate_mismatch`; the runbook requires the flag in `.env`, and the watchdog knobs are now Settings fields so `.env` lines are valid. |
| **F6b** enabled without URL retries silently | **Fixed.** One startup error log, and each refused pass records a failed attempt, so the streak pages. |
| **F7** fixed per-pass call cost | **Partly fixed.** Chain id is checked once per session and again after failures. The probe has a call ceiling and an optional calls-per-minute criterion. Overlap headers and two block-number calls per pass remain: they carry reorg and lag guarantees. |
| **F8** timeouts lose context | **Fixed.** Timeout and error results carry the pass stage, block range, head and header count; `rpc_responses` counts answered calls separately from sends. |
| **F9** `RH_PONS_POLL_EVERY_N_CYCLES` inert in the loop | **Documented** as `poll_once`-only in config and runbook. |

The reviewer inferred a provider burst cap of roughly 100 calls. **I have not
adopted that as a quota.** Every rate and size here stays a provisional
setting, and the official documentation gives no number.

Verification this round:
- Local: DB suites 22 passed (scope and hardening, including the attempt-head
  test); the header cutoff matched brute force on 5000 cases.
- **Native REQUEST 5 (head `2ab6354a`): FAILED, 284 passed / 54 failed.**
  - **Root cause, a regression I introduced:** `_validate_config` compared the
    unset argparse default of `--max-consecutive-failed-passes` (None) with
    `< 1`. That raised TypeError for every caller without the new flag, in
    both the DEX and RH lanes. It accounts for 45 failures: 17 in
    `test_rh_pons_watchdog.py` and 28 in `test_dex_discovery_watchdog_script.py`.
  - **Fix:** unset now means "no streak check", the previous behaviour; only an
    explicit value is range-checked. A guard test covers both cases.
  - **The other 9 are pre-existing Windows-only failures, not caused by this
    branch.** Six `test_dex_discovery_watchdog_script.py` tests reach the
    Linux-only send/lock path, which imports `fcntl`. Three
    `test_dex_discovery_watchdog_wrapper.py` tests run bash with a Windows
    `GECKO_PYTHON`.
  - **Evidence for the 9:** with the fix, the DEX script and wrapper suites
    run locally give 28 passed / 9 failed. Base `dcb5dd26`, exported with
    `git archive`, gives the identical 9 with the same suites. They still
    need confirmation on Linux CI.
- **Native REQUEST 6(a):** 35 passed (pacing and review-fix tests).
- **Native focused rerun after the fix (Codex, head `49f119e7`):**
  - Suites: RH watchdog, DEX watchdog script and wrapper
    (`.claude-native-watchdog-fix.txt`).
  - Result: **52 passed, 9 failed.** The 9 are exactly the Windows-only set
    above (6 `fcntl` send/lock tests, 3 bash-wrapper tests); nothing else
    fails.
  - The new guard test passes natively.
  - The collector suites were unchanged by this fix and were not repeated;
    REQUEST 6(a) passed 35.
- **Baseline accepted by Codex:** the unchanged PR572 integration worktree
  shows the same 9 DEX watchdog failures (28 passed), so no new watchdog
  failure remains locally. Linux CI is still required for those 9.
- **Mutation round 3 (Codex, author worktree): complete.** 28 assertion kills,
  1 expected equivalent (the pacer lock), no timeouts, skips or survivors;
  source restored.

Mutation round 2 (Codex, review worktree `59dfc74c`): 31 mutants gave 28
assertion kills, 2 hangs and 1 survivor. The hangs are not counted as kills.

- **Hang: `oversized_call_deadlocks`.** The fake-clock sleep never yielded, so
  a looping `acquire` could not be cancelled. The fake clock now yields and
  fails the test after 1000 simulated sleeps.
- **Hang: `budget_ignores_pacer`.** The pass waited in real time on a
  0.5 calls/s bucket. The test now runs the pass under a 5 s `asyncio.timeout`.
- **Survivor: `pacer_waits_not_serialized` is judged equivalent for the rate
  requirement.**
  - `acquire` checks tokens and deducts them in one synchronous step (no await
    in between), so two tasks cannot both spend the same tokens.
  - A waiter recomputes the bucket after every sleep, so tasks woken together
    serialize themselves.
  - Removing the lock therefore changes only FIFO ordering and redundant
    wake-ups, not the enforced rate or overdraw. No test asserts fairness, and
    I did not add that requirement.
  - The docstring's claim that the lock prevents overdraw was wrong and is
    corrected. The lock is kept for ordering.
- **Round-3 harness** (`rh_capture_mutations_round3.py`, REQUEST 6):
  - a 180 s timeout per test run, killing the whole process tree;
  - TIMEOUT reported separately from KILLED;
  - covers the two formerly hanging mutants, the equivalent lock mutant
    (reported as expected), round-1/2 guards whose code changed, and 23 new
    round-3 guards across the collector, DB and watchdog.
  - Results: see mutation round 3 above.

## Round 4 — ops re-review (`claude-review-ops-silent-round2.json`)

The re-review found no merge blockers for capture-only and confirmed F1–F9
closed or dispositioned.

| Item | Disposition |
|---|---|
| **R1** one 24 h cooldown silences every later RH breach reason | **Fixed**, using the existing cooldown mechanism. RH alerts also record the paged reason, so a different reason pages inside the window and the same reason stays suppressed. Legacy state with no recorded reason pages once. DEX behaviour is unchanged. Tests were written first: the A-then-B test failed before the fix. |
| **R2** deliberately disabling the lane pages `enabled_gate_mismatch` | **Documented** disable order (watchdog first, then lane) and re-enable order; the page itself is correct when the order is reversed. |
| **R3** unset streak limit fails open; no wrapper argument contract | **Fixed.** A wrapper test with an argument-echo interpreter stub asserts `--staleness-minutes` and `--max-consecutive-failed-passes` reach Python (defaults and `.env` values). The runbook preview command passes both, with a note that omitting the streak flag disables that check. |
| **R4** runbook contradicted batch-fallback code | **Fixed.** 413 shrinks; only 400/404/405/415/501 or -32600/-32601 disable batching; other top-level errors are transient; Retry-After accepts HTTP-date. |
| Verification (local, Git Bash `--noconftest`) | New `tests/test_rh_pons_watchdog_ops.py`: 2 failed / 3 passed before the fix, 5 passed after. With the DEX script and wrapper suites: 33 passed, 9 failed, the same known Windows baseline set. Cooldown-guard mutants (reason not compared, reason not written) both KILLED; source restored. Native check requested as REQUEST 8. |
| **R5** owned connection runs full `Database.initialize()` | **Deferred, residual.** It is idempotent after pipeline startup but adds write-lock attempts at worker start and reopen, and a migration failure there surfaces only through the minutes-scale staleness page (no attempt row without a DB). A lighter owned open is a follow-up, not part of this increment. |

## Not established

- Sustained capacity, real-time latency and provider quota. The isolated
  capacity smoke DID run once against the public RPC and FAILED:
  `investigation/rh_capacity_smoke_throttled_20260913.json` shows the
  3000-block backlog did not drain and three of seven passes were throttled.
  That run was on the pre-pacing code. The paced collector has not been
  probed live since, and no passing capacity run exists.
- Wall-clock performance is not asserted in tests; bounded query and RPC
  scope is pinned instead (5000 unrelated evidence rows, 500 unrelated curves).
- A process restart before the first completed pass re-derives cold-start
  coverage from the then-current head (logged, not persisted).
- Mid-write cancellation IS tested on the collector-owned connection: a real
  loop pass whose commit is cancelled by the deadline leaves nothing durable,
  and replay matches an uninterrupted run. Not tested: cancellation inside
  aiosqlite's worker thread at every statement boundary, or process kill.
- Linux CI confirmation of the 9 Windows-only DEX watchdog failures and of the
  wrapper tests on the real cron path.
- Useful alerts, quote approval, safety and execution remain out of scope.

## Approvals log

| Action | Class | Approval record | When |
|---|---|---|---|
| Implement bounded capture increment on this branch, commit owned paths | implementation | Operator authorization relayed by Codex in this session's implementation prompt | 2026-09-13 |
| Live capacity probe, activation, merge, deploy | ops/merge | Not requested or performed here | — |

## Integration verification — Codex, 2026-09-13

The implementation was integrated onto merged master in `feat/rh-capture-ready`.
The original checkout was preserved. Claude Max supplied implementation and
independent reviewers; Codex ran native tests, fault checks and integration.

- REQUEST 5: 284 passed; the watchdog default regression was subsequently fixed.
- Focused watchdog verification after the fixes: 57 passed, nine Windows failures.
  The identical nine failures reproduce on the unchanged PR572 baseline: six
  missing `fcntl` cases and three Bash-wrapper cases. Linux CI remains required.
- Pacing/review-fix verification: 35 passed.
- Mutation round 3: 28 assertion failures, one independently confirmed equivalent
  lock mutation; no timeout, survivor or skipped anchor. All files restored.
- Final cooldown checks: both changed-reason and persistence mutations caused
  the expected assertion failure; source restored.
- Black checked all 15 changed Python application/test/probe files; no changes
  required. `git diff --check` passed.
- Capacity is not established: the isolated public smoke failed to drain its
  backlog and stopped after three rate-limited passes. Raw evidence is retained
  under `investigation/`; the corrected probe has not been rerun on a suitable
  provider. No activation, deployment or trade was performed.

| Authorized action | Record |
|---|---|
| Complete implementation, coordination, review and integrate verified fixes | User: “Go on and take it to finish line, coordiante with Claude for required tasks.” |
| Production activation, live trading, provider purchase | Not performed; remains outside this increment |

Final independent clearance and exact-head CI records will accompany the PR.

PR: https://github.com/Trivenidigital/gecko-alpha/pull/576

All four independent vectors are terminal and cleared at
`5ca39f9c731aed521068755595bc76d22eb2508e`. The final write-order fix passed
six native watchdog-ops tests, including injected failure between state writes.
The two Claude reviewer session IDs and watched paths are in `.reviewers/576.toml`.
Integration and merge of this verified increment use the user authorization
recorded above; production activation remains excluded. Exact-head Linux CI is
pending and must pass before merge.

CI run 34787996119 completed with 7,849 passed, 12 skipped and one failure:
the repository silent-swallow guard caught an exception/pass in owned DB
connection cleanup. Commit ab824ac2 logs only the exception type, preserving
the original exception. Native guard plus collector verification: 63 passed.
Both reviewers re-cleared that fix and the subsequent upstream PR575 merge
at f268fb4f1511d21a66fbda989ed5e3f3f017bec0. Upstream PR575 dashboard/tests
match its reviewed head 44aee2c7. Full CI must rerun on this final integration.
