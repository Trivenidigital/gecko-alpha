# Global CLAUDE.md Promotion Drafts — 2026-05-11

**New primitives introduced:** four global CLAUDE.md rule additions (§9b, §9c, §9d, §12c) — NOT yet promoted, drafted for next-session review-and-adopt-or-revise.

**Date:** 2026-05-11
**Status:** DRAFT (not yet adopted into global CLAUDE.md)
**Trigger:** silent-failure audit closure session (PR #107) produced four rule candidates with evidence bases. Synthesis context hot; capturing here before decay so next session has fully-drafted text to review.

---

## Adoption procedure (recommended)

1. Next session opens. Operator reviews this document fresh.
2. For each rule: accept-as-drafted / revise / reject / defer.
3. Accepted rules go into global CLAUDE.md as numbered sections (§9b, §9c, §9d, §12c) alongside existing §9a / §12a / §12b.
4. Rules deferred or rejected: update the rule's memory file with rationale; keep capturing instances until threshold met or rule discarded.

The rule texts below are written to drop into CLAUDE.md verbatim if accepted (with minor formatting adjustments for numbering consistency).

---

## §9b — Structural-attribute verification before shipping a behavior-dependent claim

**Status:** READY FOR PROMOTION. 5 instances documented in `feedback_section_9_promotion_due.md`.

### Proposed CLAUDE.md text

When proposing a change whose downstream value depends on a structural attribute of the system (a database column being populated, a config value matching a known constraint, a function call site being reached, a tier-classification being applied correctly), **verify the structural attribute empirically before shipping the change**. Don't assume — query, grep, or test directly.

The 30-second checklist before any "ship this and X will happen" claim:

1. **Identify the structural assumption** the proposed change rests on. State it in one sentence.
2. **Verify it empirically.** Query the DB, grep the codebase, run a targeted test that produces the attribute as output. The verification step should produce a concrete data point (a row count, a function-found-at-line-N, a config-matches-expected, etc.).
3. **If verification fails:** the proposed change's expected behavior is built on a phantom. Re-scope before shipping.
4. **If verification surfaces a partial truth** (e.g., "attribute exists for some signals but not others"): the change scope shrinks to match the actual coverage.

Cost asymmetry: 15-30 minutes of verification vs. days-to-weeks of vibe-tier-soak waste on a change whose pre-condition didn't hold.

**Composes with §9a (runtime-state verification) and §9c (lever-vs-data-path attribution) — different stages of the same discipline.** §9a verifies *runtime* state before pulling a lever. §9b verifies *structural* state before claiming a behavior-dependent change will work. §9c verifies *attribution* — that the lever you're crediting actually reached the outcome.

### Evidence base

5 documented instances in `feedback_section_9_promotion_due.md`:
1. gainers_early SQL UPDATE — claimed coverage broader than actual coverage
2. BL-074 Minara — claimed CEX coverage that didn't exist
3. Agent #32 v0.1 — claimed tier-classification correctness that wasn't verified
4. BL-075 Phase A — claimed mcap-population coverage that wasn't verified
5. P1-tiered mcap rejection — claimed gate behavior that didn't hold

---

## §9c — Lever-vs-data-path attribution discipline

**Status: ALREADY ADOPTED IN GLOBAL CLAUDE.md** (at lines 192+ — "Post-hoc attribution discipline (backward-looking)"). The 4-instance evidence file `feedback_lever_vs_data_path_pattern.md` is supporting documentation, not a promotion candidate.

**Action:** none. Existing global §9c already codifies this rule. The §9c entry below is preserved for cross-reference visibility only.

Reference: when attributing an outcome to a mechanism, trace the data path end-to-end before crediting the visible lever. The visible lever is rarely the one that controlled the result. Composes with §9a (runtime-state verification, forward-looking) and §9b (structural-attribute verification, sibling). Tonight's session produced one additional suggestive instance — the §2.4 → §2.7 inversion (pre-registered hypothesis "§2.4 feeds §2.7" turned out wrong-direction; real cause was §2.12 upstream cessation). The instance counts toward §9d (hypothesis anchoring) primarily; partial-credit for §9c.

---

## §9d — Pre-registered hypotheses anchor investigation toward confirming them

**Status:** READY FOR PROMOTION. 5 instances documented in `feedback_pre_registered_hypothesis_anchoring.md`. Drafted tonight; reviewable cold.

### Proposed CLAUDE.md text

When an investigation has a pre-registered hypothesis (audit doc, plan, design spec, prior memory, prior conversation turn), **the framing itself shapes what gets verified and what doesn't**. Once a hypothesis is written, subsequent investigation tends to anchor toward confirming it rather than disproving it.

The right answer often emerges only by running the cheap empirical check **past the point where the original frame feels complete** — because completeness against the wrong frame doesn't surface the right one.

The discipline:

1. **Treat pre-registered hypotheses as priors, not destinations.** They commit you to a frame before data arrives — good. They do NOT specify the complete answer space.
2. **When the registered (a)/(b)/(c) checks produce a satisfying classification, ASK:** "what cheap check would distinguish 'genuinely (a/b/c)' from 'fourth case not enumerated' OR 'frame inverted'?" Run that check.
3. **The cheap-empirical-check shapes that surface fourth cases:** prod state inspection, per-component distribution, pre/post-event-X distribution, substrate completeness verification.
4. **Update the pre-registered framing with the discovered fourth case or frame inversion.** Don't force-fit.

**Smell test:** if the investigation feels like it's "clicking into place" with the pre-registered hypotheses unusually well, run one more empirical check beyond the frame's edge. The cost is small; the cost of closing on a wrong frame is the rest of the workstream.

**Composes with §9b (structural-attribute verification) and §9c (lever-vs-data-path attribution).** All three are forms of "verify against reality before locking in the frame." §9b focuses on *structural prerequisites* the change rests on. §9c focuses on *attribution causation*. §9d focuses on *the framing itself* — the hypothesis space the investigation operates within.

### Evidence base

5 documented instances in `feedback_pre_registered_hypothesis_anchoring.md`:
1. §2.9 six-layer investigation (silent-rendering corruption) — six wrong frames before the right one
2. Date reconciliation (2026-05-11) — operator's "4 days passed" frame disproven by same-day timestamp check
3. §2.2 cryptopanic sub-checks — (a/b/c) framing missed (b'-new) "deploy-without-activate"
4. §2.4 → §2.7 inversion (BL-071a) — audit's "§2.4 feeds §2.7" hypothesis flipped by pre/post-PR-#64 distribution
5. Deleted-branch close call — substrate-incompleteness variant; check that would have caught the frame was beyond the loaded MEMORY.md surface

---

## §12c — Heartbeat counters are not health signals

**Status:** DRAFT, 1 strong direct instance. NOT YET AT PROMOTION THRESHOLD. Drafted for early-adoption consideration OR continued evidence-collection until 3+ instances.

### Proposed CLAUDE.md text

When building monitoring for any pipeline writer, the watchdog must verify the writer's **intended output** (table rows, file timestamps, downstream events) — NOT the writer's **internal status** (heartbeat counters, "I'm alive" pings, periodic stats logs).

**Heartbeat counters that can legitimately be zero are not health signals.** When a counter being zero is ambiguous between "healthy idle" (writer producing output recently, this minute happens to be quiet) and "starved" (writer producing zero output since service start), the heartbeat cannot distinguish them. A watchdog reading heartbeats sees both as green throughout actual failure.

The discipline when designing or reviewing any monitoring surface:

1. **Identify the writer's intended output.** What concrete artifact does it produce?
2. **Build the watchdog to read that output directly.** Row count queries with timestamps, file mtime checks, event-stream tail.
3. **Treat heartbeats as orthogonal liveness signals, NOT as health signals.** "Process is alive" ≠ "process is doing work." A heartbeat saying "I'm alive" is correct AND insufficient.
4. **If you only have a heartbeat counter:** add a "saw-this-counter-non-zero-since" timestamp to the watcher state. The "no-non-zero-counter-for-N-minutes" check distinguishes starved from idle. Better: just read the table.

**Composes with §12a (freshness SLO + watchdog):** §12a says every new pipeline table ships with freshness SLO + watchdog. §12a is correct *because* heartbeat-based monitoring is insufficient — §12a reads against output (table writes), not against component status. §12c is the underlying *why*.

**Composes with §12b (alert at write site for automated state reversals):** §12b is also output-oriented — fires on the actual write event, not on a status indicator wrapping it.

### Evidence base

1 strong direct instance documented in `feedback_heartbeat_vs_output_monitoring.md`:
1. perp_watcher (gecko-alpha §2.6 / §2.11, 2026-05-11): heartbeat green for 20+ days while `perp_anomalies` table empty. All nine counters read zero; "healthy idle" and "starved" structurally indistinguishable.

Suggestive but not direct:
- Anthropic credit dry — heartbeat showed `narrative_predictions: 0` for 4 days; operator had to manually grep. Different shape (counter WAS the right signal, just unsupervised).
- cryptopanic (§2.2) — no heartbeat counter at all; orthogonal issue.

**Promotion recommendation:** defer to 3+ direct instances accumulating, OR accept 1-instance + structural-argument adoption now if operator decides the rule's prevention value justifies it. The §12a discipline that's already promoted IS this rule applied; making it explicit prevents future watchdogs from re-introducing the blind spot.

---

## Combined adoption considerations

If §9b + §9c + §9d are adopted together, they form a coherent "verification before locking in a claim" sub-section under §9. They're orthogonal axes:

- §9b: did the structural prerequisite hold? (input-side)
- §9c: did the lever actually reach the outcome? (causation-side)
- §9d: was the hypothesis space itself correct? (framing-side)

§12c can adopt separately — different domain (monitoring infrastructure, not investigation discipline).

**Verified against current global CLAUDE.md (2026-05-11):** §9a (runtime-state verification, forward-looking) + §9c (post-hoc attribution / lever-vs-data-path, backward-looking) are already adopted. **§9b is the open slot** for "structural-attribute verification." **§9d is a new slot** for "pre-registered hypothesis anchoring" — sibling of the existing §9a/§9c. §12c is also a new slot.

Adoption sequence recommended: §9b first (clean evidence base, ready), §9d second (composes with both §9a and §9c, completes the verification-discipline trio), §12c third (when evidence threshold met OR operator decision).

## Cross-references

- `feedback_section_9_promotion_due.md` — §9b evidence
- `feedback_lever_vs_data_path_pattern.md` — §9c evidence
- `feedback_pre_registered_hypothesis_anchoring.md` — §9d evidence (revised 2026-05-11)
- `feedback_heartbeat_vs_output_monitoring.md` — §12c evidence
- `tasks/findings_silent_failure_audit_2026_05_11.md` — concrete worked examples driving each rule's instances tonight
