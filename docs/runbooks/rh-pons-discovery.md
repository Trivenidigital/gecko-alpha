# RH/Pons observation readiness

The collector is observe-only: discovery is not a trade recommendation and
execution eligibility remains false. Keep the collector off until deployment
verification and RPC access have passed the live read checks below.

## Health and timing

`ingest_watchdog_state.source='rh_pons'` is a successful-scan heartbeat.
An empty but fully completed scan updates it; failed or partial scans must not.
The generic ingestion starvation watchdog must not refresh it on restart.

Provisional liveness SLO: at least one completed scan per hour while enabled.
Poll cadence should be comfortably inside that limit. A quiet launch market
can yield zero new event/discovery rows, so row age is diagnostic rather than
an outage verdict. Measure actual poll p50/p95 duration and launch rows/hour
after activation; do not infer them from retrospective event counts.

`RH_PONS_POLL_TIMEOUT_SEC` defaults to 30 seconds (valid range 1–300).
Capture runs as a dedicated worker (`run_rh_pons_loop`), spawned by the
pipeline only when `RH_PONS_COLLECTOR_ENABLED=true`; the detection cycle makes
no RH calls. Each pass is cancelled at the deadline. Incomplete coverage is
retried from its durable checkpoint; a timeout is not a successful heartbeat.
The worker never exits while enabled and is cancelled with the other workers
at shutdown.

Loop control (all in memory; a restart re-derives it):

- The window starts at `RH_PONS_MIN_SCAN_SPAN_BLOCKS` (default 100, capped at
  `RH_PONS_BACKFILL_BLOCK_SPAN`) and doubles after a pass that stayed behind
  the head in under half its deadline, up to `RH_PONS_BACKFILL_BLOCK_SPAN`.
- A pass that did not reach its starting head is followed immediately. The
  loop sleeps `RH_PONS_IDLE_SLEEP_SEC` (default 2) only after reaching that
  head, or when the provider head is behind the checkpoint.
- Timeouts, failures and HTTP 429 halve the window toward its floor and back
  off exponentially up to `RH_PONS_FAILURE_BACKOFF_MAX_SEC` (default 60),
  honouring a numeric Retry-After within that ceiling. Refusals (no URL, no
  verified deployment, chain mismatch) back off without shrinking.
- With no checkpoint, the first coverage start is pinned for the process, so
  a failed or shrunken cold-start retry never skips launches. A restart before
  the first completed pass derives it again from the then-current head.
- A provider head below the checkpoint never moves coverage backwards.

Transports:

- Headers are read in strict JSON-RPC batches of `RH_PONS_HEADER_BATCH_SIZE`
  (default 50, at most two POSTs in flight). Duplicate, bool or unknown ids,
  per-item errors, wrong heights and invalid hash/timestamp fail the pass;
  they never downgrade the transport. Only an explicit refusal (HTTP
  400/404/405/413/415/501 or a top-level JSON-RPC error object other than
  throttling) falls back to single requests, eight in flight, for the rest of
  the process. Reorg anchor, per-log hash and final-block checks are unchanged.
- `RH_PONS_TOPIC_ONLY_TRADE_QUERY=true` (default) fetches CurveBuy/CurveSell by
  topic in one query per pass and checks each emitter against known curves
  (indexed lookup for returned emitters, curves launched in the pass, and
  curves referenced by overlap evidence) before decoding. Other emitters are
  counted as `excluded_foreign_logs` and never recorded. Setting it false
  restores address-batched queries, whose RPC count grows with every known
  curve.
- Projection repair is scoped to the log identities a pass processed; the
  full rebuild remains available as `reconcile_curve_launch_projection`
  without `identities` for diagnostics.

Pacing and header budget (loop only; `poll_once` is unpaced):

- Every logical JSON-RPC call is charged to an in-memory token bucket
  (`RH_PONS_RPC_CALLS_PER_SEC`, default 8; `RH_PONS_RPC_BURST_CALLS`, default
  100); a header batch of N costs N. These are provisional, not a known
  provider quota. A 429 empties the bucket, halves the rate (not below
  `RH_PONS_RPC_MIN_CALLS_PER_SEC`, default 1) and holds calls for a numeric
  Retry-After capped at `RH_PONS_FAILURE_BACKOFF_MAX_SEC`; each completed pass
  restores a tenth of the configured rate.
- Header reads per pass are capped by `RH_PONS_MAX_HEADERS_PER_PASS` (default
  80) and by what the pacer can supply in half the remaining pass deadline
  (minus the two tail calls). If a window needs more, the pass covers the
  largest block prefix whose headers fit and checkpoints only that prefix;
  logs above it are re-fetched next pass. The next window is sized to about
  twice the verified progress.
- Per-pass logs report `truncated`, `header_budget`, `rpc_s` (time in HTTP
  exchanges), `pacing_wait_s` and the current `rpc_rate`.

Why: the 2026-09-13 public-RPC smoke
(`investigation/rh_capacity_smoke_throttled_20260913.json`) needed ~0.5
header reads per scanned block. Doubling the window produced a second
~100-call header burst seconds after the first and three passes failed with
429. Checkpoint integrity held, but the 3000-block backlog did not drain.

The existing watchdog supports `--source rh_pons`; it reads the DB read-only,
uses the RH heartbeat and discovery table, and reports missing/stale/invalid
heartbeats. Preview on Windows or Linux with:

```text
python scripts/dex_discovery_watchdog.py --source rh_pons --db scout.db --enabled true --discovery-enabled true --staleness-hours 1 --dry-run
```

For Linux deployment, install `scripts/rh-pons-watchdog.sh` alongside the
collector and invoke every five minutes through the existing scheduler.
Set `RH_PONS_WATCHDOG_ENABLED=true` in the scheduler environment and
`RH_PONS_COLLECTOR_ENABLED=true` in the pipeline environment only after
activation review. The wrapper captures the watchdog gate before loading
`.env`, so a stray `.env` line cannot arm it. Default watchdog gate is off.
`RH_PONS_POLL_STALENESS_ALERT_HOURS=1` selects the provisional SLO;
`RH_PONS_WATCHDOG_STATE_DIR` separates cooldown state from the DEX lane.
The RH watchdog also requires a fresh checkpoint for the exact verified
factory and a measured head. `RH_PONS_MAX_HEAD_LAG_BLOCKS` (default 2000)
sets the maximum permitted `head_block - next_block + 1`. Missing head,
stale checkpoint, and excessive lag are distinct breaches even with a fresh
heartbeat. At the observed roughly 10 blocks/second, 2000 blocks is about
200 seconds; measure the current chain rate before selecting a stricter SLO.
Alerts are plain text with dispatched/delivered structured logs. Sending and
locking use the existing Linux watchdog implementation; Windows supports
read-only previews. This runbook does not itself install a scheduler job.

## Acceptance before observation activation

- Verified chain ID, deployment block, factory bytecode, launch/trade ABI and
  real log examples agree; alternate deployments are not decoded as V2.
- The selected provider supports chain/head, logs and block-header reads.
  A successful chainId response alone is insufficient.
- Completed scan progress advances across empty ranges, catches the first
  trade in a newly discovered launch block, and survives interruption/reorg.
- Timestamp and chain identity evidence is retained. No URL credentials are
  written into provenance or logs.
- An enabled collector completes a real scan; its scan progress and heartbeat
  advance. An injected provider failure leaves success heartbeat unchanged and
  the dry-run watchdog identifies the outage.

## Evidence for early usefulness

First startup begins near the head, with `RH_PONS_INITIAL_LOOKBACK_BLOCKS`
defaulting to the configured scan span. It does not claim complete history.
`RH_PONS_START_BLOCK` explicitly requests archival coverage on a fresh DB;
subsequent restarts resume the durable checkpoint. Historical backfill should
use a separate evidence DB and cannot qualify as real-time capture. The
watchdog deliberately reports its lag until it catches up.

Capacity limits still matter. Topic-only trade queries and identity-scoped
projection repair keep per-pass work tied to the blocks and events scanned,
not to accumulated curves or history, but that has only been pinned by
fixture tests. A tiny successful sample proves transport and decoding, not
capacity or signal quality.

## Capacity acceptance (before observation activation)

Run `investigation/rh_pons_sustained_capacity_probe_20260913.py` on the target
host with its temporary database. It uses in-code settings, never `.env`, and
reports quota as unknown (it does not load-test to 429). Judge two phases
separately, over the same monotonic windows:

- **Backlog drain** (finite cold-start backlog): unique coverage, excluding
  the reorg overlap, must reach at least 1.2x the chain rate measured over the
  same window, until completion-time lag is within the catch-up threshold.
- **Steady state** (passes after catch-up): new blocks cannot be covered faster
  than they arrive, so do not apply the 1.2x ratio. Require completion-time lag
  p50 <= 50 and p95 <= 150 blocks, timeouts <= 1% of passes and zero checkpoint
  regressions over a pre-registered pass count (default 300).
- **Real-time event delay**: `observed_at - event_time` p50 <= 10 s and
  p95 <= 30 s, counting only events in blocks after the catch-up head.
  Backfilled events never count. Block timestamps have 1 s resolution.

Record the JSON report with the endpoint class, pass count and RPC calls per
minute. A throttled probe (repeated 429s) is a provider-capacity finding, not
a collector pass.

Run `scripts/compare_discovery_latency.py --db <captured-db>` over observations
captured by the running lanes. Use the paired sample count and RH advantage on
the same chain-qualified tokens. Keep unobserved, invalid-clock and unsupported
identity cases in the denominator. Fixture and historical backfill samples
cannot establish real-time detection advantage. Pre-register a required sample
count based on the decision being tested and observed arrival rate; do not
substitute a calendar soak or a claim based on synthetic tests.
