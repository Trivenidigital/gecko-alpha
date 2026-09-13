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
