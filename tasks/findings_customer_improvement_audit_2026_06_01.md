# Customer-Lens Improvement Audit — gecko-alpha (2026-06-01)

**Author:** autonomous session `session/autonomous-2026-06-01` (base `origin/master@30821309`).
**Backup/revert point:** tag `backup/pre-autonomous-2026-06-01` (pushed to origin). Revert with
`git reset --hard backup/pre-autonomous-2026-06-01` or simply do not merge the session PRs.
**Method:** 5 parallel read-only analysis agents along orthogonal customer-value vectors (signal
quality, detection speed, coverage/recall, reliability/silent-failure, decision-support UX). Every
finding is grounded in `file:line` evidence and drift-checked against shipped work (#291–#347),
`backlog.md`, `tasks/findings_*.md`, and memory. This is the "what would I want improved as a
customer" list the operator requested.

---

## 0. Framing: what a customer of this product actually wants

gecko-alpha is an early crypto-pump detector. A paying customer wants four things, in order:
1. **Trustworthy signals** — when it alerts, the alert should mean something.
2. **Speed** — find the move minutes before they could themselves (the stated product goal:
   "beat CoinGecko Highlights by minutes").
3. **It actually runs** — the pipeline and alerts don't silently die.
4. **Act fast** — alert/dashboard give enough to make and execute a decision without friction.

The codebase is **mature** (50 PRs of relevant work merged since this checkout's stale HEAD). Most
obvious features already exist. The audit therefore targets *residual* gaps, correctness bugs, and
trust-eroding structural issues — not generic feature requests.

---

## 1. What is already strong (baseline — do NOT rebuild)

- Per-signal **trust scorecards** (7/14/30d, n-gated, INSUFFICIENT_DATA shown) — PR #289/#239.
- **Today's Focus**, **Trade Inbox**, **Now Tradable**, **What-Changed** dashboard surfaces with
  thorough staleness handling, dedup, n-gates, honest empty states.
- **§12b auto-suspend** alerting is exemplary (`auto_suspend.py:160-196`): `parse_mode=None` +
  `*_alert_dispatched/delivered/failed` log triplet. This is the reference pattern.
- **Class-3 parse_mode hygiene** is clean across ~20 production Telegram call sites (only 2 use
  `Markdown`, both via field-escaping formatters — verified safe).
- **Speed primitives** shipped: parallel provider ingestion, CG-lane 429 cascade + held-position-first
  ordering, rate-limiter burst profile, TG burst pacing, MiroFish gated + 180s timeout + fallback
  (never blocks alerts), CryptoPanic 10s cap.
- **Two-corpus architecture** ($10K–$500K scorer corpus vs $10K–$500M markets-watcher) is ratified
  and intentional — **do not fight it**.
- `missed-winner` audit is **SHELVED** for a documented structural reason (≤7d `volume_history_cg`
  retention; no persisted Today's-Focus membership table). Do not reopen.

---

## 2. Prioritized improvement list

Disposition key:
- **FIX-NOW** — safe, additive or clearly-correct, ships this session.
- **FIX+TEST** — real fix that shifts the *alert* stream (not trades); ship with rigorous tests +
  monitoring note; per-PR revertable.
- **EVIDENCE** — produce offline analysis to unblock an operator-deferred decision (safe).
- **FLAG** — structural / needs soak / operator judgment; documented, not auto-shipped.

### P0 — Reliability & trust (highest value, lowest risk; all additive)

| # | Finding | Evidence | Customer impact | Disposition |
|---|---------|----------|-----------------|-------------|
| P0-1 | **Kill-switch trigger has no operator alert (§12b violation).** Halts live trading, only logs `live_kill_event_triggered` at WARNING; auto-clear reverses operator-relevant state silently. | `scout/live/kill_switch.py:94-165` (`:141`), `:200-213`, `:216-287` | The single highest-stakes automated state reversal fires silently. Latent today (live gated) but activates with **zero soak** the instant `LIVE_MODE` flips. | FIX-NOW (wire `parse_mode=None` TG + dispatched/delivered/failed triplet, mirror `auto_suspend._send_suspend_alert`) |
| P0-2 | **Decision-bearing tables with no freshness watchdog (§12a).** `velocity_alerts`, `perp_anomalies`, `second_wave_candidates`, `slow_burn_candidates`, `narrative_alerts_inbound`. | `db.py:1130/1166/797/3463/4128`; no timer/crontab references them | Any writer can silently stop (config flip, refactor, dry upstream); operator learns weeks later — the exact institutional-fear pattern. `perp_anomalies` already a known multi-week-dead table. | FIX-NOW (shared row-rate freshness check cloning `check_source_calls_lag.py`; wire to in-repo crontab) |
| P0-3 | **`calibrate.apply` alerts without the dispatched/delivered log triplet.** | `scout/trading/calibrate.py:354-359` | "No logs" is ambiguous between delivered-cleanly and skipped for an automated param change. | FIX-NOW |
| P0-4 | **`send_telegram_message` logs only on failure** — successful sends are silent system-wide. | `scout/alerter.py:209,217` | Root enabler of P0-3; most alert paths can't distinguish "delivered" from "never called." | FIX-NOW (one INFO `telegram_message_delivered` log inside the function) |

### P1 — Speed & actionability (high value)

| # | Finding | Evidence | Customer impact | Disposition |
|---|---------|----------|-----------------|-------------|
| P1-1 | **Detection→alert latency is unmeasured.** No metric for per-cycle wall-clock or `first_seen→alerted_at`. Real bottleneck is the live CG budget (6/min + 8s spacing + 120s cooldown), not the 60s poll. | `main.py:645-693` (serial CG lanes), `findings_cg_budget_attribution_2026_05_18.md:133` | The product promises "minutes faster"; under 429 pressure a late-lane token can wait several minutes to detect — and nobody can see it. Can't tune what you can't measure (§9a). | FIX-NOW (additive latency instrumentation — gates all later tuning) |
| P1-2 | **Paper-trade alert (highest-frequency alert) is thin.** Bare `coingecko.com/en/coins/{id}` plain link (404s for on-chain `coin_id`s, no DexScreener fallback unlike scorer path); `chain_completed` has an emoji but no extras branch → no mcap/move/why; no SL/TP risk envelope. | `tg_alert_dispatch.py:143-201` (`:170-200`, `:176-190`); cf. `alerter.py:87` | Customer must open the dashboard to reach a chart or see risk before acting → friction at the moment speed matters. | FIX-NOW (DexScreener fallback link + `chain_completed` extras + factual `SL -X% · TP +Y%` line from existing `entry_snapshot`) |
| P1-3 | **GeckoTerminal polls its 3 chains sequentially.** | `geckoterminal.py:102` | Minor serial latency; ethereum endpoint also 404s ~40/hr adding wasted serial time. | FIX-NOW (`asyncio.gather` per-chain) |
| P1-4 | **Stale-price not visible in Today's Focus headline row.** Move%/mcap/sparkline render with no stale indicator; staleness buried in `current_risk_facts[3:]` which the UI truncates to first 3. Now Tradable does this right. | `db.py:2074-2093`, `TodayFocusPanel.jsx:53,235-258`; cf. `NowTradableTab.jsx:163` | Trader reads a move% computed off a stale price as if live → wrong action. | FIX-NOW (explicit factual `stale` chip in the always-visible price row; rebuild + commit `dist/`) |

### P2 — Signal quality / scoring correctness (shifts the ALERT stream only; trades unaffected — see Framing)

| # | Finding | Evidence | Customer impact | Disposition |
|---|---------|----------|-----------------|-------------|
| P2-1 | **Co-occurrence multiplier applied AFTER `min(100)` clamp.** Double `int()` truncation rounds down; no separation at the ceiling (raw 200 and 350 both →100); confluence under-rewarded and non-monotonic at top. | `scorer.py:256,259-262` | The confluence bonus (the whole point of co-occurrence) is compressed and biased low → alerts under-rank multi-signal tokens. | FIX+TEST (multiply raw → normalize+clamp once) |
| P2-2 | **`momentum_ratio` (20pts) fires on decelerating/exhausted pumps** — no upper 24h bound (scanner path has `PAPER_GAINERS_MAX_24H_PCT=50`, scorer doesn't). | `scorer.py:140-149` | Alerts can fire near local tops. | FIX+TEST (add `MOMENTUM_MAX_24H_CHANGE_PCT` ceiling) |
| P2-3 | **`signal_confidence` HIGH/MED/LOW counts signals, ignores weight.** 3 weak signals (12 raw) → HIGH; one dominant (vol_liq +30) → LOW. | `scorer.py:266-276` | Misleading trust cue shown to the customer. | FIX+TEST (band on normalized score / weighted mass) |
| P2-4 | **`stable_paired_liq` double-counts liquidity AND can trip the 3-signal co-occurrence multiplier**, manufacturing false "HIGH confidence." | `scorer.py:248-253` | Single liquidity property inflates base points + confluence multiplier. | FIX+TEST (exclude from co-occurrence count; pairs with P2-1) |
| P2-5 | **`social_mentions` (15pts) is dead (0 fires / 6M+ rows) but still in the `SCORER_MAX_RAW=208` denominator** → every score structurally deflated ~7%, suppressing borderline candidates below MIN_SCORE/CONVICTION. | `scorer.py:37,121,256` | Real alerts are systematically under-scored; some valid candidates never clear the gate. | EVIDENCE (operator-deferred since 2026-05-17 with 0-flip recipe; produce VPS-data flip-count evidence so Variant B can ship safely — do NOT auto-ship the threshold recalibration) |
| P2-6 | **MiroFish/LLM narrative is a near-constant ~45-55 floor yet carries 40% weight** → conviction ≈ `0.6*quant + ~20`; narrative barely discriminates. | `mirofish/fallback.py:23-25`, `config.py NARRATIVE_WEIGHT=0.4` | A 40%-weighted "signal" is largely a constant offset; the 70 gate is really `quant≥~83`. | EVIDENCE (offline: measure stored `narrative_score` variance + outcome correlation; recommend weight change with data) |
| P2-7 | **Scorer/conviction machinery is disconnected from the trade outcomes used to judge signals** (drives only alerts + dead `first_signal`). | `signals.py:450-469`, `findings_live_evaluable_signal_audit_2026_05_17.md:91-103` | Biggest *trust* gap: "conviction ≥ 70" does not gate trades; scorecards measure scanner signals, not the headline machinery. | FLAG (architectural; document the two-track reality clearly — do not restructure autonomously) |

### P2 — Coverage / recall (mostly structural/intentional)

| # | Finding | Evidence | Customer impact | Disposition |
|---|---------|----------|-----------------|-------------|
| P2-8 | **CG trending hard-capped at 15** (scorer only rewards rank ≤10; CG returns up to 30). | `coingecko.py:211`, `scorer.py:163` | Tokens surging into trending rank 16–30 never ingested. Low blast radius (mostly out-of-corpus) but a silent uninstrumented exclusion. | FIX-NOW (cheap: widen to 30 OR document intentional) |
| P2-9 | **DexScreener universe = boosted (paid) tokens only** → the richest scorer signals (buy_pressure, token_age, real liquidity, quote_symbol) can only fire for paid-boosted or CG/GT-cross-sourced tokens. | `dexscreener.py:92,116`; `models.py:101-109` | Product structurally favors paid-promotion tokens for its best signals; organic un-boosted memecoins under-scored. | FLAG (partly intentional; operator judgment on adding a non-boosted lane) |
| P2-10 | **CG-sourced tokens never get token_age/buy_pressure/vol_liq_ratio** (hardcoded 0). | `models.py:101-109`, `scorer.py:107` | CG micro-caps systematically under-scored vs DEX-sourced; may never clear MIN_SCORE. | FLAG (corpus asymmetry; liquidity-enrichment cron partially addresses; document as recall ceiling) |

### P3 — Process / hygiene (this session)

| # | Finding | Disposition |
|---|---------|-------------|
| P3-1 | `.gitignore` does not cover `.codex_*`, `.pr*`, `.wt_*`, `_deploy_*` scratch (~350 untracked files) → footgun for accidental commits. | FIX-NOW (additive `.gitignore` patterns; never `git add -A`) |
| P3-2 | Shipped watchdog scripts (`revival-verdict`, `cron-drift`, `held-position-price`) referenced only in `cron/README.md` prose, not in committed timer/crontab → "shipped but unscheduled" meta-silent-failure. | FLAG (commit deploy-side scheduling or document; partially VPS-side, can't verify from source) |

---

## 3. Phase B execution plan (this session, full ceremony per PR)

Per-PR workflow: TDD → **internal multi-agent review (orthogonal vectors §8)** → address → **Codex
independent review (quick check)** → comprehensive tests → green CI → merge → deploy to srilu.

- **PR-1 — Reliability/§12 hardening** (P0-1..P0-4) — all additive, near-zero risk. *First.*
- **PR-2 — Detection latency observability** (P1-1) — additive instrumentation; gates future tuning.
- **PR-3 — Alert actionability enrichment** (P1-2) — additive alert content.
- **PR-4 — Speed micro-fixes** (P1-3 GeckoTerminal parallelize, P2-8 trending cap) — small, low-risk.
- **PR-5 — Scorer correctness** (P2-1..P2-4) — shifts alert stream; rigorous tests + monitoring note.
- **PR-6 — Today's Focus stale chip** (P1-4) — frontend; rebuild+commit `dist/`.
- **PR-7 — Offline evidence scripts** (P2-5 social-mentions flip-count, P2-6 narrative variance) — safe.
- **Hygiene** (P3-1) folded into PR-1.

**Flagged-not-shipped** (documented for operator): P2-7 (scorer↔outcome architecture), P2-9/P2-10
(coverage structural), P3-2 (watchdog scheduling), CG-budget lane reservation (revisit after PR-2
latency data lands, per §9a/§11).

This split honors "fix all the issues" for everything safely fixable autonomously while respecting
the project's own disciplines (§9a runtime verification, §11 soak-before-shipping behavior changes,
two-corpus, anti-scope). Risky/operator-deferred items are advanced with evidence, not recklessly shipped.

---

## 4. PR-1 review dispositions (2026-06-01)

PR-1 (`feat/reliability-silent-failure-hardening`) passed two internal orthogonal reviews
(silent-failure vector + structural-correctness vector). Applied:

- **HIGH (fixed):** the kill-switch alert hook created an *unbounded* aiohttp session, awaited inline
  on the engine hot path and inside the 60s shadow-evaluator close loop → a slow Telegram send could
  stall the daily-loss re-check. Now bounded (`ClientTimeout(total=15)` + `asyncio.wait_for(20s)`); a
  timeout surfaces as `kill_switch_alert_failed`, not a stall.
- **MED (fixed):** calibrate logged `calibrate_alert_delivered` unconditionally though the alerter
  swallows HTTP failures. Now `raise_on_failure=True` + delivered-on-success /
  `calibrate_alert_failed`-on-exception (swallowed so the calibration still commits). Truthful
  triplet; +1 failure-path test.
- **LOW (fixed):** `send_telegram_message` failure logs now carry `source=` so kill-switch/calibrate
  delivery failures are attributable (symmetry with the new `telegram_message_delivered`). Event
  names kept (avoid breaking any log-string watchdog).
- **P1 (fixed — Codex independent review):** the kill-switch alert hook was missing
  `raise_on_failure=True`, so a Telegram non-200 (bad token / 429) would log a *false*
  `kill_switch_alert_delivered` instead of `kill_switch_alert_failed` — a misleading delivery audit
  on the highest-stakes path. Now passes `raise_on_failure=True`. (Same truthfulness fix the internal
  reviewers caught for calibrate; Codex caught the parallel gap on the kill-switch hook — the
  multi-reviewer process working as intended.)

Tracked / deferred (not blockers):
- **`auto_clear_if_expired` is unscheduled** → the kill-switch "CLEARED" alert is latent (only the
  manual CLI clear path exists, and it's hookless by design). The clear-alert code is correct and
  dormant; wiring auto-expiry is a behavior change in the gated live subsystem — out of PR-1 scope.
  Follow-up: `BL-NEW-KILL-SWITCH-AUTO-EXPIRY-SCHEDULE`.
- **`kill_switch_alert_failed` is in-band** (a structured ERROR log). If Telegram is down when a kill
  fires, that log is the operator's only signal — it should be on the §12a log/error watchdog
  (PR-2 area). Tracked there.
- **Stale docstring** on `maybe_trigger_from_daily_loss` (describes a count-based winner check; code
  uses the returned `i_won`). Pre-existing; deferred to Phase C.
