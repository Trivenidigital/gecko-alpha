**New primitives introduced:** `PAPER_GAINERS_MIN_MCAP` (Settings field, optional override of global `PAPER_MIN_MCAP` scoped to `trade_gainers` only)

# plan: lower gainers_early MC floor from $5M to $3M (paper-only soak)

| Field | Value |
|---|---|
| Backlog ID | BL-NEW-GAINERS-MIN-MCAP-3M |
| Status | DRAFT — pending operator approval |
| Author | claude (session 2026-05-08) |
| Trigger | USDUC analysis (2026-05-06) → 30d gap study identified $5M floor as a structural miss for sub-$5M pre-pump entries |
| Reversibility | trivial — `.env` removal + restart |
| Live impact | none — paper-only; live unaffected until separate flip |

## 1. Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Trading parameter tuning / micro-cap MC floor | none found | build in-tree (already 90% built — single-knob extension) |
| Backtest harness for paper-trade signals | none found | reuse in-tree pattern (`scripts/backtest_*.py` family — see `backtest_high_peak_existing_data_battery.py`) |
| Soak-window measurement | none found | reuse in-tree pattern (combo_performance + paper_trades aggregations) |

Awesome-hermes-agent ecosystem check: no skill in the trading-parameter-tuning or pump-detection domains. **Verdict:** custom in-tree extension is correct; this proposal does not duplicate any external capability.

## 2. Drift-check (existing primitives audit)

Tree state at 2026-05-08:

| Primitive | Status | File |
|---|---|---|
| `PAPER_MIN_MCAP` (global, $5M) | exists, used by 6 call sites | `scout/config.py:223`, `scout/main.py:556,581,766,1108`, `scout/narrative/agent.py:138,537` |
| `PAPER_GAINERS_MIN_MCAP` (gainers-scoped) | **does NOT exist** | n/a |
| `PAPER_GAINERS_MAX_MCAP` (gainers-scoped) | **does NOT exist** | n/a (`PAPER_MAX_MCAP` is global, used analogously) |
| `trade_gainers(min_mcap=..., max_mcap=...)` parameters | exist | `scout/trading/signals.py:80–180` |
| Late-pump filter `PAPER_GAINERS_MAX_24H_PCT` | exists, $50% | `scout/config.py:245` |
| `gainers_early` signal lifecycle (auto-suspend, revival) | exists | `signal_params` table; PR #79 (BL-NEW-AUTOSUSPEND-FIX) |
| Backtest precedent (existing-data battery) | exists | `scripts/backtest_high_peak_existing_data_battery.py`; CLAUDE.md §11b |

**Conclusion:** the change is additive (new optional knob), uses existing dispatch and ladder logic, and follows the existing-data-battery + soak protocol pattern. No redundant primitives.

## 3. Motivation — existing-data battery (pre-spec)

Run on prod `scout.db` 2026-05-08 (last 30 days, `gainers_snapshots` cohort).

### Cohort: ≥+100% peak / ≥$5M peak MC

27 qualifying pumps. **44% (12/27)** were first observed >50% — blocked by late-pump filter. **10 of those 12 are sub-$30M MC.** Pattern: small-cap CG-listed tokens are indexed post-pump.

### $3M-floor backtest (counterfactual)

Eligibility model: `market_cap ∈ [3M, 5M] AND price_change_24h ∈ (0, 50%]` (i.e., rows that would pass at $3M but were blocked at $5M).

**16 unique coins newly eligible over 30 days.**

Outcome distribution (peak within 7d hold = `max_duration_hours=168`):

| Class | n | % | Per-trade gross @ $300 (ladder applied) |
|---|---|---|---|
| Big winner (peak ≥ 50%) | 3 (GOBLIN, PENGUIN, BELIEVE) | 19% | ~+$80 |
| Mid winner (peak 20–49%) | 3 (EVAA, ST, UNC) | 19% | ~+$30 |
| Flat (peak < 10%) | 9 | 56% | ~$0 |
| Negative (peak ≈ 0%, min < −5%) | 1 (OBOL) | 6% | ~−$15 |
| **Outright SL hits** | **0** | **0%** | (no row dropped past −25% in window) |

**Estimated gross PnL: +$315 / 30d on $300 sizing → ~$3.8K/yr.** Linear with size.

**Strike rate: 38% useful, 62% noise.** Zero outright SL hits in this cohort — better than expected for $3–5M micro-caps, but n=16 → ±15pp CI.

### Cross-validation against missed-pump cohort

3 of 5 cleanly-missed pumps from the broader 30d analysis are recovered:
- GOBLIN (peak +500% / $5.7M MC) — $3M floor catches +21% entry
- BELIEVE (peak +999% / $9.6M MC) — catches +49% entry
- PENGUIN (peak +155% / $6.1M MC) — catches +46% entry

Two not recovered (USDUC, FOREST) were already pumping when CG indexed them — those need a CEX feed, not an MC floor change.

## 4. Scope

### IN

1. New optional Settings field `PAPER_GAINERS_MIN_MCAP: float | None = None` defaulting to `None` (= use global `PAPER_MIN_MCAP`).
2. Resolver in `main.py` call site for `trade_gainers`: `min_mcap = settings.PAPER_GAINERS_MIN_MCAP if settings.PAPER_GAINERS_MIN_MCAP is not None else settings.PAPER_MIN_MCAP`.
3. `.env` flip on VPS: `PAPER_GAINERS_MIN_MCAP=3000000`.
4. Pydantic validator: must be ≥ 0 or None.
5. Unit test: resolver returns override when set, falls back to global when unset.
6. Soak measurement (existing dashboard + ad-hoc SQL — no new tooling).
7. Pre-registered promotion / kill criteria (§6).

### OUT (explicit non-goals)

- Lowering `PAPER_MIN_MCAP` globally — losers_contrarian and narrative paths have NOT been backtested at $3M; out of scope.
- Adjusting `PAPER_GAINERS_MAX_24H_PCT` — late-pump filter is doing real work at $3M floor (TIME entered at +49% → peak +3%); separate experiment if pursued.
- Adding a `PAPER_GAINERS_MAX_MCAP` override — no operator demand; current global cap fits.
- CEX (MEXC/Gate) feed integration — separate, larger spec; this change is the cheap win that runs first.
- Live mode — paper-only soak; no `LIVE_MODE` change.

## 5. Implementation

### 5.1 `scout/config.py` change

Add after existing `PAPER_MIN_MCAP` definition (line 223 region):

```python
PAPER_MIN_MCAP: float = 5_000_000  # min $5M mcap to paper trade (filters junk) — UNCHANGED

# Gainers-scoped override of PAPER_MIN_MCAP. None = inherit global. Lowering
# this only affects trade_gainers; losers/narrative paths still use the global
# floor. Backtest 2026-05-08 (n=16, 30d) showed 38% useful strike rate, +$315
# net at $3M; see tasks/plan_paper_gainers_min_mcap_3m.md.
PAPER_GAINERS_MIN_MCAP: float | None = None
```

Add validator block:

```python
@field_validator("PAPER_GAINERS_MIN_MCAP")
@classmethod
def _validate_gainers_min_mcap(cls, v: float | None) -> float | None:
    if v is None:
        return v
    if v < 0:
        raise ValueError("PAPER_GAINERS_MIN_MCAP must be >= 0 or None")
    return v
```

### 5.2 `scout/main.py` change

Replace the `trade_gainers` dispatch block to resolve the override before passing to the dispatcher:

```python
gainers_min_mcap = (
    settings.PAPER_GAINERS_MIN_MCAP
    if settings.PAPER_GAINERS_MIN_MCAP is not None
    else settings.PAPER_MIN_MCAP
)
await trade_gainers(
    trading_engine,
    db,
    min_mcap=gainers_min_mcap,
    max_mcap=settings.PAPER_MAX_MCAP,
    settings=settings,
)
```

No other call sites change. `trade_losers` and narrative paths continue to use `settings.PAPER_MIN_MCAP` directly.

### 5.3 Unit test (new)

`tests/test_paper_gainers_min_mcap.py` — 5 tests:

1. Falls back to global when override unset.
2. Uses override when set.
3. Default is None (verifies field default).
4. Rejects negative values.
5. Zero is allowed (sentinel for "no floor at all" — distinct from None).

If the resolver gets reused elsewhere later, extract to a helper. For now inline keeps blast radius minimal.

### 5.4 `.env` change on VPS (post-merge)

```bash
echo "PAPER_GAINERS_MIN_MCAP=3000000" >> /root/gecko-alpha/.env
systemctl restart gecko-pipeline
```

Verify post-restart: `journalctl -u gecko-pipeline -n 50 | grep -i paper_gainers_min_mcap` should show the value loaded by Settings.

## 6. Soak protocol — pre-registered

| Field | Value |
|---|---|
| Soak duration | 14 days from `.env` flip |
| Sample-size gate | n ≥ 7 newly-eligible trades (mid-cohort lower bound from backtest projection of ~16/30d × 14/30 = 7.5) |
| Halt-early | YES — if data threshold met before 14d, evaluate; if not met by 14d, extend up to 21d before kill |
| Measurement window | only `gainers_early` paper trades opened with `signal_data.mcap < 5_000_000` after the flip timestamp |

### 6.1 Promotion criteria (ALL must hold)

1. **Strike rate (peak ≥ 20%) ≥ 25%** — backtest showed 38% with ±15pp CI; 25% is the lower CI floor.
2. **Mean realized PnL per trade > $0** at default $300 size, after closes (open trades excluded; reaching `max_duration` counts as a close).
3. **SL/total ratio ≤ 30%** — backtest showed 0% SL but n=16 is thin; this caps catastrophic divergence.
4. **No new structural failure mode** — manual review of any trade with PnL < −$50 to confirm no junk-filter / chain-mismatch / orphan-token issue surfaces.

### 6.2 Kill criteria (ANY triggers revert)

- Strike rate < 25% at n ≥ 7 closed trades.
- Mean realized PnL/trade < $0.
- SL/total ratio > 30%.
- 3+ trades with PnL < −$50 from the same structural cause.
- Soak elapsed 21 days without n ≥ 7 closed trades — insufficient signal, abandon as "not worth the noise."

### 6.3 Measurement queries

Run on day 7 and day 14 from `.env` flip timestamp `T0`:

```sql
-- Strike rate + per-trade PnL
SELECT
  COUNT(*) AS n,
  SUM(CASE WHEN peak_pct >= 20 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS strike_rate,
  SUM(CASE WHEN status = 'closed_sl' THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS sl_ratio,
  AVG(realized_pnl_usd) AS mean_pnl,
  SUM(realized_pnl_usd) AS total_pnl
FROM paper_trades
WHERE signal_type = 'gainers_early'
  AND opened_at >= '<T0>'
  AND status LIKE 'closed%'
  AND CAST(json_extract(signal_data, '$.mcap') AS REAL) < 5000000;

-- Loss-driver review
SELECT id, token_id, opened_at, peak_pct, status, realized_pnl_usd, signal_data
FROM paper_trades
WHERE signal_type = 'gainers_early'
  AND opened_at >= '<T0>'
  AND realized_pnl_usd < -50
ORDER BY realized_pnl_usd;
```

## 7. Rollback

Single-line revert. No code changes need to roll back if PR is merged — only the `.env`:

```bash
sed -i '/^PAPER_GAINERS_MIN_MCAP=/d' /root/gecko-alpha/.env
systemctl restart gecko-pipeline
```

`PAPER_GAINERS_MIN_MCAP` reverts to `None` → resolver falls back to global `PAPER_MIN_MCAP=5M`. Existing trades opened during soak continue under their original ladder; no force-close.

## 8. Risks

| # | Risk | Mitigation | Severity |
|---|---|---|---|
| 1 | n=16 backtest sample → strike-rate CI ±15pp | Pre-registered kill criteria with hard floors; halt-early on data threshold | medium |
| 2 | $3–5M cohort skews to junk meme-coins not caught by existing filter | `_is_tradeable_candidate` already runs first; junk filter catches `test-` etc. (PR #67); soak will surface any new junk pattern | low-medium |
| 3 | Concurrent slot pressure (paper trades use no live capital but `would_be_live` subset may displace) | Paper mode has no slot cap; `would_be_live` shadow-tag is informational only — no live impact during soak | low |
| 4 | Late-pump filter too generous at $3M (TIME entered at +49% → +3% peak) | 4-of-16 cohort entries had entry_pct > 45% but only 1 had material drawdown; if soak shows late-pump correlation with losses, separate spec to tighten `PAPER_GAINERS_MAX_24H_PCT` for sub-$5M only | medium |
| 5 | Settings change conflicts with future global `PAPER_MIN_MCAP` retunes | Override-only design preserves global semantics; future changes to global still apply when override is `None` | low |
| 6 | Soak straddles a regime shift (e.g., new auto-suspend trigger, market regime) | Manual review at T+7 of any structural anomalies; extend soak rather than promote on contaminated data | low |

## 9. Open questions

1. **Should the override extend to `losers_contrarian`?** Currently `losers_contrarian` is disabled (`PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED=False`); deferred. If revived, separate `PAPER_LOSERS_MIN_MCAP` spec — same shape as this one.
2. **Should the soak start with $4M instead of $3M?** Bisecting could reduce risk but doubles the calendar cost. Backtest covered the full $3–5M band; argument for $3M is that GOBLIN ($4.6M first MC) is the load-bearing recovery case.
3. **Telemetry: should new `signal_event` type fire (`gainers_early_below_global_floor`)?** Optional. Existing `signal_data.mcap` already tells the story via SQL filter. Skip unless dashboard exposure is needed.

## 10. Acceptance checklist

- [ ] `scout/config.py` adds `PAPER_GAINERS_MIN_MCAP` with validator
- [ ] `scout/main.py` `trade_gainers` dispatch uses resolver
- [ ] `tests/test_paper_gainers_min_mcap.py` passes (5 tests)
- [ ] Existing test suite passes (no regression)
- [ ] PR description references this spec + n=16 backtest summary
- [ ] Post-merge: `.env` flip on VPS, restart, journalctl verification
- [ ] Day 7 measurement query run and recorded
- [ ] Day 14 promotion / kill decision logged in this file's "Outcome" section

## 11. Outcome (filled post-soak)

_To be completed at T+14 days from `.env` flip._

- Flip timestamp:
- Newly-eligible trades opened (n):
- Strike rate (peak ≥ 20%):
- Mean PnL/trade:
- SL ratio:
- Promotion / kill decision:
- Memory file written: `~/.claude/projects/C--projects-gecko-alpha/memory/project_gainers_min_mcap_3m_<status>.md`
