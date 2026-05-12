**New primitives introduced:** NONE — sweep doc only. Identifies candidate §12c-narrow instances + sibling-pattern observations for the dedicated §12c promotion session.

# Finding: §12c-narrow pre-emptive sweep (2026-05-12)

**Purpose.** Pre-promotion sweep to test the generality of the proposed
§12c-narrow rule before adding it to global CLAUDE.md. Two instances are
already known (perp_anomalies empty-since-deploy + price_cache held-position
staleness). With the rule shape in hand, the risk is **confirmation bias**:
counting any failure that *looks like* §12c-narrow as an instance, when in
fact different rules describe the same surface symptom with different
remediation paths.

## Methodology — the discriminator

**§12c-narrow rule shape (proposed):**

> When monitoring a process or pipeline, the health signal must read the
> specific OUTPUT SUBSET that downstream consumers depend on, not just
> the process-level heartbeat. A heartbeat that correctly reports
> "service running, table being written to, cache being updated" is
> structurally indistinguishable from no monitoring at all when the
> failure mode is "a specific subset of the output is dead while the
> aggregate looks fine."

**Critical discriminator: REMEDIATION SHAPE, not symptom shape.**

For each candidate site, the test is:

> *Would "read the specific subset's output, not aggregate health" have
> prevented or detected the failure?*

- If **YES** → genuine §12c-narrow.
- If a **different fix** would have worked (parse_mode hygiene, schema
  validation, deploy-time activation check, fill confirmation against
  exchange, threshold-monitoring-on-existing-signal, schema retirement
  decision, etc.) → surface-similar-but-different-rule.
- If the surface is healthy → clean.

The trap: many failures have the §12c-narrow SHAPE (general health green,
specific subset broken) but the right remediation lives in a different
sibling rule. Counting these as §12c-narrow inflates the evidence base for
a rule they don't actually belong to.

**Negative-space visibility.** This sweep lists everything inspected, not
just hits. An operator reading "X §12c-narrow hits out of Y candidates
inspected" learns more than "X hits" alone — the ratio is information.

## Candidate enumeration + classification

**Source base.**
- `tasks/findings_silent_failure_audit_2026_05_11.md` § Class 1 (writer
  stopped, nobody watched) findings — 7 findings already taxonomized
- `tasks/findings_silent_failure_audit_2026_05_11.md` § Class 2 + parse_mode
  Class 3 findings — known siblings
- `scout/heartbeat.py` — canonical heartbeat module, 10 counters
- Explore agent enumeration of health-claim sites — 23 candidate surfaces
- Watchdog scripts under `scripts/`

### Bucket A — Genuine §12c-narrow (high confidence)

**A1. `perp_anomalies` empty-since-deploy** (audit §2.6, 2026-04-20 → 2026-05-11, 20+ days). Watcher heartbeat reports "perp_watcher_stats" every 60s with counters (parse_rejects, exchange_errors, etc.); the *table* `perp_anomalies` was empty the whole time. Health signal = process-level liveness. Output the consumer depends on = `perp_anomalies` rows feeding `quant_score` enrichment. Remediation: monitor table row-rate, not process liveness. **This is INSTANCE #1.**

**A2. `price_cache` held-position staleness** (just-shipped fix, 2026-05-12). `price_cache_updated` log reports rows-updated-this-cycle (always > 0 if any ingestion lane has data); held-position subset can be stale 24h+. Health signal = aggregate cache write activity. Output the consumer depends on = per-held-token freshness. Remediation: per-held-token freshness watchdog (shipped) + held-position refresh lane. **This is INSTANCE #2.**

### Bucket B — Likely §12c-narrow (needs verification)

**B1. `holder_snapshots` empty** (audit §2.5). Per the audit's Class-1 taxonomy: writer exists (BL-020), table is empty. Need to verify whether this is "ship-time never-worked" (would be §12c-narrow if a per-table watchdog would have caught) vs "deploy-without-activate" (sibling pattern — see B-sib1 below). The audit doesn't fully diagnose; flagged as "Unknown (BL-020 never wired?)". **Promotion-evidence verdict pending diagnosis.** Treating as Likely (not Confirmed) until the diagnosis distinguishes never-wired vs flag-default-off.

**B2. heartbeat counter increment-vs-DB-row divergence** (general pattern across `scout/heartbeat.py` increments — `mcap_null_with_price_count`, `slow_burn_detected_total`, `slow_burn_coins_skipped_total`). Counters increment per detection event, NOT per successful DB row insert. A DB write failure (FK constraint, lock contention) would increment the in-memory counter while no row reaches the table. This IS §12c-narrow (signal-reads-in-memory-state, consumer-reads-table-rows). **No confirmed prod incident.** Speculative until an actual divergence is observed. Listed for completeness; does NOT count toward promotion evidence without a concrete incident.

### Bucket C — Surface-similar, different rule

These are §12c-shaped (heartbeat says one thing, specific output says another) but the right remediation is NOT "read the specific output subset." Each one belongs to a different rule.

**C1. `alerts` table — 2 rows total** (audit §2.1). Looks like §12c-narrow (heartbeat fires, `alerts` table dead). Actual remediation per audit's Legacy-displaced classification: **schema retirement decision.** The writer is supposed to be off — newer signal types route through `tg_alert_log` instead. The right fix is documenting the retirement, not monitoring a dead writer. **Rule: schema-state-retirement-decision.**

**C2. `outcomes` table — 2 rows total** (audit §2.4). Same shape as C1 — Legacy-displaced.

**C3. `cryptopanic_posts` empty** (audit §2.2). 22 days zero rows. Per audit's diagnosis: `CRYPTOPANIC_ENABLED=False` and `CRYPTOPANIC_API_TOKEN=""` in prod `.env` — deploy-without-activate. Reading the table's row-rate would have surfaced the empty state, but the **right** remediation is "post-deploy ritual that verifies flagged-on features actually fire," not row-rate monitoring. **Rule: deploy-without-activate** (already documented in `feedback_deploy_without_activate_pattern.md`).

**C4. Auto-suspension reversals (trending_catch §2.9)** (audit §2.9). Operator-applied state silently reversed by automated action. Surface health was actually correct (system reported "trending_catch auto-suspended"); the failure was **rendering corruption** — the TG alert used `parse_mode=Markdown` and underscores in `trending_catch` were consumed as italics markers, mangling the message body to "trendingcatch ... (hardloss)". Different surface entirely. **Rule: parse_mode hygiene (Class-3 silent rendering)** — already documented in `feedback_class_3_silent_failure_rendering_corruption.md` and `feedback_resilience_layered_failure_modes.md`. NOT §12c-narrow.

**C5. correction_counter fill-without-verify** (Explore agent #6). Counter increments on `status='filled'` from adapter, but doesn't verify the fill against the exchange. A silently-rejected order would increment the counter. Remediation: post-fill reconciliation against exchange position, not output-subset monitoring. **Rule: signal-doesn't-verify-result.**

**C6. Venue health probe timeout-vs-auth conflation** (Explore agent #11). On timeout, `auth_ok=1` is recorded because the probe can't distinguish auth failure from timeout. Remediation: distinguish timeout from auth failure with separate error paths. **Rule: signal-conflates-failure-modes.**

**C7. Dormancy flag attempt-vs-genuine** (Explore agent #12). `is_dormant=1` set on 30-day-zero-fills, doesn't distinguish "genuinely dormant" from "attempted but all orders failed." Remediation: separate attempted-but-failed counter. **Rule: signal-doesn't-distinguish-cause.**

**C8. Dashboard `pipeline_running` proxy** (Explore agent #13). Reads `candidates.first_seen_at < 180s ago` as proxy for "pipeline running." False negative when no new tokens discovered in 3 cycles even though loop is alive. Remediation: monitor the loop directly (e.g., heartbeat-file mtime), not a proxy table. **Rule: signal-uses-proxy-not-actual.**

**C9. `signal_type` coercion to "unknown"** (Explore agent #21). Null/empty signal_type silently maps to "unknown" bucket, collapsing multiple distinct silent-signal-type cases. Remediation: refuse-or-flag null at write site. **Rule: signal-loses-cardinality.**

**C10. Live trading `open_count` gate** (Explore agent #20). Counts `live_trades.status='open'` rows; doesn't verify position exists on exchange. Ghost-position pattern. Remediation: exchange-side position reconciliation. **Rule: signal-trusts-internal-state-without-exchange-confirmation.**

**C11. Heartbeat-existing-but-not-thresholded** (general pattern across `scout/heartbeat.py`). `alerts_fired: 0` in every heartbeat for 9 days produced no alert because **no threshold is configured**. The signal IS reading the right granularity here (DB row count) — it's the absence of threshold-based alerting that's the gap. Remediation: thresholded alerting on existing counters. **Rule: signal-without-actionable-threshold.** This is a sibling rule that probably deserves its own promotion candidacy after sweep. It's also the most general pattern in this list — many of the gecko-alpha audit findings reduce to "the counter existed but no one alerted on it."

### Bucket D — Clean

**D1. Perp baseline eviction** (Explore agent #8). Deterministic state cleanup; no output-truth dependency.

**D2. `gecko-backup-watchdog.sh`** (Explore agent #4). The heartbeat file is touched ONLY on rotation success (verified in script). False-positive alert is impossible — if file is fresh, rotation succeeded. Clean.

**D3. `held-position-price-watchdog.sh`** (Explore agent #5). Just shipped; reads the specific output subset (per-held-token `price_cache.updated_at`) directly. By construction, it's a §12c-narrow REMEDIATION, not an instance.

**D4-D6. Tg social listener_state, signal_params flag_enabled, dashboard system_health.** All are accurately-reported aggregate states. Operator reading them does NOT naturally infer a different specific-subset claim. Different rule shapes may apply (e.g., `flag_enabled` is a config-runtime divergence, not a §12c-narrow issue), but the surface itself isn't lying.

### Bucket E — Out of scope for sweep

**E1. §2.7 memecoin chain auto-retirement** (audit §2.7). The failure mode is downstream of §2.4 (`outcomes` table). The remediation is upstream — fix the data path that feeds chain dispatch. Not a separate instance at the visible site. The audit's classification treats it as a side-effect, not a primary finding.

**E2. §2.3 `shadow_trades` correctly idle.** Audit reframed this from "broken writer" to "policy-blocked unlock." Not a bug; not a §12c-narrow candidate.

## Promotion-evidence summary

**Total candidate sites inspected: 26**
- Audit findings (Class 1+2+3): 9
- Explore agent enumeration: 23 (3 overlapping with audit, deduplicated)

**Classification:**
- **Genuine §12c-narrow:** 2 (the two already-known instances; perp_anomalies + price_cache held-position)
- **Likely §12c-narrow (needs verification):** 2 (holder_snapshots + heartbeat counter increment-vs-DB-row, the latter speculative without concrete prod incident)
- **Surface-similar-but-different-rule:** 11 (C1-C11) — distributed across at least 8 distinct sibling rules
- **Clean:** 6 (D1-D6)
- **Out of scope:** 2 (E1-E2)

**Sweep verdict against pre-registered criteria:**

| Criterion | Match |
|---|---|
| 3-5 additional genuine instances → **promote** | ✗ (sweep found 0 confirmed additional; 2 unconfirmed candidates) |
| 0-1 additional → **rule is real but rare; reconsider threshold** | ✓ (closest match — 0 confirmed additional) |
| 2 additional in-between → **judgment call** | partial (only if both B1 + B2 confirm) |

**My read:** the rule is REAL (the two known instances are structurally clear) but **RARER than the broad sweep suggests**. The high-frequency pattern across gecko-alpha is NOT "monitoring at the wrong granularity" but rather **"signal exists, alert action absent"** (C11) — that's its own pattern with much broader applicability.

**Recommendation for the dedicated §12c promotion session:** 

1. **Verify B1 (holder_snapshots) diagnosis first.** If it's confirmed §12c-narrow (writer was supposed to fire but never did, and a per-table watchdog would have caught), promotion to **3 confirmed instances** is well-founded.
2. **Promote §12c-narrow with the n=2 (or n=3 if B1 confirms) caveat made explicit** — the rule is real but instances are rare, so the global rule's value is "make the watchdog co-shipping cheap to do consistently" not "catch a frequent recurring bug class."
3. **OR defer promotion** until a third independent instance surfaces organically — preserves the conservative posture and avoids overclaiming.

## Sibling-pattern observations (high-value side findings)

The sweep surfaced multiple genuine sibling patterns that may themselves be promotion candidates. The most promotion-worthy:

**Signal-without-actionable-threshold (C11).** Already-documented existing counters in `scout/heartbeat.py` produced no alerts despite long zero-streaks. `alerts_fired: 0` for 9 days. `narrative_predictions: 0` for 4 days during Anthropic credit dry. This is structurally distinct from §12c-narrow (the signal IS reading the right granularity; the gap is action). Promotion candidate: **§12e — every monitored counter must have an actionable threshold OR be explicitly classified as informational-only.** Three+ instances in the audit alone.

**Signal-doesn't-verify-result (C5, C10).** Pattern: in-process state mutation without external-system reconciliation. correction_counter on fill; live trading open_count vs exchange position. Two instances; probably more in money-flow paths.

**Deploy-without-activate (C3).** Already documented as `feedback_deploy_without_activate_pattern.md`. Worked example: cryptopanic.

**Class-3 rendering corruption (C4).** Already documented; instance count = 1 (trending_catch auto_suspend). May be unique to that case.

## Meta-result: taxonomy completeness

11 surface-similar cases distributed across 8+ candidate rules is itself a
structural finding. The audit's `§0.5 Classification update` block enumerated
**5 root-cause shapes** (Legacy-displaced, Ship-time never-worked, Auto-
retirement loop, Low-frequency probably fine, Real Class-2 bug). The sweep
surfaced rule candidates that don't slot cleanly into those 5:

| Candidate rule | Instances from this sweep | Status |
|---|---|---|
| §12a (pipeline table freshness SLO + watchdog) | known, already promoted | promoted |
| §12b (alert at write-time for operator-state reversal) | 1 (audit §2.9) | promoted |
| §12c-narrow (health-claim vs specific output subset) | 2 confirmed | promotion candidate, see §12c-promotion-evidence memory |
| §12e candidate (signal-without-actionable-threshold) | **3+** (`alerts_fired`, `narrative_predictions`, `mcap_null_with_price`) | promotion candidate, see §12e-candidate memory |
| Legacy-displaced (schema retirement decision) | 2 (alerts, outcomes) | holding-thread; may be 1-off |
| Parse_mode rendering corruption (Class 3) | 1 known (trending_catch) | holding-thread; **parse-mode audit pending** to determine count |
| Deploy-without-activate | 1 (cryptopanic) | already documented as feedback memory |
| Signal-doesn't-verify-result | 2 (correction_counter fill, live open_count) | holding-thread; both money-flow |
| Signal-conflates-failure-modes | 1 (venue health probe timeout/auth) | holding-thread |
| Signal-loses-cardinality | 1 (signal_type → "unknown" coercion) | holding-thread |
| Signal-uses-proxy-not-actual | 1 (dashboard `pipeline_running`) | holding-thread |
| Signal-doesn't-distinguish-cause | 1 (dormancy attempted vs genuine) | holding-thread |

**The audit's 5-root-cause classification is under-resolved at the rule
layer.** The right read isn't "5 patterns total" but "5 highest-frequency
patterns + 7-8 additional candidate patterns with single-instance evidence
each." Promotion candidates ≥2 instances:

1. **§12e** (signal-without-threshold) — 3+ instances, broadest
2. **§12c-narrow** — 2 instances, narrow surface area
3. **Legacy-displaced** — 2 instances, but may be specific to the
   memecoin→paper-trade pipeline-displacement era; not a recurring
   structural pattern
4. **Signal-doesn't-verify-result** — 2 instances, both money-flow;
   worth tracking for a 3rd

**Sibling-pattern observations beyond promotion-candidate scope** —
the single-instance cases (signal-conflates-failure-modes, etc.) are
holding-thread material. Re-evaluate after each new audit cycle whether
any have accumulated a second instance.

## Note on the §12a vs §12e collapse question

Operator-surfaced 2026-05-12 (sweep review): is §12e (signal-without-
actionable-threshold) a sibling rule, or is it §12a generalized to all
counters (not just table-freshness counters)? The mechanism is the same
(signal + threshold + actor pulling action), but the scope differs (any
observability counter vs just table-write-rate counters). If §12e is a
one-line scope expansion of §12a, the right move is to amend §12a in
the global CLAUDE.md, not add a sibling rule.

Resolution deferred to the dedicated promotion session. The 3+ §12e
instances (`alerts_fired`, `narrative_predictions`, `mcap_null_with_price`)
are evidence either way — the question is whether existing §12a, with
its narrower table-freshness scoping, would cover them under a generous
reading.

## Carry-forward

1. Verify B1 (`holder_snapshots`) diagnosis before next promotion session
2. Decide §12a-vs-§12e at the promotion session — sibling rule, §12a
   scope expansion, or defer
3. Run BL-NEW-PARSE-MODE-AUDIT (audit-only scope, separate finding doc) —
   determines whether parse_mode hygiene is a 1-instance holding-thread
   or a promotion candidate
4. Re-evaluate signal-doesn't-verify-result holding-thread after the
   next money-flow audit cycle; both current instances are in live
   trading code
5. Re-run this sweep if a third independent §12c-narrow instance
   surfaces organically — that's the trigger to revisit §12c
   promotion specifically
