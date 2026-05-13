**New primitives introduced:** NONE — read-only audit doc. Companion to `tasks/plan_cycle_change_audit.md`. Captures the cross-cutting decisions that don't fit in step-by-step procedure.

# BL-NEW-CYCLE-CHANGE-AUDIT — Design

## 1. The reframe (load-bearing)

The backlog filing for `BL-NEW-CYCLE-CHANGE-AUDIT` (`backlog.md:326`) framed this as: "`SCAN_INTERVAL_SECONDS` decreased from 300s to 60s — audit what design-time math broke." Verified via `git log --all -S "SCAN_INTERVAL_SECONDS" -- scout/config.py`: the only commit touching that line is `bbf6810` (the initial coinpump-scout scaffold import, 2026-03-20), where the value was already `60`. **gecko-alpha has never had a 300s cycle.**

The "300s era" referenced in the backlog (and in BL-053's design doc) is **coinpump-scout's** history. gecko-alpha was scaffolded from coinpump-scout, inheriting design docs and code patterns written in coinpump-scout's 300s context. The actual failure pattern is: **inherited design-doc math that assumed the upstream's 300s cycle was carried into gecko-alpha without re-doing the math against gecko-alpha's actual 60s cycle.**

This reframe is not a footnote — it is the audit's substantive thesis. Two consequences:

1. **Per-module assumed cycle is per-design-doc, not global.** The audit cannot assume "everything was designed at 300s." Some modules' designs explicitly assume 60s (because they were authored after the project's actual cycle was known); some inherit 300s (because they were authored against coinpump-scout's heritage); some have no documented cycle assumption at all. **Each module must be located on this spectrum individually.**

2. **The "no documented assumption" case is itself a finding.** A math claim ("12 req/hr") without a stated assumption ("at 300s cycle") is brittle by construction — the assumption can be silently invalidated by deployment-context drift. This is structurally identical to the §9b structural-attribute-verification rule: lever/data-path/constraint must be triangulated, not assumed.

## 2. Four-bucket classification — defensibility

The plan defines four buckets + one qualifier:

| Bucket | Definition | Defensibility |
|---|---|---|
| **Phantom** | ≥ 3× headroom vs **documented** constraint; constraint is stable. | Standard "no concern" classification. |
| **Phantom-fragile** | Same as Phantom, but constraint is undocumented / volatile / from a provider with history of unilateral tightening (DexScreener, GeckoTerminal, GoPlus all qualify per known practice). | Plan-review reviewer methodology fold. Addresses the case where 3× headroom is illusory because the denominator (constraint) is a guess. Composable with §9b: "the lever I'm relying on (margin) is real only if the constraint is stable." |
| **Watch** | 1.5×–3× headroom. One upstream regime shift or burst tips it. | Plan-review fold; filled the gap between Phantom and Borderline. Real "silent middle zone" otherwise lost. |
| **Borderline** | < 1.5× headroom, or current math matches/just-fits the constraint. | BL-053 at 60/200 = 30% of band is canonical Borderline (well into the band but not exceeding). |
| **Broken** | Exceeds constraint, or violates documented operator-experience target. | Hard fail; needs immediate remediation. |

**Why not collapse Watch into Phantom-fragile?** The two are orthogonal:
- Watch describes **margin** (numerically close to the constraint).
- Phantom-fragile describes **constraint stability** (denominator is fuzzy).

A module can be Watch + documented-stable-constraint (numerically tight but predictable), or Phantom + undocumented-volatile-constraint (numerically loose but the denominator could change). Distinct concerns; distinct remediation shapes.

**Why not add a sixth "no math claim at all" bucket?** Captured instead as the "unstated assumption" finding — that's a *meta*-finding about the audit's evidence, not a verdict about the module's behavior. Classified separately in the carry-forward section.

## 3. The "flag absence, don't fabricate" principle

For operator-experience targets (max alerts/hour acceptable, max LLM spend/day acceptable, etc.), the audit explicitly forbids fabricating heuristics. The original plan's "5–10× scaling is acceptable, >10× is broken" heuristic was rejected by plan-review reviewer methodology — it's undefended and would inflate the Broken count with false positives.

Instead: if a target is documented (in `tasks/lessons.md`, memory `feedback_*.md`, commit history, backlog "deferred for noise" entries), cite it. If not, **the absence is the finding** — a non-cycle-related but real audit output (the operator should set explicit targets so future cycle / deployment changes have falsifiable success criteria).

This is consistent with the project's broader pattern: the §9 family of rules + the silent-failure audit produced explicit telemetry / SLO requirements; this audit produces explicit operator-target requirements.

## 4. Cross-audit composition

This audit composes with two prior audits:

- **`findings_silent_failure_audit_2026_05_11.md`** — table-freshness-based. Asks: "does the writer still produce rows?" Some sites are in both; cross-reference rather than re-litigate.
- **`findings_parse_mode_audit_2026_05_12.md`** — class-3 rendering hygiene. No overlap with cycle-change scope, but the audit-methodology lesson (grep `send_telegram_message` only missed `send_alert`) is structurally identical to this audit's gap: a too-narrow grep pattern misses the actual instances. This audit's plan-review-fold extended grep is the same shape of correction.

## 5. §9b promotion data point

`feedback_section_9_promotion_due.md` proposes promoting §9b (structural-attribute verification) to global CLAUDE.md §9.5. This audit adds a new data point: the audit's own backlog entry contained a wrong premise (assumed 300→60 transition that never happened in gecko-alpha). That premise was the proposal text's structural attribute, accepted without verification at proposal time. Plan-review caught it via 1-line git query; the fix was a substantial reframe.

Composition note for the findings doc: **the audit-methodology cost asymmetry is real even at proposal-text level** (~30 seconds to `git log` the SCAN_INTERVAL line vs days-to-weeks of audit work pointing at the wrong assumption). The §9b promotion's "verify structural attributes before reasoning from them" rule applies to PROPOSAL text just as it applies to code.

## 6. What this audit is NOT doing (explicit non-goals)

- **Not implementing fixes.** Audit produces findings + backlog filings; remediations are separate PRs with their own ship paths.
- **Not building a CI guard.** No mechanical enforcement of "every cycle-coupled call site declares its cycle assumption." That'd be a useful follow-up, but out of scope.
- **Not back-dating design docs.** The audit notes which assumptions are inherited from coinpump-scout vs native-gecko-alpha. It does not rewrite the design docs.
- **Not measuring runtime cost.** The audit computes per-cycle math (calls/hr, writes/hr); it does not benchmark actual CPU / network / DB cost.
- **Not validating the 60s cycle itself.** The audit takes 60s as given and asks "does each module's math hold at 60s?" — it does not propose that 60s be changed.
- **Not auditing dependencies' design choices.** If a provider (CoinGecko, GoPlus) silently changes its rate limit, that's external; the audit captures the current documented (or inferred) limit at audit time.

## 7. Sub-loop fan-out — methodology load-bearing

Plan-review reviewers (both vectors) flagged this as the dominant scaling axis. Per-token loops in `run_cycle` (e.g., GoPlus per candidate, Helius/Moralis per enriched token) scale as:

```
rate = cycles_per_hour × tokens_per_cycle × calls_per_token
```

At 60s cycle, `cycles_per_hour = 60`. The dominant unknown is `tokens_per_cycle`, which is:
- The aggregator output size, post-MIN_SCORE filter
- Variable: depends on ingestion volume + scoring distribution
- **Must be measured, not assumed.**

The plan's Task 2 (Tier B2) probes prod-DB for hourly candidate count and divides by 60. Variance is real (peak hours vs off-peak); the audit should record both mean and approximate p95.

If `tokens_per_cycle` is N, then a Tier-B per-token call that fans out to GoPlus + Helius + Moralis is:
- `60 × N × 1` (per provider per token) = `60N` calls/hr per provider
- At N=50 (reasonable estimate without DB probe): 3000 calls/hr per provider

This dwarfs the top-level Tier-B per-cycle math (60 calls/hr for non-fan-out paths). **The audit's classification verdicts will be dominated by sub-loop fan-out math for any per-token call.**

## 8. Test plan (audit edition)

The audit produces a single doc; "testing" reduces to verification that:
1. Every Tier-B/B2/C/C2/E/F site listed in the plan is reached by the audit
2. Each non-Phantom finding has supporting evidence (file:line + math + constraint source)
3. Each carry-forward backlog filing is concretely actionable

Acceptance: a fresh reviewer can read the findings doc and (a) reconstruct every per-module verdict, (b) decide whether to file each carry-forward without needing additional context, (c) cross-reference each finding to the originating file:line.

## 9. Rollback semantics

Doc-only PR. `git revert <merge-sha>` restores prior backlog statuses + removes the findings file. No production impact possible. No service restart needed.

## 10. Cost analysis

- **Plan + design + plan-review fold:** ~2 hours (this work).
- **Audit execution:** ~2-3 hours (Tier A-F walkthrough + prod-DB probe + classification).
- **PR review + fold:** ~1-2 hours.
- **Operator-side**: 0 (no deploy).

Value: surfaces N silent-degradation candidates that would otherwise drift further. Each surfaced finding shrinks future audit cost (cycle assumptions are documented going forward via the carry-forward backlog items). §9b promotion candidate gains another data point — the audit's own framing error is empirical evidence for the rule.
