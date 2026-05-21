# Plan — Overnight Decision Harvest 2026-05-21

**New primitives introduced:** NONE — plan document only. P5 may produce a docs-only PR with backlog status flips, stale closures, design-record disposition notes, and data-bound re-check gates; no code, schema, config, or runtime primitive in P5 either.

## Hermes-first table (P5 docs PR — no new runtime work proposed)

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Backlog status maintenance | none (project-internal docs) | KEEP_CUSTOM (no skill applies; markdown docs in gecko-alpha repo) |
| PR triage / decision recording | none | KEEP_CUSTOM (gh CLI + repo edits) |
| Source-call substrate, price coverage, X/KOL evaluation | already deferred to operator-approved vendor sample (per merged PR #208) | DEFER — implementation gated, not in tonight's scope |
| **Vector-B-C2 fold: Hermes narrative scanner cron** | `jobs.json` contains EXACTLY ONE job: `gecko-x-narrative-scanner` (id `c849fffec986`, schedule `0 * * * *`, hourly, `no_agent: true`, `last_status: ok`, `last_run 2026-05-21T04:00:43Z`, `completed: 172`). jobs.json only persists the LATEST status, not last 5 — prior runs not directly verifiable from this surface. **What is verified:** (a) scanner exited ok on its last cycle, (b) 92 new `narrative_alerts_inbound` rows in 24h confirms classifier+writer path is producing output. **What is NOT directly verified tonight:** (c) per-run status for the prior 4-5 cycles, (d) dispatcher delivery (the `last_delivery_error: null` suggests OK but is single-cycle scoped). | NO_CHANGE — adequate health signal for tonight; file follow-on for jobs.json N≥5 history |
| **Vector-B-C3 fold: awesome-hermes-agent / HermesHub** | NOT rechecked tonight — no WebFetch performed in this session. KEEP_CUSTOM by default because P5 scope is internal markdown only (backlog status flips), and a Hermes skill that owns gecko-alpha-specific backlog hygiene is implausible. **Honest framing:** Hermes-first audit deferred for this docs-only PR rather than asserted. | KEEP_CUSTOM (deferred recheck) |
| In-tree drift-check (§7a) for backlog cleanup primitives | `ls scripts/ \| grep -iE "backlog\|cleanup\|status.flip"` returned empty — no existing primitive. P5 markdown edits are correctly custom. | KEEP_CUSTOM (negative drift evidence confirmed) |

**Drift-check (in-tree):** No tree-level work is proposed tonight that would have existing primitives. The proposal is decisions over new code; nothing to drift-check.

## P0 — State reconciliation (read-only) — RESULTS

| Surface | Result | Evidence |
|---|---|---|
| origin/master tip | `d57f6d59` (PR #208 merged) | git log |
| prod srilu HEAD | `df76d85` (PR #207 — 1 commit behind master; the missing commit is #208 which is docs-only) | ssh git log |
| prod "uncommitted" tracked files | benign `chmod +x` mode-bit difference on `scripts/source-calls-*.sh`; content identical | `git diff --stat` shows 0/0 lines, only mode change |
| **Vector-B-C1 fold: prod untracked-state inventory** | 25 untracked items: 15× `.env.bak*` / `.env.*preflip*` / `.env.*pre_flip*`, 3× `channels.yml.bak*`, `channels.yml`, `backups/`, `paper.db`, `prod.db`, `gecko_alpha.db`, `paper_trades_archive_20260421.csv`, 3× `scout.db.bak.*`, 2× dist artifacts. The `.env.bak*` files are operator-managed safety snapshots for past config flips — sensitive but not committed/exposed. Drift-of-interest answer: runtime is reading `.env` at md5 `dfb81df18dcc04bdfd5f7a607d365c78` (recorded as next-session baseline). | `git status --short`, `md5sum .env` |
| open PRs (master-targeting) | #209 (clean docs flip), #184 (design draft), #183 (audit draft), #105 (WIP draft FAILED), #34 (impl OPEN), #33 (impl OPEN) | gh pr list |
| backlog entries | 115 `BL-NEW-*` headings, 182 status banners | `grep -c` |
| todo.md | 695 lines, contains stale "Resume hook" section referring to TG bot token as `placeholder` (operator memory says Telegram is wired since 2026-05-06) | `tail -30 tasks/todo.md` |
| worktrees | 60+ active worktrees, many representing past/stalled work — operator instructed do-not-clean | `git worktree list` |
| CI workflow on master | `.github/workflows/test.yml` is test-only — no deploy or prod-touching step. **Verified safe to merge docs-only PRs without prod side-effect.** | reviewer verified |
| CODEOWNERS | no CODEOWNERS file in repo root or `.github/`. Required-review rules rely on operator's branch-protection setup; not inspected tonight. | `ls .github/` |

## P1 — Live health (read-only) — RESULTS

| Surface | Status | Evidence |
|---|---|---|
| `gecko-pipeline` service | ✓ ACTIVE since 2026-05-20T16:53Z, 0 restarts | `systemctl is-active` |
| `gecko-dashboard` service | ✓ ACTIVE | `systemctl is-active` |
| `source_calls` writer parity | ✓ 100% (tg=858/858, x=399/399, total=1257, latest_updated_at=04:25:02Z) | sqlite |
| `source_calls` ALERT_SENT last 24h | ✓ 0 | journalctl |
| `source_calls` cron tick count | ⚠️ irregular: 52 in last 6h vs 108 expected (~half-rate) — same pattern observed in earlier 6.5h check. SLO contract still being met (zero alerts) because watchdog measures upstream `observed_at` lag, and upstream gaps coincide with writer gaps | journalctl |
| Hermes narrative cron | ✓ `last_status=ok`, 172 completions, last_run 2026-05-21T04:00Z, hourly schedule | jobs.json |
| narrative_alerts_inbound throughput | ✓ 92 new rows in last 24h | sqlite |
| paper_trades touched by ledger work | ✓ NO — 1,610 rows, normally active (latest open 02:42Z, latest close 03:54Z) | sqlite |
| Exceptions in journal | ✓ none beyond known MiroFish-timeout DEBUG noise | journalctl |
| Dashboard endpoint smoke | ✓ 10/11 endpoints return 200; `/api/x_alerts` times out after 5s (pre-existing perf issue, NOT new from #207/#208) | curl probes |
| outcome_status distribution | ✓ stable: 1,241 unresolvable / 11 partial / 5 pending (consistent with backfill baseline — no regression) | sqlite |

## P2 — Actionability validation — RESULTS

**Verdict: STATISTICALLY INSUFFICIENT *and* both cohorts NET-NEGATIVE on n=7 and n=2 respectively. Do not graduate #183/#184 from data on hand. Per `feedback_n_gate_verdicts_against_dashboard_noise.md`, the 86% win-rate display is anchor-bait and must be paired with Wilson LB + net-PnL context wherever it appears downstream.**

| Cohort (since cutover 2026-05-19T11:39Z) | n | total PnL | avg PnL | wins | losses | win-rate | Wilson 95% one-sided LB |
|---|---|---|---|---|---|---|---|
| actionable=1 | 7 | **-$11.72** | **-$1.67** | 6 | 1 | 6/7 = 86% | **~42%** (uninformative) |
| actionable=0 (exploratory) | 2 | **-$10.85** | **-$5.42** | 0 | 2 | 0/2 = 0% | 0% (uninformative) |
| unknown | 0 | — | — | — | — | — | — |

**Vector-A-C2/C3 fold — headline corrections:**
- The **actionable cohort lost money** (`net -$11.72` driven by 1 outlier loss). The 86% win-rate masks net-negative expectancy — the §8-statistical "outlier dominance" pattern. Reading "86% win-rate" without "net -$11.72" misleads.
- Both cohorts net-negative on tiny n. The exploratory cohort's 0/2 losses are anecdotal, not evidence of gate effectiveness.
- Per `feedback_n_gate_verdicts_against_dashboard_noise.md`, any downstream surface (dashboard, weekly digest, BL re-eval) consuming this data MUST show INSUFFICIENT_DATA explicitly + Wilson LB + net-PnL — not raw win-rate.

Reason-code distribution among exploratory (n=2): 1× `v1_block_tg_social_low_n`, 1× `v1_block_core_signal_mcap_below_10m`. Both losing trades — consistent with gate intent but n is anecdotal.

**False-negative exploratory winners: ZERO** at n=2.

**Data growth since last validation:** n=1+0 → n=7+2 = 9 total. Real growth, still well below significance.

**Vector-A-C1 fold — symmetric re-check gate:** previous draft had asymmetric early-fire (1 exploratory winner triggered re-check but n=20+5 was required for confirmatory direction). Symmetric gate:

**Pre-registered data-bound re-check gate (any one triggers):**
1. **Primary:** n_actionable_closed ≥ 20 AND n_exploratory_closed ≥ 5 (symmetric power on both cohorts).
2. **Early-fire (false-negative):** ≥1 exploratory closed with `pnl_usd > 0` (gate may be over-rejecting).
3. **Early-fire (confirmatory loss):** n_exploratory ≥ 5 AND ≥4 losses with one-sided binomial 95% LB exceeding null (gate's stated thesis holds even on small n).
4. **Early-fire (actionable drawdown):** n_actionable ≥ 15 AND cohort `total_pnl < -$50` (net negative survives sample growth).

Vector-A-M1 fold: calendar floor removed. Re-check is fully data-bound.

## P3 — Open-PR disposition recommendations

| PR | State | Title | Recommendation | Rationale |
|---|---|---|---|---|
| #183 | DRAFT, +666/-0, 3 files | docs(audit): peak-giveback/freshness historical audit | **MERGE as durable design record** | CI green; pure audit script + findings doc, no behavior change; closes the peak-giveback investigation thread |
| #184 | DRAFT, +418/-0, 1 file | docs(design): TG/X outcome linkage | **MERGE as durable design record — CONDITIONAL** on body containing an explicit IMPLEMENTATION-GATED-ON-PRICE-COVERAGE banner (Vector-A-I1 fold). If absent in the body, add via PR-body amend BEFORE merge so the design record cannot be silently consumed by future code without the gate. The plan's non-goals line is one-level-removed from the artifact itself. | CI green; design doc only, "New primitives: NONE" marker present; same shape as #208 which merged cleanly yesterday |
| #209 | OPEN, +6/-2, 2 files | docs: mark source-call price design shipped | **MERGE** | CI green; clean post-merge status flip for #208; cannot defer or it accumulates drift |
| #33 | OPEN, +2654/-18, 12 files | feat(bl-050): paper-trade edge detection | **Operator decision — recommend KEEP DRAFT, re-evaluate at next data sufficiency window** (Vector-A-I2 fold: do NOT close without operator input) | Data-gated by paper_trade outcomes; not the bottleneck tonight; do not merge under stop-build discipline |
| #34 | OPEN, +2380/-59, 20 files | feat(bl-051): DexScreener top-boosts poller + velocity_boost | **Operator decision — recommend OPERATOR CLOSE as PARKED-PENDING-PRICE-COVERAGE** (Vector-A-I2 fold: months of work attached; close is operator's click) | Substrate (DexScreener spot/boost API) was rejected as historical-coverage source in PR #208 design; velocity_boost is +20pts but unmeasurable without forward-window resolution; tempting-but-blocked |
| #105 | DRAFT, +3264/-0, 17 files | feat(audit): Phase B daily snapshot volume_history_cg [WIP] | **Operator decision — recommend OPERATOR CLOSE as STALE-WIP** (Vector-A-I2 fold) | CI test FAILED; body says "DO NOT MERGE"; no updates since 2026-05-18; expands substrate before price coverage measurable |

## P4 — Cost / noise / stop-build brief (compressed)

**What is costing money or attention right now?**
- 60+ active worktrees on Windows side. Storage + cognitive overhead. Operator said do not clean — left alone.
- 115 BL entries / 182 status banners in `backlog.md`. Operator-facing review of "what's next" becomes a search problem.
- todo.md at 695 lines with a stale "Resume hook" referencing pre-Telegram-wired state (May 6 reality, file content still suggests bot token is `placeholder`).
- `/api/x_alerts` 5s timeout on dashboard — small but real attention drag.
- MiroFish DEBUG-level `fallback_raw_response` events accumulate noisy journal lines (~daily). Functionally fine, monitoring-noise nuisance.

**Noisy sources (volume/duplicates only, no rank-grade evidence):**
- KOL/TG channels populate `source_calls` at honest volume + duplicate rates — but cannot be ranked because price coverage is at 0.8% (11 partial / 1,257 total).
- Per memory: `@nebukadnaza` has the single highest coverage rate (1 eligible cluster out of 131) — still `biased_low_coverage`, not rankable.

**Cannot be ranked because price coverage is limited:**
- All 20 TG/X sources currently in `source_calls`.
- Any "best source" or "trust list" dashboard surface.
- Any actionability-gate input that consumes source quality.
- BL-NEW-DISCOVERY-VS-ENTRY-ATTRIBUTION (PROPOSED 2026-05-19, blocked).
- BL-NEW-PEAK-GIVEBACK-FRESHNESS-FILTER (PROPOSED 2026-05-19, blocked — same substrate).

**Can safely be ignored for 7 days:**
- Cron tick-count irregularity (writer fires every 5-30 min instead of clockwork every 5 min) — SLO contract met, no alerts, no parity gap. Not on critical path.
- Dashboard `/api/x_alerts` slow endpoint — pre-existing, not regressed.
- The 60+ worktrees — operator instructed do-not-clean.
- All `**Status:** PROPOSED 2026-05-20` and `2026-05-19` entries without an explicit decision-by date in the same week.

**Tempting but blocked by data/coverage:**
- BL-050 (first_signal edge detection, PR #33) — needs more closed paper_trades cohort data.
- BL-051 (DexScreener velocity_boost, PR #34) — needs price coverage to measure whether the +20pt signal converts.
- BL-NEW-SOURCE-CALL-PRICE-COVERAGE-EXPANSION implementation — design merged (#208), blocked on operator-approved vendor sample per the design's own IMPLEMENTATION-GATED status.
- All KOL ranking / pruning items.

**Single next implementation worth doing tonight: NONE.**

**Vector-A-I4 fold — implementations evaluated and not-recommended tonight (auditable list):**
| Candidate | Cost | Why not tonight |
|---|---|---|
| Price-coverage expansion (PR #208 implementation) | days | Explicitly IMPLEMENTATION-GATED on operator-approved vendor sample per #208 design |
| BL-050 first_signal edge detection (PR #33 merge) | days | Data-gated by paper_trade outcomes; need more closed cohort |
| BL-051 DexScreener velocity_boost (PR #34 merge) | days | Substrate rejected by #208 design; unmeasurable without coverage |
| Volume_history_cg Phase B snapshot (PR #105 merge) | days | CI FAILED + stale-WIP; expands substrate before measurement |
| Source-call cron-tick parity watchdog (Vector-B-I1 / §12a follow-on) | ~1hr | File as BL tonight in P5; impl deferred so #12a debt is recorded but not built tonight |
| MiroFish DEBUG-noise journal-suppression (Vector-A-M2) | ~30min | File as BL tonight in P5; low priority, journal-noise nuisance only |
| Dashboard `/api/x_alerts` 5s perf fix | hours | Pre-existing perf issue, not regressed by recent PRs; data-gated by user impact |
| jobs.json N≥5-prior-runs history (Vector-B-C2 follow-on) | upstream | Hermes-side change, not gecko-alpha-side; defer to Hermes maintainers / future session |

The honest call is: do not build tonight. The build spiral cannot be cured by another build. Decisions over code:

1. Merge #209 (post-merge status flip for #208 — CI green, mergeable=CLEAN).
2. Merge #183 (audit findings docs+script — no behavior change, CI green).
3. Merge #184 (TG/X linkage design) **only after** verifying body contains explicit IMPLEMENTATION-GATED-ON-PRICE-COVERAGE banner; if absent, comment-amend the PR body first, then merge.
4. File P5 docs-only PR with:
   - Symmetric data-bound re-check gate for actionability validation (n≥20+5 primary + 3 early-fire clauses)
   - File BL-NEW-SOURCE-CALL-CRON-TICK-WATCHDOG (Vector-B-I1 §12a follow-on)
   - File BL-NEW-MIROFISH-DEBUG-NOISE-SUPPRESS (Vector-A-M2, low priority)
   - "Do not build yet — coverage-blocked" annotations on PR #34 / #105 candidates (recommendation, not close)
   - Clear stale "Resume hook" section in tasks/todo.md (referenced pre-Telegram-wired state)
   - Add `.env` md5 baseline `dfb81df1...` to a memory note for next-session drift reference (memory file, not committed to repo)
5. Operator-decision items NOT closed by this session — recommendations only: PR #33 / #34 / #105.

## P5 — Backlog-cleanup PR plan (docs-only, if warranted)

**File scope (≤4 files):**
- `backlog.md` — status flips for entries with shipped evidence; "do not build yet — coverage-blocked" annotations on the tempting-but-blocked items
- `tasks/todo.md` — clear stale "Resume hook" referencing pre-Telegram-wired state; add data-bound re-check gate for actionability
- `tasks/findings_overnight_decision_harvest_2026_05_21.md` — this brief, committed as a durable record
- `tasks/plan_overnight_decision_harvest_2026_05_21.md` — this plan, committed for traceability

**Hard constraints honored:**
- No code, schema, config, runtime change.
- No trading/paper-trade/scoring/classifier change.
- No KOL/TG pruning. No source ranking.
- No paid API calls. No prod DB writes.
- No `_fetch_snapshot_rows` implementation.
- No live config change.

**Final recommendation: DO NOT BUILD TONIGHT.** Merge 3 design-records, file P5 backlog-cleanup docs PR, surface data-bound gates. The next implementation decision is made by the operator after the actionability n≥20+5 gate triggers OR after operator authorizes the vendor sample call for price-coverage expansion.

## Reviewer attack vectors to apply

**Vector A — strategy/statistical validity, stop-build discipline, data sufficiency:**
- Is the actionability re-check gate (n≥20+5 OR ≥1 winner) defensible, or does it implicitly endorse the 86% win-rate observation as already conclusive?
- Does any merge recommendation (e.g., #184 TG/X linkage design) silently endorse a substrate that's blocked by coverage?
- Is "do not build tonight" actually the bias-protected answer, or am I avoiding work the operator wanted done?
- Are the P3 close-recommendations (#34, #105) premature without operator input?

**Vector B — Hermes-first/drift/runtime-state/operational safety:**
- Are prod-vs-master drift findings complete? Is the `chmod +x` mode-bit explanation correct?
- Is the Hermes narrative cron health verification adequate (`jobs.json last_status=ok` + 92 inbound rows / 24h)?
- Does the P5 docs-only PR have any path that could accidentally touch runtime?
- Is the cron tick-count irregularity safe to defer 7 days, or should we file a §12a follow-on now?
