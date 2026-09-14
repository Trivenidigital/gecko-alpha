# RH/Pons observation readiness

The collector is observe-only: discovery is not a trade recommendation and
execution eligibility remains false. Keep the collector off until deployment
verification and RPC access have passed the live read checks below.

## Health and timing

`ingest_watchdog_state.source='rh_pons'` is a successful-scan heartbeat.
An empty but fully completed scan updates it; failed or partial scans must not.
The generic ingestion starvation watchdog must not refresh it on restart.

Provisional liveness SLO: a completed scan within
`RH_PONS_POLL_STALENESS_ALERT_MINUTES` (default 10) while enabled, and fewer
than `RH_PONS_MAX_CONSECUTIVE_FAILED_PASSES` consecutive unsuccessful passes. A quiet launch market
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

The worker reads and writes through its OWN SQLite connection to the pipeline
database, closed at shutdown. The shared pipeline connection is used by
writers that open bare transactions (chains tracker), so a collector commit
there could make their half-finished work durable, and their rollback could
discard collector evidence. The owned connection waits at most half the pass
deadline for the write lock; contention fails the pass, which retries. After
any unsuccessful pass, and before every pass, uncommitted work on the owned
connection is rolled back, so a write cancelled by the deadline is replayed
from the checkpoint rather than committed piecemeal or left holding a lock.

`RH_PONS_POLL_EVERY_N_CYCLES` affects only the `poll_once` compatibility
entry point; the loop ignores it. Reduce loop RPC load with
`RH_PONS_RPC_CALLS_PER_SEC` or `RH_PONS_IDLE_SLEEP_SEC`. The loop verifies the
chain id once per session and again after any unsuccessful pass.

Loop control (all in memory; a restart re-derives it):

- The window starts at `RH_PONS_MIN_SCAN_SPAN_BLOCKS` (default 100, capped at
  `RH_PONS_BACKFILL_BLOCK_SPAN`) and doubles after a pass that stayed behind
  the head in under half its deadline, up to `RH_PONS_BACKFILL_BLOCK_SPAN`.
- A pass that did not reach its starting head is followed immediately. The
  loop sleeps `RH_PONS_IDLE_SLEEP_SEC` (default 2) only after reaching that
  head, or when the provider head is behind the checkpoint.
- Timeouts, failures and HTTP 429 halve the window toward its floor and back
  off exponentially up to `RH_PONS_FAILURE_BACKOFF_MAX_SEC` (default 60),
  honouring Retry-After (delta-seconds or HTTP-date) within that ceiling.
  Refusals (no URL, no
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
  400/404/405/415/501, or a top-level JSON-RPC error -32600/-32601) falls back
  to single requests, eight in flight, for the rest of the process. HTTP 413
  halves the batch size and keeps batching; any other top-level JSON-RPC error
  is transient (the pass fails, batching stays on); throttle errors count as
  rate limiting. Reorg anchor, per-log hash and final-block checks are unchanged.
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
  `RH_PONS_RPC_MIN_CALLS_PER_SEC`, default 1) and holds calls for a
  Retry-After (delta-seconds or HTTP-date) capped at
  `RH_PONS_FAILURE_BACKOFF_MAX_SEC`; each completed pass
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
python scripts/dex_discovery_watchdog.py --source rh_pons --db scout.db --enabled true --discovery-enabled true --staleness-minutes 10 --max-consecutive-failed-passes 10 --dry-run
```

Omitting `--max-consecutive-failed-passes` disables the failure-streak check,
so previews should pass the same limits as the wrapper.

For Linux deployment, install `scripts/rh-pons-watchdog.sh` alongside the
collector and invoke every five minutes through the existing scheduler.
Set `RH_PONS_WATCHDOG_ENABLED=true` in the scheduler environment and
`RH_PONS_COLLECTOR_ENABLED=true` in `.env` only after activation review. The
flag must be in `.env`, which is where the wrapper reads the lane gate; a
value set only in a systemd `Environment=` line reads as false there. If the
gate reads false while the DB shows a heartbeat or attempt within the SLO, the
watchdog breaches with `enabled_gate_mismatch` instead of silently disarming.
The wrapper captures the watchdog gate before loading `.env`, so a stray
`.env` line cannot arm it. Default watchdog gate is off.

To disable the lane deliberately, disarm the watchdog first
(`RH_PONS_WATCHDOG_ENABLED=false` in the scheduler environment), then set
`RH_PONS_COLLECTOR_ENABLED=false` in `.env` and restart the pipeline. If the
lane is disabled first, the last heartbeat or attempt stays recent for up to
one SLO window and the next watchdog run correctly pages
`enabled_gate_mismatch`. Re-enable in the reverse order: lane first, then the
watchdog once a completed scan exists.

RH breach alerts use the existing send cooldown
(`RH_PONS_WATCHDOG_COOLDOWN_HOURS`), keyed by breach reason: the same reason
is suppressed inside the window, but a different reason (for example
`head_lag_exceeded` followed by `stale`) pages immediately. State written by
older versions has no reason and pages once. DEX discovery keeps a single
cooldown for all reasons.

Watchdog knobs are Settings fields, so `.env` lines are valid:
`RH_PONS_POLL_STALENESS_ALERT_MINUTES` (default 10; the collector passes
every few seconds, so the SLO is in minutes),
`RH_PONS_MAX_CONSECUTIVE_FAILED_PASSES` (default 10),
`RH_PONS_MAX_HEAD_LAG_BLOCKS`, `RH_PONS_WATCHDOG_CLOCK_SKEW_SECONDS`,
`RH_PONS_WATCHDOG_COOLDOWN_HOURS` and `RH_PONS_WATCHDOG_STATE_DIR`. The
earlier `RH_PONS_POLL_STALENESS_ALERT_HOURS` knob is replaced by minutes.

Per-attempt health is separate from the success heartbeat:
`ingest_watchdog_state.source='rh_pons_attempt'` holds the consecutive
unsuccessful-pass streak and the last attempt time. The watchdog breaches with
`attempt_failures_exceeded` once the streak reaches the limit, even while the
success heartbeat is fresh. Unsuccessful passes also raise the checkpoint's
`head_block` to the newest head observed, without touching `next_block` or
`updated_at`, so a collector that keeps failing shows growing head lag instead
of a frozen head. The generic ingestion watchdog never hydrates or persists
either row. Enabling the collector without `RH_PONS_RPC_URL` logs
`rh_pons_enabled_without_rpc_url` once at startup; every pass then records a
failed attempt, so the streak check pages.
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

### Authenticated provider endpoints

Never put a keyed URL on the command line (`--rpc-url`): arguments show up in
`ps`, `/proc/<pid>/cmdline`, shell history and ssh logs. Pass the variable
NAME with `--rpc-url-env`; the probe reads only that variable and keeps every
other setting in code. A missing, empty or non-http(s) value is refused
before any traffic, and the message names the variable without its value.
The report shows only `scheme://host` plus the variable name. Redaction keeps
the full hostname, so a provider that puts its key in a subdomain still
leaks it. Only use providers that put the key in the path, query or userinfo.

Do not add a new key such as `ALCHEMY_API_KEY` or `RH_PROBE_RPC_URL` to the
project `.env`. Settings uses `extra="forbid"`, so an unknown line stops the
pipeline from starting. Use the existing `RH_PONS_RPC_URL` name: in `.env`
only at collector activation, and for the probe entered for one shell.
Run the three steps separately. Never paste them as one block: `read` would
consume the next pasted line, and the key pasted afterwards would run as a
command and land in shell history.

1. Run this line alone, then paste the URL at the prompt (not echoed, not
   saved to history):

   ```bash
   IFS= read -rsp 'RH RPC URL: ' RH_PONS_RPC_URL; echo; export RH_PONS_RPC_URL
   ```

2. Run the probe:

   ```bash
   python investigation/rh_pons_sustained_capacity_probe_20260913.py \
       --rpc-url-env RH_PONS_RPC_URL \
       --provider-log-range-cap <provider max blocks> --provider-batch-cap <provider max batch> \
       --stop-after-failed-passes 5 --max-rpc-calls 2000 --max-seconds 900 \
       --output rh_capacity.json
   ```

3. Always clear the variable afterwards, even if the probe failed or was
   interrupted:

   ```bash
   unset RH_PONS_RPC_URL
   ```

If the URL must live in a file, single-quote it (`?` and `&` break
`source`, which `scripts/rh-pons-watchdog.sh` uses on `.env`), `chmod 600` it
and keep it out of the repo. Do not put it in a systemd `Environment=` line,
which `systemctl show` exposes:

```bash
RH_PONS_RPC_URL='https://provider.example/v2/KEY?opt=1&x=2'
```

Provider limits checked before traffic:

- `--provider-log-range-cap N`: a checkpointed pass queries eth_getLogs
  over `--max-span` plus `RH_PONS_REORG_OVERLAP_BLOCKS` (12) blocks
  inclusive. The probe refuses a span where `max_span + 12 > N`, so set
  `--max-span` to at most `N - 12`.
- `--provider-batch-cap N`: refuses `--header-batch-size` above N. An
  oversized batch that the provider rejects with HTTP 400 or -32600 would
  otherwise silently switch the collector to single header requests.
- `--stop-after-failed-passes N`: stops after N consecutive
  failed/timeout/error/refused passes with `stop_reason=failed_passes` and
  verdict fail. Without it, a provider that rejects every range keeps using
  budget until `--max-rpc-calls` or `--max-seconds`. The rate-limit stop
  still takes priority.

All flags need positive integers. The probe session ignores proxy environment
variables (`trust_env=False`), like the pipeline.

Alchemy's free tier allows 10 blocks per eth_getLogs query (see
`tasks/findings_rh_provider_readiness_20260914.md`). With the current reorg
overlap of 12 (fixed in the probe, and the collector default), the smallest
checkpointed query is 1 + 12 = 13 blocks. So the free tier cannot run this
probe or the collector at its current configuration.
`--provider-log-range-cap 10` refuses it before traffic. The
pay-as-you-go tier documents an unlimited Robinhood mainnet range with a
150 MB response cap. Confirm your own account's range, batch and throughput
limits first. The call budget counts calls, not compute units: header reads
and log reads are billed differently, and batches are billed per method.

Run `scripts/compare_discovery_latency.py --db <captured-db>` over observations
captured by the running lanes. Use the paired sample count and RH advantage on
the same chain-qualified tokens. Keep unobserved, invalid-clock and unsupported
identity cases in the denominator. Fixture and historical backfill samples
cannot establish real-time detection advantage. Pre-register a required sample
count based on the decision being tested and observed arrival rate; do not
substitute a calendar soak or a claim based on synthetic tests.
