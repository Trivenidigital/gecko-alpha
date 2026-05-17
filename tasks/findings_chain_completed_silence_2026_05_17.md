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

### Mechanism (per CLAUDE.md §9c — visible lever ≠ controlling lever)

The visible levers (signal_params enabled=1, code path unchanged) suggest "nothing should be broken." But the actual data path:

1. Scorer emits `candidate_scored` events → still firing (confirmed via journalctl)
2. `chains.tracker._check_active_chains` reads events to match against patterns → **not advancing**
3. `active_chains` table has rows but `MAX(anchor_time) = 2026-05-11T16:42Z` → no new anchors
4. Without new anchors, no chain can complete → no `chain_matches` row written
5. Without `chain_matches`, no `chain_completed` paper trade fires

Step (3) is the controlling lever. Something stopped creating new `active_chains` rows on 2026-05-11T16:42Z for the narrative pipeline (and on 2026-05-04T00:51Z for the memecoin pipeline). Code is unchanged, so the change must be in data shape (event payload schema), data rate (event types stopped firing for the patterns the anchor matcher looks for), or runtime config (`.env` flag).

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
SELECT pipeline, COUNT(*), MIN(completed_at), MAX(completed_at)
FROM chain_matches GROUP BY pipeline;

SELECT COUNT(*), MIN(anchor_time), MAX(anchor_time) FROM active_chains;

SELECT signal_type, enabled FROM signal_params WHERE signal_type='chain_completed';
```

## Decision

**Surface as HIGH-priority follow-up.** This audit's role per the original backlog item was to **confirm regression vs quiet window**. Confirmed: regression. The fix item (`BL-NEW-CHAIN-ANCHOR-PIPELINE-FIX`) inherits the operator urgency.

## Hermes-first verdict

No Hermes skill covers chain-detection regression diagnostics. Project-internal investigation.

## Drift verdict

NET-NEW. No prior chain-silence audit exists. The earlier `project_chain_revival_2026_05_03.md` documented the April incident's CLOSURE but didn't write an audit doc.
