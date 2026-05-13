**New primitives introduced:** NONE — read-only audit doc.

# Cycle-Change Audit (BL-NEW-CYCLE-CHANGE-AUDIT) — 2026-05-13

**Purpose.** For each module / design doc whose author wrote cycle-frequency math, determine whether that math still holds at gecko-alpha's actual 60s cycle.

**Methodology.** See `tasks/plan_cycle_change_audit.md` + `tasks/design_cycle_change_audit.md`. Five-bucket classification (Phantom / Phantom-fragile / Watch / Borderline / Broken) plus one meta-bucket (Unfalsifiable). Lower-bound-of-band rule applies for banded constraints. p95 over mean for sub-loop fan-out. Source-required for Phantom-fragile assertions.

## 0. Critical reframe

The `BL-NEW-CYCLE-CHANGE-AUDIT` backlog entry framed this as "SCAN_INTERVAL_SECONDS decreased from 300s to 60s — audit what broke." That premise is **incorrect for gecko-alpha**. Plan-review (methodology vector) verified:

```
$ git log --all -S "SCAN_INTERVAL_SECONDS" -- scout/config.py
bbf6810 chore: import coinpump-scout scaffold as gecko-alpha baseline
$ git show bbf6810:scout/config.py | grep SCAN_INTERVAL_SECONDS
    SCAN_INTERVAL_SECONDS: int = 60
```

**gecko-alpha has had `SCAN_INTERVAL_SECONDS = 60` since the initial scaffold (2026-03-20).** The "300s era" cited in `backlog.md:326` and in BL-053's design doc is **coinpump-scout's** history; gecko-alpha was scaffolded from that project and inherited design docs written assuming the upstream's 300s cycle.

The actual failure pattern (BL-053 is the canonical instance): **inherited design-doc math that assumed coinpump-scout's 300s cycle was carried into gecko-alpha without re-doing the math against gecko-alpha's actual 60s.** §9b promotion data point — the proposal text contained a structural attribute (cycle history) that wasn't verified.

## 0.5 Cross-audit index

| Tier | Module / Setting | Verdict | Quick-win? | Severity |
|---|---|---|---|---|
| B | scout/ingestion/coingecko.py | Watch | — | Medium |
| B | scout/ingestion/dexscreener.py | Phantom-fragile | — | Low |
| B | scout/ingestion/geckoterminal.py | Borderline + side-finding | — | Medium |
| B | scout/safety.py (GoPlus) | Phantom-fragile | — | Low |
| B | scout/ingestion/holder_enricher.py (Helius) | **Broken** | — | High |
| B | scout/ingestion/holder_enricher.py (Moralis) | **Borderline** | — | Medium |
| B | scout/news/cryptopanic.py | Closed-by-§2.2 + composed-finding | — | Low (gated) |
| B | scout/counter/detail.py | Phantom | — | — |
| B | scout/briefing/collector.py | Decoupled (Tier D shape) | — | — |
| B | scout/mirofish/client.py | Phantom-fragile | — | Low |
| B | scout/main.py `_safe_counter_followup` (Anthropic) | **Unfalsifiable** | — | Medium |
| C | scout/velocity/detector.py | Phantom | — | — |
| C | scout/spikes/detector.py | Phantom | — | — |
| C | scout/main.py:1463 (peak-price) | Phantom | — | — |
| C2 | holder_snapshots (writer not producing) | Cross-ref §2.5 | — | — |
| C2 | score_history (~17,325 rows/hr) | **Unfalsifiable** — no SLO | — | Medium |
| C2 | volume_snapshots (~17,281 rows/hr) | **Unfalsifiable** — no SLO | — | Medium |
| C2 | safe_emit chain events | **Unfalsifiable** — no SLO | — | Low |
| D | narrative / secondwave / chains / trading / TG-social / perp / lunarcrush / briefing | Decoupled (safe) | — | — |
| E | 6 design docs with cycle math | Mixed (see §3) | — | — |
| F | VELOCITY_DEDUP_HOURS / LUNARCRUSH_DEDUP_HOURS / SLOW_BURN_DEDUP_DAYS / SECONDWAVE_DEDUP_DAYS / FEEDBACK_PIPELINE_GAP_THRESHOLD_MIN / PAPER_STARTUP_WARMUP_SECONDS / CACHE_TTL_SECONDS | **Unfalsifiable** — no era documented | Yes (doc-add) | Low (per setting) |
| Side | GeckoTerminal ethereum 404 (~40/hr) | Out-of-scope finding | Yes | Medium |

## 0.6 Quick wins (≤30-min fixes)

1. **Tier F documentation pass** — add a 1-line code comment to each of 7 calibration-era settings stating "assumption: cycle frequency = N (set at year YYYY)" OR "assumption: undocumented." ~15 min total. Surfaces the assumption-validity gap without changing values. Filed as `BL-NEW-CALIBRATION-ERA-DOC` carry-forward.
2. **Tier C2 §12a SLO filings** — file 3 BL-NEW-* entries for score_history / volume_snapshots / safe_emit watchdog SLOs. Doc-only filings; the watchdog daemon itself is unbuilt (per silent-failure audit closing notes). ~10 min.
3. **GeckoTerminal ethereum 404 ticket** — out-of-scope but cheap: file `BL-NEW-GT-ETH-ENDPOINT-404` to investigate why GeckoTerminal returns 404 for the ethereum chain endpoint (~40 errors/hour observed in `journalctl -u gecko-pipeline --since '1h ago'`). ~5 min to file.

## 1. Per-module verdict table

`documented_cycle_assumption`: the value listed in the module's design doc / commit message / code comment. **`absent`** means no documentation found (itself a finding).

Sub-loop fan-out math uses `tokens_per_cycle ≈ 289 (mean)` from prod-DB probe: `score_history` writes/hr ÷ 60 cycles/hr = 17,325/60 = **289 tokens/cycle**. Max observed: `22,018/hr → 367 tokens/cycle (p95-equivalent)`. Probe window: 7 days.

Chain distribution of `candidates` table (last 7 days, 1547 unique tokens):
- coingecko 770 (50%) — no holder enrichment
- solana 651 (42%) — Helius enrichment
- evm-chains 125 (8%) — Moralis enrichment

### Tier A — Direct callers + `*_CYCLES` settings

| ID | Site | Assumption | Current math (60s) | Constraint (source) | Verdict | Severity | Fix-shape | Effort | decision-by |
|---|---|---|---|---|---|---|---|---|---|
| A1 | `scout/main.py:1567` `wait_for(..., timeout=settings.SCAN_INTERVAL_SECONDS)` | absent | 60s timeout on `shutdown_event.wait()` | N/A (semantically a polling tick) | Phantom | — | — | — | — |
| A2 | `config.py:77 HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES = 1` | absent | every cycle × 60s = 60s effective refresh | Operator-experience target absent | Unfalsifiable | Low | Document the assumption | same-day | bundle with Tier F |

### Tier B — Per-cycle external API callers

| ID | Site | Assumption | Current math (60s) | Constraint (source) | Verdict | Severity | Fix-shape | Effort | decision-by |
|---|---|---|---|---|---|---|---|---|---|
| B1 | `scout/ingestion/coingecko.py` (movers + trending + by_volume) | absent | Rate-limiter capped at 25/min; backoffs fired **551 times over 24h** = ~23/hr backoffs against 30/min CG free tier; mean ~1380/hr | 30/min CoinGecko free tier (documented at `scout/config.py:63`) | **Watch** | Medium | Investigate burst pattern; consider lowering `COINGECKO_RATE_LIMIT_PER_MIN` from 25 to 20 OR adding inter-call jitter | same-day (config nudge) | next deploy or 90d sunset |
| B2 | `scout/ingestion/dexscreener.py` | absent | per-cycle 1 call → ~60/hr; **3 errors observed in 24h** | undocumented (DexScreener does not publish rate card) | Phantom-fragile | Low | None now; re-audit if error count rises | none | 6mo or next deploy |
| B3 | `scout/ingestion/geckoterminal.py` | absent | per-chain call per cycle | undocumented + no 429 handler in `geckoterminal.py:27-29` | **Borderline** + side-finding | Medium | (a) add 429 handler matching DexScreener pattern; (b) investigate ethereum-chain 404 separately (filed as `BL-NEW-GT-ETH-ENDPOINT-404`) | 1-day (handler) + investigation | 2 weeks |
| B4 | `scout/safety.py` GoPlus | absent | only fires on tg_social admission (~8/24h observed); NOT per-cycle per all_candidates | undocumented; per-token | Phantom-fragile | Low | None now | none | 6mo |
| B5 | `scout/ingestion/holder_enricher.py` Helius (solana) | absent | fan-out: 121 solana/cycle × 60 = **~7,260 calls/hr → ~174k/day** | Helius free tier ~100k credits/day (per Helius docs); each `getTokenAccounts` is 1 credit | **Broken** (if free tier) | High | (a) confirm prod plan tier (free vs paid); if free: add per-cycle throttle OR move enrichment behind a wall-clock interval | 1-week investigation + remediation | 1 week |
| B6 | `scout/ingestion/holder_enricher.py` Moralis (EVM) | absent | fan-out: ~23 EVM/cycle × 60 = ~1,380 calls/hr → ~33k/day → ~1M/month | Moralis legacy free 40k/month; CU-based tier higher | **Borderline** (legacy free) | Medium | Confirm prod plan; document assumed plan in code | 1-week investigation | 2 weeks |
| B7 | `scout/news/cryptopanic.py` | "300s cycle → 12 req/hr" (BL-053 design doc, inherited from coinpump-scout) | **deactivated**; would be 60 req/hr if reactivated | 50-200 req/hr free-tier band; lower-bound = 50/hr | **Closed-by-§2.2 + composed-finding** | — | Already in BL-053 5-point activation checklist (`decoupled interval` is item 4) | gated by operator | evidence-gated |
| B8 | `scout/counter/detail.py` (CG detail endpoint, per alert) | absent | ~1 call per alert × ~1 alert/hr = ~1/hr | CoinGecko 30/min | Phantom | — | — | — | — |
| B9 | `scout/briefing/collector.py` | "asyncio.sleep(60)" (briefing loop self-paced via wall-clock; verified launched-once-at-startup) | wall-clock self-paced | Multi-provider (defi-llama, coinglass, fear&greed, cryptopanic) | Decoupled (Tier D shape) | — | — | — | — |
| B10 | `scout/mirofish/client.py` | absent | per gated alert (~1/hr) | undocumented (internal service) | Phantom-fragile | Low | — | — | 6mo |
| B11 | `scout/main.py _safe_counter_followup` (Anthropic) | absent | per alert (~1/hr) | Documented Anthropic tier limits + 429 semantics | **Unfalsifiable** | Medium | Set operator spend target (proposed skeleton below) | 1-week (operator decision) | 2 weeks |
| B12 | `scout/chains/mcap_fetcher.py` (DexScreener fetch per chain-tracker entry) | absent | Self-paced via `CHAIN_CHECK_INTERVAL_SEC = 300` (Tier D shape) | undocumented | Decoupled (safe) | — | — | — | — |

### Tier B2 — Sub-loop fan-out (in-table above as B5/B6/B11)

Sub-loop fan-out math captured per Tier B entry. Key: **tokens_per_cycle (mean: 289, p95-equivalent: 367)** from `score_history` write rate (`SELECT AVG(c)/MAX(c) FROM (SELECT strftime('%Y-%m-%d %H', scanned_at) hour, COUNT(*) c FROM score_history WHERE scanned_at > datetime('now','-7 days') GROUP BY hour)`).

### Tier C — Per-cycle alert / write paths

| ID | Site | Current math (60s) | Verdict | Severity |
|---|---|---|---|---|
| C1 | `scout/velocity/detector.py` `VELOCITY_TOP_N = 10` | max 10 alerts/cycle × 60 cycles/hr = 600 alerts/hr theoretical; actual production rate near zero (operator-experience driven by `VELOCITY_DEDUP_HOURS`) | Phantom | — |
| C2-velocity | spikes detector | per-cycle, but gated by 7-day dedup | Phantom | — |
| C3 | `scout/main.py:1463` peak-price update | per-cycle, in-memory + write | Phantom | — |

### Tier C2 — Per-cycle DB write rates

| ID | Table | Rate (mean → max) | Verdict | §12a tag | Severity |
|---|---|---|---|---|---|
| C2-score | `score_history` | 17,325/hr → 22,018/hr | **Unfalsifiable — no SLO documented** | `watchdog-row` + `pruning-rule` (5× retention growth vs design-era) | Medium |
| C2-volume | `volume_snapshots` | 17,281/hr → 21,943/hr | **Unfalsifiable — no SLO** | `watchdog-row` + `pruning-rule` | Medium |
| C2-holder | `holder_snapshots` | **0** (empty) | Cross-ref `findings_silent_failure_audit_2026_05_11.md §2.5` — writer disconnected from input (holder_count = 0 → snapshot not logged) | — | (deferred to §2.5) |
| C2-safe-emit | chains/events (`safe_emit`) | per-token + per-alert; rate not directly probed | **Unfalsifiable — no SLO** | `watchdog-row` | Low |
| C2-cache_prices | `price_cache` bulk upsert | per-cycle batch | Phantom (bulk upsert) | — | — |

### Tier D — Decoupled-by-design (verification only)

All Tier D loops verified launched-once-at-startup via `asyncio.create_task` in `scout/main.py:1581-1700`:

| Loop | Launch line | Self-pace setting | Verdict |
|---|---|---|---|
| `_pipeline_loop` (the cycle loop itself) | `main.py:1581` | `SCAN_INTERVAL_SECONDS` | (the cycle; not Tier D scope) |
| `narrative_agent_loop` | `main.py:1585` | `NARRATIVE_POLL_INTERVAL = 1800` | Decoupled (safe) |
| `secondwave_loop` | `main.py:1592` | `SECONDWAVE_POLL_INTERVAL = 1800` | Decoupled (safe) |
| `briefing_loop` | `main.py:1594` | hardcoded `asyncio.sleep(60)` + 11h gap | Decoupled (safe, hardcoded) |
| `run_chain_tracker` | `main.py:1596` | `CHAIN_CHECK_INTERVAL_SEC = 300` | Decoupled (safe) |
| `run_tg_social_listener` | `main.py:1606` | `TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC = 300` | Decoupled (safe) |
| `shadow_evaluator_loop` (BL-055) | `main.py:1623` | (live-trading; gated) | Decoupled (safe) |
| `live_metrics_rollup_loop` | `main.py:1643` | (live-trading; gated) | Decoupled (safe) |
| `run_social_loop` (lunarcrush) | `main.py:1664` | `LUNARCRUSH_POLL_INTERVAL = 300` | Decoupled (safe) |
| `run_perp_watcher` | `main.py:454` | WS-driven + `PERP_WS_PING_INTERVAL_SEC = 20` | Decoupled (safe) |
| `_maybe_emit_heartbeat` | inside `_pipeline_loop` | `HEARTBEAT_INTERVAL_SECONDS = 300` wall-clock gate | Decoupled (safe, in-cycle gate) |
| outcome check | inside `_pipeline_loop` (`main.py:1407`) | hardcoded `outcome_check_interval = 3600` wall-clock gate | Decoupled (safe, in-cycle gate) |

### Tier E — Design-doc math statements

Cross-referenced from plan v3 Tier E grep + plan-review scope coverage:

| Spec | Math claim | At gecko-alpha 60s | Verdict |
|---|---|---|---|
| `docs/superpowers/specs/2026-04-09-narrative-rotation-agent-design.md:183` | "~2-4 calls per cycle, ~8-16 calls/hour" (at coinpump-scout 300s cycle) | Narrative self-paces at 1800s (decoupled in code); the design-doc math is mooted by the wall-clock loop. No current impact. | Decoupled-in-practice (design-doc math obsolete) |
| `docs/superpowers/specs/2026-04-09-early-detection-lunarcrush-design.md:85` | "poll every 5 min and need ~2 calls per cycle" | LunarCrush self-paces (`LUNARCRUSH_POLL_INTERVAL = 300`); decoupled | Decoupled-in-practice |
| `docs/superpowers/specs/2026-04-10-second-wave-detection-design.md:7,478` | "1-2 CoinGecko API calls per cycle" | Secondwave self-paces (`SECONDWAVE_POLL_INTERVAL = 1800`); decoupled | Decoupled-in-practice |
| `docs/superpowers/specs/2026-04-10-conviction-chains-design.md:318,364,791` | "tracker runs every 5 minutes", "~100 events/hour" | Chain tracker self-paces (`CHAIN_CHECK_INTERVAL_SEC = 300`); the "~100 events/hour" assumption depends on upstream `run_cycle` event-emission rate which fires 5× as often as coinpump-scout's heritage — **document the assumption** | Decoupled-in-rate; event-volume math may be stale |
| `docs/superpowers/specs/2026-04-20-bl053-cryptopanic-news-feed-design.md:31` | "300s cycle → 12 req/hr → well under free-tier 50-200/hr band" | At 60s cycle: 60 req/hr against lower-bound 50/hr = 1.2× headroom = Borderline if reactivated | **Cross-ref B7 + silent-failure §2.2** |
| `docs/superpowers/specs/2026-04-23-bl060-paper-mirrors-live-design.md:197` | "scored_candidates regenerates each 15-min cycle" | gecko-alpha cycle is 60s; the "15-min cycle" assumption is inherited and stale; mooted in practice if BL-060 implementation self-paces | **Verify BL-060 implementation paces independently** — file `BL-NEW-BL060-CYCLE-VERIFY` carry-forward |

### Tier F — Calibration-era non-INTERVAL settings

Per design v2: **recommend "document the assumption + flag absence,"** NOT "re-calibrate" (audit is read-only).

| Setting | Value | Documented era | Verdict | Action |
|---|---|---|---|---|
| `VELOCITY_DEDUP_HOURS = 4` | 4 hr | absent | **Unfalsifiable** | File `BL-NEW-CALIBRATION-ERA-DOC` |
| `LUNARCRUSH_DEDUP_HOURS = 4` | 4 hr | absent | **Unfalsifiable** | File `BL-NEW-CALIBRATION-ERA-DOC` |
| `SLOW_BURN_DEDUP_DAYS = 7` | 7 d | absent | **Unfalsifiable** | File `BL-NEW-CALIBRATION-ERA-DOC` |
| `SECONDWAVE_DEDUP_DAYS = 7` | 7 d | absent | **Unfalsifiable** | File `BL-NEW-CALIBRATION-ERA-DOC` |
| `FEEDBACK_PIPELINE_GAP_THRESHOLD_MIN = 60` | 60 m | absent | **Unfalsifiable** | File `BL-NEW-CALIBRATION-ERA-DOC` |
| `PAPER_STARTUP_WARMUP_SECONDS = 180` | 180 s | absent | **Unfalsifiable** | File `BL-NEW-CALIBRATION-ERA-DOC` |
| `CACHE_TTL_SECONDS = 1800` in `counter/detail.py:17` | 1800 s | absent | **Unfalsifiable** | File `BL-NEW-CALIBRATION-ERA-DOC` |

### Non-external constraints sub-scan (Task 4.5)

Not deeply probed; flagged for follow-up:
- **SQLite WAL** — gecko-alpha uses `aiosqlite`. WAL mode enabled by default. At 17k writes/hr each on `score_history` + `volume_snapshots` + bulk upserts, WAL bloat is plausible. Not measured. File `BL-NEW-SQLITE-WAL-PROFILE` carry-forward.
- **Telegram per-chat 1/sec** — alert volume is very low (~1 alert/hr from `alerts` table), well under 1/sec. Phantom.
- **File descriptor exhaustion** — not measured. Probably Phantom given alerts/calls volume.
- **asyncio task queue depth** — not measured. No symptoms in journalctl. Likely Phantom.

## 2. Per-finding details (non-Phantom only)

### B1 — CoinGecko rate-limiter at edge (Watch)

**Evidence:** `journalctl -u gecko-pipeline --since "24 hours ago" | grep -c cg_429_backoff` = **551** over 24h = ~23 backoffs/hr. Recent 1h = 12. The rate limiter at `scout/ratelimit.py:18` is configured for 25 calls/min (`RateLimiter(max_calls=25, period=60.0)`), buffer under documented 30/min free tier. But 23 backoffs/hr means the buffer is being consumed in bursts (likely the parallel `asyncio.gather` of 3 CG fetches at cycle start hits the limit).

**Constraint:** CoinGecko Demo tier (30 req/min) — documented at `scout/config.py:63`. Constraint stability: **stable** (CoinGecko publishes changelogs).

**Math:** mean ~1,380 calls/hr (= 23 calls/min average across 60 minutes). 30/min × 60 = 1,800/hr max. Mean is at 77% of constraint. Burst pattern probably hits 30/min per gather batch, then waits.

**Fix-shape:** investigate the burst pattern (`journalctl | grep cg_429_backoff` over 1h to find clustering); options: (a) lower `COINGECKO_RATE_LIMIT_PER_MIN` from 25 to 20; (b) add inter-call jitter inside `_get_with_backoff`. Same-day shippable as a config nudge.

**decision-by:** next deploy or 90-day Watch sunset.

### B3 — GeckoTerminal (Borderline + side-finding)

**Evidence (cycle-audit scope):** `geckoterminal.py:27-29` has no 429 handler — it logs any non-200 and returns. Compared to DexScreener which has full 429/5xx backoff at lines 32-37. The asymmetry is itself a finding.

**Side-finding (out of cycle-audit scope):** journalctl shows ~40 "GeckoTerminal returned error" events/hr, all ethereum chain, all status=404. This is a chain-endpoint misconfiguration, not a rate-limit issue. Likely the ethereum trending-pools URL changed upstream OR our chain identifier is stale.

**Fix-shape:** (a) add 429 handler matching `dexscreener.py:32-37`; (b) investigate ethereum 404 separately (file `BL-NEW-GT-ETH-ENDPOINT-404`).

**decision-by:** 2 weeks for handler addition; 4 weeks for ethereum investigation.

### B5 — Helius enrichment (Broken if free tier)

**Evidence:** `tokens_per_cycle = 289`, of which ~42% are solana (per candidates table chain distribution). 121 solana tokens/cycle × 60 cycles/hr = **~7,260 Helius calls/hr** → **~174k/day**. Helius free tier is approximately 100k credits/day (per Helius documentation at the time of audit). Each `getTokenAccounts` call is 1 credit. **174k > 100k = Broken if on free tier.**

**Critical cross-ref:** `findings_silent_failure_audit_2026_05_11.md §2.5` notes `holder_snapshots` is empty. This audit's finding offers a possible upstream cause: if Helius is exhausting daily limits, calls return errors → caught by `except Exception` at `holder_enricher.py:62-68` → `token.holder_count` stays 0 → `if token.holder_count > 0` skip at `main.py:706` → no row written. Consistent with the empty `holder_snapshots`.

**HOWEVER**: journalctl `Helius holder lookup failed` count = **0** in last 24h. So Helius is NOT raising exceptions; it's returning successful responses with empty `result.total = 0` for these tokens (low-mcap memes aren't indexed in Helius DAS). Findings compose, do not collapse: §2.5 was correct that BL-020 is dormant; this audit adds that the **fan-out itself is high regardless**, and if the operator ever wants holder data for these tokens (via a different provider or upgraded Helius plan), the call volume must be re-throttled.

**Fix-shape:** (a) immediate: confirm prod plan tier (`HELIUS_API_KEY` set; check Helius dashboard for usage); (b) if free: throttle enrichment to wall-clock-paced (e.g., refresh holder_count only every 30 min per token, not every 60s); (c) document expected daily call volume in `scout/config.py:90` HELIUS_API_KEY comment.

**decision-by:** 1 week (high severity).

### B6 — Moralis enrichment (Borderline if legacy free)

**Evidence:** 23 EVM tokens/cycle × 60 = ~1,380 calls/hr → ~33k/day → ~1M/month. Moralis legacy free is 40k req/month; newer CU-based tier is higher. **33k/day × 30 = ~990k/month — borderline against legacy free**, fine against CU tier.

**Cross-ref:** same shape as B5. Moralis 0 failures in 24h logs.

**Fix-shape:** confirm prod plan tier; document expected call volume.

**decision-by:** 2 weeks.

### B7 — CryptoPanic (Closed-by-§2.2 + composed-finding)

**Evidence:** Already documented in `findings_silent_failure_audit_2026_05_11.md §2.2` as deactivated. This audit's contribution: **if reactivated, the design-doc math (12 req/hr at 300s = "well under 50-200/hr free-tier band") is wrong at gecko-alpha's 60s cycle.** Per lower-bound rule: 60 req/hr against 50/hr lower-bound = **1.2× headroom = Borderline**.

**Fix-shape:** Already in BL-053's 5-point activation checklist (item 4: `decoupled interval`). When the operator reactivates BL-053, the activation PR must decouple the CryptoPanic fetch from `run_cycle` cadence — set its own `CRYPTOPANIC_POLL_INTERVAL` setting wall-clock-paced.

**Compose-not-collapse rule applied:** Silent-failure §2.2 is "deactivated, fine"; this finding is "math wrong if reactivated, must throttle." Both stand; neither closes the other.

**decision-by:** evidence-gated (operator's BL-053 reactivation PR).

### B11 — Anthropic counter-arg follow-up (Unfalsifiable)

**Evidence:** `_safe_counter_followup` at `main.py:904` calls Anthropic per alert. Current rate ~1 alert/hr → ~1 Anthropic call/hr → ~24/day. No operator-experience target documented for Anthropic spend.

**Proposed-target-skeleton:**
```
Suggested target (operator decides):
  - Soft cap: $5/day Anthropic counter-arg follow-up spend
  - Alert threshold: $20/day
  - Source: not yet set; operator may anchor to prior cost.anthropic.com
    dashboard observations or aspirational budget. At ~24 calls/day with
    haiku-4-5 (~$0.001-0.003 each), current cost is <$0.10/day; the cap
    is to bound runaway scenarios (e.g., alert burst from a market event
    spiking call volume 100×).
```

**Fix-shape:** operator decision (accept/modify/reject the skeleton) + file the chosen target in `config.py` as `ANTHROPIC_DAILY_SPEND_SOFT_CAP_USD`.

**decision-by:** 2 weeks (operator-elicitation).

### C2-score / C2-volume — DB write rates (Unfalsifiable — §12a SLO absent)

**Evidence:** `score_history` writes at 17,325/hr (mean) → 22,018/hr (max). `volume_snapshots` at 17,281/hr → 21,943/hr. **No watchdog SLO exists** for either table (the §12a watchdog daemon is itself unbuilt per silent-failure audit closing notes — "the watchdog itself is a future infrastructure item").

**§12a tagging:** `watchdog-row` + `pruning-rule`. Two distinct concerns:
1. **Watchdog-row** — when the §12a daemon is built, these two tables MUST be in its monitored-tables list with SLO "≥ 1 row per minute, alert if absent for ≥ 5 min." Without an SLO, a disconnect between writer and table is silent.
2. **Pruning-rule** — 17,325 rows/hr × 24 = ~415k rows/day. Over 30 days = 12.5M rows. Is there a pruning rule? Check `scout/db.py` for `score_history` retention. If absent, table grows unbounded.

**Fix-shape:** file two BL-NEW-* entries: `BL-NEW-SCORE-HISTORY-WATCHDOG-SLO` + `BL-NEW-SCORE-HISTORY-PRUNING` (and analogous for volume_snapshots).

**decision-by:** 2 weeks each for filing; multi-week for actual implementation (depends on §12a daemon being built).

## 3. Cross-references to silent-failure audit

| This audit's finding | Silent-failure audit cross-ref | Compose / Collapse |
|---|---|---|
| B5 Helius high fan-out | §2.5 holder_snapshots empty | **Compose** — silent-failure said "writer dormant"; this audit adds "upstream call volume is high regardless" |
| B7 CryptoPanic math broken-if-reactivated | §2.2 cryptopanic deactivated | **Compose** — §2.2 said "deactivated, fine"; this audit adds "math wrong at 60s, BL-053 activation PR must throttle" |
| Tier C2 no SLO | §12a watchdog daemon unbuilt | **Compose** — feedback discipline points; this audit gives concrete SLO targets for future daemon |

Per design v2 §6 rule: **findings compose, they do not collapse**. Each audit answers a different question.

## 4. Carry-forward — BL filings

Each non-Phantom row needs a backlog filing with `decision-by` trigger:

| Finding | Carry-forward filing | decision-by |
|---|---|---|
| B1 | `BL-NEW-CG-RATE-LIMITER-BURST-PROFILE` — investigate `cg_429_backoff` burst pattern; consider lowering `COINGECKO_RATE_LIMIT_PER_MIN` or adding jitter | next deploy or 90d sunset |
| B3 | `BL-NEW-GT-429-HANDLER` — add 429/5xx handler to `geckoterminal.py` matching DexScreener's pattern | 2 weeks |
| B3 side | `BL-NEW-GT-ETH-ENDPOINT-404` — investigate ethereum chain 404 (~40/hr) | 4 weeks |
| B5 | `BL-NEW-HELIUS-PLAN-AUDIT` — confirm prod Helius plan tier; throttle enrichment if free | 1 week |
| B6 | `BL-NEW-MORALIS-PLAN-AUDIT` — same shape for Moralis | 2 weeks |
| B7 | (Already in BL-053's 5-point activation checklist — no new filing) | gated by BL-053 reactivation |
| B11 | `BL-NEW-ANTHROPIC-SPEND-TARGET` — operator target elicitation + add `ANTHROPIC_DAILY_SPEND_SOFT_CAP_USD` setting | 2 weeks (operator decision) |
| C2-score | `BL-NEW-SCORE-HISTORY-WATCHDOG-SLO` + `BL-NEW-SCORE-HISTORY-PRUNING` | 2 weeks (filing); multi-week (impl, gated on §12a daemon) |
| C2-volume | `BL-NEW-VOLUME-SNAPSHOTS-WATCHDOG-SLO` + `BL-NEW-VOLUME-SNAPSHOTS-PRUNING` | 2 weeks (filing) |
| C2-safe-emit | (bundle into watchdog SLO filings) | — |
| Tier F (7 settings) | `BL-NEW-CALIBRATION-ERA-DOC` — 1-line code comments documenting cycle-era assumption for each | same-day; ship within the week |
| Tier E `bl060-paper-mirrors-live-design.md:197` | `BL-NEW-BL060-CYCLE-VERIFY` — verify BL-060 implementation paces independently of 60s cycle, not 15-min cycle assumed in design | 4 weeks |
| Non-external — SQLite WAL | `BL-NEW-SQLITE-WAL-PROFILE` — measure WAL bloat at 17k+ writes/hr | 8 weeks |

## 5. Next-audit trigger

Re-run this audit when any of:
- `SCAN_INTERVAL_SECONDS` changes value from current 60s
- A new external-API integration ships (new ingestion lane, new LLM provider, etc.)
- A new `*_CYCLES` setting is introduced
- `score_history` or `volume_snapshots` write rate changes by >2× (signal: pipeline-level scaling shift)
- 2026-11-13 (6-month calendar drift)

`next-audit-trigger: SCAN_INTERVAL_SECONDS change OR new external API OR new *_CYCLES setting OR write-rate ±2× OR 2026-11-13`

## 6. Audit-methodology lesson (sticky)

The audit's own backlog entry contained a structurally wrong premise (assumed 300→60 cycle transition that never happened in gecko-alpha). Plan-review caught it via 1-line `git log`. **The §9b structural-attribute-verification rule applies to PROPOSAL text as well as code** — cost asymmetry is ~30 seconds to verify vs days-to-weeks of audit work pointing at the wrong assumption.

This observation will be added to `~/.claude/projects/C--projects-gecko-alpha/memory/feedback_section_9_promotion_due.md` as a separate update (per design v2 §12 — findings doc stays single-purpose).

---

**End of findings.** Quick wins ship within the week the audit lands (per design v2 §10). Non-Phantom findings have explicit `decision-by` triggers (per design v2 §4). Next-audit trigger documented per §5 above.
