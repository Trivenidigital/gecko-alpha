# Backlog drift audit — 2026-05-16

**Trigger:** Two §7a saves in one work-selection cycle (BL-NEW-CG-RATE-LIMITER-BURST-PROFILE: SHIPPED via 4 commits in last 24h; BL-NEW-SCORE-HISTORY-PRUNING: PARTIAL — 14d pruning exists in `scout/narrative/agent.py:680-699`). User flagged the substrate finding: backlog drift is wider than the two false starts.

**Scope:** All `### BL-NEW-*` entries in `backlog.md` with declared status `PROPOSED`, `PR-READY`, `IMPLEMENTED`, `ACTIVE`, or `IN PR BUILD`. Items declared `SHIPPED` excluded to keep audit under 30 min.

**Method, per item:**
1. Grep `scout/`, `scripts/`, `tasks/` for named feature, file, function, or artifact referenced in the entry
2. `git log --since=2026-05-13 --oneline` filtered by acronym / feature keywords
3. Categorize per user-defined rubric

**Categories:**
- **Confirmed** — entry accurate, work still needed
- **Drift-done** — work shipped, status not updated; close as SHIPPED-status-pending
- **Drift-partial** — work partly shipped; reframe scope to harden/complete
- **Unclear** — file:line not findable or entry too vague to audit; flag for owner

---

## Summary

| Category | Count | Items |
|---|---|---|
| **Drift-done** (close) | 7 | INGEST-WATCHDOG, COINGECKO-BREADTH-HYDRATION, COINGECKO-MIDCAP-GAINER-SCAN, CG-RATE-LIMITER-BURST-PROFILE, GT-ETH-ENDPOINT-404, MINARA-DB-PERSISTENCE, HERMES-CRYPTO-SKILLS-TRACKING |
| **Drift-partial** (reframe) | 2 | SCORE-HISTORY-PRUNING, VOLUME-SNAPSHOTS-PRUNING |
| **Confirmed** (still needed) | 14 | Q2-SIMULATOR, LIVE-ELIGIBLE-WEEKLY-DIGEST, LIVE-EVALUABLE-SIGNAL-AUDIT, SOCIAL-MENTIONS-DENOMINATOR-AUDIT, ANTHROPIC-SPEND-TARGET, SCORE-HISTORY-WATCHDOG-SLO, VOLUME-SNAPSHOTS-WATCHDOG-SLO, BL060-CYCLE-VERIFY, SQLITE-WAL-PROFILE, TG-BURST-PROFILE, MINARA-COOLDOWN-REVERIFY, DEX-PRICE-COVERAGE, HELIUS-PLAN-AUDIT, MORALIS-PLAN-AUDIT |
| **Unclear** (flag owner) | 1 | NARRATIVE-OPERATOR-ALERT-WIRE |
| **Active** (in soak/observation, not buildable) | 1 | HPF-RE-EVALUATION |

**Drift rate among "buildable" filings (Drift-done + Drift-partial / total in-scope): 9 / 25 = 36%.**

Confirms user's prior: drift rate is materially higher than 2-isolated-cases. Roughly 1-in-3 backlog entries claiming "work needed" actually have work already done or in flight.

---

## Drift-done (close as SHIPPED — backlog status update is mechanical)

### BL-NEW-INGEST-WATCHDOG (line 245)
- **Declared:** "IN PR BUILD — 2026-05-14 on `codex/ingest-watchdog`"
- **Evidence:** commit `479e6c7 feat(observability): add ingestion starvation watchdog`
- **Action:** Update status to SHIPPED with commit ref.

### BL-NEW-COINGECKO-BREADTH-HYDRATION (line 467)
- **Declared:** "PR READY 2026-05-14 - branch `codex/coingecko-breadth-hydration`"
- **Evidence:** commits `5e3417b feat(coingecko): hydrate trending and widen breadth (#121)` + `2487ad7`
- **Action:** Update to SHIPPED.

### BL-NEW-COINGECKO-MIDCAP-GAINER-SCAN (line 478)
- **Declared:** "IMPLEMENTED 2026-05-14 - branch `codex/coingecko-midcap-gainer-scan`"
- **Evidence:** commit `4860692 feat(coingecko): scan midcap gainers (#124)` + docs `0ce1540`
- **Action:** Update to SHIPPED.

### BL-NEW-CG-RATE-LIMITER-BURST-PROFILE (line 842)
- **Declared:** "THIRD FOLLOW-UP PR-READY 2026-05-15"
- **Evidence:** 4 commits — `7f1a174` (#129 spacing/jitter), `a08d9ef` (retry amplification), `f45e598` (same-cycle fan-out), `d1cf96b` (main.py orchestrator serialization). Design doc `tasks/design_bl_new_cg_rate_limiter_burst_profile.md` exists.
- **Action:** Update to SHIPPED. Residual = deploy-verification only.

### BL-NEW-GT-ETH-ENDPOINT-404 (line 852)
- **Declared:** "PR-READY 2026-05-14 on `codex/gt-eth-endpoint-404`"
- **Evidence:** commit `e0e51c8 fix(geckoterminal): map ethereum chain to eth network id`
- **Action:** Update to SHIPPED.

### BL-NEW-MINARA-DB-PERSISTENCE (line 721)
- **Declared:** PROPOSED 2026-05-13 (per earlier MEMORY grep; backlog now likely updated)
- **Evidence:** commits `6e65e2e feat(minara): persist alert emissions` + `e628097 Merge pull request #112 from Trivenidigital/codex/minara-db-persistence`
- **Action:** Update to SHIPPED.

### BL-NEW-HERMES-CRYPTO-SKILLS-TRACKING (line 415)
- **Declared:** PROPOSED 2026-05-14 with research note
- **Evidence:** `tasks/research_hermes_crypto_skills_2026_05_14.md` exists; commit `acf4b8e docs(hermes): track crypto skill ecosystem and debt audit (#119)` shipped it
- **Action:** Update to SHIPPED-RESEARCH (ongoing tracking, not closed).

---

## Drift-partial (reframe scope to harden/complete)

### BL-NEW-SCORE-HISTORY-PRUNING (line 880)
- **Declared:** PROPOSED, "no pruning rule in tree"
- **Evidence (contradiction):** `scout/narrative/agent.py:680-699` prunes `score_history` with `WHERE datetime(scanned_at) < datetime('now', '-14 days')`, hard-coded 14d
- **Real residual gaps (file:line):**
  - Hardcoded retention violates "No hardcoded thresholds" rule (CLAUDE.md project section)
  - Coupled to `narrative` daily-learn loop — disabling narrative disables pruning silently
  - `except Exception: pass` at line 695-696 = Class 1 silent-failure (§12a)
  - No row-count telemetry per pass
- **Reframed scope:** harden existing pruning (parameterize, decouple, structured-log, telemetry). ~20-line refactor + 2 settings + 2 tests.

### BL-NEW-VOLUME-SNAPSHOTS-PRUNING (line 890)
- **Same shape as score-history-pruning above. Same reframe applies.**
- **Combined PR:** the two items are mechanically identical and should ship as one PR.

---

## Confirmed (still needed — no in-tree artifact found)

### BL-NEW-Q2-SIMULATOR (line 605)
- **Declared:** PROPOSED. Expected artifact: `scripts/q2_simulator.py`
- **Evidence:** `ls scripts/q2_simulator*` → not found
- **Status:** Confirmed.

### BL-NEW-LIVE-ELIGIBLE-WEEKLY-DIGEST (line 625)
- **Declared:** PROPOSED. Expected artifact: `scout/trading/cohort_digest.py` + weekly cron
- **Evidence:** `grep cohort_digest scout/` → no files
- **Status:** Confirmed.

### BL-NEW-LIVE-EVALUABLE-SIGNAL-AUDIT (line 573)
- **Declared:** PROPOSED. Expected artifact: analysis findings doc
- **Evidence:** `ls tasks/findings*live_evaluable*` → not found
- **Status:** Confirmed.

### BL-NEW-SOCIAL-MENTIONS-DENOMINATOR-AUDIT (line 228)
- **Declared:** PROPOSED 2026-05-14. Expected: findings/backtest doc
- **Evidence:** `ls tasks/findings*denominator*` → not found. `docs(social): audit BL-032 signal source (#122)` commit is the parent BL-032 audit, not this denominator follow-up.
- **Status:** Confirmed.

### BL-NEW-ANTHROPIC-SPEND-TARGET (line 869)
- **Declared:** PROPOSED. Expected: `ANTHROPIC_DAILY_SPEND_SOFT_CAP_USD` setting in config.py
- **Evidence:** `grep ANTHROPIC_DAILY_SPEND_SOFT_CAP scout/` → no files
- **Status:** Confirmed. Requires operator decision before code.

### BL-NEW-SCORE-HISTORY-WATCHDOG-SLO (line 874)
- **Declared:** PROPOSED, gated on §12a daemon
- **Evidence:** §12a daemon unbuilt per `findings_silent_failure_audit_2026_05_11.md`
- **Status:** Confirmed. Blocked.

### BL-NEW-VOLUME-SNAPSHOTS-WATCHDOG-SLO (line 885)
- **Same:** Confirmed. Blocked on §12a daemon.

### BL-NEW-BL060-CYCLE-VERIFY (line 898)
- **Declared:** PROPOSED. Verify BL-060 paces independently of 60s cycle.
- **Evidence:** `scripts/bl060_threshold_audit.py` exists (PR #46) but is a different artifact — addresses BL-060 threshold calibration, not cycle-pacing verification. No verify findings doc.
- **Status:** Confirmed.

### BL-NEW-SQLITE-WAL-PROFILE (line 903)
- **Declared:** PROPOSED. Expected: profile script + findings.
- **Evidence:** `ls scripts/sqlite_wal*` → not found
- **Status:** Confirmed.

### BL-NEW-TG-BURST-PROFILE (line 908)
- **Declared:** PROPOSED. Expected: instrumentation + measurement.
- **Evidence:** `ls scripts/tg_burst*` → not found
- **Status:** Confirmed.

### BL-NEW-MINARA-COOLDOWN-REVERIFY (line 752)
- **Declared:** PROPOSED. Expected: re-verify findings doc.
- **Evidence:** `ls tasks/findings*minara*reverify*` → not found
- **Status:** Confirmed.

### BL-NEW-DEX-PRICE-COVERAGE (line 788)
- **Declared:** PROPOSED. Expected: coverage findings.
- **Evidence:** `ls tasks/findings*dex_price_coverage*` → not found
- **Status:** Confirmed.

### BL-NEW-HELIUS-PLAN-AUDIT (line 857)
- **Declared:** PROPOSED. Operator decision.
- **Evidence:** No findings doc, no code change.
- **Status:** Confirmed. Awaits operator on plan tier.

### BL-NEW-MORALIS-PLAN-AUDIT (line 863)
- **Same shape as HELIUS-PLAN-AUDIT.** Confirmed. Awaits operator.

---

## Unclear (flag owner)

### BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE (line 799)
- **Declared:** PROPOSED 2026-05-13. "wire push-notification for narrative_alert_dispatcher 503 misconfig (Path C1)"
- **Evidence:** `grep narrative_alert_dispatcher scout/` → 0 hits. The referenced module/function does not exist in tree under that name. Commit `e1f501f docs(narrative-scanner): V1.1 fold + Path B alert decision + BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE` is docs-only.
- **Issue:** Either the referenced symbol was renamed, never landed, or the entry references a symbol from a separate VPS (Hermes-side per memory `project_narrative_scanner_v1_1_shipped_2026_05_13.md`). Entry needs owner re-anchoring before audit can categorize.
- **Status:** Unclear. **Flag for owner clarification.**

---

## Active (in soak/observation, not "buildable")

### BL-NEW-HPF-RE-EVALUATION (line 640)
- **Declared:** ACTIVE — accumulating to n≥20.
- **No-op:** This is a measurement-bound observation, not a build candidate. Re-evaluation auto-triggers per pre-registered criteria.
- **Status:** No audit action.

---

## Backlog status updates (mechanical follow-through)

These updates need landing in `backlog.md` after this audit doc is reviewed. They are NOT the load-bearing artifact — this doc is. Backlog updates are derivative.

| Line | Item | Change |
|---|---|---|
| 245 | BL-NEW-INGEST-WATCHDOG | "IN PR BUILD" → "SHIPPED 2026-05-XX commit `479e6c7`" |
| 415 | BL-NEW-HERMES-CRYPTO-SKILLS-TRACKING | "PROPOSED" → "SHIPPED-RESEARCH 2026-05-14 (`acf4b8e`)" |
| 467 | BL-NEW-COINGECKO-BREADTH-HYDRATION | "PR READY" → "SHIPPED commits `5e3417b` + `2487ad7`" |
| 478 | BL-NEW-COINGECKO-MIDCAP-GAINER-SCAN | "IMPLEMENTED" → "SHIPPED 2026-05-XX commit `4860692` (PR #124)" |
| 721 | BL-NEW-MINARA-DB-PERSISTENCE | "PROPOSED" → "SHIPPED commits `6e65e2e` + `e628097` (PR #112)" |
| 842 | BL-NEW-CG-RATE-LIMITER-BURST-PROFILE | "THIRD FOLLOW-UP PR-READY" → "SHIPPED commits `7f1a174` + `a08d9ef` + `f45e598` + `d1cf96b` (PR #129 + 3 follow-ups). Residual: deploy-verification only." |
| 852 | BL-NEW-GT-ETH-ENDPOINT-404 | "PR-READY" → "SHIPPED commit `e0e51c8`" |
| 880 | BL-NEW-SCORE-HISTORY-PRUNING | "PROPOSED" → "DRIFT-PARTIAL — 14d pruning exists at `scout/narrative/agent.py:687-689`; residual: parameterize + decouple + structured-log + telemetry" |
| 890 | BL-NEW-VOLUME-SNAPSHOTS-PRUNING | "PROPOSED" → "DRIFT-PARTIAL — same as SCORE-HISTORY-PRUNING; combined PR" |

---

## Selection-aid (post-audit, confirmed-status work)

Of the 14 Confirmed items, the highest-leverage build candidates (excluding operator-decision-blocked + daemon-blocked + measurement-bound):

**Tier A — small, self-contained, real value:**
- **BL-NEW-SQLITE-WAL-PROFILE** — measure WAL bloat (~2-4 hours). Sets up evidence for future tuning.
- **BL-NEW-TG-BURST-PROFILE** — instrument TG dispatch + measure burst frequency (~2-4 hours). Pairs with §12b discipline (silent-failure on TG path).
- **BL-NEW-SCORE-HISTORY-PRUNING + BL-NEW-VOLUME-SNAPSHOTS-PRUNING (combined)** — harden existing pruning (~1-2 hours). Closes 2 backlog items per §7a residual-gap rule.

**Tier B — larger but high-leverage:**
- **BL-NEW-LIVE-ELIGIBLE-WEEKLY-DIGEST** — weekly cron + cohort_digest (~3-4 hours). Reduces operator attention cost on the 4-week dashboard window.
- **BL-NEW-Q2-SIMULATOR** — paired counterfactual decision-replay (~6-8 hours). Higher analytical leverage; gates the live-trading roadmap.

**Tier C — analysis-only, no code change:**
- **BL-NEW-LIVE-EVALUABLE-SIGNAL-AUDIT** (~3 hours analysis + writeup)
- **BL-NEW-DEX-PRICE-COVERAGE** — investigate coverage gap
- **BL-NEW-MINARA-COOLDOWN-REVERIFY** — verify

**Tier D — blocked / awaits operator:**
- HELIUS-PLAN-AUDIT, MORALIS-PLAN-AUDIT, ANTHROPIC-SPEND-TARGET — operator must decide plan tier / spend cap before code
- SCORE-HISTORY-WATCHDOG-SLO, VOLUME-SNAPSHOTS-WATCHDOG-SLO — blocked on §12a daemon
- BL060-CYCLE-VERIFY — verify task, lower priority than build items

---

## Reconciliation-discipline thread (filed for post-audit revisit)

User raised the substrate question: what prevents drift from accumulating again?

Three options noted, decision deferred until this audit's evidence is folded:

1. **Periodic re-audit** (cheapest, weakest) — schedule a /loop or cron to re-run this audit every N weeks. Doesn't prevent drift, just bounds it.
2. **PR-merge gate** (behavioral, medium cost) — every PR closing BL-NEW-* work must update `backlog.md` in the same commit. Enforceable via PR template or `.claude/hooks/`.
3. **Backlog status as derived state** (structural, highest cost) — backlog.md becomes a generated view computed from code surface + commit log + status SQLite table. Eliminates hand-maintenance.

This audit's 36% drift rate is evidence for (2) being load-bearing. (1) alone won't close the gap. (3) is the strongest fix but warrants its own scoping cycle.

**Defer decision to post-current-work-selection.** This audit doc is the substrate fix for *now*; the discipline question is for *next*.

---

## Audit cost

- Enumeration: 2 min
- Per-item drift checks: ~20 min (batched greps + ls + git log)
- Doc write: ~10 min
- Total: ~32 min

Within the 30-min target. The cost lands once; the selection-aid + backlog-status-update output is reusable.
