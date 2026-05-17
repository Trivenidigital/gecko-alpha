# BL-NEW-CHAIN-COMPLETED-SILENCE-AUDIT — Findings 2026-05-17

**Filed:** 2026-05-17 (cycle 8 of autonomous backlog knockdown)
**Source:** srilu-vps `prod.db` (`/root/gecko-alpha/scout.db`) + journalctl
**Triggered by:** Finding 1 of `findings_live_evaluable_signal_audit_2026_05_17.md` (cycle 7)

## Confirmed: chain_completed is a real production outage

`chain_matches` table is **strongly silent** for 5.5 days (`narrative` pipeline) and **13+ days** (`memecoin` pipeline).

### Evidence

| metric | value |
|---|---|
| chain_matches total | 603 rows |
| chain_matches all-pipeline date range | 2026-05-01T15:22Z → 2026-05-11T16:43Z |
| chain_matches memecoin pipeline | 216 rows, **last 2026-05-04T00:51Z** (13+ days dead) |
| chain_matches narrative pipeline | 387 rows, **last 2026-05-11T16:43Z** (5.5 days dead) |
| active_chains total | 95 rows |
| active_chains anchor_time range | 2026-05-05T03:34Z → **2026-05-11T16:42Z** (no new anchors in 5.5 days) |
| signal_params(chain_completed) | enabled=**1** (NOT auto-suspended) |
| Recent `chain_event_emitted` events | YES — last 2026-05-17T01:21:55Z (6h ago at audit time); event emitter functioning |
| Code changes in scout/chains/ during May | **NONE** — no commits modify the chain code path |

### Mechanism (per CLAUDE.md §9c — visible lever ≠ controlling lever) — V37 fold

**V37 MUST-FIX correction (2026-05-17 review of this audit):** the earlier draft said "candidate_scored events still firing — event emitter works; anchor-creation doesn't." That framing conflated event-emission with anchor-eligibility. The narrative `full_conviction` pattern (per `scout/chains/patterns.py:64-86`) anchors at step 1 on `event_type="category_heating"`, NOT on `candidate_scored` — `candidate_scored` is step 4.

Re-running the diagnostic correctly:

**signal_events table — last-firing per (event_type, pipeline):**

| pipeline | event_type | n | last_fire |
|---|---|---:|---|
| memecoin | conviction_gated | 138,083 | 2026-05-17T07:23Z (LIVE) |
| memecoin | candidate_scored | 7,898,791 | 2026-05-17T07:23Z (LIVE) |
| memecoin | chain_complete | 146 | **2026-05-04T00:26Z (13d dead)** |
| memecoin | counter_scored | 2 | 2026-05-02 (effectively never used) |
| narrative | category_heating | 1,805 | 2026-05-17T07:04Z (LIVE — anchor IS firing) |
| narrative | laggard_picked | 434 | 2026-05-17T07:04Z (LIVE) |
| narrative | narrative_scored | 431 | 2026-05-17T07:04Z (LIVE) |
| narrative | counter_scored | 435 | 2026-05-17T07:04Z (LIVE) |
| narrative | chain_complete | 109 | **2026-05-11T16:43Z (5.5d dead)** |

**This is the revised attribution:** the narrative pipeline's anchor event (`category_heating`) is firing 7 minutes ago at audit time. ALL upstream step events (laggard_picked / narrative_scored / counter_scored) are firing today. The data path INTO the anchor matcher is intact.

The break is INSIDE the chain-step-matching layer or the `active_chains` writer. Possible mechanisms:

(a) `chains.tracker._check_active_chains` matches the anchor event but `INSERT INTO active_chains` silently fails or short-circuits (cooldown dedup, conviction_boost gate, etc.)
(b) Step-1 pattern match logic recently rejects what used to match (regex/payload schema drift)
(c) Anchors ARE created but immediately marked complete with 0 steps and pruned before counted

**active_chains rowcount per day (last 14d):**

| date | anchors_created |
|---|---:|
| 2026-05-11 | 4 |
| 2026-05-07 | 12 |
| 2026-05-06 | 63 |
| 2026-05-05 | 16 |
| (no rows post-2026-05-11) | 0 |

Rules out the truncate/prune hypothesis (V37 SHOULD-FIX): pruning `_prune_stale` only deletes `is_complete=1 AND completed_at<cutoff` OR stale incomplete past `CHAIN_ACTIVE_RETENTION_DAYS` — not enough to explain the cliff at 2026-05-11. Falsified.

Memecoin chain_complete died 2026-05-04 (13d ago) — earlier than narrative's 2026-05-11 (5.5d). **V37 SHOULD-FIX:** memecoin and narrative pipelines use disjoint event sets at the anchor; two different last-fire dates from two different upstream emitter sets are MORE LIKELY two separate failures with different proximate causes. The fix item should split diagnostics.

### Prior art (memory `project_chain_revival_2026_05_03.md`)

"Chain dispatch can auto-retire silently on broken outcome telemetry (was 17d dead Apr 14–May 1, fixed PR #60/#61)." This is the **second** chain-pipeline silence in ~6 weeks. The same substrate class — "chain detection silently dies, signal_params still shows enabled=1" — has recurred.

The April 14–May 1 incident was 17 days of silence; this one is 5.5 days (narrative) + 13 days (memecoin) and counting. **The §12c-narrow promotion-evidence trio (perp_anomalies + open-position price-freshness + this) now has a third instance** — `cohort_digest_state` writers monitored at table-write-rate but `active_chains` writer rate has zero monitoring.

## What's NOT in scope for this audit

- The fix itself — this audit confirms the regression, files a fix-action backlog item, ships
- Memecoin-pipeline-specific diagnostic — the 13-day gap predates and outranks the cohort the operator just deployed cycle 7's audit against
- Watchdog for `active_chains` write-rate — `BL-NEW-CHAIN-ACTIVE-WATCHDOG-SLO` should be a §12a daemon entry (gated on the daemon itself); skip for now

## Recommended follow-up

| ID | Trigger | Cost | Priority |
|---|---|---|---|
| `BL-NEW-CHAIN-ANCHOR-PIPELINE-FIX` | This audit confirms silence | ~3-5h diagnostic + fix | **HIGH** — Tier 1a's strongest signal has been STRUCTURALLY DEAD for 5.5+ days |
| `BL-NEW-CHAIN-ACTIVE-WATCHDOG-SLO` | §12c-narrow + recurrence | ~1h after §12a daemon | MEDIUM — gated on §12a daemon (unbuilt) |
| (§9c promotion update) | 5th attribution-discipline instance | ~30min doc | LOW — accumulate before promotion |

## Empirical reproducibility

```sql
-- (a) The headline result — chain_matches dead per pipeline
SELECT pipeline, COUNT(*), MIN(completed_at), MAX(completed_at)
FROM chain_matches GROUP BY pipeline;

-- (b) Confirm no new anchors
SELECT COUNT(*), MIN(anchor_time), MAX(anchor_time) FROM active_chains;

-- (c) Confirm not auto-suspended
SELECT signal_type, enabled FROM signal_params WHERE signal_type='chain_completed';

-- (d) V37 fold: localize the regression to inside the chain-matcher
-- (NOT upstream event emitters — those fire today)
SELECT event_type, pipeline, COUNT(*) AS n, MAX(created_at) AS last_fire
FROM signal_events GROUP BY event_type, pipeline ORDER BY pipeline, last_fire DESC;

-- (e) V37 fold: rule out truncate/prune via daily anchor count
SELECT substr(anchor_time,1,10) AS day, COUNT(*)
FROM active_chains GROUP BY day ORDER BY day DESC LIMIT 14;
```

**Operator cheap interim check (V37 SHOULD-FIX):** add the (d) query to morning checks — 5min, surfaces continued silence at signal-event granularity without waiting for the BL-NEW-CHAIN-ANCHOR-PIPELINE-FIX investigation.

## Decision

**Surface as HIGH-priority follow-up.** This audit's role per the original backlog item was to **confirm regression vs quiet window**. Confirmed: regression. The fix item (`BL-NEW-CHAIN-ANCHOR-PIPELINE-FIX`) inherits the operator urgency.

## Hermes-first verdict

No Hermes skill covers chain-detection regression diagnostics. Project-internal investigation.

## Drift verdict

NET-NEW. No prior chain-silence audit exists. The earlier `project_chain_revival_2026_05_03.md` documented the April incident's CLOSURE but didn't write an audit doc.
