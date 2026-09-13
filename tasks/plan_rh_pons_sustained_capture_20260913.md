**New primitives introduced:** `run_rh_pons_loop` (dedicated enabled-only asyncio worker for the RH/Pons collector); private `_ScanState` / `_PassResult` (in-memory adaptive window, pinned cold-start coverage, per-pass metrics); strict JSON-RPC batch header transport; topic-only trade-log query with indexed emitter membership (`Database.curve_launch_members`); identity-scoped projection repair (`reconcile_curve_launch_projection(..., identities=...)`); index `idx_curve_launch_ev_block` (no new table, no schema_version row); settings `RH_PONS_IDLE_SLEEP_SEC`, `RH_PONS_MIN_SCAN_SPAN_BLOCKS`, `RH_PONS_FAILURE_BACKOFF_MAX_SEC`, `RH_PONS_HEADER_BATCH_SIZE`, `RH_PONS_TOPIC_ONLY_TRADE_QUERY`; isolated capacity runner `investigation/rh_pons_sustained_capacity_probe_20260913.py`.

# Plan — RH/Pons sustained early capture (2026-09-13)

Branch `feat/rh-sustained-capture`, base `dcb5dd26`. Approved design: the
design turn in this session plus Codex corrections 1–6. Scope is capture only.
No production activation, config, DB, service, alert or trade change.

## Hermes-first analysis

| Need | Hermes / ecosystem candidate | Checked | Verdict |
|---|---|---|---|
| EVM log collector with checkpoint + reorg evidence | Hermes skills docs (hermes-agent.nousresearch.com/docs/skills), awesome-hermes-agent | Codex, 2026-09-13; the client-side skill catalog did not render, so the catalog itself could not be enumerated | No verified drop-in chain collector found. This is a limit of what could be inspected, not a claim that the catalog has zero skills |
| Long-running worker lifecycle | Existing `scout/main.py` task list + FIRST_COMPLETED shutdown drain | In repo | Reuse |
| Durable evidence, checkpoints, projection | Existing `curve_launch_*` tables, `curve_scan_checkpoints`, `ingest_watchdog_state` | In repo | Reuse; add one index only |
| Dedicated RH loop already present | Drift check of `scout/ingestion/*`, `secondwave_loop`, `run_chain_tracker`, `tg_shadow_loop` | Design turn | None; pattern follows `tg_shadow_loop` (flag-gated spawn, never exits while enabled) |

No new dependencies.

## Runtime assumptions and probe evidence (recorded before code)

- Chain produces roughly 10 blocks/s (earlier probes: head advanced 230 blocks in 23.2 s).
- Earlier in-cycle sample: 189 new blocks in 23.2 s, about 8.2 blocks/s scanned, dominated by
  one header request per distinct event block (8 concurrent). A 2000-block cold scan timed
  out at 30 s in header reads. About 0.9 events/block in that sample.
- `investigation/rh_rpc_capacity_probe_20260913.json` (read-only, public RPC):
  - batch of 50 headers valid in 1.213 s; batch of 100 valid in 1.059 s; 8 parallel single calls 1.297 s.
  - topic-only `[[CurveBuy, CurveSell]]` getLogs accepted: 200/1000/2000 blocks →
    145/1047/1949 logs over 36/98/161 emitters in 0.156/1.49/1.907 s.
  - logs carry `blockTimestamp`; independent canonical header verification is still kept.
- This establishes capability only. Provider quota and sustained capacity are unknown.
  No load-to-429 probing. Start conservative (header batch 50, at most 2 batch POSTs in flight),
  back off on 429, report quota as unknown.

## Invariants kept

- Checkpoint written only after evidence + scoped projection succeed; never regresses on a stale provider head.
- Reorg anchor, per-log header hash, final to-block hash checks unchanged (hashes compared lowercase).
- Foreign/mimic emitters are excluded before decoding and never recorded as Pons evidence.
  Member emitters that fail decoding still fail the pass.
- Every discovery stays execution-ineligible; no CandidateToken; default-off.

## Checklist

- [ ] Record test-request file for Codex (Git Bash cannot import aiohttp here)
- [ ] DB: scoped projection repair by touched log identities (old + new tokens, NULL-token markers, append order, one atomic publish); full rebuild API kept for diagnostics
- [ ] DB: `curve_launch_members` indexed emitter membership; `idx_curve_launch_ev_block` for range evidence
- [ ] DB tests: scoped == full on A-B-A-B forks and NULL-token markers; token change across replacement; evidence scope insensitive to 5000 unrelated rows; query plan uses indexes
- [ ] Config: new bounded settings + min ≤ max validator
- [ ] Collector: strict batch headers (ids, duplicates, bools, errors, heights, hash/timestamp) with unsupported→fallback and malformed→fail
- [ ] Collector: topic-only trade query + membership; address-batched mode retained (unbounded growth documented)
- [ ] Collector: `_scan_pass` + `_ScanState`/`_PassResult`; pinned cold start; stale-head no-regression; `poll_once` compatible (legacy transports)
- [ ] Collector: `run_rh_pons_loop` adaptive span, bounded backoff, 429 handling, idle sleep only when caught up, never exits while enabled
- [ ] Main: remove in-cycle polling; spawn loop behind flag; real `main()` wiring + shutdown-cancel test
- [ ] Capacity runner (temporary DB, public RPC, bounded, drain-then-steady metrics)
- [ ] Runbook + report update; commit owned paths only
