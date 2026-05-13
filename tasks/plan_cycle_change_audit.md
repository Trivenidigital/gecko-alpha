**New primitives introduced:** NONE — read-only audit producing a findings document. No production code changes in this PR; any concrete remediations get filed as separate backlog items with their own ship paths.

# BL-NEW-CYCLE-CHANGE-AUDIT — Implementation Plan

> Read-only audit. Output: `tasks/findings_cycle_change_audit_2026_05_13.md` + status updates on the backlog item.

**Goal:** Determine which modules' design-time time-based assumptions have been silently invalidated by the `SCAN_INTERVAL_SECONDS` shift from 300s (design-era) to 60s (current prod).

**Architecture:** Two-pass audit. Pass 1 inventories every place where cycle-time math appears (direct `SCAN_INTERVAL_SECONDS` references + indirect: `*_CYCLES` multipliers + `per cycle` / `every cycle` / "max per cycle" comments + design-doc math statements in `docs/superpowers/specs/`). Pass 2 classifies each finding per the backlog spec axis: Phantom / Borderline / Broken, with severity + concrete fix-shape.

**Tech Stack:** grep, ast, sqlite3 (for prod-state probes). No runtime code modified.

**Branch:** `audit/cycle-change-2026-05-13` (already created from master at `325369d`).

---

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Config-change-impact analysis | none found | Project-internal; same shape as the silent-failure audit (`findings_silent_failure_audit_2026_05_11.md`) which was bespoke. |
| Time-based-assumption verification | none found | Same. |

Awesome-hermes-agent ecosystem check: no skill for config-drift-against-design audits. Verdict: project-internal audit, no Hermes opportunity.

## Drift-check

`tasks/findings_cycle_change_audit_*.md` does not exist (verified via Glob). No prior audit doc exists. BL-NEW-CYCLE-CHANGE-AUDIT entry in `backlog.md:326` confirms PROPOSED status with no work started.

Known-concrete instance already surfaced: BL-053 (CryptoPanic) — design-doc assumed 300s → 12 req/hr; current 60s → 60 req/hr at low end of 50-200/hr free-tier band. Deactivated. Recovery path is operator-side per `tasks/findings_silent_failure_audit_2026_05_11.md` §2.2 closure.

## Audit scope — modules / files to inspect

**Tier A — Direct `SCAN_INTERVAL_SECONDS` callers** (mechanical impact):
1. `scout/main.py` — pipeline orchestrator; only direct user found in this session's grep (`main.py:1567` as timeout)
2. Any `*_CYCLES` multiplier setting (multiplies SCAN_INTERVAL frequency):
   - `HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES = 1` (config.py:77) → effective period = SCAN_INTERVAL × CYCLES
   - search for others

**Tier B — Per-cycle external API callers** (any function reachable from `run_cycle` that posts to an external API; the cycle bump means N× more calls per hour):
3. `scout/ingestion/coingecko.py` — CoinGecko markets + trending
4. `scout/ingestion/dexscreener.py` — DexScreener boosts poller
5. `scout/ingestion/geckoterminal.py` — GeckoTerminal trending pools
6. `scout/safety.py` — GoPlus security checks (per-token, async)
7. `scout/news/cryptopanic.py` — CryptoPanic news feed (BL-053, known-broken, in scope to confirm)
8. `scout/ingestion/held_position_prices.py` — held-position refresh lane (DexScreener + CoinGecko paths)
9. `scout/counter/detail.py` — counter-arg LLM call + CoinGecko detail
10. `scout/briefing/collector.py` — briefing data collection

**Tier C — Per-cycle alert / write paths** (where per-cycle frequency affects operator-visible state):
11. `scout/velocity/detector.py` — `VELOCITY_TOP_N = 10` max alerts per cycle
12. `scout/spikes/detector.py` — has "every cycle for 7 days" comment per grep
13. `scout/main.py:1463` — "Update peak prices for Early Catches + Top Gainers (every cycle)" — peak-price write rate

**Tier D — Decoupled-by-design** (have their own `*_INTERVAL` setting; cycle bump should NOT have affected them; verify by reading the loop drivers):
14. `scout/narrative/agent.py` — `NARRATIVE_POLL_INTERVAL = 1800` self-paces
15. `scout/secondwave/detector.py` — `SECONDWAVE_POLL_INTERVAL = 1800` self-paces
16. `scout/social/lunarcrush/` — `LUNARCRUSH_POLL_INTERVAL = 300` self-paces; rate-limit cap separately
17. `scout/chains/tracker.py` — `CHAIN_CHECK_INTERVAL_SEC = 300` self-paces
18. `scout/trading/` — `TRADING_EVAL_INTERVAL = 1800` self-paces
19. `scout/social/telegram/listener.py` — `TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC = 300` self-paces; `_SILENCE_CHECK_INTERVAL_SEC = 3600`
20. `scout/perp/` — `PERP_WS_PING_INTERVAL_SEC = 20`, `PERP_DB_FLUSH_INTERVAL_SEC = 2.0` — WS-driven, not cycle-driven

**Tier E — Design-doc math statements** (verify design-doc assumptions still hold):
21. `docs/superpowers/specs/2026-04-20-bl053-cryptopanic-news-feed-design.md` — already-known case
22. All other `docs/superpowers/specs/*.md` files containing "req/hr", "req/min", "per cycle", "cycle", "interval", "rate limit" strings

## Audit methodology

For each Tier-A/B/C site:
1. **Read the call site**: identify what external API or write is performed per cycle, and any inline rate-cap (`COINGECKO_RATE_LIMIT_PER_MIN`, `LUNARCRUSH_RATE_LIMIT_PER_MIN`).
2. **Compute design-time math**: at 300s cycle, what is the rate? (calls/hr, alerts/hr, writes/hr, etc.)
3. **Compute current math**: at 60s cycle, what is the rate?
4. **Look up the external constraint**: API free-tier rate limit, or operator-experience target.
5. **Classify** per backlog spec:
   - **Phantom drift** — current math still has ≥3x headroom vs constraint. Cycle change irrelevant.
   - **Borderline** — current math is within 1.5x of constraint. One bad cycle, traffic spike, or upstream change tips over.
   - **Broken** — current math exceeds constraint, OR operator-experience target violated (e.g., alert noise).

For each Tier-D site:
1. **Verify decoupling** — read the loop driver to confirm the interval-setting controls cadence, NOT cycle multiplication.
2. If verification confirms decoupling: mark **Decoupled (safe)** and move on.

For each Tier-E design-doc statement:
1. Extract the math claim (e.g., "12 req/hr at 300s cycle").
2. Re-compute at 60s; check against the doc's stated tolerance / constraint.

## Output shape

Single findings document at `tasks/findings_cycle_change_audit_2026_05_13.md`. Top-level structure:

```markdown
**New primitives introduced:** NONE -- read-only audit doc.

# Cycle-Change Audit (BL-NEW-CYCLE-CHANGE-AUDIT) -- 2026-05-13

**Purpose.** ...
**Methodology.** ...

## Per-module verdict table

| Module | Design assumption | Current math (60s cycle) | Constraint | Verdict | Severity | Fix shape |
| --- | --- | --- | --- | --- | --- | --- |
| scout/ingestion/coingecko.py | ... | ... | ... | Phantom | LOW | — |
| ... | | | | | | |

## Per-finding details
[Only for non-Phantom rows]

### Finding A1: scout/news/cryptopanic.py (Borderline)
...

## Carry-forward
[Each non-Phantom finding gets a one-line backlog-item filing recommendation]
```

## File structure

**Files to create:**
- `tasks/findings_cycle_change_audit_2026_05_13.md` — the findings doc

**Files to modify (optional, status-update bundle):**
- `backlog.md` — flip `BL-NEW-CYCLE-CHANGE-AUDIT` status from PROPOSED → SHIPPED with link to findings doc; if the parse-mode audit chore is bundled, also flip `BL-NEW-PARSE-MODE-AUDIT` PROPOSED → SHIPPED with link to PR #111.

**Files to create per-finding (only if remediation justified):**
- New backlog entries — but those are typically appended to backlog.md inline, not separate files. Filed via the same backlog.md edit above.

**Files NOT to touch:**
- All `scout/` production code — the audit is read-only.
- All test files — no test changes; the audit produces analysis, not assertions.

## Self-review notes (in advance)

1. **Sample size**: Tier A/B/C is ~13 named files; Tier D is ~6 (verification-only); Tier E is N specs (open-ended count). Methodology is per-site reading, not statistical — small N is correct shape.
2. **External rate-limit lookups**: CoinGecko free tier is 30/min documented in `scout/config.py:63`. GoPlus / DexScreener / GeckoTerminal need fresh API-doc lookup; if those are not visible without a web fetch, fall back to "rate-limited 429 handling exists or doesn't" as the proxy signal.
3. **Operator-experience targets** for alert rates (Tier C): no documented threshold; use grep over `feedback_*.md` / lessons for prior operator complaints about alert noise, defaulting to "5-10x increase is borderline; >10x is broken" as a heuristic only.
4. **§9b promotion data**: this audit is a fresh data point for the `feedback_section_9_promotion_due.md` rule (structural-attribute verification). The methodology design + per-module classifications add to the evidence base.

---

## Task 1: Inventory pass (Tier A + Tier B)

**Files:**
- Read: `scout/config.py`, `scout/main.py`, `scout/ingestion/coingecko.py`, `scout/ingestion/dexscreener.py`, `scout/ingestion/geckoterminal.py`, `scout/safety.py`, `scout/ingestion/held_position_prices.py`, `scout/counter/detail.py`, `scout/briefing/collector.py`, `scout/news/cryptopanic.py`

- [ ] **Step 1.1: Grep for `*_CYCLES` settings**

Run: `grep -nE "_CYCLES" scout/config.py`
Capture: every setting name + default value + comment.
Expected outputs include `HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES = 1`. Note any others.

- [ ] **Step 1.2: Trace `run_cycle` invocations**

Read `scout/main.py:466-580` (`run_cycle` function body) — list every external API call site reachable from inside one cycle. For each, note:
- API endpoint hit
- Whether it's inside a per-token loop (multiplies)
- Whether there's an inline rate limiter

- [ ] **Step 1.3: For each Tier-B file, find its public entry point + rate-limit handling**

For each of `coingecko.py`, `dexscreener.py`, `geckoterminal.py`, `safety.py`, `held_position_prices.py`, `counter/detail.py`, `briefing/collector.py`, `cryptopanic.py`:
- Find the public function called from `run_cycle` (or its caller chain)
- Note: does it call `COINGECKO_RATE_LIMIT_PER_MIN` / a backoff helper? Does it 429-handle?

- [ ] **Step 1.4: Compile Tier A/B inventory into the findings doc skeleton**

Write the per-module verdict table header + one row per Tier-A/B site with the "design assumption" column populated (the constraint that matters).

---

## Task 2: Tier C inventory (per-cycle alert/write rates)

**Files:**
- Read: `scout/velocity/detector.py`, `scout/spikes/detector.py`, `scout/main.py:1463`

- [ ] **Step 2.1: Compute alert/write rates per Tier-C site**

For each:
- Read the alert/write loop
- Compute current rate (at 60s cycle) vs design-time rate (at 300s cycle)
- Note any "max N per cycle" cap that does NOT auto-scale with cycle frequency

- [ ] **Step 2.2: Search for operator-experience feedback on alert noise**

`grep -rE "alert noise|too noisy|too many alerts|alert spam" memory/ feedback_*.md tasks/` to find prior operator complaints.
- If found, use as evidence for severity classification
- If not found, use the "≤5x scaling acceptable, >10x noisy" heuristic + flag the absence of a target

---

## Task 3: Tier D verification (decoupled-by-design sites)

**Files:**
- Read: `scout/narrative/agent.py`, `scout/secondwave/detector.py`, `scout/social/lunarcrush/` (top-level loop), `scout/chains/tracker.py`, `scout/trading/` (loop driver), `scout/social/telegram/listener.py`, `scout/perp/` (WS loop)

- [ ] **Step 3.1: For each Tier-D site, locate the loop driver**

Read each module's main loop. Identify the line that controls cadence (`asyncio.sleep`, `wait_for(..., timeout=...)`, etc.).

- [ ] **Step 3.2: Confirm decoupling**

For each: confirm the cadence-controlling line uses the module's own `*_INTERVAL` setting and is NOT inside `run_cycle`. If yes, mark **Decoupled (safe)** in the verdict table.

- [ ] **Step 3.3: Flag any site that looks decoupled but actually inherits from cycle**

If a module's docstring claims decoupling but the actual code path runs inside `run_cycle` (e.g., narrative-agent task launched per-cycle), flag as a false-decoupling and classify per Tier B methodology.

---

## Task 4: Tier E (design-doc math review)

**Files:**
- Read: `docs/superpowers/specs/*.md` (any with cycle / rate / interval math)

- [ ] **Step 4.1: Grep design-spec corpus for math statements**

Run: `grep -lE "req/hr|req/min|per cycle|cycle frequency|rate limit|free tier|free-tier|throttle" docs/superpowers/specs/`
List every matching file.

- [ ] **Step 4.2: For each matching design doc, extract the math claim**

For each: find the assumption sentence (e.g., "300s cycle → 12 req/hr → well under free tier"). Re-compute at 60s. Compare against the doc's stated tolerance.

- [ ] **Step 4.3: Cross-reference each design-doc finding to the Tier-B inventory**

If the design doc covers a Tier-B module, the doc's math claim should match (or contradict) the Tier-B verdict. If contradiction: investigate and resolve (one of them is wrong).

---

## Task 5: Classification pass + per-finding detail write-up

- [ ] **Step 5.1: Apply the Phantom / Borderline / Broken axis to every row**

Per the methodology section. For each non-Phantom row, write a per-finding details section (severity, evidence, fix-shape).

- [ ] **Step 5.2: Write the Carry-forward section**

For each non-Phantom finding, recommend one of:
- **File a separate backlog item** (with proposed BL-NEW-* ID)
- **Bundle into an existing backlog item** (with cross-reference)
- **No follow-up** (e.g., already mitigated, low severity)

- [ ] **Step 5.3: Cross-reference §9b promotion candidate evidence**

Add a brief note in the findings doc on how this audit's methodology composes with the §9b structural-attribute-verification rule per `feedback_section_9_promotion_due.md` (data point for the promotion case).

- [ ] **Step 5.4: Commit findings doc**

```bash
git add tasks/findings_cycle_change_audit_2026_05_13.md
git commit -m "docs(audit): cycle-change audit findings — N modules classified, M flagged (BL-NEW-CYCLE-CHANGE-AUDIT)"
```

---

## Task 6: Backlog status updates (optional bundle)

- [ ] **Step 6.1: Flip BL-NEW-CYCLE-CHANGE-AUDIT status**

Edit `backlog.md` entry from `**Status:** PROPOSED ...` to `**Status:** SHIPPED 2026-05-13 — findings at \`tasks/findings_cycle_change_audit_2026_05_13.md\`. See doc for per-finding remediation backlog items.`

- [ ] **Step 6.2: Flip BL-NEW-PARSE-MODE-AUDIT status (chore bundle)**

Edit the `BL-NEW-PARSE-MODE-AUDIT` entry from `**Status:** PROPOSED ...` to `**Status:** SHIPPED 2026-05-13 — per-site fixes shipped via PR #111 (commit \`325369d\`). 7 HIGH ACTUAL sites closed (6 from audit + 1 plan-review discovery). AST coverage test mechanically enforces the audit-methodology lesson. 3 HIGH POTENTIAL sites in \`scout/main.py\` deferred per audit policy (need 7-day production log review).`

- [ ] **Step 6.3: Commit backlog updates**

```bash
git add backlog.md
git commit -m "docs(backlog): flip BL-NEW-CYCLE-CHANGE-AUDIT + BL-NEW-PARSE-MODE-AUDIT to SHIPPED"
```

## Rollback plan

Doc-only PR. Rollback is `git revert <merge-sha>` — restores prior backlog statuses + removes the findings file. No production impact possible.

## Deploy plan

After PR merges: no deploy. Findings doc lives in the repo; any concrete remediations get filed as their own backlog items with their own ship paths.
