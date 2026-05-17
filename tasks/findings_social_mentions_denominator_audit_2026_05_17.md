**New primitives introduced:** NONE.

# BL-NEW-SOCIAL-MENTIONS-DENOMINATOR-AUDIT — Empirical Findings 2026-05-17

**Data freshness:** Computed against srilu prod scout.db 2026-05-17. **CITATIONS BEYOND 2026-08-17 REQUIRE RE-VERIFICATION** via `tasks/audit_v2_queries.sql` (90d backstop per design-review fold). To re-run: `ssh srilu-vps 'sqlite3 /root/gecko-alpha/scout.db' < tasks/audit_v2_queries.sql`.

**Source:** srilu `/root/gecko-alpha/scout.db`, HEAD `a20891f` (= origin/master, includes merged PR #150).
**Companion:** `tasks/plan_social_mentions_denominator_audit.md` (v2 — post-2-reviewer-fold), `tasks/design_social_mentions_denominator_audit.md`.

## TL;DR

`social_mentions_24h` is **structurally dead**: 0 of 1,672 candidates have nonzero values, 0 of 6,098,976 scoring rows over the 15d retention window would trigger Signal 5. The 15-point contribution to `SCORER_MAX_RAW=208` is a phantom. **Closed-form approximation from stored final scores** (caveats below) suggests removing it + recalibrating gates 60→65 / 70→75 produces **0 candidate flips across 6M+ rows under both truncation AND rounding** (Q3 + Q3b). Variant C would promote 35 score-instances (=4 distinct candidates) past MIN_SCORE; **0 of those 4 had paper_trade outcomes** (Q4b) — no missed profitable signals. Hermes X bridge not data-ready (0/131 resolved 7d); TG bridge not data-ready (6 distinct contracts/24h). awesome-hermes-agent `x-twitter-scraper` exists but does not cover per-token 24h aggregation (raw search/timeline/mentions API only).

**Closed-form caveat** (per PR-stage Reviewer 1 finding): `score_history` stores ONLY final post-multiplier score. The closed-form reverse `score * 208 / 193` is approximate — it cannot recover raw points OR multiplier eligibility (signals count). It IS mathematically sound under the conditions that (a) Signal 5 contributes 0 to everyone today (verified: `social_mentions_24h = 0` for all 1,672 candidates), so multiplier eligibility is unaffected by removal, AND (b) double-truncation interactions are accounted for via Q3b rounding sensitivity (which produced identical 0/0/0/0). **An exact re-score from candidate inputs is not feasible because raw points + signal list aren't stored in score_history.** Treat the 0-flip claim as a closed-form approximation, not a hard proof.

**Operator decision required by 2026-06-14:** confirm Option B (recommended) or override to Option C.

**Recommendation: Option B — remove + recalibrate gates.** Code change DEFERRED to explicit operator approval per scope constraint "no live config flips this PR." This PR ships findings + one-line `# DEAD SIGNAL` annotation + backlog flip + follow-ups + `tasks/audit_v2_queries.sql` (with SCHEMA/COUPLING blocks, sensitivity Q3b, paper-trade attribution Q4b).

## Operator acceptance criteria verification

| Criterion (from operator scope) | Met? | Evidence |
|---|---|---|
| Quantifies score/ranking impact of removing the dead 15-point feature | ✓ (closed-form approximation; see TL;DR caveat) | Variant B: 0 flips / 6,098,976 rows under closed-form truncation AND rounding (Q3 + Q3b); approximation valid because Signal 5 contributes 0 to all candidates (multiplier eligibility unaffected). Variant C: 35 score-instances promoted at MIN_SCORE (~0.0006% of corpus = 4 distinct contracts per Q4b); Variant A: no change (status quo) |
| Identifies whether any profitable/missed signals would change ranking materially | ✓ | Top-10 score_history rows all = 58.0; under Variant B/C inflation → 62. **No candidate reaches CONVICTION=70 under any variant** (max stays at 62). **Q4b: 0 of 4 distinct Variant-C-promoted contracts had paper_trade outcomes** — no missed profitable signals |
| Documents Hermes-first result | ✓ | Hermes skill hub Social Media category (7 listed) + awesome-hermes-agent BOTH checked. **awesome-hermes IS reachable** (prior cycle-7/8/9 "404 consistent" claim was stale — corrected); `x-twitter-scraper` exists but does not cover per-token 24h aggregation. Bridge gate not met (0/131 resolved 7d + 6 distinct TG contracts/24h) |
| Updates backlog/todo/memory/context | ✓ | backlog status AUDITED 2026-05-17 + 5 follow-ups filed (3 audit + 2 VARIANT-{B,C}-IMPL PENDING-OPERATOR-DECISION); todo board entry; memory checkpoint outside repo |

## Recommendation: Option B (remove + recalibrate gates)

Rationale:
1. **Closed-form approximate 0-flip blast radius** across 6,096,576 historical scoring rows (Q3 in audit queries; see TL;DR caveat — `score_history` stores only post-multiplier final score, so this is a closed-form approximation, not an exact re-score)
2. **Removes 15-point intellectual debt** from `SCORER_MAX_RAW=208`; future engineers reading `scorer.py:121` no longer see a phantom signal
3. **Closes Variant C stealth-suppression**: gate recalibration maintains intentional friction that Variant C would silently widen

### Alternatives considered

| Option | Why not | One-line summary |
|---|---|---|
| C (remove without recalibrating) | Operator preference call; widens MiroFish funnel by 35 candidates (0.0006% corpus) | Viable if operator values recall over precision |
| A (defer entirely) | Carries 15-point intellectual debt + 35-signal stealth-suppression forever | Only if zero risk surface desired |
| D (Hermes/TG bridge) | Data not ready: 0/126 resolved Hermes X; 6 distinct TG tokens/24h | Re-eval at trigger threshold |

## Empirical evidence

### Runtime-state verification (per CLAUDE.md §9a)

| Assumption | Result | Source |
|---|---|---|
| `social_mentions_24h` is structurally dead | total=1,672, would_fire_signal_5 (>50) = **0**, nonzero = **0**, max = **0** | Q1 |
| Max score over score_history retention window | **58.0** (across 6,098,976 rows over 15d window 2026-05-02→2026-05-17); gte60=0; gte70=0 | Q2 + Q2b |
| `score_history` retention window | **2026-05-02 → 2026-05-17 (15 days)** — claim is recent-data observation, not project-lifetime (per PR-review fold R1 #6) | Q2b |
| Variant B rounding sensitivity | rounded version produces **identical** numbers (0/0/0/0) — 0-flip claim survives both truncation AND rounding (per PR-review fold R1 #1) | Q3b |
| Paper-trade dispatch path | `signals.py:325` gates on `quant_score > 0` — bypasses CONVICTION_THRESHOLD entirely | code read |
| Hermes X resolution rate (7d) | 0/131 (Wilson 95% two-sided UB ≈ 2.86%) | Q5 |
| TG per-token rollup 24h | 6 distinct contracts / 6 messages — insufficient for replacement | Q6 |
| social_signals / social_baselines / social_credit_ledger row counts | 0 / 0 / 0 | Q7 |
| **Variant C 35-candidate paper-trade attribution** | 4 distinct contracts, **0 with paper_trade outcomes** — no missed profitable signals (per PR-review fold R1 #7) | Q4b |

### Backtest variants (closed-form over 6,096,576 score_history rows)

#### Variant A: status quo — Signal 5 stays dead in MAX_RAW=208
- Behavior unchanged. MiroFish gate (MIN_SCORE=60) never reached. 35 historical scores at 58 silently suppressed.

#### Variant B (RECOMMENDED): remove Signal 5 + recalibrate gates 60→65 + 70→75
- 0 demoted at MIN_SCORE; 0 promoted at MIN_SCORE
- 0 demoted at CONVICTION; 0 promoted at CONVICTION
- **0-flip under closed-form approximation across 6,096,576 rows** (Q3; see TL;DR caveat — exact re-score not feasible from stored final scores)

#### Variant C: remove Signal 5 + DO NOT recalibrate gates (let inflation through)
- 35 newly pass MIN_SCORE=60 (historical 58s → 62 under MAX_RAW=193)
- 0 newly pass CONVICTION=70 (max post-inflation = 62, still under 70)
- Effect: MiroFish funnel widens by 35 candidates over entire history (Q4)

#### Variant D: Hermes/TG bridge — deferred
- Hermes X: 126/0 resolved → fails ≥20-resolved gate
- TG: 6 distinct tokens/24h → fails ≥50-tokens/24h gate
- Defer until any bridge gate satisfied

## Re-evaluation triggers

Re-run `tasks/audit_v2_queries.sql` when ANY trigger fires (first-fire wins; if multiple fire same window, run once and address all):

1. `narrative_alerts_inbound.resolved_coin_id` populated count ≥ 20 in any 30d window (currently 0/131)
2. `tg_social_messages` distinct-contract 24h rollup ≥ 50 (currently 6)
3. **`scorer.py` signal weight change OR `SCORER_MAX_RAW` change** (per design-review fold R2 #5 — invalidates Variant B's 0-flip math)
4. **2026-08-17** (calendar backstop, ~90d from 2026-05-17, per cycle-9 `keep_on_provisional_until_<iso>` convention)
5. Operator explicit request
6. **Any 30d window with top-10 `score_history` scores ≥ 60** (forward-stability detector per PR-review fold R3 #5 — current 5-point headroom 58→60 shrinks to 0)

The watchdog for triggers 1+2 is filed as `BL-NEW-SOCIAL-DENOMINATOR-RE-EVAL-WATCHDOG` (single follow-up, merged per design-review fold R1 #5).

## Open questions (operator decision)

1. **Confirm Option B (recommended) OR override to Option C (35-candidate funnel widening)?**
   - B preserves current MIN_SCORE/CONVICTION friction; C widens MiroFish funnel
   - Decision-by: 4 weeks from PR merge (next-cycle PR file decision; if no response by then, default action = stamp interim Option A + auto-file re-eval at 2026-08-17 backstop) — see `BL-NEW-SOCIAL-DENOMINATOR-OPERATOR-PREFERENCE`

2. **Acceptable cadence for re-eval triggers (current: 4 data + 1 calendar)?**
   - Adjust trigger #2 (TG threshold) if 50/24h is too high or low for the operator's funnel intent

## What this PR commits and does NOT commit

**Commits (read-only / one-line cleanup):**
- This findings doc
- `tasks/audit_v2_queries.sql` — re-runnable queries for triggers
- One-line annotation on `scout/scorer.py:121` (`# DEAD SIGNAL — pending BL-NEW-SOCIAL-MENTIONS-DENOMINATOR-AUDIT re-eval`; zero behavior change; mirrors Signal 13 gated-comment convention at scorer.py:184-198)
- Backlog status flip PROPOSED → AUDITED 2026-05-17 + 3 follow-up entries
- todo.md Active Work entry

**Does NOT commit:**
- Variant B implementation (Settings change deferred)
- Variant C implementation (operator preference call)
- Hermes/TG bridge (data not ready)
- Watchdog cron (deferred to `BL-NEW-SOCIAL-DENOMINATOR-RE-EVAL-WATCHDOG`)

## Cross-references

- **backlog.md:228** — originating `BL-NEW-SOCIAL-MENTIONS-DENOMINATOR-AUDIT` entry
- **PR #150** (merged 2026-05-17 at `a20891f`) — established `keep_on_provisional_until_<iso>` time-boxed-decision convention; this audit's 90d backstop mirrors that
- **tasks/findings_bl032_social_signal_audit_2026_05_14.md** — closed build-custom-Twitter direction; this audit drills the residual 15-point phantom
- **memory `feedback_lunarcrush_dropped.md`** — LunarCrush/Santiment/Nansen all dropped 2026-04-19
- **memory `feedback_lever_vs_data_path_pattern.md` (§9c)** — phantom features that look load-bearing but aren't pull this same shape
- **CLAUDE.md §11b** — bootstrap CI + Wilson LB mandate (Wilson UB applied here: 0/126 → 2.91%)
- **scorer.py:121** — Signal 5 declaration site (annotated by this PR)
- **scorer.py:37** — `SCORER_MAX_RAW = 208` (the 15-point phantom denominator)
- **config.py:27-28** — MIN_SCORE=60, CONVICTION_THRESHOLD=70 (Variant B targets)

## Reviewer fold provenance

Plan-stage: 2 parallel reviewers (empirical-rigor + strategy/deferral-risk). 2 CRITICAL + multiple IMPORTANT folded into plan v2.
Design-stage: 2 parallel reviewers (operator-UX + risk/deferral). 3 CRITICAL + multiple IMPORTANT folded into design v2 + this findings doc structure.
Empirical corrections (R1 #1/#2/#3): MIN_SCORE=60 not 25; paper dispatch bypasses CONVICTION; max historical = 58 (full 6M+ rows not just 1,677 candidates).
