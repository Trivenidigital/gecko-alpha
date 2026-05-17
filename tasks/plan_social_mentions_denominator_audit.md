**New primitives introduced:** NONE. This is a findings-only audit. Optional one-line comment annotation on `scout/scorer.py:121` (`# DEAD SIGNAL — pending BL-NEW-SOCIAL-MENTIONS-DENOMINATOR-AUDIT re-eval`) is zero-behavior-change and explicitly scoped per Reviewer 2 finding #2. Output is one new findings doc, one backlog status flip with re-evaluation triggers + 90d calendar backstop, one todo board update, one memory checkpoint, and (conditional) one scorer.py comment line.

# social_mentions_24h Denominator Audit Implementation Plan (v2 — post-2-reviewer fold)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Empirically determine whether `social_mentions_24h` should remain in the scorer denominator, be removed (with downstream gate recalibration), or be replaced later by Hermes X / TG narrative evidence. Ship a findings doc with quantitative recommendation. Reviewer-1 BLOCK was on CRITICAL empirical errors (wrong MIN_SCORE value, wrong dispatch path); both corrected below. New recommendation: **Variant B (remove + recalibrate gates) is preferred over Variant A (defer)** — bounded 0-flip blast radius across 6M+ historical scoring rows + removes 15-point phantom + closes Variant C's 35-candidate stealth-suppression. Code change deferred to explicit operator approval; findings doc + cleanup comment + backlog flip + memory ship this PR.

**Architecture:** Read-only audit (one optional one-line annotation). SQL queries against srilu prod `scout.db` (HEAD = `a20891f` = origin/master, includes PR #150). NO test files, NO new code modules.

**Tech Stack:** SQL via SSH-to-file pattern; Python only for verification scripts under tasks/ (not shipped). Wilson upper-bound computed inline per CLAUDE.md §11b.

## v2 fold summary (post-plan-review)

| Reviewer 1 finding | Severity | Resolution in v2 |
|---|---|---|
| #1 — MIN_SCORE=60 (not 25) | CRITICAL | All backtest variants re-run against true gate. Original 8/13 flip-counts were artifacts; true counts are 0/35 |
| #2 — Paper-trade dispatch uses `quant_score > 0`, not CONVICTION_THRESHOLD | CRITICAL | Plan §dispatch-path documents the actual pipeline; conviction-blast-radius reframed to MiroFish-alert path only |
| #3 — max=47 was sample-of-prod, not historical | IMPORTANT | Full `score_history` queried (6,096,576 rows); true historical max = 58 |
| #4 — Historical SCORER_MAX_RAW drift (198 → 208 at BL-054) | IMPORTANT | Closed-form reconstruction stratified; impact: candidates with score>=56 pre-BL-054 are over-estimated, but corpus is now scored at 208 uniformly per recalibration |
| #5 — No Wilson LB / bootstrap CI | IMPORTANT | Wilson UB added for "0/126 resolved" claim: 95% one-sided UB ≈ 2.91% (negligible) |
| #6 — SA1 used `> 0` not `> 50` | MINOR | V2-4 query uses `> 50` (matches Signal 5's actual threshold); result unchanged (0 either way) |
| #7 — Hermes was category-exhaustive | MINOR | Plan qualifier added: "category-exhaustive (Social Media, 7 listed), not name-exhaustive across all 689" |
| #8 — Re-eval trigger no watchdog | MINOR | Filed as follow-up `BL-NEW-SOCIAL-DENOMINATOR-RE-EVAL-WATCHDOG` in Task 4 |
| #9 — Dropped "OR explicit symbol-resolution design" branch | MINOR | Restored in re-eval triggers verbatim from backlog L237 |

| Reviewer 2 finding | Severity | Resolution in v2 |
|---|---|---|
| #1 — Code-vs-config over-interpretation | IMPORTANT | Findings doc now states "Variant B is code-eligible per operator's strong-evidence test; recommendation is Variant B with operator-explicit-approval gate" |
| #2 — Variant A creates intellectual debt | IMPORTANT | Recommendation shifted from Option A → Option B. One-line `# DEAD SIGNAL` annotation on scorer.py:121 included in this PR (zero behavior change) per Reviewer 2 §2 |
| #3 — Variant C dismissal missed operator preference | IMPORTANT | Variant C now surfaces 35-candidate funnel widening; explicit operator preference question added to findings doc Open Questions section |
| #4 — CONVICTION counterfactual missing | IMPORTANT | Top-10 historical scores computed (all are 58→62 under Variant B/C); none reach 70 even under inflation — counterfactual closed empirically |
| #5 — Re-eval trigger memory-dependent (§12a) | IMPORTANT | Filed `BL-NEW-SOCIAL-DENOMINATOR-RE-EVAL-WATCHDOG` per Task 4 |
| #6 — Missing follow-up backlog items | IMPORTANT | 4 follow-ups filed in Task 4: re-eval watchdog, scorer DEAD-SIGNAL comment convention, TG per-token rollup feasibility, operator-preference question |
| #9 — Defer not time-boxed | IMPORTANT | Re-eval triggers now include 90d calendar backstop (2026-08-17) |
| #11 — TG path inadequately scoped | IMPORTANT | V2-5 query: 24h rollup = 6 distinct contracts / 6 messages → insufficient for replacement; bridge gate would need ≥50 distinct tokens/24h |
| #12 — Paper-trade attribution on demoted candidates | IMPORTANT | At corrected gates Variant B has 0 demotions, so attribution check is vacuous; Variant C's 35 promotions to outcomes is checked at findings-doc time |
| #14 — SHIPPED-AS-FINDINGS-ONLY is invented | MINOR | Replaced with `AUDITED 2026-05-17` per BL-032 precedent at backlog L216 |

## Hermes-first analysis (corrected per R1 #7)

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Per-token social mention aggregation (24h) | None (Hermes skill hub Social Media category 7 skills + awesome-hermes-agent x-twitter-scraper checked) | Defer; no drop-in Hermes primitive |
| Raw X/Twitter API access | YES — `x-twitter-scraper` at `https://github.com/Xquik-dev/x-twitter-scraper` (43 SKILL.md folders: search, timelines, mentions, trends, monitors, webhooks) | Could serve as raw data source for a custom aggregation layer; out of scope for this audit |
| Narrative mention counting | None at aggregate-by-token | Hermes X consumer side exists (`narrative_alerts_inbound` 131 rows 7d) but resolution is 0/131 — bridge data not ready |
| KOL signal rollup | None at aggregate-by-token | TG path (`tg_social_messages`) has per-message contract extraction but only 6 distinct contracts in 24h rollup |

**Hermes-first staleness corrected (post-merge fold):** prior cycle-7/8/9 "awesome-hermes-agent 404 consistent" claim was incorrect — the repo IS reachable and lists `x-twitter-scraper`. Audit verdict UNCHANGED: no drop-in primitive for per-token 24h aggregation. But the diligence framing is honest now. Verdict: **no Hermes primitive applies; bridge to Hermes X/TG defer until resolution rate or per-token TG rate matures.**

## Drift-check (per CLAUDE.md §7a)

`git fetch origin && git log -10 origin/master` performed 2026-05-17 at session start. Worktree HEAD = `a20891f` = origin/master (zero divergence; includes PR #150 merge). Grep for `social_mentions_24h|SOCIAL_MENTIONS` returns 19 files: scorer.py:121 (live consumer, Signal 5), models.py, db.py, dashboard/db.py + dashboard/frontend/CandidatesTable.jsx, 4 test files, 4 doc files. **No drift** — field is wired as documented in originating backlog entry at L228. Prior 2026-05-14 `findings_bl032_social_signal_audit_2026_05_14.md` closed the build-custom-Twitter direction; this audit drills the residual 15-point denominator phantom.

## Runtime-state verification (per CLAUDE.md §9a — corrected per R1 #1/#2/#3)

| Assumption | Verification | Result |
|---|---|---|
| `social_mentions_24h` is unwired in production | `SELECT MAX(social_mentions_24h), SUM(>50), SUM(>0) FROM candidates` | total=1,671, would_fire_signal_5=0, nonzero=0, max=**0**. 100% confirmed dead. |
| Signal 5 contributes 15 points to `SCORER_MAX_RAW = 208` | scorer.py:121 (`if token.social_mentions_24h > 50: points += 15`) | Confirmed |
| **Max historical score across full corpus** | `SELECT MAX(score) FROM score_history` (6,096,576 rows) | **max = 58.0**; gte_60 = 0; gte_70 = 0; gte_80 = 0 |
| Paper-trade dispatch gate | `signals.py:325` (`if quant_score <= 0 or not signals_fired:`) | Confirmed — **paper dispatch bypasses CONVICTION_THRESHOLD entirely**; only MiroFish-alert path uses CONVICTION |
| Hermes X resolution path is mature enough to bridge | `SELECT COUNT(*), resolved_count FROM narrative_alerts_inbound 7d` | total=126, resolved=**0**; Wilson 95% UB on 0/126 = 2.91% (no resolution today, no near-term resolution either) |
| TG social per-token aggregation feasibility | `SELECT COUNT(DISTINCT contracts) FROM tg_social_messages WHERE 24h AND contracts != '[]'` | **6 distinct contracts in 24h** — insufficient for replacement signal |
| social_signals/social_baselines/social_credit_ledger | `SELECT COUNT(*)` each | 0 rows / 0 rows / 0 rows |

## Empirical backtest variants (corrected per R1 #1/#3)

### Variant A: status quo (keep dead field) — recommendation status REVOKED per R2 #2

Current behavior. SCORER_MAX_RAW=208 with 15-point dead phantom. Max historical score = 58, below MIN_SCORE=60. Effect: **MiroFish alert gate is currently never crossed**; the dead phantom IS the reason 35 candidates with score=58 are silently suppressed below MIN.

### Variant B: remove Signal 5 + recalibrate gates 60→65 + 70→75 — RECOMMENDED

Closed-form on full `score_history` (6,096,576 rows):
- 0 demoted at MIN_SCORE (60 → 65): no candidate flips
- 0 promoted at MIN_SCORE: no candidate flips
- 0 demoted at CONVICTION (70 → 75): no candidate flips
- 0 promoted at CONVICTION: no candidate flips
- **Net effect: 0-flip across 6M+ historical rows.** Honest accounting + zero blast radius.

### Variant C: remove Signal 5, DO NOT recalibrate gates (let inflation through)

- 35 newly pass MIN_SCORE=60 (out of 6M+) — historical 58s inflate to 62
- 0 newly pass CONVICTION=70 — historical max 58 inflates to 62, still under 70
- Effect: **35 candidates would gain MiroFish alerts they currently don't get.** Whether this is good depends on operator preference (precision vs recall on the alert path).

### Variant D: hypothetical Hermes bridge — defer per data-readiness gate

- Backlog scope: ≥50 narrative_alerts_inbound rows AND (≥20 resolved OR explicit symbol-resolution design)
- Current: 126 rows / 0 resolved / no symbol-resolution design shipped
- TG bridge alternative: ≥50 distinct tokens/24h rollup — current 6 distinct contracts/24h
- **Bridge NOT eligible today.** Both Hermes X and TG paths fail the gate.

## Recommendation (corrected per R2 #2)

**Primary: Option B (remove + recalibrate gates).** Recommended for next code-change cycle pending explicit operator approval.

Why B over A:
1. **Empirical: 0-flip blast radius** verified across 6M+ historical rows. No candidate's MiroFish/CONVICTION eligibility changes.
2. **Removes 15-point intellectual debt** from SCORER_MAX_RAW. Future engineers reading scorer.py:121 no longer see a phantom signal.
3. **Closes the Variant C stealth-suppression**: without recalibrating gates, removal of Signal 5 would automatically promote 35 historical scores past MIN_SCORE; gate recalibration restores intentional friction.
4. **Per CLAUDE.md §9a runtime-state verification**: the dead field is structurally certain (max=0 across all 1,671 prod candidates AND max_signal_fires=0 historically).

Why B is NOT shipped in this PR:
- Operator constraint: "do not touch ... live config" was interpreted by Reviewer 2 #1 as ambiguous (code vs runtime-config). To respect both interpretations, this PR ships findings-only + one-line cleanup comment; the code change waits for explicit operator approval.

**Secondary: Option C (remove without recalibrating)** — viable if operator values funnel-widening at MiroFish gate (35 historical promotions, ~0.0006% of historical scoring corpus).

**Tertiary: Option A (defer entirely)** — only if operator wants zero risk surface. Carries the 15-point intellectual debt + 35-signal stealth-suppression.

**Option D (Hermes bridge)** — deferred per data-readiness gate. Re-evaluate when narrative_alerts_inbound.resolved_coin_id reaches ≥20 (currently 0/126) OR an explicit symbol-resolution design is shipped OR TG distinct-token rollup reaches ≥50/24h (currently 6).

## Re-evaluation triggers (per R1 #8/#9 + R2 #5/#9 — file as `BL-NEW-SOCIAL-DENOMINATOR-RE-EVAL-WATCHDOG` follow-up)

Re-run this audit when ANY of the following triggers fire:
1. `narrative_alerts_inbound.resolved_coin_id` populated count reaches ≥20 in any 30d window (currently 0/126)
2. OR an explicit symbol-resolution design is shipped for narrative_alerts_inbound
3. OR `tg_social_messages` distinct-token rollup reaches ≥50/24h (currently 6)
4. OR **2026-08-17** (90d calendar backstop, per cycle-9 `keep_on_provisional_until_<iso>` convention)
5. OR operator explicitly requests removal of the 15-point phantom for accounting hygiene

## Files to create / modify

### Create
- `tasks/findings_social_mentions_denominator_audit_2026_05_17.md` — empirical findings + recommendation (Option B preferred)

### Modify
- `backlog.md` — flip `BL-NEW-SOCIAL-MENTIONS-DENOMINATOR-AUDIT` PROPOSED → **AUDITED 2026-05-17** (per BL-032 precedent at L216) with re-evaluation triggers + 4 follow-up items filed inline:
  - `BL-NEW-SOCIAL-DENOMINATOR-RE-EVAL-WATCHDOG`
  - `BL-NEW-SCORER-DEAD-SIGNAL-COMMENT-CONVENTION`
  - `BL-NEW-TG-PER-TOKEN-ROLLUP-FEASIBILITY`
  - `BL-NEW-SOCIAL-DENOMINATOR-OPERATOR-PREFERENCE`
- `tasks/todo.md` — Active Work board entry for the audit cycle
- `scout/scorer.py` — **one-line annotation only**: `# DEAD SIGNAL — pending BL-NEW-SOCIAL-MENTIONS-DENOMINATOR-AUDIT re-eval` above L121. Zero behavior change; mirrors Signal 13's gated-comment convention at L184-198. Per R2 §2.

### Do NOT modify
- `scout/config.py` (no MIN_SCORE/CONVICTION_THRESHOLD changes — defer to operator approval for Variant B)
- `scout/models.py`, `scout/db.py`, dashboard surfaces
- Any test files
- `.env` on srilu-vps

## Task decomposition

### Task 0: Empirical evidence gathering (DONE; corrected v2)

- [x] Drift-check confirmed
- [x] Hermes-first WebFetch (category-exhaustive caveat noted)
- [x] Runtime SQL audit corrected against MIN_SCORE=60, dispatch path
- [x] Full `score_history` backtest (6,096,576 rows): max=58, Variant B = 0-flip, Variant C = 35-promote-min
- [x] TG per-token 24h rollup feasibility: 6 distinct contracts — insufficient
- [x] Wilson UB on resolution rate: 0/126 = 2.91% UB

### Task 1: Write findings doc

**Files:** Create `tasks/findings_social_mentions_denominator_audit_2026_05_17.md`

- [ ] Step 1: Write findings doc with `**New primitives introduced:** NONE.` header
- [ ] Step 2: Sections: TL;DR, drift verdict, Hermes-first verdict, runtime-state verification table, 4 backtest variants with corrected numbers, recommendation (Option B preferred), Open Questions section (per R2 §8), re-evaluation triggers with 90d backstop, cross-references
- [ ] Step 3: Verify against operator acceptance criteria checklist
- [ ] Step 4: Commit

### Task 2: One-line scorer.py annotation (R2 §2)

**Files:** Modify `scout/scorer.py` (one line above L121)

- [ ] Step 1: Add `    # DEAD SIGNAL — pending BL-NEW-SOCIAL-MENTIONS-DENOMINATOR-AUDIT re-eval` above the existing comment `# Signal 5: Social Mentions -- 15 points (optional)`
- [ ] Step 2: Run `uv run pytest tests/test_scorer.py -v` on srilu to verify zero test breakage
- [ ] Step 3: Commit

### Task 3: Update backlog.md (status flip + 4 follow-ups)

- [ ] Step 1: Edit existing entry at L228: status PROPOSED → AUDITED 2026-05-17 + re-eval triggers
- [ ] Step 2: File 4 new entries for the follow-ups
- [ ] Step 3: Commit

### Task 4: Update todo.md + memory

- [ ] Step 1: Active Work board entry
- [ ] Step 2: Memory checkpoint
- [ ] Step 3: Update MEMORY.md index
- [ ] Step 4: Commit board updates

### Task 5: PR + 3 reviewers

- [ ] Push, create PR
- [ ] Dispatch 3 parallel reviewers (statistical-defensibility + integration + strategy-deferral-risk)
- [ ] Fold findings
- [ ] Post-merge bookkeeping per PR #150 convention

## Self-review checklist

- [ ] Spec coverage — every operator scope item addressed:
  - ✓ Drift-check (Task 0)
  - ✓ Hermes-first (Task 0)
  - ✓ Runtime-state verification (Task 0; corrected gates)
  - ✓ 3 backtest variants (Task 0; recomputed)
  - ✓ Clear recommendation (Task 1 — Option B preferred)
  - ✓ Quantifies score/ranking impact (Variant B = 0 flips; Variant C = 35 MiroFish promotions)
  - ✓ Identifies missed signals (Variant C reveals 35 historical 58→62 candidates currently below MIN_SCORE)
  - ✓ Documents Hermes-first (no skills found; category-exhaustive)
  - ✓ Updates backlog/todo/memory/context
- [ ] No live config flips
- [ ] No PR #150 file touches (scope: tasks/, backlog.md, todo.md, scorer.py one-line comment only)
- [ ] CLAUDE.md plan-doc gate satisfied
- [ ] Reviewer 1 folds complete (all CRITICAL + IMPORTANT addressed)
- [ ] Reviewer 2 folds complete (all CRITICAL + IMPORTANT addressed)

## Out of scope

- Building custom Twitter/X scraper (closed permanently 2026-04-19)
- Implementing Variant B Settings change (defer to explicit operator approval)
- Implementing Variant C (operator preference call)
- Building Hermes bridge (data not ready)
- Modifying CONVICTION_THRESHOLD or MIN_SCORE values
- Touching live config on srilu

## Execution handoff

Proceeding to inline execution. Per CLAUDE.md §10 heuristic-invocation rule, design review is justified here because the audit's recommendation deferral has the highest rot-risk outcome of strategic review work. Full Plan→2-reviewers→Design→2-reviewers→PR→3-reviewers chain warranted.
