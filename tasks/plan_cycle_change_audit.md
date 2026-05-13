**New primitives introduced:** NONE — read-only audit producing a findings document. No production code changes in this PR; any concrete remediations get filed as separate backlog items with their own ship paths.

# BL-NEW-CYCLE-CHANGE-AUDIT — Implementation Plan (v2, folded plan-review)

> Read-only audit. Output: `tasks/findings_cycle_change_audit_2026_05_13.md` + status updates on the backlog item.

## CRITICAL REFRAME (plan-review fold)

The BL-NEW-CYCLE-CHANGE-AUDIT backlog entry framed this as "SCAN_INTERVAL_SECONDS decreased from 300s to 60s; audit what broke." Plan-review reviewer (methodology vector) verified via `git log -S "SCAN_INTERVAL_SECONDS" -- scout/config.py` that **gecko-alpha has had `SCAN_INTERVAL_SECONDS = 60` since the initial scaffold commit `bbf6810` (2026-03-20).** The "300s era" referenced in the backlog (and in BL-053's design doc) is **coinpump-scout's** history — `gecko-alpha` was scaffolded from coinpump-scout and inherited some design docs written assuming the upstream's 300s cycle.

So the audit's real question is **NOT** "what broke when we changed cycle from 300 to 60?" It is:

> **For each module / design doc whose author wrote cycle-frequency math (regardless of whether they were thinking in coinpump-scout 300s heritage or gecko-alpha 60s native), does that math still hold at gecko-alpha's actual 60s cycle?**

The set of findings is likely similar — modules with design docs inherited from coinpump-scout will have 300s-era math that's wrong at 60s. BL-053 is the canonical instance: its design doc references "300s cycle → 12 req/hr" math; the writer was deployed into gecko-alpha at 60s without re-doing the math. That's the actual failure pattern, not a "cycle change."

**Goal (reframed):** identify every module / design doc whose cycle-frequency math was authored against a cycle assumption that differs from gecko-alpha's actual 60s cycle, and classify whether the math holds at 60s.

**Architecture:** Three-pass audit.
- **Pass 0 (pre-read)**: read `tasks/findings_silent_failure_audit_2026_05_11.md` §2.2/§2.6/§2.9 to avoid duplicate findings; cite cross-references where they overlap.
- **Pass 1 (inventory)**: enumerate every place where cycle-frequency math appears in code or design docs.
- **Pass 2 (classification)**: per-site classify on a **four-bucket** axis (Phantom / Watch / Borderline / Broken — plus a "Phantom-fragile" qualifier for undocumented-limit dependencies); with severity + concrete fix-shape.

**Tech Stack:** grep, ast, sqlite3 (prod-state probes for per-cycle row rates), `git log` (per-module commit-time anchor for design-era), optional `WebFetch` (provider rate-limit docs). No runtime code modified.

**Branch:** `audit/cycle-change-2026-05-13` (already created from master at `325369d`).

---

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Config-change-impact / assumption-validity audits | none found | Project-internal; same shape as the silent-failure audit |

Verdict: project-internal audit, no Hermes opportunity.

## Drift-check

No prior `findings_cycle_change_audit_*.md` exists (Glob verified). Methodology composes with `findings_silent_failure_audit_2026_05_11.md` — that audit was **table-freshness-based** (does the writer still produce rows?); this audit is **assumption-validity-based** (does the code's math still hold given the cycle reality?). The two audits' findings can cross-reference where the same module appears in both.

Known-concrete instance: BL-053 (CryptoPanic). Design doc assumed 300s cycle → 12 req/hr; actual at 60s is 60 req/hr at low end of 50-200 req/hr CryptoPanic free-tier band. Carried over from coinpump-scout design-doc context. Deactivated.

## Audit scope — modules / files to inspect

### Tier A — Direct `SCAN_INTERVAL_SECONDS` callers + `*_CYCLES` settings

1. `scout/main.py:1567` — `wait_for(..., timeout=settings.SCAN_INTERVAL_SECONDS)`. Direct usage.
2. `scout/config.py:77` — `HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES = 1` (multiplies SCAN_INTERVAL).
3. Grep `_CYCLES` for any others (plan-review confirmed only 1 currently exists).

### Tier B — Per-cycle external API callers (running inside `run_cycle`)

**Plan v1 missed Helius/Moralis (holder_enricher); plan-review fold adds:**

4. `scout/ingestion/coingecko.py` — CoinGecko markets + trending (rate-cap at `COINGECKO_RATE_LIMIT_PER_MIN = 25`)
5. `scout/ingestion/dexscreener.py` — DexScreener boosts poller
6. `scout/ingestion/geckoterminal.py` — GeckoTerminal trending pools
7. `scout/safety.py` — GoPlus security checks (**per-token sub-loop fan-out**)
8. `scout/ingestion/held_position_prices.py` — held-position refresh (per-position fan-out)
9. `scout/counter/detail.py` — counter-arg LLM + CoinGecko detail
10. `scout/briefing/collector.py` — briefing fetches (fear/greed, defi-llama, coinglass, cryptopanic)
11. `scout/news/cryptopanic.py` — BL-053 (known-broken case; confirm)
12. `scout/ingestion/holder_enricher.py` — **NEW: Helius + Moralis (fan-out per token)**
13. `scout/mirofish/client.py` — **NEW: MiroFish API (per gated alert)**
14. `scout/main.py:_safe_counter_followup` — **NEW: Anthropic API (per alert)**
15. `scout/chains/mcap_fetcher.py` — **NEW: DexScreener fetch per chain-tracker entry**

### Tier B2 — Sub-loop fan-out (NEW, plan-review fold)

For every Tier-B entry that's invoked inside a per-token loop in `run_cycle`, methodology must compute:

```
rate = (cycle_freq_per_hour) × (tokens_per_cycle) × (calls_per_token)
     = (60 cycles/hr at 60s) × (N tokens) × M
```

where `tokens_per_cycle` is the candidate-pool size after the aggregator + MIN_SCORE gate. **Cannot be assumed; must be measured.** Probe approach:
- Read `scout/main.py:466-911` for per-token `for ... in` loops; identify which Tier-B calls fire inside each
- Query prod-DB: `sqlite3 /root/gecko-alpha/scout.db "SELECT strftime('%Y-%m-%d %H', created_at) hour, COUNT(*) c FROM candidates WHERE created_at > datetime('now','-7 days') GROUP BY hour ORDER BY hour DESC LIMIT 24"` to get per-hour candidate volume → divide by 60 cycles/hr = avg tokens/cycle
- Cross-check against MIN_SCORE filter at `config.py:21`

### Tier C — Per-cycle alert / write paths (operator-visible)

16. `scout/velocity/detector.py` — `VELOCITY_TOP_N = 10` max alerts per cycle
17. `scout/spikes/detector.py` — has "every cycle for 7 days" comment
18. `scout/main.py:1463` — peak-price updates ("every cycle" comment)

### Tier C2 — Per-cycle DB write rates (NEW, plan-review fold)

If a watchdog SLO was calibrated for "expect ≥ 1 row per 5 min" but the writer now fires every 60s, the SLO is silently over-provisioned (still passes, but cannot catch under-write regressions). Inversely, retention/pruning rules may have assumed slower rates.

19. `scout/db.py log_holder_snapshot` (called from `main.py:713`) — one row per enriched token per cycle
20. `scout/db.py log_volume_snapshot` (called from `main.py:721`) — same shape
21. `scout/db.py log_score` (called from `main.py:781`) — one row per scored token per cycle
22. `scout/db.py cache_prices` (called from `main.py:530`) — bulk upsert per cycle
23. `scout/chains/events.py safe_emit` (called from `main.py:405,782,875`) — per-token + per-alert event writes

For each: read the writer + read any `*_RETENTION_*` / pruning code in `db.py`; cross-reference against §12a (freshness SLO + watchdog from CLAUDE.md).

### Tier D — Decoupled-by-design (verification only)

24. `scout/narrative/agent.py` — `NARRATIVE_POLL_INTERVAL = 1800`
25. `scout/secondwave/detector.py` — `SECONDWAVE_POLL_INTERVAL = 1800`
26. `scout/social/lunarcrush/` — `LUNARCRUSH_POLL_INTERVAL = 300`
27. `scout/chains/tracker.py` — `CHAIN_CHECK_INTERVAL_SEC = 300`
28. `scout/trading/` — `TRADING_EVAL_INTERVAL = 1800`
29. `scout/social/telegram/listener.py` — `TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC = 300`
30. `scout/perp/` — WS-driven, `PERP_WS_PING_INTERVAL_SEC = 20`

**For each Tier-D site, two verification steps (plan-review fold):**
- (a) cadence-controlling line uses the module's own `*_INTERVAL`?
- (b) **loop is launched ONCE at startup via `asyncio.create_task` from `main.py` startup region, NOT spawned inside `run_cycle`?** (grep `asyncio.create_task` in `main.py`)

Plus check wall-clock-gated-but-inside-`run_cycle` candidates (plan-review fold):
31. `scout/main.py:_maybe_emit_heartbeat` (`HEARTBEAT_INTERVAL_SECONDS = 300`)
32. `scout/main.py:1407 outcome_check_interval = 3600` (hardcoded literal)
33. `scout/main.py:361 briefing_loop` — hardcoded `asyncio.sleep(60)` + `>11h` gap

### Tier E — Design-doc math statements (NEW grep patterns, plan-review fold)

Original Tier E grep: `req/hr|req/min|per cycle|cycle frequency|rate limit|free tier|free-tier|throttle`

**Plan-review fold — extended pattern:**
- `every \d+ ?(s|sec|second)`
- `every \d+ (min|minute)`
- `\d+s cycle` / `\d+-min cycle` / `\d+-minute cycle`
- `interval\s*=`
- `calls?/hour` / `calls?/hr` / `calls?/day`
- `at our current cycle`
- Math-statement shape `~?\d+\s*(req|calls?|alerts?)\s*/\s*(hr|min|day)`
- Provider-name proximity: `(coingecko|goplus|dexscreener|geckoterminal|helius|moralis|cryptopanic|miroFish|anthropic).{0,30}(rate|limit|req|interval|cycle)`

Known design-doc candidates (from plan-review scope coverage):
- `docs/superpowers/specs/2026-04-09-narrative-rotation-agent-design.md:183` — "~2-4 calls per cycle, ~8-16 calls/hour"
- `docs/superpowers/specs/2026-04-09-early-detection-lunarcrush-design.md:85` — "poll every 5 min and need ~2 calls per cycle"
- `docs/superpowers/specs/2026-04-10-second-wave-detection-design.md:7,478` — "1-2 CoinGecko API calls per cycle"
- `docs/superpowers/specs/2026-04-10-conviction-chains-design.md:318,364,791` — "tracker runs every 5 minutes", "~100 events/hour"
- `docs/superpowers/specs/2026-04-20-bl053-cryptopanic-news-feed-design.md:31` — known canonical case
- `docs/superpowers/specs/2026-04-23-bl060-paper-mirrors-live-design.md:197` — "scored_candidates regenerates each 15-min cycle" (15-min framing — would be missed by the original grep without the new "15-min cycle" pattern)

### Tier F — Calibration-era non-INTERVAL settings (NEW, plan-review fold)

Settings that are wall-clock but were calibrated against an assumed cycle frequency:

34. `VELOCITY_DEDUP_HOURS = 4` (`config.py:182`) — at higher cycle rate, more re-evaluations of same token; calibrated against unknown era
35. `LUNARCRUSH_DEDUP_HOURS = 4` (`config.py:231`) — same shape
36. `SLOW_BURN_DEDUP_DAYS = 7` (`config.py:167`) — same
37. `SECONDWAVE_DEDUP_DAYS = 7` (`config.py:212`) — same
38. `FEEDBACK_PIPELINE_GAP_THRESHOLD_MIN = 60` (`config.py:498`) — at 60s cycle, 60 consecutive missed cycles before alert; verify operator intent
39. `PAPER_STARTUP_WARMUP_SECONDS = 180` (`config.py:330`) — was this 180s = 0.6 cycles (at 300s) or 3 cycles (at 60s)? Burst-suppression semantics differ
40. `CACHE_TTL_SECONDS = 1800` in `scout/counter/detail.py:17` — hardcoded module-level constant

## Audit methodology (v2)

### Pass 0 — Pre-read overlap context

- [ ] Read `tasks/findings_silent_failure_audit_2026_05_11.md` §2.2 (cryptopanic), §2.6 (perp_anomalies), §2.9 (Class-3 rendering). Note overlapping subjects for cross-reference.

### Pass 1 — Inventory + design-era anchor per module

For each Tier-A/B/B2/C/C2/E/F site:
1. **Locate the assumed cycle.** Per plan-review methodology fold: cycle assumption is **per-module**, not global. Find the assumption by either:
   - Reading the module's design doc (preferred) — e.g., `docs/superpowers/specs/<module>-design.md`
   - Reading the introducing commit message (`git log --follow --diff-filter=A -- <file>`) — sometimes states cycle context
   - Reading inline code comments referencing cycle (`# at 300s cycle this gives ...`)
   - **Defaulting to "unknown / unstated"** — itself a finding (a math claim with no assumption documented is brittle)
2. **Compute current math at 60s cycle** for the rate / write rate / dedup window / etc.
3. **Look up the external constraint** (preferred order):
   - WebFetch the provider's rate-limit doc page (if practical mid-session)
   - Otherwise, count `429` events in production `journalctl` over last 30 days (`journalctl -u gecko-pipeline | grep "status.*429"`)
   - Last resort: "429 handler exists" as proxy + flag as "constraint inferred, not documented"
4. **Run false-positive screen** (plan-review fold). For each non-Phantom row, check the call site for:
   - Local cache lookups (`_cache`, `lru_cache`, `_seen`, `_last`) — effective rate may be lower than nominal
   - Time-gated guards inside the loop (`datetime.now() - last_X > Y`) — effectively self-paced even inside `run_cycle`
   - Recent provider rate-limit increases (check provider changelog if available)

### Pass 2 — Four-bucket classification

| Bucket | Definition |
|---|---|
| **Phantom** | Current math has ≥ 3× headroom vs **documented** constraint; constraint is stable. |
| **Phantom-fragile** | Current math has ≥ 3× headroom vs constraint, BUT constraint is undocumented / volatile / from a provider with history of unilateral tightening (DexScreener, GeckoTerminal, GoPlus — none publishes a public rate card). |
| **Watch** | Current math has 1.5×–3× headroom vs constraint. One upstream regime shift or traffic spike tips it. (NEW bucket, plan-review fold) |
| **Borderline** | Current math has < 1.5× headroom, OR matches/just-fits the constraint. One bad cycle or burst tips over. (BL-053 is this case at 60/200 = 30% of band, well into Borderline.) |
| **Broken** | Current math exceeds the constraint, OR operator-experience target is violated. |

For each non-Phantom row, write a per-finding details section: severity, evidence (the math + the constraint source), and fix-shape (`add throttle`, `widen interval`, `decouple from cycle`, `move setting from cycle-coupled to wall-clock`).

### Operator-experience target sources (plan-review fold)

Sources to check beyond the original `feedback_*.md` grep:
- `tasks/lessons.md` (canonical lessons)
- `~/.claude/projects/C--projects-gecko-alpha/memory/feedback_*.md` (rules learned)
- `git log --grep="alert.{0,5}noise\|too noisy\|noisy"` (commit messages flagging noise issues)
- `backlog.md` entries with "deferred for noise" / "operator complained"
- Operator commentary in PR review comments (`gh pr list --state merged --limit 50` then `gh pr view N --comments` selectively)

If no operator-experience target documented for a site: **flag the absence as a finding** rather than fabricate a heuristic.

## Output shape

`tasks/findings_cycle_change_audit_2026_05_13.md` structure:

```markdown
**New primitives introduced:** NONE -- read-only audit doc.

# Cycle-Change Audit (BL-NEW-CYCLE-CHANGE-AUDIT) -- 2026-05-13

## Critical reframe (per plan-review)

[Restate the framing-error finding: gecko-alpha was always 60s; the audit
question is per-design-doc-vs-60s, not global-transition.]

## Per-module verdict table

| Module | Assumed cycle | Source of assumption | Current math (60s) | Constraint (source) | Verdict | Severity | Fix shape |
| --- | --- | --- | --- | --- | --- | --- | --- |
...

## Per-finding details (non-Phantom only)
...

## Cross-references to silent-failure audit
...

## Carry-forward
[Each non-Phantom finding: file separate BL-NEW-* / bundle into existing item / no follow-up]

## §9b promotion data point
[Brief note for the §9b structural-attribute-verification promotion candidate.]
```

## File structure

**Files to create:**
- `tasks/findings_cycle_change_audit_2026_05_13.md`

**Files to modify (optional bundle):**
- `backlog.md` — flip `BL-NEW-CYCLE-CHANGE-AUDIT` PROPOSED → SHIPPED with link
- `backlog.md` — flip `BL-NEW-PARSE-MODE-AUDIT` PROPOSED → SHIPPED with PR #111 link (chore bundle)

**Files NOT to touch:**
- All `scout/` production code
- All test files

## Self-review notes (v2)

1. **Sample size**: ~40 named sites across 6 tiers. Per-site reading required; not statistical.
2. **External rate-limit doc availability**: WebFetch may not be reliable mid-session for every provider; fallback is `journalctl 429` count.
3. **Operator-experience targets**: defer to documented evidence; flag absence as finding rather than fabricate.
4. **Sub-loop fan-out**: prod-DB probe required for `tokens_per_cycle`. Cannot be assumed.
5. **Framing-error finding itself**: the audit's own backlog entry had a wrong premise (300→60 transition). That's itself surfaceable in the findings doc — adds to §9b promotion evidence (assumptions baked into proposal text without verification).

---

## Task 0: Pre-read overlap audit (plan-review fold)

- [ ] **Step 0.1: Read `tasks/findings_silent_failure_audit_2026_05_11.md` §§2.2/2.6/2.9** and note overlapping subjects (cryptopanic, perp_anomalies, Class-3 rendering).

- [ ] **Step 0.2: Verify the framing error in repo history.**

Run: `git log --all -S "SCAN_INTERVAL_SECONDS" -- scout/config.py`
Confirm: only commit is `bbf6810 chore: import coinpump-scout scaffold` with value `60`. Already verified at plan v2 time.

Run: `git show bbf6810:scout/config.py | grep SCAN_INTERVAL_SECONDS`
Confirm: `SCAN_INTERVAL_SECONDS: int = 60` in the initial scaffold.

Record evidence for the "Critical reframe" section.

---

## Task 1: Tier A + Tier B inventory

- [ ] **Step 1.1: Tier A direct callers + `*_CYCLES` settings**

Run: `grep -nE "_CYCLES|SCAN_INTERVAL_SECONDS" scout/`
List every result; note expected hits (`config.py:77`, `main.py:1567`).

- [ ] **Step 1.2: Tier B file inventory + sub-loop tagging**

For each of 12 Tier-B files (per scope above), read the public function called from `run_cycle` (or transitively reachable). For each, capture:
- API endpoint hit
- Inside a per-token loop? (yes/no) — flag for Tier B2
- Inline rate limiter (yes/no/which kind)
- 429 handling (yes/no)

- [ ] **Step 1.3: Locate per-module assumed cycle**

For each Tier-B file, find the assumption (per Pass 1 methodology):
- Module's design doc → if found, extract the cycle assumption
- Introducing commit message → `git log --follow --diff-filter=A -- scout/<path>`
- Inline cycle comments
- **Or mark "unstated"** — itself a finding

---

## Task 2: Tier B2 — sub-loop fan-out methodology (NEW)

- [ ] **Step 2.1: Identify all per-token loops in `run_cycle`**

Read `scout/main.py:466-911` (`run_cycle` body + tail). Mark every `for token in ...:` / `for cand in ...:` loop and record which Tier-B calls fire inside each.

- [ ] **Step 2.2: Probe prod-DB for `tokens_per_cycle`**

SSH to srilu-vps, run:
```
sqlite3 /root/gecko-alpha/scout.db "SELECT strftime('%Y-%m-%d %H', created_at) hour, COUNT(*) c FROM candidates WHERE created_at > datetime('now','-7 days') GROUP BY hour ORDER BY hour DESC LIMIT 24"
```
Compute avg candidates/hour ÷ 60 cycles/hr = avg tokens/cycle. Note variance.

- [ ] **Step 2.3: Cross-check MIN_SCORE filter**

Read `scout/config.py:21` for `MIN_SCORE` value. Confirm post-MIN_SCORE candidate count is the right `tokens_per_cycle` for Tier-B2 math, not pre-filter.

---

## Task 3: Tier C + Tier C2 (per-cycle alert + DB write rates)

- [ ] **Step 3.1: Tier C — alert/write rate math per site**
Per-site math: cycle_freq × per-cycle-cap.

- [ ] **Step 3.2: Tier C2 — DB write rate per table**
Per-table math + cross-check any §12a SLO + pruning rules in `db.py`.

- [ ] **Step 3.3: Operator-experience target sources (plan-review fold)**

Grep over all sources listed in "Operator-experience target sources" section above. If a target is documented, cite it. If absent, flag the absence in the findings doc.

---

## Task 4: Tier D verification + Tier E design-doc grep

- [ ] **Step 4.1: Tier D — confirm decoupling + once-at-startup launch**

For each Tier-D file, locate:
- (a) cadence-controlling line in the module's own loop
- (b) the `asyncio.create_task(...)` call in `main.py` startup region that launches it ONCE

If both: mark Decoupled (safe).

- [ ] **Step 4.2: Tier E — extended grep over design docs**

Run extended pattern (see Tier E above). List every matching file + line + math claim.

- [ ] **Step 4.3: Tier F — calibration-era non-INTERVAL settings**

For each of 7 settings in Tier F, document the claim and current behavior. Most likely outcome: "unstated assumption — flag the absence."

---

## Task 5: Classification pass + per-finding details

- [ ] **Step 5.1: Apply 4-bucket classification (Phantom / Phantom-fragile / Watch / Borderline / Broken)**

- [ ] **Step 5.2: False-positive screen** — per Pass 1 step 4

- [ ] **Step 5.3: Per-finding details** — non-Phantom only

- [ ] **Step 5.4: Cross-reference silent-failure audit findings**

- [ ] **Step 5.5: Carry-forward** — each non-Phantom finding → backlog filing recommendation

- [ ] **Step 5.6: §9b promotion data point** — brief composition note

- [ ] **Step 5.7: Commit findings doc**

```bash
git add tasks/findings_cycle_change_audit_2026_05_13.md
git commit -m "docs(audit): cycle-change audit findings — N modules classified, M flagged (BL-NEW-CYCLE-CHANGE-AUDIT)"
```

---

## Task 6: Backlog status updates (optional bundle)

- [ ] **Step 6.1: Flip BL-NEW-CYCLE-CHANGE-AUDIT status** (with framing-error fold note)

- [ ] **Step 6.2: Flip BL-NEW-PARSE-MODE-AUDIT status** (chore bundle, PR #111 link)

- [ ] **Step 6.3: Commit**

```bash
git add backlog.md
git commit -m "docs(backlog): flip BL-NEW-CYCLE-CHANGE-AUDIT + BL-NEW-PARSE-MODE-AUDIT to SHIPPED"
```

## Rollback plan

Doc-only PR. Rollback is `git revert <merge-sha>`. No production impact possible.

## Deploy plan

After PR merges: no deploy. Concrete remediations from findings get filed as their own backlog items with their own ship paths.
