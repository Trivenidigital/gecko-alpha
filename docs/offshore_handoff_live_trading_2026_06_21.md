# Gecko-Alpha Offshore Handoff: Expansion and Live Trading

Date: 2026-06-21
Audience: offshore engineering team taking over gecko-alpha
Repository: `C:\projects\gecko-alpha`
Production host referenced by repo docs: `srilu-vps`

## Read This First

gecko-alpha is a standalone CoinGecko-centered early pump detection pipeline.
It is not `coinpump-scout`; do not modify that older project when working on
gecko-alpha.

The system is already much more than a scanner. It includes:

- multi-source ingestion and enrichment
- quantitative and narrative scoring
- signal-specific paper trading
- live-trading scaffolding with Binance as the real implemented venue path
- venue routing, venue health, exposure gates, kill switches, and shadow/live ledgers
- Telegram alerting with pacing/backoff
- a FastAPI plus React dashboard
- operational watchdogs for SQLite, source calls, backups, Minara emissions,
  systemd drift, chain anchors, and prospective conviction snapshots

Current safe posture: paper and observe-first by default. Do not enable live
trading only by flipping a flag. Live trading requires the runbook checks in
`docs/runbooks/live-trading-deploy.md`, runtime-state verification, signal
eligibility review, exchange credentials, and operator sign-off.

Important integration posture:

- Binance spot is the only real venue path implemented deeply enough for first
  live execution.
- CCXT exists as scaffold for long-tail venues. Treat each new venue as a real
  adapter project with tests and health probes.
- Minara is currently alert-only. gecko-alpha can emit copy-paste Minara swap
  commands; it does not execute Minara swaps.
- Kraken MCP is not currently a production execution path inside gecko-alpha.
  Treat it as an external integration candidate until you verify the runtime
  host, credentials, MCP availability, and a designed adapter path.

## Repository Map

Core directories:

| Path | Purpose |
|---|---|
| `scout/main.py` | Long-running async pipeline orchestrator. |
| `scout/config.py` | Pydantic `Settings`; all thresholds and flags live here. |
| `scout/db.py` | Async SQLite layer, schema, migrations, query helpers. |
| `scout/ingestion/` | CoinGecko, DexScreener, GeckoTerminal, holder and price sources. |
| `scout/trading/` | Paper trading, signal dispatch, exits, calibration, auto-suspend, TG alert dispatch. |
| `scout/live/` | Live/shadow execution, routing, exchange adapters, venue health, kill switch. |
| `scout/gainers/`, `scout/spikes/`, `scout/chains/`, `scout/velocity/` | Signal-specific detectors and analytics. |
| `scout/narrative/`, `scout/mirofish/`, `scout/social/` | Narrative, MiroFish, LunarCrush and Telegram social layers. |
| `dashboard/` | FastAPI backend and React frontend. |
| `systemd/` | Production unit files mirrored from `/etc/systemd/system`. |
| `scripts/` | Operational scripts, deploy helpers, audits, watchdogs, backtests. |
| `docs/` | Runbooks, alignment docs, setup notes. |
| `tasks/` | Specs, findings, plans, backlog, lessons. Many files are historical; verify against code. |
| `tests/` | Pytest suite. Use TDD for changes. |

## Core Pipeline Flow

The main loop is async and source-parallel where possible.

High-level flow:

1. Ingest market and token data from CoinGecko, DexScreener, GeckoTerminal,
   social/narrative sources, and signal-specific trackers.
2. Normalize and aggregate candidate tokens.
3. Enrich with holders, volume history, price cache, CryptoPanic/news, perp
   anomalies, and chain/social context when enabled.
4. Score quantitative signals.
5. Run signal-specific paper dispatchers such as volume spikes, gainers early,
   losers contrarian, momentum, slow burn, velocity, chain completion, and
   first signal.
6. For general candidates, run conviction gating with MiroFish narrative
   simulation and fallback logic.
7. Apply safety checks and alerting.
8. Open paper trades through `TradingEngine`.
9. If live/shadow config allows it, hand the paper open event to `LiveEngine`.
10. Evaluate open paper/shadow trades, exits, health checks, and maintenance
    jobs on scheduled loops.

The pipeline is intentionally paper-first. Live execution is a downstream
handoff from a paper-trade open event, not a separate scanner.

## Ingestion and Detection Surfaces

CoinGecko is the primary source. The repo explicitly avoids paid Pro-only
endpoints such as `/coins/top_gainers_losers`.

Major source and detector surfaces:

| Surface | Main modules | Notes |
|---|---|---|
| CoinGecko markets and trending | `scout/ingestion/coingecko.py` | Free Demo tier, rate-limited. Top markets, trending, by-volume, midcap/deep-volume paths. |
| DexScreener | `scout/ingestion/dexscreener.py` | Boosts and DEX context. |
| GeckoTerminal | `scout/ingestion/geckoterminal.py` | Trending pools and chain liquidity context. |
| Holder enrichment | `scout/ingestion/holder_enricher.py` | Holder snapshots and related enrichment. |
| Volume spikes | `scout/spikes/` and `scout/trading/signals.py` | Records volume history and detects abnormal volume. |
| Gainers/losers | `scout/gainers/`, `scout/trading/signals.py` | Tracks early gainers, losers contrarian, gainers comparisons. |
| Chain patterns | `scout/chains/` | Active chains, chain matches, chain completion signals. |
| Momentum and slow burn | `scout/trading/signals.py` plus DB tables | Multi-day momentum and slow-burn detectors. Slow burn is currently retired/suspended by prior findings. |
| Velocity | `scout/velocity/` | Alerts based on acceleration/velocity surfaces. |
| Perp anomalies | `scout/perp/` | Binance futures/perp anomaly monitoring. This is a signal/enrichment source, not a futures execution path. |
| Narrative | `scout/narrative/`, `scout/mirofish/` | Narrative scan, MiroFish simulation, fallback. |
| Social | `scout/social/lunarcrush/`, `scout/social/telegram/` | LunarCrush and Telegram MTProto listener paths. |
| Prospective conviction watchlist | `scout/conviction_watchlist/` and dashboard tab | Observe-only sub-$30M high-conviction candidates before they pump. |

## Scoring and Conviction

Quantitative scoring lives in `scout/scorer.py`. The current model uses
multiple normalized signals such as momentum ratio, volume acceleration,
CoinGecko trending rank, stable-paired liquidity, holder and liquidity context,
and co-occurrence weighting.

Narrative scoring uses:

- MiroFish via `MIROFISH_URL`
- timeout controlled by `MIROFISH_TIMEOUT_SEC`
- daily cap controlled by `MAX_MIROFISH_JOBS_PER_DAY`
- fallback in `scout/mirofish/fallback.py`

The primary general gate is a conviction blend of quantitative and narrative
evidence. Signal-specific paper trades can also open via dedicated dispatchers
before a general candidate alert path.

Conviction has two dashboard concepts:

- `Conviction Shortlist`: retrospective. Coins already appeared on the
  gainers tracker; the page asks which surfaces caught them early.
- `Prospective Watchlist`: forward-looking, observe-only. Coins are not yet
  on gainers comparisons, are under the configured market-cap threshold, and
  have sustained multi-surface detection. It is explicitly unvalidated and
  must not be treated as an automatic live-trade trigger.

## Paper Trading

Paper trading is the current center of truth.

Main modules:

- `scout/trading/engine.py`: opens paper trades and applies signal params,
  duplicate checks, exposure checks, stale-price checks, and alert handoff.
- `scout/trading/paper.py`: paper trade persistence and close logic.
- `scout/trading/evaluator.py`: evaluates open paper trades for exits, peaks,
  moonshot/high-peak behavior, stop-loss, take-profit, and max duration.
- `scout/trading/params.py`: signal-level defaults and DB-backed params.
- `scout/trading/calibrate.py`: performance-driven parameter updates.
- `scout/trading/auto_suspend.py`: automatically disables bad signals and
  alerts the operator when reversing operator-applied state.
- `scout/trading/live_eligibility.py`: pure observability stamp for rows that
  would qualify for live consideration.

Key DB tables include:

- `paper_trades`
- `paper_daily_summary`
- `signal_params`
- `signal_params_audit`
- `trade_decision_events`
- `combo_performance`
- signal-specific tables such as `volume_spikes`, `gainers_comparisons`,
  `momentum_7d`, `slow_burn`, `signal_events`, `chain_patterns`,
  `active_chains`, `chain_matches`

Paper trades are append-oriented by contract because live/shadow ledgers
reference them. Do not casually delete paper trade rows.

Signal control happens in layers:

1. Environment/config flags in `Settings`.
2. `signal_params.enabled`.
3. Signal-specific eligibility such as `tg_alert_eligible`,
   `live_eligible`, conviction locks, and high-peak/moonshot options.
4. Auto-suspend and audit rows.

Always inspect both source code and live DB state before changing signal
behavior.

## Alerting

Telegram is the primary operator alert channel.

Main modules:

- `scout/alerter.py`
- `scout/trading/tg_alert_dispatch.py`
- `scout/trading/auto_suspend.py`
- `scout/live/kill_switch.py`
- `scout/trading/minara_alert.py`

Important alerting rules:

- System-health and state-change alerts should pass `parse_mode=None` unless
  all dynamic values are Markdown-escaped. Signal names include underscores,
  and Telegram Markdown can mangle them without returning an error.
- Automated reversals of operator-applied state must log dispatched and
  delivered events around the Telegram call.
- Telegram 429 handling is centralized with `retry_after` pacing and bounded
  retry. New callsites must pass a useful `source=` label so future 429s are
  attributable.
- Operator-labeling of Telegram alerts may still be a workflow gap. Check the
  current dashboard and DB before relying on label-derived training data.

## Minara Integration

The repo name is `Minara` in source and docs. If external notes say `Minra`,
treat that as the same intended tool unless proven otherwise.

Current behavior:

- gecko-alpha does not execute Minara swaps.
- It emits copy-paste Minara swap commands for eligible Solana-listed
  paper-trade-open signals.
- Command generation lives in `scout/trading/minara_alert.py`.
- Relevant config:
  - `MINARA_ALERT_ENABLED`
  - `MINARA_ALERT_FROM_TOKEN`
  - `MINARA_ALERT_AMOUNT_USD`
- Persistence and freshness are monitored by Minara emission tables and the
  `minara-emission-persistence-watchdog` systemd timer.

If the offshore team wants actual Minara execution, design it as a new live
venue project. Minimum requirements:

- wallet custody and key-management model
- local vs VPS execution decision
- operator approval or deterministic policy gate
- idempotency key and duplicate-prevention strategy
- order/fill reconciliation
- venue health and balance probes
- cross-venue exposure integration
- kill-switch integration
- tests that prove failed execution cannot be mistaken for filled execution

Do not silently replace the current alert-only posture with execution.

## Live Trading Surface

Live trading code exists, but live mode is not the default posture.

Main modules:

| Module | Purpose |
|---|---|
| `scout/live/config.py` | `LiveConfig`: maps `LIVE_*` settings to execution decisions. |
| `scout/live/engine.py` | Chokepoint after paper trade opens. Decides skip, shadow, reject, or live dispatch. |
| `scout/live/gates.py` | Liquidity, slippage, exposure, balance, and risk gates. |
| `scout/live/routing.py` | Venue selection from listings, health, overrides, and adapter metadata. |
| `scout/live/binance_adapter.py` | Binance spot implementation, signed requests gated by config. |
| `scout/live/ccxt_adapter.py` | Generic CCXT scaffold; not a complete production venue by itself. |
| `scout/live/kill_switch.py` | Live kill switch and daily loss cap logic. |
| `scout/live/idempotency.py` | Client order id and duplicate submit protection. |
| `scout/live/reconciliation.py` | Shadow reconciliation; live reconciliation must be reviewed before expansion. |
| `scout/live/services/` | Venue health, dormancy, rate state, and service runner. |

Key tables:

- `shadow_trades`
- `live_trades`
- `kill_events`
- `live_control`
- `venue_overrides`
- `resolver_cache`
- `venue_health`
- `wallet_snapshots`
- `venue_listings`
- `venue_rate_state`
- `symbol_aliases`
- `cross_venue_exposure` view
- `cross_venue_pnl` view
- `signal_venue_correction_count`
- `live_operator_overrides`

Primary live config flags:

- `LIVE_TRADING_ENABLED`
- `LIVE_MODE` with values `paper`, `shadow`, `live`
- `LIVE_USE_REAL_SIGNED_REQUESTS`
- `LIVE_USE_ROUTING_LAYER`
- `LIVE_TRADE_AMOUNT_USD`
- `LIVE_SIGNAL_SIZES`
- `LIVE_SIGNAL_ALLOWLIST`
- `LIVE_MAX_EXPOSURE_USD`
- `LIVE_MAX_OPEN_POSITIONS`
- `LIVE_MAX_OPEN_POSITIONS_PER_TOKEN`
- `LIVE_DAILY_LOSS_CAP_USD`
- `LIVE_SLIPPAGE_BPS_CAP`
- `LIVE_DEPTH_HEALTH_MULTIPLIER`
- `LIVE_VENUE_PREFERENCE`
- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`

The engine intentionally crashes or refuses to boot for some unsafe
misconfigurations, such as routing-layer live mode without real signed
requests.

## Binance First-Live Path

Use `docs/live-mode-setup.md` and `docs/runbooks/live-trading-deploy.md` as
the primary runbooks. The compressed checklist is:

1. Confirm production DB, `.env`, services, and branch are exactly what you
   think they are. Do not rely on docs alone.
2. Confirm Binance API key permissions:
   - read enabled
   - spot trading enabled
   - withdrawals disabled
   - margin/futures/transfer disabled unless a separate design explicitly
     requires them
   - IP whitelist includes the production VPS
3. Run config check:

   ```bash
   uv run python -m scout.main --check-config
   ```

4. Verify systemd hardening and OnFailure Telegram notification are installed.
5. Verify signed `/api/v3/account` succeeds from the VPS.
6. Keep first live size tiny, normally `LIVE_TRADE_AMOUNT_USD=10`.
7. Enable only one signal at a time and wait for enough real fires to measure.
8. Verify `venue_health`, `venue_listings`, and `wallet_snapshots`.
9. Watch for:

   ```bash
   journalctl -u gecko-pipeline -f
   ```

   Expected live lifecycle logs include `live_dispatch_entered` and
   `live_dispatch_terminal`.

10. Query `signal_venue_correction_count` after fills. Manual SQL closes do
    not necessarily reset correction counters; review the runbook before
    interpreting them.

Do not set `live_eligible=1` for a signal because it looks promising in the
dashboard. Verify its closed-cohort performance, current `signal_params`,
runtime fire rate, risk gates, and venue availability first.

## Kraken and Additional Venues

The project has CCXT as a dependency and a generic CCXT adapter scaffold. It
does not mean Kraken is production-ready.

If using Kraken MCP, first decide whether it is:

1. a research/data plane feeding gecko-alpha, or
2. an execution adapter that can submit and reconcile orders.

For an execution adapter, the minimum implementation contract should match the
existing `ExchangeAdapter` shape:

- fetch venue metadata and symbol/listing info
- fetch order book/depth for slippage checks
- fetch balance and health
- place order with idempotency
- await fill or terminal failure
- write `live_trades` correctly
- update `venue_health`, `wallet_snapshots`, and correction counters
- integrate with kill switch, exposure gates, and route ranking

Before building Kraken support:

- Run a drift check. Search the repo for existing Kraken, CCXT, MCP, and venue
  support so you do not duplicate primitives.
- Verify the production host actually has the MCP, credentials, network access,
  and service permissions needed.
- Add tests at the adapter contract level and routing level.
- Run in `shadow` first, then tiny live size, then only expand after measured
  fills and reconciliation are clean.

## Dashboard

Dashboard backend:

- FastAPI app in `dashboard/api.py`
- DB query helpers in `dashboard/db.py`
- models in `dashboard/models.py`
- served by `gecko-dashboard.service`

Frontend:

- React app in `dashboard/frontend/`
- Vite build artifacts in `dashboard/frontend/dist/`

Main tabs and surfaces:

- Signals
- Trading
- Today's Focus
- What Changed
- Trade Inbox
- Now Tradable
- Conviction
- Prospective Watchlist
- Chains
- Pipeline / funnel
- Briefing
- Health
- TG Alerts
- X Alerts
- Signal Trust

Important endpoints include:

- `/api/candidates`
- `/api/alerts/recent`
- `/api/tg_alerts/recent`
- `/api/signals/today`
- `/api/status`
- `/api/funnel/latest`
- `/api/trading/positions`
- `/api/trading/history`
- `/api/trading/stats`
- `/api/trading/actionability`
- `/api/trade_inbox`
- `/api/todays_focus`
- `/api/live_candidates`
- `/api/conviction/shortlist`
- `/api/conviction/prospective`
- `/api/system/health`
- `/api/source_calls/health`

The dashboard is not just reporting. Some endpoints record operator actions
or close paper trades. Treat API changes as operational changes.

## SQLite, Persistence, and Maintenance

The production DB is SQLite in WAL mode. Recent work converted it to
incremental auto-vacuum and added durable maintenance.

Important maintenance behavior:

- hourly WAL checkpoint when threshold is exceeded
- incremental vacuum when freelist exceeds threshold
- stale-reader watchdog for long-lived non-service readers
- structured logs for checkpoint tuple and busy state
- busy checkpoint alerts after consecutive failures

Relevant settings:

- `SQLITE_WAL_CHECKPOINT_ENABLED`
- `SQLITE_WAL_CHECKPOINT_THRESHOLD_BYTES`
- `SQLITE_WAL_CHECKPOINT_BUSY_ALERT_THRESHOLD`
- `SQLITE_INCREMENTAL_VACUUM_ENABLED`
- `SQLITE_INCREMENTAL_VACUUM_FREELIST_THRESHOLD`
- `SQLITE_INCREMENTAL_VACUUM_MAX_PAGES`
- `SQLITE_STALE_READER_WATCHDOG_ENABLED`
- `SQLITE_STALE_READER_MAX_AGE_HOURS`
- `SQLITE_EXPECTED_SERVICE_UNITS`

Do not run ad-hoc long-lived SQLite readers against production. Stale readers
can pin WAL frames and defeat checkpoint truncation.

## Systemd and Production Services

Tracked unit files live in `systemd/`. They are mirrored from production so
reviewers can see drift.

Key units:

- `gecko-pipeline.service`
- `gecko-dashboard.service`
- `gecko-backup.service` and timer
- `gecko-backup-watchdog.service` and timer
- `minara-emission-persistence-watchdog.service` and timer
- `systemd-drift-watchdog.service` and timer
- `chain-anchor-health-watchdog.service` and timer
- Codex/Hermes failure-alert and auto-remediation templates

For systemd changes, use the repo workflow in `systemd/README.md`. Avoid
`systemctl edit`; it creates invisible drop-ins that bypass source review.

Restart blast radius:

- restarting `gecko-pipeline` interrupts scans and paper evaluation for about
  10-20 seconds
- restarting `gecko-dashboard` drops in-flight HTTP connections but is less
  trading-critical

Prefer deploy windows between scan cycles.

## Local Development and Verification

Common commands:

```bash
uv run pytest --tb=short -q
uv run python -m scout.main --dry-run --cycles 1
uv run python -m scout.main --check-config
uv run black scout/ tests/
```

Frontend:

```bash
cd dashboard/frontend
npm install
npm run build
```

If a change touches frontend source, rebuild and commit `dashboard/frontend/dist`
unless the repo policy changes.

If working from the Windows Codex/Bash setup documented in `AGENTS.md`, SSH
stdout capture is broken. Use the two-step file redirect pattern:

```bash
ssh user@host 'command' > .ssh_out.txt 2>&1
# then read .ssh_out.txt locally
```

Do not try to fix this by piping, `tee`, command substitution, or `&& cat`.

## Operational Safety Rules for Expansion

Use these as hard rules for offshore work:

1. Verify runtime state before changing anything whose effect depends on DB
   rows, `.env`, services, exchange state, feature flags, cached values, or
   external approval status.
2. Do not assume backlog status is current. Search the tree and verify live
   state before opening work.
3. Every new pipeline table needs a freshness SLO and watchdog at ship time.
4. Every automated reversal of operator-applied state needs a Telegram alert
   at the write site.
5. Every new execution venue needs idempotency, health, balance, slippage,
   exposure, reconciliation, and kill-switch integration.
6. Every live-mode change needs a rollback path and a journal/dashboard
   verification plan.
7. Do not put secrets in git. `.env`, API keys, Telegram session files, wallet
   keys, and exchange credentials must stay out of commits and backups unless
   explicitly designed otherwise.
8. Use small live sizing first. Prove fill, reconciliation, and close behavior
   before increasing notional.

## Suggested Offshore Roadmap

### Phase 0: Orientation

- Read this document, `AGENTS.md`, `docs/gecko-alpha-alignment.md`,
  `docs/live-mode-setup.md`, and `docs/runbooks/live-trading-deploy.md`.
- Run the full test suite locally.
- Start the dashboard locally or inspect the live dashboard with operator
  permission.
- Map current production `.env`, `signal_params`, services, DB health, and
  venue health before proposing live changes.

### Phase 1: Live-Readiness Audit

- Confirm current `LIVE_*` settings on production.
- Confirm which signals are enabled, auto-suspended, TG-alert eligible, and
  live eligible.
- Confirm current paper performance by signal and by cohort.
- Confirm Binance credentials and whitelist.
- Confirm `venue_health`, `venue_listings`, `wallet_snapshots`, and
  `cross_venue_exposure`.
- Confirm systemd hardening and OnFailure alerts.

Deliverable: a signed-off readiness report, not a code change.

### Phase 2: Shadow Mode

- Run one narrowly chosen signal through `LIVE_MODE=shadow`.
- Prove that shadow rows open, evaluate, reconcile, and close.
- Confirm dashboard and system health show expected state.
- Confirm no unexpected kill-switch, exposure, or routing behavior.

Deliverable: shadow-mode evidence with DB rows and journal lines.

### Phase 3: First Tiny Binance Live Trade

- Use a small notional, normally 10 USD.
- Use one signal only.
- Watch the full route: paper open -> live dispatch -> venue order -> fill ->
  ledger write -> dashboard -> reconciliation/close.
- Do not expand until the first live path is fully understood and reviewed.

Deliverable: live-trade postmortem with fill, fees/slippage, DB rows, logs, and
operator observations.

### Phase 4: Add Venues

- Add one venue at a time.
- Prefer adapter-contract tests before wiring into routing.
- Kraken MCP, Kraken via CCXT, Bybit, MEXC, Kucoin, Coinbase, and on-chain
  routes should each be treated as separate venue projects.
- Do not mix a new venue and a new signal-policy change in the same release.

Deliverable: adapter, tests, health probes, shadow evidence, tiny-live evidence.

### Phase 5: Minara Execution, If Desired

- Keep the current Minara alert-only path intact until execution is explicitly
  designed.
- Add custody, approval, idempotency, reconciliation, kill switch, and health
  monitoring before any wallet execution.

Deliverable: Minara execution design, then implementation behind a hard kill
switch and shadow/dry-run equivalent.

### Phase 6: Auto-Enable and Scaling

- Only after live fills and reconciliation are boring should the team consider
  performance-gated auto-enable or larger sizing.
- Use data-bound gates: required number of live fires, fill quality, realized
  slippage, reconciliation accuracy, and drawdown limits. Calendar duration is
  secondary.

## Known Watch Items

These are not all blockers, but they should be treated as active context:

- Prospective Watchlist is observe-only and unvalidated. It is not a live
  trading trigger.
- Slow burn was previously retired/suspended after failed forward soak. Do not
  revive without a new finding and explicit gate.
- `first_signal` was previously an extend-soak verdict rather than a retirement
  verdict. Refresh the cohort before changing it.
- Telegram alert operator labels may still lack enough data for training or
  precision analysis.
- HPF and other historical backlog items may have stale statuses. Verify before
  acting.
- Manual DB interventions can affect counters and audit semantics. Prefer
  code paths and documented scripts.

## Recommended First Questions for the Offshore Team

Before changing code, answer these from live runtime evidence:

1. What branch and commit are deployed on `gecko-pipeline` and
   `gecko-dashboard`?
2. What are current values of all `LIVE_*`, `BINANCE_*`, `MINARA_*`, and
   Telegram alert settings?
3. Which `signal_params` rows are `enabled=1`, `tg_alert_eligible=1`, and
   `live_eligible=1`?
4. Which signals have enough closed trades to justify live eligibility?
5. Are `venue_health`, `venue_listings`, and `wallet_snapshots` fresh?
6. Are there open `shadow_trades` or `live_trades`?
7. Is `live_control` clear of active kill events?
8. Are SQLite maintenance, backup, source-call, and Minara watchdogs green?
9. Is Telegram alerting healthy, including 429 pacing source labels?
10. What is the rollback command and who approves it?

## Final Guidance

Treat gecko-alpha as a production trading system even while it is in paper mode.
The codebase already contains the hard parts that prevent silent failure:
audits, watchdogs, kill switches, health surfaces, and append-only ledgers.
Keep that discipline when expanding it.

The safest path to live trading is not "enable everything." It is:

1. verify runtime state,
2. choose one signal,
3. choose one venue,
4. run shadow,
5. run tiny live,
6. prove reconciliation,
7. scale only after measured evidence.
