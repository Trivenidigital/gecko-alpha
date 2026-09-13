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

## Not established

- Sustained capacity, real-time latency and provider quota. The capacity
  probe (`investigation/rh_pons_sustained_capacity_probe_20260913.py`) has not
  been run; see the runbook's capacity acceptance section. Earlier transport
  samples show capability only.
- Wall-clock performance is not asserted in tests; bounded query and RPC
  scope is pinned instead (5000 unrelated evidence rows, 500 unrelated curves).
- A process restart before the first completed pass re-derives cold-start
  coverage from the then-current head (logged, not persisted).
- Cancelling a pass mid-write on the shared connection relies on existing
  append-only replay; no new fault injection beyond the scoped replay test.
- Useful alerts, quote approval, safety and execution remain out of scope.

## Approvals log

| Action | Class | Approval record | When |
|---|---|---|---|
| Implement bounded capture increment on this branch, commit owned paths | implementation | Operator authorization relayed by Codex in this session's implementation prompt | 2026-09-13 |
| Live capacity probe, activation, merge, deploy | ops/merge | Not requested or performed here | — |
