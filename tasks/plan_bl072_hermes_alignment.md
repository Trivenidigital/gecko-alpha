# BL-072 (alignment + hook) + BL-073 (Hermes roadmap) — split per plan-review

**Status:** PLAN v2 — applied 2 plan-reviewer findings, split into two backlog items
**Branch (target):** `feat/bl-072-alignment-and-hook`
**Estimated effort (overnight build scope):** 3–4 hours
**Date:** 2026-05-03
**New primitives introduced:** `docs/gecko-alpha-alignment.md`, `.claude/settings.json` PreToolUse hook, `.claude/hooks/check-new-primitives.py` script

---

## What changed from v1 (per reviewer fixes)

| v1 | v2 |
|---|---|
| 3-value drift-tag vocabulary (`gecko-native`/`extends-gecko`/`drifts-from-gecko`) | **Dropped.** Replaced with single mandatory line at top of every plan/spec/design doc: `**New primitives introduced:** [list or NONE]`. Architect: vocabulary searching for a question. Adversarial: renaming "consistency" adds overhead, not signal. |
| Single BL-072 entry covering alignment doc + 5-phase Hermes roadmap | **Split.** BL-072 = alignment doc + new-primitives convention + pre-write hook. BL-073 = Hermes integration roadmap, scoped honestly. Both reviewers flagged the conflation. |
| Pre-write hook deferred to "future PR" | **Included in overnight scope.** Both reviewers were unambiguous: doc without enforcement is the failure mode. Hook is small `.claude/settings.json` + ~50-line Python script. Risk: zero (only blocks plan/spec/design writes, not production code). |
| New top-level "Drift rules" section in CLAUDE.md | **Folded into existing "Coding Conventions"** as a "Plan/Design Document Conventions" sub-heading. Architect: don't add visual weight. |
| Part 2 catalog of silent-failure surfaces | **Gated:** entries must have owner + due date OR be marked `deferred-indefinitely (explicit reason)`. Adversarial: prevents catalog becoming a TODO graveyard. |
| Phase 0 (agentskills.io browse) acceptance: "findings note, positive or negative" | **Sharpened:** "≥1 skill imported and concretely evaluated against gecko-alpha data, OR ≥3 candidate skills documented with specific reject reasons (not 'no clear fit')". Adversarial: unfalsifiable as written. |
| BL-073 Hermes roadmap Phase 2-5 each as concrete future work | **Re-framed honestly:** Phase 1 (GEPA on narrative_prediction) is the one concrete value-add at $10 + ~2d. Phases 2-5 noted with explicit "gated on X, may never happen" status. Adversarial: "fiction tree" otherwise. |
| 2 plan + 2 design + 3 PR reviewers (5 total roles) | **Add 6th reviewer lens for design + PR rounds:** one reviewer with the explicit prompt "could existing gecko-alpha primitives already do this — is the scope itself needed?" Adversarial: shift-agent learned this exact lesson at PR-B2. |

---

## Why this exists (unchanged from v1)

User asked for two things:
1. Genuine analysis of Hermes Agent integration for gecko-alpha (after I dismissed it reductively first)
2. Copy "Hermes-first rules" from `Trivenidigital/shift-agent` into gecko-alpha

Both threads share a substrate: codify operator-discovered conventions so future sessions don't drift. The chain_patterns auto-retire incident (silent for 17 days) is the canonical case.

---

## Critical adaptation note (unchanged)

shift-agent **runs on Hermes** as production runtime. Their drift-tags directly reflect "are we using the Hermes substrate as designed."

**gecko-alpha does NOT run on Hermes.** It's a vanilla async Python pipeline (aiohttp + aiosqlite + Pydantic v2 + structlog) deployed via systemd. Claude Code is used for development assistance only.

The adaptation is structural, not literal. We keep the **shape** of shift-agent's pattern (operational hygiene doc + plan-time self-disclosure + read-deployed-code rule + mechanical enforcement). We don't copy the Hermes-specific terminology.

The new-primitives line answers: "what new infrastructure does this proposal add?" That's the meaningful question for a non-substrate codebase, and it's answerable in seconds without memorizing a tag vocabulary.

---

## OVERNIGHT BUILD SCOPE (BL-072)

### In scope

1. **`docs/gecko-alpha-alignment.md`** — adapted 4-part doc:
   - Part 1: Deployed patterns (the things we DO — Settings, signal_params Tier 1a, chain_patterns, scout/trading/* dispatchers, evaluator, db.py BEGIN EXCLUSIVE migration pattern, datetime() wrapper convention from PR #24, Pydantic v2 settings, structlog JSON logs, async-everywhere, no-global-state).
   - Part 2: Operational drift checklist with mandatory owner + due-date (or `deferred-indefinitely`):
     - chain_patterns auto-retire on stale outcome telemetry — owner: BL-071 guard ✓ done; BL-071a/b owner: TBD, deferred-research
     - gainers_early kill on stale 30d data (resolved by reversal 2026-05-03)
     - Telegram bot token placeholder — owner: operator, deferred-explicitly per user instruction
     - narrative_prediction token_id divergence — owner: TBD, deferred-pending-evidence
     - memecoin `outcomes` table empty (BL-071a) — owner: TBD, deferred-research
     - narrative chain_matches start at `outcome_class='EXPIRED'` (BL-071b) — owner: TBD, deferred-research
   - Part 3: Working agreement — "read deployed code before proposing schema/test/architecture work" (the one rule that matters). Plus the new-primitives-declaration convention.
   - Part 4: What this doc is NOT (explicit limits — not philosophy, not prescriptive about future patterns, not substitute for reading code).

2. **`CLAUDE.md` updates** — extend existing "Coding Conventions" section with new sub-heading "Plan/Design Document Conventions":
   - Every `tasks/*.md` plan/design/spec MUST start with `**New primitives introduced:** [list or NONE]`
   - Pointer to `docs/gecko-alpha-alignment.md` for deployed patterns
   - Read-deployed-code rule (one paragraph)

3. **`.claude/settings.json`** — `PreToolUse` hook on `Write|Edit` for files matching `tasks/*.md`, calling `.claude/hooks/check-new-primitives.py`.

4. **`.claude/hooks/check-new-primitives.py`** — small Python script (~50 lines):
   - Exit 0 if file content includes `**New primitives introduced:**` line
   - Exit 2 + stderr message if not, returning to Claude as feedback
   - Allowed bypass: file contains literal comment `<!-- new-primitives-check: bypass -->` for legitimate cases (e.g., note files that aren't plans)

5. **`backlog.md`** — split into:
   - **BL-072: Alignment doc + new-primitives convention + pre-write hook** — overnight build delivers this complete
   - **BL-073: Hermes integration roadmap** — separate entry, honestly scoped (see below)

6. **`tasks/notes_agentskills_browse_2026_05_03.md`** — Phase 0 findings with sharpened acceptance: ≥1 imported skill OR ≥3 candidate skills with specific reject reasons.

### Out of overnight scope (deferred to future PRs)

- **BL-073 Phase 1 (GEPA on narrative_prediction prompt)** — needs API keys, ~$10, real LLM calls, eval harness. Dedicated PR.
- **BL-073 Phase 2 (Hermes ops agent on VPS)** — production change, operator approval needed.
- **BL-073 Phase 3 (model routing)** — needs Phase 1 eval harness first.
- **BL-073 Phase 4 (BL-064 cross-platform)** — gated on BL-064 14d soak (2026-05-11).
- **BL-073 Phase 5 (Atropos RL)** — gated on ≥1000 trades/signal.

### Out of scope explicitly (per adversarial reviewer)

- The 6th-reviewer scope-skeptic lens is procedural — added to the design + PR review rounds tonight, not codified as a permanent CLAUDE.md addition (would itself need its own lifecycle decision).

---

## BL-073 honesty re-frame (per adversarial)

The Hermes roadmap entry will state explicitly:

> **Realistic outlook:** Phase 1 (GEPA on `narrative_prediction` prompt) is the one Hermes capability with concrete projected value for gecko-alpha. Cost gate: $10 + ~2 days work. Trigger to start: operator commits funding + bandwidth.
>
> **Phases 2–5 status:** Each is gated on a separate condition (VPS service install, BL-064 soak result, ≥1000 trades/signal). Each has been listed for completeness, **not** as commitment. Most likely realistic outcome: BL-073 = Phase 0 (agentskills browse) done + Phase 1 funded-and-shipped OR funded-and-skipped, with Phases 2-5 sitting indefinitely.
>
> **Honest cancellation criteria:** if Phase 1 isn't started within 90 days of this entry's creation, close BL-073 as won't-fix. Don't let it accrete as theatre.

This re-framing addresses the adversarial reviewer's "fiction tree" concern directly.

---

## File inventory (v2)

**New:**
- `docs/gecko-alpha-alignment.md` (~3-5KB, scoped down from v1)
- `.claude/hooks/check-new-primitives.py` (~50 lines)
- `tasks/notes_agentskills_browse_2026_05_03.md` (Phase 0 findings — ≥1 skill imported OR ≥3 reject reasons)

**Modified:**
- `CLAUDE.md` — extend "Coding Conventions" section (no new top-level)
- `.claude/settings.json` — add PreToolUse hook
- `backlog.md` — add BL-072 entry (alignment + hook) + separate BL-073 entry (Hermes roadmap, honest)

**Test impact:** None. Hook only fires on `tasks/*.md` Write/Edit. No production Python touched. Smoke-test that existing pytest still imports cleanly.

---

## Acceptance criteria (v2)

- [ ] `docs/gecko-alpha-alignment.md` exists with 4 parts, gecko-alpha-specific content
- [ ] Part 1 catalogues at least 8 deployed patterns with code-file pointers (not aspirational)
- [ ] Part 2 entries each have owner + due-date OR explicit `deferred-indefinitely (reason)` — NO bare TODOs
- [ ] Part 3 states the "read deployed code" rule + new-primitives convention
- [ ] Part 4 includes "this doc is reference, not enforcement; the hook is the enforcement"
- [ ] `CLAUDE.md` "Coding Conventions" section extended with sub-heading; no new top-level section
- [ ] `.claude/hooks/check-new-primitives.py` exists and is executable
- [ ] `.claude/settings.json` PreToolUse hook configured
- [ ] Hook **demonstrably blocks** a tasks/*.md write missing the new-primitives line (manual smoke test in PR description)
- [ ] Hook allows a write that includes the line
- [ ] Bypass comment works for legitimate notes
- [ ] `backlog.md` BL-072 entry covers alignment doc + hook (not the Hermes roadmap)
- [ ] `backlog.md` BL-073 entry covers Hermes roadmap honestly (Phase 1 only concrete; Phases 2-5 with explicit "gated, may never happen"; 90-day cancellation criterion)
- [ ] `tasks/notes_agentskills_browse_2026_05_03.md` records Phase 0 findings with sharpened acceptance
- [ ] No production code modified
- [ ] Existing tests still pass
- [ ] Design + PR review rounds include the 6th scope-skeptic lens

---

## Open questions for design reviewers (now narrower)

1. **Hook implementation** — match against `tasks/*.md` glob, or only `tasks/plan_*.md` and `tasks/design_*.md` (to allow scratch notes)? My lean: use a regex that matches `plan_*.md|design_*.md|spec_*.md`, allow scratch like `notes_*.md` to bypass automatically.

2. **Hook bypass mechanism** — single inline comment `<!-- new-primitives-check: bypass -->`, or an env var `CHECK_PRIMITIVES_SKIP=1`, or a settings.json allowlist? My lean: inline comment, because it lives next to the file's purpose explanation.

3. **Should the hook also check `backlog.md` and `CLAUDE.md` updates?** Probably not — those are project-level docs with their own lifecycle. Hook scope is plan/design/spec only.

4. **Part 2 of alignment doc — what's the upper bound on entries?** I propose ~10 max. Beyond that, age out the oldest with explicit close-out (won't-fix or done).

5. **BL-073 90-day cancellation criterion** — should this be a hard auto-close or a recurring "review status" reminder? My lean: recurring status check at +30d, +60d, +90d. Hard close at 90d only if Phase 1 hasn't started.

---

## Risk assessment (v2 — much shorter, hook addresses the main one)

| Risk | Mitigation |
|---|---|
| Hook fires false-positive and blocks a legitimate file | Inline bypass comment + the script exit 2 returns the explanation to Claude (= self-correcting) |
| Hook misses an edge case (e.g., file edited not Write'd) | `Write|Edit` matcher covers both |
| BL-073 sits at "Phase 0 done forever" | 90-day cancellation criterion in entry itself |
| Alignment doc Part 2 grows into a graveyard | Acceptance criterion: every entry has owner + due-date OR explicit defer-with-reason |
| Drift-tag rot (v1 risk) | **Eliminated** — convention is mechanical now, not discipline-only |
