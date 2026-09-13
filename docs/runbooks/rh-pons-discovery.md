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

Capacity limits still matter: active ungraduated curves accumulate, and
projection reconciliation reads retained evidence. Measure sustained scans
and latency as the dataset grows before enabling a production observation
lane. A tiny successful sample proves transport and decoding, not capacity
or signal quality.

Run `scripts/compare_discovery_latency.py --db <captured-db>` over observations
captured by the running lanes. Use the paired sample count and RH advantage on
the same chain-qualified tokens. Keep unobserved, invalid-clock and unsupported
identity cases in the denominator. Fixture and historical backfill samples
cannot establish real-time detection advantage. Pre-register a required sample
count based on the decision being tested and observed arrival rate; do not
substitute a calendar soak or a claim based on synthetic tests.
