# RH/Pons discovery readiness — 2026-09-13

The original observe-only increment has been repaired and integrated on
`feat/early-discovery-ready`, based on production/master `6c56186e`.
This establishes a testable early-launch evidence lane. It does not yet
establish that the system finds profitable tokens or beats existing feeds.

## Direct collaboration

The installed authenticated Claude Code CLI is sufficient; no plugin was
needed. Claude supplied review feedback and authored the RPC capability CLI.
Codex integrated and corrected it, repaired collection/recovery/monitoring,
and ran independent durable-evidence reviews. The coordination runbook records
the reusable CLI procedure, session IDs and process-ownership safeguards.
The user's shared root checkout and unrelated interactive sessions were preserved.
The installed memory plugin twice resumed an old coding helper unexpectedly.
Task-owned helpers were stopped; a subsequent `--safe-mode` Claude review
succeeded without that helper. The runbook now uses safe mode for coordinated
jobs, preserving normal authentication and permissions without global changes.

## Delivered behavior

- Durable scan progress advances through empty blocks and resumes after restart.
- Factory plus curve-emitter queries capture trades in the launch transaction.
- Canonical evidence survives partial persistence and repeated A/B/A/B reorgs;
  projection publication is atomic on the shared SQLite connection.
- Real block timestamps and chain-qualified paired latency comparisons prevent
  cross-chain address collisions and invalid-clock claims.
- Successful-scan heartbeat is isolated from generic ingestion hydration;
  the watchdog checks liveness, exact checkpoint identity, and measured head lag.
- Public verified-source, bytecode, deployment and real launch/buy/sell evidence
  identifies the V2 factory at block 26841846. The alternate factory is direct V3
  and is excluded from the V2 decoder.
- Collector activation remains explicitly gated. Quote approval and safety
  remain unresolved, and every discovery remains execution-ineligible.

## Verification evidence

169 focused and relevant regression tests passed before the final throughput
repair, including DB migration uniqueness, configuration and main-cycle tests.
Independent review at `a83e3bad` found no durable-evidence blockers.

An isolated public-RPC probe on the production host used a temporary database
and no production configuration: two scans captured 14 launches and 133 events.
Injected RPC failure preserved the success heartbeat; zero discoveries were
execution-eligible. Raw evidence is in
`investigation/rh_pons_live_probe_before_parallel_20260913.json`.
The scans took 23.469 and 71.010 seconds, exposing sequential-header overhead;
this sample must not be presented as sustained capacity acceptance.

After batching header requests in groups of eight, a second real probe captured
19 launches and 363 events in scans taking 18.907 and 23.177 seconds. Both
completed inside the new 30-second pipeline deadline; injected failure again
preserved the heartbeat. Completion-time head measurement reported 164 and
205 blocks of lag. The samples differ, so these timings are operational
observations, not a controlled speedup estimate. Evidence:
`investigation/rh_pons_live_probe_final_20260913.json`.

Final structural/cancellation review at `c23ad743` found no concrete blockers
and passed 58 collector/DB/deadline regressions. A broader run exposed an
overbroad test assertion against unrelated CoinGecko heartbeat writes;
`7f0e6ff1` scopes it to RH and passed the combined main/deadline tests.
Production Python has no pytest installed; no packages were added to that
environment. Linux-wide regression verification is delegated to PR CI.

Final combined local regression run: **176 passed**, with six existing
marker/mock warnings. The final independent durable-evidence reviewer cleared
`1e8ded72` after inspecting the assertion-only delta. Claude Code session
`3c9deda4-5c60-4ecb-9007-96c7ebfbce0b` returned a bounded correctness clearance
for header batching, completion head and deadline handling at `c23ad743`.
It did not run tests. PR: https://github.com/Trivenidigital/gecko-alpha/pull/572.

Claude's residual observations are retained: a fixed scan span can repeatedly
time out on a slow provider; a temporarily regressed provider head can cause
redundant rescanning; final hash casing can falsely refuse a pass; and real
mid-write cancellation deserves additional fault injection before activation.
These are activation/capacity limits, not evidence of successful live readiness.

The mandatory four-vector review is recorded at `1e8ded72`: independent
durable reviewer cleared concurrency and silent-failure; the latency worker
independently cleared collector logic (58 tests); the collector worker cleared
complementary ops-safety paths (45 tests). The latter authored the head-lag
extension, which was independently covered by the durable reviewer instead.
Clearances authorize the default-off merge scope only, not live activation.

Production read-only inspection found `gecko-pipeline` active at `6c56186e`,
the RH collector disabled, and no configured RH RPC URL. The host can reach
the official public RPC. Native Windows Python DNS could not, although public
PowerShell reads worked: provider access must be tested on the actual host.
No production service, database, configuration, scheduler or trading state
was changed. Temporary probe code/data were separate from the production repo.

## Remaining acceptance boundaries

Production observation requires an explicit configured provider, scheduler
installation, sustained capacity checks, and paired first-observation data.
Use a data-count decision gate appropriate to the latency claim, not a fixed
calendar soak. Real-time superiority has not been measured yet.

This lane intentionally emits no scored candidates or user token alerts.
Turning raw launches into useful early alerts still requires measured
qualification/ranking and a reviewed connection into the signal path.
Auxiliary router/hook/pool-manager verification, quote approval, strict safety,
execution integration and live trading are separate unresolved boundaries.
