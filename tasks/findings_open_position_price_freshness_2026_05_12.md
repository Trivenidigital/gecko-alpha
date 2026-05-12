**New primitives introduced:** NONE — finding doc only. Triage script + memory entries surface the gap; the architectural fix is design-pass work, deferred.

# Finding: Open-position price_cache staleness — silent trailing-stop blindness on 33% of held positions

**Date:** 2026-05-12
**Severity:** HIGH (silent functional bug, not just display)
**Status:** filed, one-off triage executed, architectural fix deferred to next-session design pass
**Classification:** §12c-narrow second instance (heartbeat-says-healthy / specific-output-subset-is-dead)

---

## TL;DR

The pipeline's price_cache writer updates rows only for tokens currently in the active ingestion lanes (CoinGecko markets top-50 by 1h change, trending list, DexScreener boosts, GeckoTerminal pools). Once a held token **drops out of all ingestion lanes**, its `price_cache` row stops being refreshed. As of 2026-05-12 12:47Z:

- **49 of 150 open paper_trades (33%) have price_cache rows stale > 24h.**
- **65 of 150 (43%) stale > 1h.**
- 1 trade (AALIEN, tg_social) has no price_cache row at all — structurally different failure shape.
- Worst offender: trade 1569 (chain_completed, May 3 open) — 227h stale (9.5 days).

The dashboard PnL freeze that surfaced this finding is the cosmetic symptom. **The load-bearing impact is that `scout/trading/evaluator.py` silently can't fire TP / SL / trailing-stop exits on these positions** — the evaluator reads `price_cache.current_price` every cycle, but every cycle reads the same frozen value, so no price-based exit can ever trigger. Held positions that drop off ingestion lanes effectively become **max_duration-only exits**.

This is the second strong instance of §12c-narrow (the first being perp_anomalies empty-since-deploy from the 2026-05-11 silent-failure audit). §12c promotion to global CLAUDE.md is now evidence-ready; deferred to a dedicated session per anti-tail-end-promotion discipline.

---

## Worked example: PAYAI (trade 1745)

Cleanest diagnostic chain in the cohort:

| Field | Value |
|---|---|
| Trade id | 1745 |
| Token | `payai-network-2` (PAYAI), `losers_contrarian` |
| Status | `open` |
| Opened | 2026-05-08T08:38:11Z |
| Entry price | $0.00474 |
| Leg 1 filled | 2026-05-08T08:39:51Z @ $0.00540 (≈ +14%) |
| Leg 2 filled | 2026-05-09T02:00:57Z (no exit_price recorded) |
| Moonshot armed | 2026-05-08T15:45:45Z |
| Peak price recorded | $0.01170 (peak_pct +146.77%) |
| `price_cache.current_price` | $0.01170 — exactly equal to peak_price |
| `price_cache.updated_at` | 2026-05-11T09:00:00Z (27.8h before this query) |
| `checkpoint_24h_pct` | +137.93% |
| `checkpoint_48h_pct` | +137.93% — identical |

The peak-equals-frozen-cache-price chain is the diagnostic: the cache was last refreshed at moonshot-arm-time, then PAYAI dropped out of the ingestion lanes (likely because it dropped off the top-50 1h-change list), and the price has not been refreshed since. The evaluator has been "checking" PAYAI's trailing-stop every cycle for 27.8h against the same $0.01170 value. If PAYAI's actual market price dropped 30% below peak in that window, the moonshot-trail-drawdown exit would have fired — but the system can't know.

The identical `checkpoint_24h_pct == checkpoint_48h_pct` is the visible signature of the same gap: checkpoint snapshots are taken at specified ages (24h and 48h after open) reading from `price_cache.current_price` — both snapshots were taken from a cache that had already frozen at the same value.

---

## Cohort data

Distribution of price_cache age across the 150 open positions:

| Bucket | Count | % of open positions |
|---|---|---|
| Fresh (< 1h) | 84 | 56% |
| Stale 1-24h | 16 | 11% |
| Stale 24-72h | 25 | 17% |
| Stale 72h-7d | 18 | 12% |
| Stale > 7d | 6 | 4% |
| No price_cache row | 1 (AALIEN) | <1% |

Stratified by signal_type, the failure rate skews toward longer-held positions:
- `chain_completed` opens from May 3-7: most have multi-day-stale cache (these are by design long-hold positions; max_duration 720h)
- `narrative_prediction` opens from May 5-6: many in 60-160h stale range (these signals fire on category-level prediction; tokens often drop off market trending fast)
- `losers_contrarian` opens: mixed; some fresh (recent rebound = still in trending), some stale
- `gainers_early`: skews fresher because tokens fired here are still on the 1h-change list at open time — but stale ones exist (the 1h-change rank decayed)

---

## Why pipeline-service-is-healthy is a misleading signal

`gecko-pipeline.service` is `active` since 2026-05-12T01:04:34Z (uninterrupted). `price_cache` overall has 7951 rows, newest written 2026-05-12T12:47:44Z (a moment before this query). General cache health is fine.

The §12c-narrow failure shape: a watchdog reading "is the pipeline running? is the cache being updated?" answers YES to both. A watchdog reading the specific output that matters — "is `price_cache.updated_at` fresh for every token currently held in an open paper trade?" — would answer NO for 65 of 150 positions.

The heartbeat is accurate. The relevant subset is dead. **Watching the wrong granularity of liveness is structurally indistinguishable from no watchdog at all for the failure mode that actually occurs.**

---

## Failure-shape distinction: two real shapes in scope

A fix that handles only one of these shapes is incomplete:

1. **Cached-then-frozen** (49 positions stale > 24h): row exists in `price_cache`; was being updated when the trade opened; stopped getting updates when token dropped out of ingestion lanes. Fix shape: refresh existing rows.

2. **Never-cached** (1 position, AALIEN trade 1750): no `price_cache` row at all. Token was discovered via tg_social path; the social dispatcher opens a trade without writing to price_cache (signal_data carries the price). The evaluator's first cycle then can't find a price_cache row for AALIEN and silently no-ops on it. Fix shape: ensure all open-trade token_ids have at least one price_cache row, even if they were never in an ingestion lane.

A "refresh stale rows" fix misses AALIEN. A "create rows for held tokens" fix duplicates work for the cached-then-frozen case. The correct fix shape handles both: **ensure every token currently held in an open paper trade has a price_cache row that is no more than N minutes old, regardless of whether the token is in any ingestion lane.**

---

## Architectural fix alternatives — enumerated, not prescribed

The design pass should evaluate at least these alternatives. Each has different tradeoffs in CoinGecko request budget, evaluator latency, cache memory, and code surface area.

### Alternative A: held-position price-refresh lane

Add a new ingestion lane to the pipeline that, every cycle (or every N cycles), queries `paper_trades WHERE status='open'`, extracts unique token_ids, and forces a price-cache refresh for those tokens regardless of whether they appear in any other ingestion lane.

**Tradeoffs:**
- Pro: bounded request budget (one batched `/simple/price` call per cycle for up to 250 ids; 150 held tokens fits)
- Pro: clear separation of concerns (held-position freshness lives in its own lane)
- Con: new lane to maintain; doubles cache writes during the overlap window between ingestion-lane and held-lane
- Con: doesn't solve "what if held-position lane itself fails silently?" — needs its own §12c watchdog (paired with the lane at ship time per CLAUDE.md §12a)

### Alternative B: ingestion-lane augmentation

Augment the existing CoinGecko markets ingestion lane: instead of just top-50-by-1h-change, also fetch any token_id currently in open paper_trades. Single API call covers both.

**Tradeoffs:**
- Pro: no new lane; reuses existing path
- Pro: simplest blast radius
- Con: conflates "current market activity" ingestion with "held-position monitoring" ingestion; the lanes have different cadence requirements
- Con: the ingestion lane's filtering (e.g., scorer corpus $10K-$500K mcap) would need to bypass for held positions

### Alternative C: evaluator-side refresh

Move price refresh out of the pipeline cache and into the evaluator: every evaluator cycle, the evaluator queries CoinGecko for held tokens directly, reads its own ephemeral cache, makes exit decisions.

**Tradeoffs:**
- Pro: clearest data-path correctness — the consumer of the price IS the refresher of the price
- Con: increases evaluator latency from ~1ms (DB read) to ~500-2000ms (network call)
- Con: changes the evaluator's failure surface (now depends on external API)
- Con: duplicates cache writes if pipeline ingestion also covers the token

### Alternative D: cache-tier policy

Don't change the writer; instead add a "held-position" tier to `price_cache` with a separate TTL policy. Held tokens get refreshed on a longer cadence regardless of ingestion-lane membership. Implementation lives in `scout/db.py` as a per-row attribute.

**Tradeoffs:**
- Pro: smallest code surface
- Con: pushes the refresh logic into the DB layer where it doesn't naturally belong
- Con: still requires SOMETHING to actually fetch the prices — this is more of an indexing helper than a fix

### What I'd recommend the design pass anchor on

Alternative A appears cleanest on the structural-correctness axis (separation of concerns; explicit visibility surface). Alternatives B and C are pragmatic but conflate concerns. Alternative D doesn't actually solve the problem on its own.

But: anchor lightly. The design pass should run a §11 existing-data battery before committing to A — specifically, does the held-cohort's price-drift distribution (from the triage log) support the urgency framing or weaken it? If material-drift cases are rare, Alternative A's cost might exceed its benefit and a simpler "alert when held-position price is stale > N hours" watchdog could be enough.

---

## Triage record (one-off, NOT the fix)

A one-off triage script (`scripts/triage_refresh_held_token_prices_20260512.py`) was executed at TIME_TBD to force-refresh `price_cache` rows for all currently-held tokens. See script docstring and log file for full record.

**Triage summary (filled post-run):**

- Started at: 2026-05-12T12:58:52Z
- Finished at: 2026-05-12T12:58:52Z (wall-clock 0.2 sec)
- Total held tokens: 150
- Skipped — already cache-fresh < 1h: 84
- Skipped — non-CG-format token_ids: 0 (this cohort had no contract-addr-shaped held tokens; all held tokens are CG coin_ids)
- Eligible for refresh: 66
- Refreshed successfully: 66 (100% CoinGecko coverage)
- Not found in CoinGecko: 0
- **Material-drift count (|delta_pct| > 10%): 17 of 66 = 25.8% of refreshed cohort**
- Log file: `/root/gecko-alpha/triage_price_refresh_20260512T125852Z.json`

**Largest drift cases (selected from top-20 by |delta_pct|):**

| Symbol | token_id | Stale (h) | Old price | New price | Δ% |
|---|---|---:|---:|---:|---:|
| RIV | riv-coin | 66.7 | $0.005637 | $0.008591 | **+52.4%** |
| (no sym) | goblin-trump | 227.4 | $5.01e-6 | $2.94e-6 | **−41.3%** |
| TRUTH | swarm-network | 34.2 | $0.01315 | $0.01589 | +20.8% |
| tibbir | ribbita-by-virtuals | 163.5 | $0.16122 | $0.12987 | −19.4% |
| BDAG | blockdag | 28.8 | $1.25e-4 | $1.03e-4 | −17.6% |
| EVAA | evaa-protocol | 85.2 | $0.7309 | $0.6131 | −16.1% |
| anon | heyanon | 143.1 | $0.8772 | $0.7368 | −16.0% |
| AIOT | okzoo | 144.4 | $0.0906 | $0.1048 | +15.7% |
| SWARMS | swarms | 45.7 | $0.02259 | $0.01920 | −15.0% |
| LMTS | limitless-3 | 42.8 | $0.1493 | $0.1282 | −14.1% |
| PAYAI | payai-network-2 | 28.0 | $0.01170 | $0.01027 | −12.2% |

**Urgency calibration update:**

25.8% material-drift rate is between the MEDIUM (<10%) and HIGH (>30%) bands
the finding pre-registered. Net read: severity remains **HIGH** but not
maximally urgent — the trailing-stop blindness has been materially affecting
exit decisions on roughly a quarter of the stale-cache cohort, but the
distribution of moves includes substantial cases in both directions (RIV +52%
would have been favorable; goblin-trump −41% would have been the more
worrying case where SL would have triggered if visible). The architectural
fix should be the immediate next priority after this session, but does not
warrant emergency same-day patching.

**Specific check on PAYAI** (the seed case): −12.2% move in 28h. Peak was
$0.01170 (moonshot armed), current $0.01027, so 12.2% below peak. Moonshot
trail-drawdown threshold is 30% per `PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT`;
not yet at the trigger but headed there. With the cache refreshed, the
evaluator's next cycle will see the actual price and resume making
trail-drawdown decisions normally.

**Observed delisted/renamed:** none. CoinGecko had all 66 held tokens. This
is a useful negative finding — the staleness was purely due to ingestion-
lane drop-off, not because tokens disappeared from CoinGecko's universe.

**Critical:** this is a one-off. Running this script twice does not constitute a fix. The architectural gap remains until the design pass ships. **Do not schedule this script as a recurring job** — that would entrench the symptom-level patch and reduce urgency to fix the underlying gap.

The triage script:
1. Reads currently-held token_ids from `paper_trades WHERE status='open'`
2. Filters to tokens that (a) look like CoinGecko coin_ids and (b) have stale (> 1h) or missing `price_cache` rows
3. Issues a single batched `/simple/price` request to CoinGecko (free tier; one call covers up to 250 ids)
4. Upserts `price_cache` with new prices via `ON CONFLICT DO UPDATE`, preserving `price_change_7d` on existing rows
5. Writes a JSON log file with per-token old_price / new_price / delta_pct
6. **Does NOT touch evaluator state, does NOT trigger exits, does NOT change any other code path**

The evaluator's next cycle will see fresh prices and decide whatever it decides. The script's blast radius is bounded to `price_cache` writes + one CoinGecko API call.

---

## Empirical signal for urgency calibration

The material-drift count from the triage log is the load-bearing number for whether this is urgent or theoretical:

- If material drift (>10%) on > 30% of positions → trailing-stop blindness has likely already cost real paper-trade decisions. Severity remains HIGH; design pass is the immediate next priority.
- If material drift on < 10% of positions → impact has been mostly theoretical; severity calibrates to MEDIUM and the design pass can be scheduled alongside other roadmap work rather than ahead of it.

Filled in the triage-summary section above once the script runs.

---

## §12c-narrow promotion-evidence note

This finding plus the perp_anomalies finding (silent-failure audit §2.6, `project_session_2026_04_20_perp_enablement.md`) gives §12c-narrow two strong, structurally-equivalent instances:

| Instance | Heartbeat says | Specific output is | Watchdog gap |
|---|---|---|---|
| perp_anomalies (2026-04-20→2026-05-11) | perp_watcher service active | `perp_anomalies` table empty since deploy | no watchdog read the table-row-count |
| open-position-price-freshness (2026-05-12) | `gecko-pipeline.service` active, `price_cache` being updated | held-position subset of `price_cache` stale 24h+ | no watchdog read freshness PER HELD TOKEN |

Both share the structural shape: a health signal is correctly reporting the existence and uptime of a process, but the relevant *output subset* of that process is silently broken. A health-claim-vs-output-truth watchdog would have caught either; both were caught by the operator observing downstream symptoms (no perp alerts ever; PnL frozen on dashboard).

**This is the narrow §12c shape, not the broad §12c shape.** Broad §12c (inference-surface-vs-ground-truth, where the surface displays accurate data but operators infer the wrong thing) is the witness-vs-dispatch case from 2026-05-12 morning. Different parent rule, kept separate per the bifurcated-verification resolution.

§12c-narrow is now ready for promotion to global CLAUDE.md. Promotion deferred to a dedicated session per anti-tail-end-promotion discipline — capturing the readiness as memory only.

---

## What is NOT in this finding

- The actual fix. Design pass is next-session, after the triage log informs urgency calibration.
- A change to the evaluator. The evaluator's behavior is correct; it's the data it reads that's stale.
- A change to the pipeline's existing ingestion lanes. Those work as designed for their intended purpose (discovering and scoring new tokens); the gap is that nothing covers the *held-position monitoring* requirement.
- A change to the dashboard PnL display. The display correctly reflects the cache; fixing the cache fixes the display.
- §12c promotion to global CLAUDE.md. Captured as memory; defer the global edit.

---

## Sequence taken in this session

1. Operator observed PAYAI showing 24h_pct == 48h_pct on dashboard
2. Investigated price_cache → discovered PAYAI cache 27.8h stale
3. Audited all open positions → 65/150 stale > 1h, 49/150 stale > 24h
4. Cross-checked pipeline service → confirmed running and writing cache for other tokens
5. Confirmed §12c-narrow shape (heartbeat green / specific output dead)
6. Filed this finding (load-bearing artifact, the triage log alone is too easily lost)
7. Ran scoped triage script → log written, drift distribution captured
8. Updated this finding with triage-summary numbers
9. Updated memory: §12c-narrow promotion-evidence shift; witness-vs-dispatch bifurcation resolved (separate §12d candidate)
10. Stopped. Design pass is next-session.
