# Whole-System Silent-Failure Audit

**New primitives introduced:** NONE in the audit itself; §4 recommends three new primitives (watchdog daemon, audit CLI, API liveness probe). Hermes-first analysis below applies to those.

**Date:** 2026-05-11
**Trigger:** Anthropic credit dry went undetected for 4 days. Operator asked: *"why didn't our existing health-check / Hermes stack catch these? What other silent failures may we not have caught yet?"*
**Method:** Per-table freshness scan across all 54 tables in prod `scout.db` + targeted drill-downs on suspicious patterns.

## Hermes-first analysis

Per CLAUDE.md §7b. Retrofit after operator pointed out the omission.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Database table freshness monitoring | None at hermes-agent.nousresearch.com/docs/skills | Build custom (watchdog daemon) |
| Post-deploy verification / drift detection | None | Build custom (audit CLI) |
| Service watchdog / SLO monitoring | Closest: `webhook-subscriptions` (event-driven runs) — doesn't fit | Build custom |
| API key liveness probing | None | Build custom (liveness probe) |

`awesome-hermes-agent` ecosystem check: repo URL returned 404 on both `NousResearch/` and `Nous-Research/` paths. Result is "no community skill found" with caveat that the canonical hub URL was inaccessible. Custom-build justified for all four primitives.

## Drift-check (CLAUDE.md §7a)

Before scoping the proposed primitives, grep for existing in-tree implementations:

| Proposed primitive | In-tree match? | Decision |
|---|---|---|
| Watchdog daemon | **`scripts/gecko-backup-watchdog.sh`** exists | **Pattern to follow**, not a duplicate. Backup watchdog watches backup-rotation freshness; we need a sibling for table-freshness. **Key lesson encoded:** alerts via curl-direct to Telegram bot API, NOT via `scout.alerter` (per R6 PR review CRITICAL — `scout.alerter` signature is wrong + swallows errors). |
| Audit CLI | **`scripts/bl060_threshold_audit.py`** exists | **Pattern to follow** (aiosqlite + argparse + structured output). The §2.x freshness queries from this report can be cleaned up into a sibling. |
| API liveness probe | No in-tree match | Custom build justified |
| Generic "freshness SLO" framework | `scout/social/telegram/listener.py` has listener_state machine; `scout/live/reconciliation.py` has staleness checks | Per-component health exists; no system-wide framework |

**Drift-check verdict:** all three Priority 1-3 primitives have prior-art patterns to follow inside `scripts/`. Custom build justified for the specific surface (table freshness vs backup rotation), but the **alert-delivery path is already solved** — reuse the gecko-backup-watchdog.sh curl-direct-to-Telegram template.

## §0 Executive summary

**There is no "Hermes stack" in gecko-alpha** — Hermes is a `shift-agent` runtime, not a gecko-alpha component. gecko-alpha has only:
1. A **heartbeat log line** every ~5 minutes with ~10 counters (no thresholds, no alerts, no anomaly detection)
2. **Telegram alerts** for paper-trade-opens only (system health does NOT go to TG)
3. **Manual operator dashboard checks**

No external watchdog. No "alert if N=0 for X minutes." No per-counter SLO. **Every silent failure encountered to date was caught either by operator-asking-a-specific-question or by working a related task.**

**Findings — 9 silent failures across TWO failure classes (revised after shadow_trades diagnosis):**

### Failure class taxonomy (post-cleanup-pass)

| Class | Shape | Fix shape | Example |
|---|---|---|---|
| **Class 1: Writer stopped, nobody watched** | A pipeline writer stopped producing rows (config gate flipped silently, refactor disconnected caller, listener crashed). System state diverges from intended. | Watchdog catches it via row-rate SLO. Audit CLI catches it on demand. | §2.2 cryptopanic, §2.5 holder_snapshots, §2.6 perp_anomalies, §2.7 memecoin chain |
| **Class 2: System worked correctly, operator wasn't told** | Pre-registered automated state change (auto-suspend, kill switch, threshold flip) executed as designed — but reversed an operator-applied state. No notification surface. Operator's mental model diverges from system state. | Wire Telegram alert at the existing write site. Cheap, no daemon needed. | §2.9 auto-suspension reversals (this audit's revision) |

The watchdog daemon / audit CLI catch Class 1. Class 2 needs a different fix shape: **alert at write time**, not "watch and detect later." Distinguishing the two prevents the watchdog from becoming the catch-all hammer.

### Findings table

| # | Finding | Class | Severity | Detected by | Duration |
|---|---|---|---|---|---|
| 1 | `alerts` table — 2 rows total, last write 2026-05-02 | 1 | **CRITICAL** | This audit | 9+ days |
| 2 | `cryptopanic_posts` — empty | 1 | **CRITICAL** | This audit | 22+ days (BL-053) |
| 3 | `shadow_trades` — last write 2026-05-01 → **REFRAMED**: writer intact + correctly idle. **BL-055 unlock is POLICY-blocked, not code-broken.** Allowlist scopes shadow routing to `first_signal`, which is auto-suspended. | n/a (no bug) | **N/A** (was CRITICAL — withdrawn) | Cleanup pass | n/a |
| 4 | `outcomes` — 2 rows total | 1 | **CRITICAL** | This audit | 9+ days |
| 5 | `holder_snapshots` — empty | 1 | HIGH | This audit | Unknown (BL-020 never wired?) |
| 6 | `perp_anomalies` — empty | 1 | HIGH | This audit | 20+ days (BL-054) |
| 7 | Memecoin chain dispatch — last fire 2026-05-04 | 1 | HIGH | This audit | 7+ days |
| 8 | `high_peak_fade_audit` — only 7 rows total | 1 | MEDIUM | This audit | Verify legitimacy |
| **9** | **Auto-suspension reversals are silent** — trending_catch operator-re-enabled 2026-05-10, kill switch RE-suspended 2026-05-11T01:00:26Z with no operator notification | **2** | **HIGH** | Cleanup pass | Ongoing — every auto_suspend event |

Plus 3 known dry-spell items resuming now Anthropic is topped up:
- `learn_logs` (last 2026-05-07) — daily LEARN reflection
- `briefings` (last 2026-05-06) — daily briefing synthesizer
- `predictions` is_control-only since 2026-05-07 (now resuming)

## §0.5 Classification update (post-annotation pass, 2026-05-11)

The git-log suspect-commit annotation pass (§2.x annotations below) collapsed the 8 findings to **5 root-cause shapes**, not 8 independent failures. Future-self reading this audit cold should orient against this taxonomy *before* re-investigating any individual finding from scratch:

- **Legacy-displaced** — §2.1 `alerts`, §2.4 `outcomes`. **NOT bugs.** The legacy memecoin alert path (`alerts` + `outcomes` writers) was displaced by paper-trade signal routing (BL-029 onward → `tg_alert_log`). The 2026-05-02 cutoff aligns with `first_signal` hard_loss removing the last live caller. "Fix" = formal schema retirement decision, NOT writer rewire. A non-trivial slice of the original CRITICAL count is, on closer inspection, **retired-but-not-formally-decommissioned**. Do not re-investigate these as broken writers — they're disconnected by design.
- **Ship-time never-worked** — §2.2 `cryptopanic_posts`, §2.6 `perp_anomalies`. Single deploy commit, zero rows since. "Fix" = diagnose why the writer never ran (listener scheduling / API key / thresholds calibrated for absent regime). NOT regressions.
- **Auto-retirement loop fed by §2.4** — §2.7 memecoin chain dispatch. Closes as side effect of §2.4 work. See §2.7 sequencing pre-registration below.
- **Low-frequency, probably fine** — §2.8 HPF audit. Single SQL classification will tell.
- **Real Class-2 bug, code-fixed pending production verification** — §2.9 auto_suspend silent rendering. See §2.9 closure tracking block.

## §1 Why monitoring missed each failure

### §1.1 The architectural gap

The heartbeat emits these counters every cycle:
```
uptime_minutes, tokens_scanned, candidates_promoted, alerts_fired,
narrative_predictions, counter_scores_memecoin, counter_scores_narrative,
mcap_null_with_price_count, slow_burn_detected_total, slow_burn_coins_skipped_total
```

**None of these counters cover:**
- `alerts` table writes (counter says "alerts_fired" but means in-process counter, not DB row)
- `cryptopanic_posts` writes
- `shadow_trades` writes
- `outcomes` table population
- `holder_snapshots`
- `perp_anomalies` detector activity (BL-054)
- chain_matches per-pipeline freshness
- API key validity (Anthropic, OpenRouter, CoinGecko, GoPlus, Helius, Moralis)
- External-service reachability (Telegram, Discord, MiroFish)

**The heartbeat counters that DO exist are not thresholded.** Even `alerts_fired: 0` in every heartbeat for 9 days produces no alert — the operator must manually grep `journalctl` to notice the zero pattern.

### §1.2 Specific instances of "silent because nobody asks"

| Failure | Heartbeat counter that would have caught it | Threshold rule | Status |
|---|---|---|---|
| Anthropic dry | `narrative_predictions: 0` was visible in every heartbeat for 4 days | Nobody read it | No threshold |
| `alerts` table empty | None | — | No counter |
| `cryptopanic_posts` empty | None | — | No counter |
| `shadow_trades` empty | None | — | No counter |

## §2 Per-finding detail + reproduction

### §2.1 [CRITICAL] `alerts` table — 2 rows total, last write 2026-05-02

```sql
SELECT id, contract_address, chain, conviction_score, alerted_at FROM alerts;
-- Returns ONLY:
-- 1  93Ur4...pump  solana  70.0  2026-05-01T18:40:28Z
-- 2  HDmoj...pump  solana  71.0  2026-05-02T00:00:19Z
```

vs. 309 paper trades opened in the last 7 days. The `alerts` table is supposed to be the canonical "alert dispatched" log, but writes stopped 9 days ago. The two rows that DO exist correlate exactly with the only two rows in `outcomes` table — suggesting this is the **memecoin pipeline alert path** specifically, not the gainers_early/narrative_prediction paper paths (which write to `tg_alert_log` instead).

**Most likely cause:** the memecoin alert path (alerter.py + db.log_alert) is the original 2026-04 pipeline. Newer paper-trade signal types (BL-029 onward) route via different writers and never touched `alerts`. The 2026-05-02 cutoff aligns with `first_signal` auto-suspension date (`signal_params.suspended_at = 2026-05-02T01:00:18Z`) — possibly the only signal type still writing to `alerts` got suspended.

**Fix:** verify whether `alerts` table is still load-bearing for any consumer. If yes, find the orphaned writer. If no, formally retire the table or stamp it deprecated in schema.

**Suspect commit / root cause (post-audit annotation 2026-05-11):**
- Writer: `scout/db.py:3589` (`INSERT INTO alerts`). **Zero commits** touching this SQL since 2026-04-01 — SQL stable since pre-April.
- Caller chain: `scout/main.py` + `scout/alerter.py`. alerter.py changes since 2026-04-01: `571617d` PR #76 (calibrate weekly), `0ec5897` PR #29 (paper-trading), `f20fb5d` PR #28 (lunarcrush) — none touch the alerts-write path.
- Most plausible: **NOT a refactor regression.** Per audit text + memory, the `alerts` table is the legacy memecoin alert path; newer signal types (BL-029 onward) route through `tg_alert_log`. The 2026-05-02 cutoff aligns with `first_signal` hard_loss suspension. Effectively retired-by-evolution.
- Diagnostic order: (1) grep consumers of `alerts` table — if none in live code, schema-deprecate. (2) If consumer exists, trace its expected writer (likely an old memecoin-pipeline `log_alert` caller in main.py that no longer fires post-paper-trade migration).

### §2.2 [CRITICAL] `cryptopanic_posts` empty — BL-053 silently dead 22+ days

Per memory `project_session_2026_04_20_bl052_bl053.md`: "GeckoTerminal per-chain trending + CryptoPanic news feed shipped. PRs #35/#36 squash-merged as 09ff21d/7eb3d10, deployed."

Reality: zero rows in `cryptopanic_posts` since deploy. BL-053 ingestion is dead. Either:
- The CryptoPanic API key expired
- The listener crashed silently on first cycle and was never restarted
- The writer code path is reachable but the API call is failing silently (gate-swallow pattern)

**Fix:** grep `journalctl` for cryptopanic errors; if dry-since-deploy, verify the listener task is actually scheduled in `main.py`.

**Suspect commit / root cause (post-audit annotation 2026-05-11):**
- Writer: `scout/news/cryptopanic.py`. **Single commit:** `7eb3d10` (PR #36 BL-053, 2026-04-20 ship). Never modified after.
- Caller scheduling: zero `cryptopanic`-string changes to `scout/main.py` since PR #36 either.
- Most plausible: **ship-time bug at PR #36.** Writer never produced a row in 22 days. Three sub-hypotheses (from audit text): (a) `CRYPTOPANIC_API_KEY` never configured / expired, (b) listener task code-exists but isn't actually scheduled in `main.py` startup, (c) API call silently failing (gate-swallow pattern).
- Diagnostic order: SSH `journalctl -u gecko-pipeline --since 2026-04-20 | grep -i cryptopanic` — error spam = case (c), silence = case (b), auth errors = case (a). Most likely (b) since BL-053 was deployed pre-CryptoPanic-API-rate-limit-issues per the project memory; (a) would have produced auth errors which presumably someone would have noticed.

**Diagnosis (2026-05-11 evening, post-annotation pass):**

Classification: **(b)-variant the original audit didn't enumerate — "feature flag never flipped from default."** Three corroborating evidence sources:

1. **`scout/main.py:672-747`** — `if settings.CRYPTOPANIC_ENABLED:` gates `fetch_cryptopanic_posts` + `db.insert_cryptopanic_post`. Code is intact and reachable.
2. **`scout/config.py:233-234`** — defaults: `CRYPTOPANIC_ENABLED: bool = False`, `CRYPTOPANIC_API_TOKEN: str = ""`. Docstring at lines 228-230 confirms: even with `_ENABLED=True`, empty token causes `fetch_cryptopanic_posts` to short-circuit to `[]` without a network call. **Both keys required for activation.**
3. **Prod state** (verified 2026-05-11T21:30Z): VPS `.env` has no `CRYPTOPANIC_*` keys (both defaults active). VPS journalctl `-n 50000` shows **zero `cryptopanic` events ever** — the listener has not executed once since BL-053 deploy 2026-04-20 (22+ days).

**Sub-finding (audit taxonomy refinement):** the original (a)/(b)/(c) classification missed the **"feature flag never flipped from default"** sub-case. Structurally distinct from both (b) "code missing in prod" and (c) "task creation fails silently" — it's the **deploy-without-activate** pattern: PR #36 (`7eb3d10`) shipped the code 2026-04-20 expecting a follow-up flag-flip that never happened.

**Fix shape:** operator-side `.env` edit only — no code change required:
```
CRYPTOPANIC_ENABLED=True
CRYPTOPANIC_API_TOKEN=<free-tier-token-from-cryptopanic.com>
```
Restart `gecko-pipeline.service`. Monitor after flip:
- `journalctl -u gecko-pipeline -g "cryptopanic_fetch_failed|cryptopanic_persist_error" --since "10 minutes ago"` — should be silent
- `SELECT COUNT(*) FROM cryptopanic_posts WHERE fetched_at > datetime('now','-1 hour')` — first non-zero count confirms end-to-end

**§2.2 status:** **Diagnosed. Not a code bug.** Pending operator decision to either (i) activate BL-053 by flipping the two `.env` flags, or (ii) formally mark BL-053 "shipped but deactivated — activate by setting `CRYPTOPANIC_ENABLED=True` + `CRYPTOPANIC_API_TOKEN`" so future audits don't re-surface this finding. **Severity downgraded from CRITICAL to OPERATOR-DECISION.** The 22-day "silent failure" is actually a 22-day "deploy without activate" — distinct failure class.

**Meta-implication worth scanning for separately:** how many other shipped-but-default-off features have the same gap? `CRYPTOPANIC_SCORING_ENABLED` (config.py:237, default False) is one immediate sibling. The live-trading family (`LIVE_TRADING_ENABLED`, `LIVE_USE_REAL_SIGNED_REQUESTS`, `LIVE_USE_ROUTING_LAYER`) is intentionally guarded by design — NOT a deploy-without-activate (different shape: guarded-by-design vs flag-flip-never-happened). Worth a one-pass scan when prioritizing P4 cleanups.

**§2.2 closure (Path A selected 2026-05-11 evening):** Sub-checks confirmed Path A is correct:

- *Sub-check 1* (prod `SCAN_INTERVAL_SECONDS`): verified **60s** on prod. Design assumed 300s (12 req/hr); current state is **60 req/hr** at the **low end** of the CryptoPanic free-tier band (50-200 req/hr per design doc §3). Rate-limit concern is real, not phantom. **Surfaced separate finding** about design-time assumptions vs current cycle frequency → filed as `BL-NEW-CYCLE-CHANGE-AUDIT` in backlog.
- *Sub-check 2* (operator intent for news data): backlog "Virality Detection Roadmap" lists CryptoPanic as Source #2 for news/macro events (roadmap-level intent EXISTS), but no "validated-and-blocked" claim was found — deploy memory documents activation as "how-to-enable-when-ready" guidance, not a blocked-on-condition workflow. Path A remains correct; the activation path is well-specified for future operator pickup.

**Deactivation formalized in backlog** as `BL-053: CryptoPanic news feed (shipped 2026-04-20, deactivated by default — operator activation pending)` with full 5-point activation conditions (both flags + token + scorer recalibration + decoupled interval + §12a SLO, as one coherent PR). §2.2 now **closed-as-operator-decision-resolved**. Severity downgrade CRITICAL → OPERATOR-DECISION sticks. Future audits should not re-surface this finding.

### §2.3 [REFRAMED — withdrawn from CRITICAL] shadow_trades correctly idle, BL-055 unlock is policy-blocked

**Original claim (incorrect):** "writer disconnected by M1.5 refactor wave, requires rewiring."

**Cleanup-pass diagnosis (verified end-to-end via grep + git log + signal_params_audit):**

```
LIVE_MODE=shadow             ← Still set, prod .env (never flipped)
LIVE_SIGNAL_ALLOWLIST=first_signal  ← Only ONE signal eligible for shadow routing
first_signal.enabled=0       ← Auto-suspended 2026-05-02T01:00:18Z (hard_loss, max_drawdown $-593, n=253)
↓
Zero first_signal paper opens after suspension
↓
LiveEngine.on_paper_trade_opened never invoked (eligibility check fails)
↓
shadow_trades table not written to (correct behavior, not a bug)
```

Every link verified:
- Writer code present at `scout/live/engine.py:181/218/263` ✓
- `LiveEngine` constructed (mode=shadow per `main.py:1194`) ✓
- `is_signal_enabled(first_signal)` returns True only when in `LIVE_SIGNAL_ALLOWLIST` ✓
- Allowlist contains exactly `"first_signal"` ✓
- All 127 historical shadow_trades rows are `signal_type=first_signal` ✓
- `first_signal.enabled=0` with `suspended_reason=hard_loss`, no re-enable in `signal_params_audit` ✓
- Last shadow_trades row (id 1525, 2026-05-01T19:52:31) was the **last first_signal paper trade ever opened** — exact timestamp match ✓

**The original audit pointed at the writer when the cause was the upstream filter.** Same lever-vs-data-path failure mode the operator pointed out in the LLM-router C2 finding earlier today (memory: `feedback_verify_output_consumers_before_scoping.md`).

### §2.3a [REAL FINDING — separate workstream] BL-055 unlock is policy-blocked

**The bigger structural issue surfaced by §2.3 reframe:**

The BL-055 7d-clean-shadow-soak gate cannot be satisfied as currently configured. Not because of a broken writer, but because:
- Only `first_signal` is in `LIVE_SIGNAL_ALLOWLIST`
- `first_signal` is auto-suspended for hard_loss (and per `feedback_trading_lessons.md` is structurally the worst-performing signal — 14.3% WR, -1.4% avg pct)
- The auto-suspension is operating correctly — combo_performance's kill switch fired on a legitimate loss pattern

**This is a policy problem, not a code problem.** The right fix is operator policy decision, NOT a writer rewire.

**DO NOT auto-widen `LIVE_SIGNAL_ALLOWLIST` to "Tier 1+2 signals" from `findings_live_eligibility_winners_vs_losers_2026_05_11.md`** as a quick fix. That analysis is **paper-trade evidence**, which is a different (weaker) standard than the BL-055 gate was designed to require. BL-055 specifies *"7d clean shadow soak per signal."* Widening the allowlist to signals that haven't done their own shadow soak moves the gate, doesn't satisfy it.

Three options for the operator (parked as a separate workstream — DO NOT fold into the audit cleanup):

| Option | What | Risk |
|---|---|---|
| A | Re-enable first_signal (override auto-suspension) | Combo data says it's losing money. Overrides the kill switch. |
| B | Widen `LIVE_SIGNAL_ALLOWLIST` to additional signals, each with their own 7d clean shadow soak | Requires running each signal through shadow → measuring → unlocking. 7-30d per signal. |
| C | Loosen BL-055 gate semantics to accept paper-trade evidence in place of shadow-trade evidence | Explicit policy decision. Cheapest path. Reduces the gate's protective intent. |
| D | Park BL-055 unlock as dormant until first_signal recovers (or until a clear policy decision is made) | Live-trading work parks. M1.5b routing scaffolding sits idle. Default option until A/B/C is chosen. |

**Sequencing:** parked as "BL-055 unlock policy review" workstream. Separate from this audit's Priority 5 (writer fixes). Gather signal-by-signal soak evidence before deciding.

### §2.4 [CRITICAL] `outcomes` — only 2 rows total

The `outcomes` table records "alert price → check price after N hours" for paper-mirrors-live correlation. Per memory `feedback_paper_mirrors_live.md`: *"paper volume ok, but must always mark the capital-constrained live-eligible subset (would_be_live bool, FCFS-20-slots); see BL-060"*.

Only 2 rows ever — both 2026-05-01/02, same trades as `alerts`. The `outcomes` writer either ran only twice in project history or was scoped to a single signal type that's no longer firing. **The paper-mirrors-live correlation analysis has zero data to work from.**

**Suspect commit / root cause (post-audit annotation 2026-05-11):**
- Writer: `scout/db.py` (function name TBD; likely `update_outcome` or similar). **Zero commits** touching `INSERT INTO outcomes` since 2026-04-01 — SQL stable.
- Caller chain: same path as §2.1 — the 2 outcomes rows correlate exactly with the 2 alerts rows (same legacy memecoin alert pipeline).
- Most plausible: **same root cause as §2.1.** Legacy memecoin pipeline displaced by paper-trade signal routing. The `outcomes` table was the memecoin-pipeline "alert price → check price after N hours" correlation surface, but new signal types do their correlation via `paper_trades.peak_pct` / `realized_pnl_pct` instead.
- **Connected to §2.7:** BL-071a (open backlog) — "Investigate why memecoin `outcomes` table is empty" — is the same investigation. Fixing the memecoin-token `outcomes` writer closes §2.4 AND removes the auto-retirement trigger feeding §2.7. Diagnostic-order priority: do §2.7 root-cause first (more value), §2.4 closes as a side effect.

### §2.5 [HIGH] `holder_snapshots` empty — BL-020 never wired

Per backlog: BL-020 "Populate holder_growth_1h from enricher." The schema column `holder_growth_1h` exists. The snapshot table that feeds it is empty. The `holder_growth_1h` signal in scorer.py is gated on this data; if absent, the signal silently scores 0 and never fires. Affects every memecoin scoring decision.

### §2.6 [HIGH] `perp_anomalies` empty — BL-054 detector silently dead

Per memory `project_session_2026_04_20_perp_enablement.md`: BL-054 was fully enabled on VPS with three phases (watcher on, recalibration, scoring flag flipped). Zero anomalies in 20+ days. Either:
- Bybit was the primary source and got disabled ("1000x symbol prefix quirk" per memory) → other sources never picked up the slack
- Detector threshold is too high for current market regime
- Detector code path stopped firing after a refactor

**Fix:** verify the WS listener is alive (`ws_perp_*` events in journalctl), check thresholds, check signal_params.

**Suspect commit / root cause (post-audit annotation 2026-05-11):**
- Writer: `scout/perp/watcher.py` + `scout/perp/enrichment.py`. **Single commit:** `b36b8ff` (BL-054 ship, PR #37). Never modified after. Adjacent: PR #38 (commit `46ba56f` per memory) recalibrated perp scoring — does not touch the watcher writer.
- Most plausible: **NOT writer regression — detector enabled but doesn't find anomalies.** Per memory `project_session_2026_04_20_perp_enablement.md`: BL-054 was fully enabled in 3 phases (watcher on, recalibration, scoring flag flipped). Memory also flags: "Bybit disabled due to 1000x symbol prefix quirk." Three sub-hypotheses:
  - Bybit disablement removed the primary source; Binance never picked up the slack (anomaly definition was Bybit-tuned)
  - Thresholds calibrated for an older market regime — current vol doesn't trigger
  - "Anomaly" definition too narrow given current order-book depth
- Diagnostic order: (1) SSH `journalctl | grep ws_perp` — listener alive? (2) Check `signal_params` for perp-related rows — disabled? (3) If listener alive + signal enabled: lower threshold by 10%, observe 24h. If still empty → definition is wrong, not threshold.

### §2.7 [HIGH] Memecoin chain dispatch — last fire 2026-05-04

```sql
SELECT pattern_name, pipeline, COUNT(*), MAX(completed_at) FROM chain_matches
WHERE completed_at > datetime('now','-7 days') GROUP BY ...
```

| pattern | pipeline | n | last_fire |
|---|---|---:|---|
| narrative_momentum | narrative | 161 | 2026-05-11 16:43 ✓ |
| full_conviction | narrative | 154 | 2026-05-11 16:43 ✓ |
| volume_breakout | **memecoin** | 5 | **2026-05-04 00:51** ❌ |

Narrative chain pipeline is healthy. **Memecoin chain pipeline died 7 days ago.** Per memory `project_chain_revival_2026_05_03.md`, chain dispatch had been dead 17 days before that revival; this looks like it died AGAIN. Likely same root cause as outcomes-telemetry-broken-→-auto-retirement.

**Suspect commit / root cause (post-audit annotation 2026-05-11):**
- Writer: `scout/chains/tracker.py`. Relevant commits in the 2026-05-04 window: `cbb1e7f` PR #64 BL-071a' (DexScreener-resolved memecoin chain outcomes, **merged 2026-05-04**), `b51324c` PR #63 Bundle A (detection telemetry hygiene, 2026-05-03).
- Post-2026-05-04 commits to `scout/chains/`: **only** `eaf3523` PR #80 (BL-NEW-CHAIN-COHERENCE 2026-05-06, narrative-only — does not touch memecoin path).
- Timeline alignment: revival happened 2026-05-01 (PR #60) + 2026-05-03 (PR #61 BL-071 systemic-zero-hits guard, commit `2a45263`). Memecoin chains fired through 2026-05-04T00:51Z, then went silent. PR #64 merged later that day.
- Most plausible: **§2.4 root cause feeds §2.7.** Memecoin `outcomes` table is empty (§2.4) → memecoin chain hits cannot be hydrated → systemic-zero-hits guard from PR #61 auto-retires the memecoin patterns again → no dispatches. Same death loop as the original 17-day outage, just with new guard.
- Alternative: PR #64 itself introduced a regression in the dispatch path while closing the silent-skip surface. Less likely (PR review history is clean) but worth a quick git-blame of the dispatcher.
- Diagnostic order: (1) SSH `SELECT pattern_name, retired_at FROM chain_patterns WHERE pipeline='memecoin' AND retired_at IS NOT NULL` — confirms auto-retirement hypothesis. (2) If retired: re-run the 2026-05-03 revival method from `project_chain_revival_2026_05_03.md`. (3) If NOT retired but no fires: bisect against `cbb1e7f`.

**§2.4 → §2.7 sequencing pre-registration (do not collapse the fix).** §2.4 and §2.7 share a suspected root cause (memecoin `outcomes` empty → can't hydrate hits → auto-retirement guard fires). The fix for §2.4 should land **first**, and §2.7 observed for **≥3 days** before any §2.7-specific work. Rationale: same investigation shape as §2.9 — single-variable change with a clean attribution window. If §2.7 closes after §2.4 alone is fixed → confirmed shared root cause. If §2.7 does NOT close → second independent root cause exists in the dispatch path itself, which is much more valuable to know than discovering it after a combined fix that conflates the two attribution paths. **Do not fix both simultaneously.**

### §2.8 [MEDIUM] `high_peak_fade_audit` only 7 rows

BL-NEW-HPF deployed 2026-05-04 per memory. 7 audit rows in 7 days = 1/day. Could be legitimately rare (high-peak fades only fire on tokens that reach high peaks) or could indicate the detector is partially broken. **Worth a manual cross-check** against trades with peak_pct ≥ HPF threshold to verify expected fire rate.

**Suspect commit / root cause (post-audit annotation 2026-05-11):**
- Writer: `scout/trading/evaluator.py`. **Single commit:** `f150f75` (PR #78 BL-NEW-HPF ship). Never modified after.
- Most plausible: **NOT broken — working at expected low frequency.** HPF only fires on tokens that reach high peaks then retrace; both conditions are rare. 7 fires in ~7d ≈ 1/day matches a low-prior detector.
- Diagnostic order: single SQL — `SELECT COUNT(*) FROM paper_trades WHERE peak_pct >= <PAPER_HPF_THRESHOLD_PCT> AND opened_at > '2026-05-04'`. If count >> 7, detector is partially broken (eligibility met but not firing). If count ≈ 7-15, working as designed.
- Lowest-severity item in §2 — defer until §2.2/§2.4/§2.6/§2.7 are closed.

### §2.9 [HIGH] [Class 2] Auto-suspension reversals are silent — operator-action silently reversed

**Trigger evidence:** `trending_catch` was operator-re-enabled 2026-05-10 (per memory `project_trending_catch_soak_2026_05_10.md`) for a pre-registered n=50 soak. At 2026-05-11T01:00:26Z (~14h later), the combo_performance auto-suspend kill switch caught the pre-registered KILL criterion (WR<50% OR total<-$100) and re-suspended the signal. Audit row in `signal_params_audit` ID 22 confirms `auto_suspend` reversed operator-enabled state.

**No Telegram alert was fired.** The operator-side workflow assumes "I re-enabled it, it's running." Reality: ran ~14h, auto-killed, silent.

The auto_suspend logic worked correctly — it caught a legitimate loss pattern and pulled the trigger as specified in the kill criterion the operator pre-registered. The failure is purely in the **operator-notification edge**. Class 2 of the failure taxonomy in §0.

**This is a different failure class from §2.1-§2.8.** Those are "writer stopped, nobody watched" (Class 1 — fix with watchdog). This is "system worked correctly, operator wasn't told" (Class 2 — fix at the existing write site, no daemon needed).

**Cheap fix (~30 min):** wire a Telegram alert at the `auto_suspend` row write site in the signal-params layer. When `signal_params_audit.applied_by='auto_suspend'`, emit:
```
🛑 Signal auto-suspended: <signal_type>
Reason: <suspended_reason>
Prior state: enabled=<old>, set by <previous_applied_by>
If operator-set: your re-enable was reversed at <timestamp>.
Review: combo_performance + decide re-enable vs accept suspension.
```

**Scope creep guard:** this alert fires ONLY when `applied_by='auto_suspend'`, NOT on every signal_params change. Operator-initiated changes don't need self-notifications.

**Generalizes to a broader rule** (see §5.5): every automated state change that reverses or overrides an operator-applied state must fire an operator alert at write time.

### §2.9 closure tracking (added post-PR-#106)

PR #106 (commit `e8758b5`, merged 2026-05-11T20:38:03Z) shipped the fix. The fix is **code-verified-only** — tests pass, manual curl replay produced correctly-rendered output — until the next production `auto_suspend` event fires and is verified end-to-end. Production verification must be detectable, not contingent on operator attention at the right moment (which is the failure shape §2.9 itself documents).

**Trigger pattern.** Watch VPS journalctl for the next `auto_suspend_alert_dispatched` line after 2026-05-11T20:38:03Z:

```
journalctl -u gecko-pipeline --since "2026-05-11 20:38" | grep auto_suspend_alert_dispatched
```

When the first match appears, the verification block below activates.

**Verification checklist** (all three must pass to declare §2.9 closed-in-production):

1. **Trace pair present** — a paired `auto_suspend_alert_delivered` line follows the `_dispatched` line for the same signal (correlate by timestamp + `signal_type` field).
2. **Body preserves underscores** — fetch the corresponding Telegram message from chat `6337722878` history. Verify the signal name and `suspended_reason` field contain literal underscores (e.g., `trending_catch` renders as `trending_catch`, NOT `trendingcatch` with surrounding italics).
3. **Closure entry recorded** — append a line to this audit doc: `§2.9 closed-in-production YYYY-MM-DDTHH:MM:SSZ: signal_type=<X>, suspended_reason=<Y>, journalctl evidence <timestamp>`.

**Time-bounded fallback.** If no `auto_suspend` event fires by **2026-05-25T20:38:03Z** (14d post-merge), §2.9 is marked `closed-by-time-bound, no production firing observed` and the audit doc records that production confirmation rests on the code-level evidence alone (PR #106 tests + manual curl replay). Time-bound closure is weaker than firing-event closure, but it prevents §2.9 from becoming permanent paperwork waiting for a low-frequency event.

**Why this shape:** §2.9's original failure was "alert fired, operator didn't recognize the mangled signal name." The closure criterion for the *fix* must not depend on the same operator-attention-at-the-right-moment the finding documents — pinning observation to a log-grep pattern + an explicit verification checklist makes the closure event detectable rather than discretionary.

## §3 Why Telegram doesn't catch this

Telegram delivery is currently scoped to **paper-trade-opens** via the BL-NEW-TG-ALERT-ALLOWLIST (PR #92, 2026-05-11). Last 24h: 30 paper-trade-open alerts, 1 M1.5b announcement, 1 M1.5c announcement.

**No Telegram channel for system-health.** The categories of failure that go silent:
- Table write rate dropping to 0
- API key validity failures
- Detector dead-without-error (most of the findings above)
- Scheduled job missed its window
- Disk space, DB lock contention, etc.

## §3.5 Root pattern (post-reviewer revision)

The 8 findings are not 8 independent failures. They are **one failure pattern repeating across 8 surfaces**:

> **A writer is shipped + works initially → a later refactor breaks it silently → no monitoring exists at the table-write rate → operator finds it weeks-to-months later via unrelated audit work.**

- §2.2 cryptopanic: shipped 2026-04-20, never wrote a row (writer disconnected from listener at deploy time?)
- §2.3 shadow_trades: shipped 2026-04-23, wrote for 8d, refactored away ~2026-05-01 (M1.5a/b live-trading refactor wave?)
- §2.4 outcomes: same trades as alerts table — single writer path that stopped
- §2.6 perp_anomalies: shipped 2026-04-20, never wrote a row
- §2.7 memecoin chain: revived 2026-05-03, re-died ~2026-05-04

**The watchdog catches the symptom. What catches the cause is a deploy-checklist audit that runs pre+post every PR merge and treats any row-rate regression as a blocking failure.** That's almost free and would prevent finding #9 through #N.

## §4 Recommended health-check stack — REVISED prioritization

(Original sequencing put watchdog daemon as Priority 1. Reviewer pushback corrected this — daemon detects, doesn't fix. The right first move is to unblock the live-trading roadmap by fixing shadow_trades, then ship the cheapest possible safety net via the audit CLI, then work through the rest in severity order before building any new monitoring infrastructure.)

### Priority 1 (this session, ~30 min): §2.9 fix — wire Telegram alert at `auto_suspend` write site

Class 2 finding (§2.9) has the cheapest fix in the entire audit and prevents recurrence of the trending_catch-style silent reversal pattern. ~30 minutes of work at the existing write site (signal-params layer), no new infrastructure. Slotted before cryptopanic because it compounds — every future `auto_suspend` event benefits.

**Build:** add Telegram alert call where `signal_params_audit` row is written with `applied_by='auto_suspend'`. Body per §2.9 template. Test: simulate an auto_suspend event in a unit test, verify alert payload. Deploy. No DB migration needed.

### Priority 2 (deferred to separate workstream): BL-055 unlock policy review (was §2.3 "fix shadow_trades")

§2.3 reframe revealed this isn't a code problem. The shadow_trades writer is intact and correctly idle. The blocker is policy: `LIVE_SIGNAL_ALLOWLIST=first_signal` pins live-trading unlock to an auto-suspended signal. **DO NOT auto-widen the allowlist to "Tier 1+2" paper-trade signals** — that conflates paper-trade evidence with shadow-trade evidence and moves the BL-055 gate rather than satisfying it.

This deserves its own analysis with the signal-by-signal soak evidence question. Park as a separate workstream. See §2.3a for the four operator-policy options (A/B/C/D).

### Priority 3: Audit CLI (the cheap "watchdog v0")

`uv run python -m scout.audit` — cleaned-up version of the freshness scan from §2 (aiosqlite + argparse, sibling of `scripts/bl060_threshold_audit.py`). Operator runs:
- Manually before/after every deploy
- As a cron once per day with `> /var/lib/gecko-alpha/audit-last.txt` + check via gecko-backup-watchdog.sh's existing pattern for staleness alert delivery

**Why this beats the daemon at Priority 2:** same detection coverage as the daemon (catches the same 8 categories), 10% of the build cost (~1h vs 4-6h), reuses the existing curl-direct-to-Telegram template, fits the project's existing scripts/ pattern. The daemon becomes worth building only once the system is clean — until then, every daemon run fires 8 alerts and operator either ignores or muffles them.

### Priority 4: Fix remaining §2 findings in severity order

In the order the reviewer recommended (Class 1 findings only — Class 2 §2.9 already handled at Priority 1):
- §2.2 cryptopanic (22 days dead — diagnose listener)
- §2.4 outcomes (paper-mirrors-live has no data — wire writer)
- §2.5 holder_snapshots (silently scoring 0 on every memecoin — wire BL-020 enricher)
- §2.6 perp_anomalies (BL-054 detector dead 20+ days)
- §2.7 memecoin chain dispatch (re-died after 2026-05-03 revival)
- §2.1 alerts table (likely retire — verify no consumer)
- §2.8 high_peak_fade audit (verify expected fire rate vs reality)

### Priority 5: §5a + §5b rules into CLAUDE.md immediately (free, prevents recurrence)

Both rules from §5 are deploy-time disciplines that prevent the drift pattern in §3.5 from recurring. Promote to CLAUDE.md §9.5 alongside the structural-attribute-verification rule already queued for promotion. §5a catches Class 1 (writer-stopped) failures. §5b catches Class 2 (operator-state-reversed) failures. Free; zero implementation cost.

### Priority 6: Watchdog daemon (deferred until system is clean)

Build the always-on watchdog daemon ONLY after §2.1-§2.8 are fixed and the audit CLI shows clean. Before that, the daemon is alerting on still-broken things — operator either ignores it (defeating the purpose) or muffles with allowlists (creating a worse silent-failure surface than the one we started with).

**Estimated build:** 4-6 hours when the time comes. ~200 LoC + tests + systemd unit. Re-uses the gecko-backup-watchdog.sh curl-direct-to-Telegram alert pattern.

### Priority 7 (still useful, sequence later): Per-API-key liveness probe

In `main.py` startup + every 1h: make a minimal API call to each configured external service (Anthropic, OpenRouter, Telegram, CoinGecko, GoPlus, Helius, Moralis). Catches credit-dry / key-rotation / service-outage scenarios. Originally Priority 3; reviewer didn't object to it but it's not blocking — defer until the daemon ships.

---

## §4-original (PRE-REVIEWER, retained for traceability)

The pre-review sequencing put Watchdog Daemon as Priority 1. This was a reflex ("build observability!") that the reviewer correctly pushed back on. The watchdog detects, doesn't fix; with 8 still-broken things, it would fire 8 simultaneous alerts and either get ignored or muffled. Section retained here only for traceability — supersede with §4 above.

### [SUPERSEDED] Priority 1: Watchdog daemon

A single Python script running as `gecko-watchdog.service` (separate from `gecko-pipeline.service`) that:
- Every 15 min, queries `scout.db` for "last write per critical table"
- If any table's last write is older than its expected SLO, fires Telegram alert
- Per-table SLO defined in config (e.g., `predictions: 6h`, `paper_trades: 4h`, `alerts: ???`, `cryptopanic_posts: 1h`)
- Also pings each external API for key validity (Anthropic, OpenRouter, GoPlus) and alerts on failure
- Single Telegram chat ID dedicated to system-health alerts (separate from trade alerts)

**Estimated build:** 4-6 hours. ~200 LoC + tests + systemd unit. Would have caught EVERY finding above within 15 min of occurrence.

### Priority 2: Heartbeat threshold escalation

Extend the existing heartbeat emitter (already running every ~5 min) to:
- Track rolling-window averages per counter
- If any counter is 0 for ≥3 consecutive heartbeats while the system claims to be running, emit `heartbeat_anomaly` event AND fire Telegram alert
- Pre-register expected non-zero counters per cycle: `narrative_predictions`, `counter_scores_memecoin`, etc.

**Estimated build:** 2-3 hours. Heart already exists; this is a thresholding layer.

### Priority 3: Per-API-key liveness probe

In `main.py` startup + every 1h: make a minimal API call to each configured external service (Anthropic, OpenRouter, Telegram, CoinGecko, GoPlus). If any fails, fire alert. Cheap (negligible API spend). Catches credit-dry, key-rotation, service-outage scenarios.

**Estimated build:** 2 hours.

### Priority 4: Audit-CLI command

`uv run python -m scout.audit` runs the freshness scan from §2 and reports findings to stdout. Operator runs manually before/after each deploy; eventually wired into CI.

**Estimated build:** 1 hour (the audit script for this report can be cleaned up + checked in).

### Priority 5: Each silent-failure category-specific fix

Independent of the watchdog, fix the underlying broken writers:
- §2.1 alerts table — investigate + retire-or-rewire
- §2.2 cryptopanic — diagnose listener
- §2.3 shadow_trades — rewire writer (load-bearing for live-trading unlock)
- §2.4 outcomes — wire writer to all signal types
- §2.5 holder_snapshots — wire BL-020 enricher
- §2.6 perp_anomalies — diagnose detector
- §2.7 memecoin chain — same auto-retirement pattern as 2026-05-03 revival

## §5 Generalized rules (proposed for CLAUDE.md §9.5)

Two distinct rules — one per failure class. They have different fix shapes and don't substitute for each other.

### §5a Class 1 rule — Pipeline tables must ship with freshness SLO + watchdog

(Captures the original §5 rule for writer-stopped failures.)

**Every new DB table that records pipeline activity MUST be paired with a freshness SLO and a watchdog check.** If you ship a new writer without simultaneously shipping the "this table not written to in X minutes" alarm, you've created a future silent-failure surface. The cost of adding the SLO at ship time is 5 minutes; the cost of discovering it via this kind of audit is days-to-weeks of degraded operation.

Compose with §9c (structural-attribute verification before ship) + the verify-output-consumers rule. Three sibling disciplines:
- §9a: runtime-state verification (current value / active state / path-reaches-lever)
- §9b: structural-attribute verification (empirical breakpoints)
- §9c (proposed): activity-rate verification (post-deploy table writes happen at expected rate)

### §5b Class 2 rule — Automated state reversals of operator-applied state MUST alert at write time

**Every automated state change that reverses or overrides an operator-applied state MUST fire an operator alert at write time.** Auto-suspend, auto-disable, kill-switch trips, threshold-driven config flips — if the system silently undoes what the operator did, the operator's mental model is wrong about the system, and that's the most dangerous state to be in.

This rule is structurally different from §5a:
- §5a catches missing rows via row-rate SLO monitoring (passive observation)
- §5b alerts on a specific write event (active notification at write site)

§5a is "build a watchdog." §5b is "wire one alert call at one line of existing code." Both rules are cheap; neither substitutes for the other.

**Trigger conditions for §5b alerts:** any audit-log row where `applied_by` indicates automated action (e.g., `auto_suspend`, `kill_switch`, `threshold_driven_flip`) AND the prior state was operator-applied. Operator-initiated reversals of operator-applied state don't need self-notifications.

**Empirical evidence for §5b:** trending_catch operator-re-enabled 2026-05-10, auto-re-suspended 2026-05-11T01:00:26Z, operator unaware. This audit's cleanup pass surfaced it.

## §5-original (PRE-CLEANUP, retained for traceability)

Original §5 had only the Class 1 rule. The cleanup-pass diagnosis (§2.3 reframe + §2.9 new finding) revealed that Class 2 failures need a separate rule (§5b) — they don't get caught by watchdog/SLO monitoring. The current §5a + §5b structure replaces the original single rule. Section retained here only for historical traceability.

## §5.7 Audit re-run cadence (meta-observation)

This audit keeps generating findings as it investigates findings. First pass surfaced 8 (§2.1-§2.8). The cleanup pass surfaced #9 (§2.9) AND reframed #3 (§2.3) entirely. The next pass will probably surface #10-12.

That's the nature of "first time anyone has looked at this." The system has accumulated drift faster than anyone has measured it. One audit doesn't reset that — **repeated audits do.**

**Proposed cadence:** monthly audit re-run until the rate of new findings per pass approaches zero. After that, quarterly. Each re-run uses the audit CLI (§4 Priority 2) when it ships, manually before then.

Each re-run should expect:
- New findings from drift accumulated since the last pass (Class 1 surface growing)
- Reframed findings as the next layer of cause-tracing happens (lever-vs-data-path lessons applied)
- New CLAUDE.md rule candidates as new failure classes are surfaced (§5.5, §5.6, etc.)

The fact that one finding (§2.3 → §2.3a) flipped from "broken writer" to "policy-blocked unlock" in the cleanup pass is itself a signal: the first-pass framing was wrong about the mechanism. Subsequent passes will probably reframe more findings the same way.

## §5.5 Reviewer-final answer on the open question

> "Want to draft the audit CLI structure, or work through the shadow_trades diagnosis first?"

Per reviewer's own §"What I'd actually sequence" — **shadow_trades first**. It's the only finding blocking forward roadmap (live-trading unlock). The audit CLI is Priority 2 and doesn't depend on shadow_trades being fixed first, so it can run in parallel if needed — but the order matters because shadow_trades is the only finding with a real-time cost (every day blocks BL-074 + M1.5b further).

Reviewer also said: skip the deeper 4-6h audit sweep — diminishing returns. Accepted.

## §6 What I haven't audited (DEFERRED — diminishing returns per reviewer)

This audit covered:
- Table freshness (54 tables in scout.db)
- Suspended-signal state
- API key presence (not validity)
- Chain dispatch per-pipeline rates
- Recent error patterns in journalctl (partial — couldn't complete the 7d grep in this session)

**Not yet audited:**
- VPS resource usage trends (disk, RAM, CPU)
- DB lock contention / WAL growth
- Outbound HTTP error rates per provider (CoinGecko, DexScreener, GeckoTerminal, GoPlus, Helius, Moralis)
- Cron / scheduled-task health (calibration weekly, daily LEARN, etc.)
- Signal-flow correctness end-to-end (a token enters ingestion → does it reach the right tables?)
- Dashboard endpoint freshness (would catch the SignalsTab predictions issue earlier)
- Comparison of deployed code vs intended behavior (drift)

Deeper sweep ETA: ~4-6 hours of focused work. Want it done?
