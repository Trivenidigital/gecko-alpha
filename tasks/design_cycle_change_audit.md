**New primitives introduced:** NONE — read-only audit doc. Companion to `tasks/plan_cycle_change_audit.md`. Captures the cross-cutting decisions that don't fit in step-by-step procedure.

# BL-NEW-CYCLE-CHANGE-AUDIT — Design (v2, folded design-review)

## 1. The reframe (load-bearing)

The backlog filing for `BL-NEW-CYCLE-CHANGE-AUDIT` (`backlog.md:326`) framed this as: "`SCAN_INTERVAL_SECONDS` decreased from 300s to 60s — audit what design-time math broke." Verified via `git log --all -S "SCAN_INTERVAL_SECONDS" -- scout/config.py`: the only commit touching that line is `bbf6810` (the initial coinpump-scout scaffold import, 2026-03-20), where the value was already `60`. **gecko-alpha has never had a 300s cycle.**

The "300s era" referenced in the backlog (and in BL-053's design doc) is **coinpump-scout's** history. gecko-alpha was scaffolded from coinpump-scout, inheriting design docs and code patterns written in coinpump-scout's 300s context. The actual failure pattern is: **inherited design-doc math that assumed the upstream's 300s cycle was carried into gecko-alpha without re-doing the math against gecko-alpha's actual 60s cycle.**

**Consequences:**

1. **Per-module assumed cycle is per-design-doc, not global.** Each module must be located individually. Some assume 300s (coinpump-scout heritage); some 60s (gecko-alpha native); some have no documented assumption.

2. **The "no documented assumption" case is itself a finding.** A math claim without a stated assumption is brittle. Same shape as the §9b structural-attribute-verification rule.

3. **No intent attribution.** Plan v2's "inherited / native / unstated" attribution is unfalsifiable from code alone. v2 design (per reviewer rigor fold) replaces it with a binary `documented_cycle_assumption: {value | absent}` column. The "absent" case is the actionable finding.

## 2. Five-bucket classification (v2)

| Bucket | Definition | Numeric rule | Example |
|---|---|---|---|
| **Phantom** | ≥ 3× headroom vs constraint; constraint is documented and stable. | min(headroom_per_burst_window, headroom_hourly) ≥ 3× | CoinGecko trending fetch at 12/min vs 30/min documented limit (2.5×) — wait, that's Watch. Re-pick example after Tier B probe. |
| **Phantom-fragile** | ≥ 3× headroom, BUT constraint is **undocumented / volatile / unverified-since-design-time**. Source field required (see §3). | Same numeric as Phantom; differs only in constraint-stability column. | Any DexScreener / GeckoTerminal / GoPlus / MiroFish call (no published rate card). |
| **Watch** | 2×–3× headroom. One regime shift or burst tips it. | 2× ≤ headroom < 3× | (TBD per Tier B probe) |
| **Borderline** | Headroom < 2×, OR matches/just-fits constraint, OR fits inside a banded constraint with margin against the **lower bound** of the band. | headroom < 2× against constraint (or band-lower-bound) | **BL-053**: 60 req/hr inside 50-200/hr band. 60/50 = 1.2× against the lower bound of the band → Borderline. The upper bound is "best-case provisioning," not the margin. |
| **Broken** | Exceeds constraint, OR violates documented operator-experience target. | headroom < 1× | (per Tier B probe) |
| **Unfalsifiable** (meta) | No documented constraint (operator-experience target absent). | N/A | Anthropic spend per alert — no target documented; cannot classify Phantom/Broken. |

**Critical rules (v2):**

- **Lower-bound rule for banded constraints**: when a provider publishes a band (e.g., CryptoPanic free-tier 50-200 req/hr), the constraint for headroom math is the **lower bound** of the band (the rate at which throttling may begin), not the upper. The upper bound is best-case provisioning; it does not contribute to the margin denominator.
- **Burst-window rule**: classification must compute headroom against the constraint's natural window (per-second for Telegram per-chat 1/sec; per-minute for CoinGecko 30/min; per-hour for CryptoPanic free-tier; per-day for Anthropic tier). Hourly-mean classification can hide per-second/per-minute Broken sites.
- **p95 over mean rule** (sub-loop fan-out): when `tokens_per_cycle` has high variance, classify on **p95** not mean. Silent failures hide in the p95 tail. If mean is Phantom but p95 is Broken, verdict is **Broken — intermittent**.
- **No-bucket-collapse rule**: Phantom + undocumented constraint ≠ Phantom-fragile. The qualifier is orthogonal — describes constraint stability, not margin. Both can co-occur on the same finding row.

## 3. Source-evidence requirement (Phantom-fragile)

Plan-review reviewer rigor fold: "history of unilateral tightening" is unsourced absent concrete evidence. Acceptable sources:
- **Published changelog entry** (cite URL + date)
- **Dated provider blog post** announcing rate-card revision
- **Dated GitHub issue** with provider-staff confirmation of a change
- **`journalctl` 429-burst** from a specific date window where the rate didn't change but enforcement did

The findings table must have a **`source` column** for each Phantom-fragile assertion. Unsourced "fragile-by-feel" assertions are not acceptable.

**Per-provider attribution table** (v2 fold, default classification, refine per audit-time evidence):

| Provider | Default fragility | Source / rationale |
|---|---|---|
| CoinGecko | **Phantom-stable** | Documented Demo tier (30/min); public changelog |
| DexScreener | **Phantom-fragile** | No public rate card; band inferred |
| GeckoTerminal | **Phantom-fragile** | Same as DexScreener |
| GoPlus | **Phantom-fragile** | Free tier limits opaque; per-token fan-out amplifies |
| Helius | **Phantom-stable** | Published per-plan limits |
| Moralis | **Phantom-stable** | Published CU model |
| Anthropic | **Phantom-stable** | Documented tier limits + 429 semantics |
| MiroFish | **Phantom-fragile** | Project-internal; no public contract |
| CryptoPanic | **Phantom-fragile-with-band** | Documented band (50-200/hr); lower-bound unknowable per key |
| Telegram | **Phantom-stable** | Bot API documented (30/sec global, 1/sec per chat) |
| Discord webhook | **Phantom-stable** | Documented + retry-after headers |

## 4. Severity → urgency mapping (v2 fold)

| Verdict | Urgency / response time | Carry-forward shape |
|---|---|---|
| **Broken** | Fix within **1 week** | File BL-NEW-* with concrete fix plan; remediation PR ships within the week |
| **Borderline** | File BL-NEW-* within **2 weeks**; ship within 30 days unless evidence-gated | Same shape as the parse-mode audit's HIGH ACTUAL handling |
| **Watch** | **90-day sunset rule** — close-or-act by D+90, else demote to Phantom (with evidence) or promote to Borderline | Watch is a hold-state, not a forever-state |
| **Phantom-fragile** | **6-month sunset OR re-audit at next deploy / external integration ship** | Use the "next-audit trigger" at §11 |
| **Phantom** | No action | Document for traceability only |
| **Unfalsifiable** | File BL-NEW-* for **operator-target elicitation** with a **proposed-target-skeleton** (see §5) | Tracks the absence as an actionable item |

Each carry-forward row in the findings doc must specify a `decision-by:` field — either a date or a trigger (`evidence-gated: re-evaluate at n≥X fires`, `deploy-gated: next service restart`, etc.). "No decision-by" is the failure mode that turns Watch into permanent backlog drift.

## 5. Operator-target absence — proposed-target-skeleton

The audit explicitly does not fabricate heuristics (per "flag absence" rule). But Unfalsifiable findings still need a path forward, so each must include a **proposed-target-skeleton** — a starting point for the operator decision, not a fabricated answer.

Example skeleton for Anthropic spend per alert:
```
Proposed target (operator decides):
  - Soft cap: $5/day Anthropic API spend across counter-arg follow-ups
  - Alert threshold: $20/day
  - Source: not yet set; operator may anchor to prior spend (grep journalctl /
    cost.anthropic.com dashboard) or aspirational budget
```

The skeleton is a starting point; the operator accepts, modifies, or rejects. The audit does not commit the skeleton's numbers as truth.

## 6. Cross-audit resolution rule

For sites that appear in BOTH `findings_silent_failure_audit_2026_05_11.md` AND this audit:

- **Findings compose; they do not collapse.** Each audit answers a different question (table-freshness vs assumption-validity). A site can have two non-conflicting findings: "writer status: deactivated" (silent-failure) AND "math: Borderline if reactivated" (cycle-change). Both live in their respective findings docs; both are referenced from each.

- **Resolution status is per-finding**, not per-site. Closing one finding does not close the other.

Concrete example (cryptopanic): silent-failure audit §2.2 closed-as-operator-decision-resolved (deactivated). This audit's Tier B finding: "if reactivated at current 60s cycle, math hits Borderline (60 req/hr vs 50/hr free-tier band lower bound)." Cross-reference cite. The cryptopanic remediation PR (operator's eventual `.env` edit) should now include a throttle decoupling the cycle frequency from the cryptopanic fetch rate, **as called out in BL-053's existing 5-point activation checklist** (`decoupled interval` is item 4 in that checklist per the silent-failure audit closure).

## 7. "Handler exists" is never sufficient

429-retry handler existence proves only that the author anticipated 429s. It does not prove the math is below the limit. Always tag as "constraint inferred, not measured."

**Silent-throttle discriminator**: absence of 429s in `journalctl` is ambiguous between "well under limit" and "provider degrades silently (latency creep without 429)." When journalctl-429 count is zero, **cross-reference response-latency p95 over time** — a silent throttler shows latency creep. Add this as a fallback signal.

## 8. Load-bearing assumptions (v2 fold)

| Assumption | Defensibility | Mitigation if wrong |
|---|---|---|
| Per-token loops in `run_cycle` dominate scaling | Defensible **IF** Tier-D launched-once-at-startup verification is rigorous (Plan task 4.1b) | If accidentally inside `run_cycle`, classify per Tier B2 |
| External rate limits are the dominant constraint | **Undefended** — non-external constraints (SQLite WAL throughput, Telegram per-chat 1/sec, file-descriptor exhaustion, asyncio task queue depth) are unmodeled | Plan v3 adds a non-external-constraint sub-scan (task 5.0) |
| Documented limits are stable enough to base verdicts on | This is exactly what Phantom-fragile names | Source field requirement enforces it |
| 60s is the current cycle | Defensible (git log verified) | None needed |
| `tokens_per_cycle` measurement window captures regime | **Fragile** — 7d may miss volatile market regime; mean under-represents peak-hour bursts | Plan v3 widens probe window to 30d; classifies on p95 |

## 9. What this audit is NOT doing (explicit non-goals)

- **Not implementing fixes.** Audit produces findings + backlog filings; remediations are separate PRs.
- **Not building a CI guard.** No mechanical enforcement.
- **Not back-dating design docs.** Audit notes inheritance vs native; does not rewrite docs.
- **Not measuring runtime cost.** Computes per-cycle math; not actual CPU / network / DB cost.
- **Not validating the 60s cycle itself.** Takes 60s as given.
- **Not re-calibrating Tier F settings.** Audit recommends "document the assumption" not "change the value." Re-calibration is a separate PR with its own soak (per actionability-reviewer fold).
- **Not auditing dependencies' design choices.** External-provider rate-limit changes since audit time are out of scope.

## 10. Test plan (audit edition)

A fresh reviewer can:
1. Reconstruct every per-module verdict from the findings doc evidence column.
2. Decide whether to file each carry-forward without needing additional context (each row has `decision-by`, `fix-shape`, `effort-class`).
3. Find ≤30-min-fix items in the **Quick wins section** at the top of the findings doc (so they ship same-week).

## 11. Next-audit trigger (v2 fold)

This audit sunsets when its findings drift again. Re-run when **any** of:
- `SCAN_INTERVAL_SECONDS` changes value
- A new external-API integration ships (new ingestion lane, new LLM provider, etc.)
- A new `*_CYCLES` setting is introduced
- > 6 months elapse since this audit (calendar drift)

Trigger language in the carry-forward section: `next-audit-trigger: SCAN_INTERVAL_SECONDS change OR new external API OR 2026-11-13 (6mo)`.

Cheaper than a CI guard; catches the drift this audit exists to surface.

## 12. §9b promotion data point — placement

This audit produces a §9b data point: "the audit's own backlog entry contained a wrong premise (assumed 300→60 transition that never happened); plan-review caught it via 1-line `git log`."

**Placement**: that data point lives in `feedback_section_9_promotion_due.md` (memory updates as a separate concern), not in this findings doc body. Findings doc stays single-purpose (per-module classification). Cross-reference: the findings doc's "Critical reframe" section cites the §9b promotion file.

## 13. Rollback semantics

Doc-only PR. `git revert <merge-sha>` restores prior backlog statuses + removes findings file. No production impact.

## 14. Cost analysis

- **Plan + design + 2 review rounds:** ~3 hours (this work).
- **Audit execution:** ~3-4 hours (Tier A–F walkthrough + prod-DB probe + classification + quick-wins triage).
- **PR review + fold:** ~1-2 hours.
- **Operator-side:** 0 (no deploy).

Value: surfaces silent-degradation candidates; each surfaced finding shrinks future audit cost (cycle assumptions documented going forward); §9b promotion gains data point.
