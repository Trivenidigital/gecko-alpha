# Runbook — gainers_early revival (two-gate) + §12b expectations

**Context (2026-07-10).** `gainers_early` — the best-performing lane on the
2026-05-13 soak verdict (`keep_on_permanent`, n=128, +$894.37, 72.7% win) —
went dark and sat unnoticed for ~7.5 weeks behind **two independent automated
suppression gates**, each of which reverses operator-favorable (active) state:

1. **Signal-level auto-suspend** — `signal_params.enabled 1→0`,
   `suspended_reason=hard_loss`, `updated_by=auto_suspend`, at
   `2026-05-19T01:02:14Z` (net −$368, drawdown −$2,509, n=251). Fired 6 days
   after `keep_on_permanent`.
2. **Combo-level auto-suppression** — `combo_performance(gainers_early, 30d)
   suppressed 0→1` at `2026-06-12T03:02:16Z` (avg −16.59%, win-rate 15.25%),
   then `parole_at=2026-06-26`, `parole_trades_remaining` decremented to 0 →
   latched at `parole_exhausted`.

Reviving **only one gate leaves the other blocking.** Clearing
`signal_params.enabled` alone leaves the combo suppressed (`parole_exhausted`
returns `(False, ...)` in `suppression.should_open`); clearing the combo alone
leaves the signal disabled (`engine.py` `signal_disabled`).

**Why it stayed dark:** neither combo write site (initial suppression at 06-12,
parole exhaustion) emitted an operator alert. The §12b residual shipped
alongside this runbook closes that — see
`scout/trading/combo_refresh.py::_process_suppression_reversals` and #424's
`_process_permanent_suppression`.

---

## 0. Do NOT revive on nostalgia

The 2026-05-13 `keep_on_permanent` verdict is **stale** — the lane gave back the
edge immediately after (n=251, net −$368 by 05-19). Revival is gated on **fresh
forward expectancy ≥ 0 at n ≥ 20**, not on the old soak number. Section 4 is the
gate; sections 2–3 are diagnosis, section 5 is the mechanism.

Relevant Settings (defaults; confirm prod `.env` before acting):

| Setting | Default | Role |
|---|---|---|
| `SIGNAL_PARAMS_ENABLED` | `False` | master flag for auto-suspend / calibrate |
| `SIGNAL_SUSPEND_HARD_LOSS_USD` / `_PNL_THRESHOLD_USD` / `_MIN_TRADES` | −500 / −200 / 20 | signal-level auto-suspend gates |
| `FEEDBACK_SUPPRESSION_MIN_TRADES` | 20 | combo suppression floor |
| `FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT` | 30.0 | combo suppression win-rate gate |
| `FEEDBACK_PAROLE_DAYS` | 14 | parole window length |
| `FEEDBACK_PAROLE_RETEST_TRADES` | 5 | bounded retest allowance |
| `FEEDBACK_REFRESH_WINDOW_DAYS` | 30 | nightly refresh + permanent-suppression window |
| `SIGNAL_REVIVAL_MIN_SOAK_DAYS` | 7 | operator-revival cool-off |

---

## 1. The two gates, in the order dispatch encounters them

| # | Gate | Table / column | Cleared by |
|---|---|---|---|
| A | Dispatcher combo-suppression | `combo_performance.suppressed / parole_at / parole_trades_remaining` (window `30d`) | `revive_signal_with_baseline` opens a **bounded parole retest**; a full clear is a manual `UPDATE` |
| B | Signal-level enable | `signal_params.enabled` (+ `tg_alert_eligible`) | `revive_signal_with_baseline` flips `enabled=1` |

`revive_signal_with_baseline(signal_type, ...)` (in `scout/db.py`) handles
**both** gates in one atomic call for the **base combo** (`combo_key ==
signal_type`): it flips `enabled=1`, restores `tg_alert_eligible` (if the signal
is in `DEFAULT_ALLOW_SIGNALS`), stamps `drawdown_baseline_at=NOW()` (so
pre-revival drawdown is excluded from the auto-suspend window), and — if the
base combo is `suppressed=1` — **opens parole now** (`parole_at=NOW()`,
`parole_trades_remaining=FEEDBACK_PAROLE_RETEST_TRADES`) while **keeping
`suppressed=1`** (bounded retest, not full exoneration).

That bounded retest is deliberate: it is the mechanism that gathers the **fresh**
data section 4 gates on. The nightly `combo_refresh` (un-frozen by #424) then
clears `suppressed` automatically if the retest win-rate recovers, or
re-suppresses (and §12b-alerts) if it fails.

---

## 2. Diagnose the current state (read-only)

Two-step SSH (redirect to file, then read — SSH stdout is not directly
capturable on this workstation):

```bash
ssh srilu-vps 'sqlite3 -header -column /root/gecko-alpha/scout.db "
  SELECT enabled, tg_alert_eligible, suspended_at, suspended_reason, updated_by,
         drawdown_baseline_at
  FROM signal_params WHERE signal_type=''gainers_early'';
"' > .ge_signal.txt 2>&1
```
```bash
ssh srilu-vps 'sqlite3 -header -column /root/gecko-alpha/scout.db "
  SELECT suppressed, suppressed_at, parole_at, parole_trades_remaining,
         win_rate_pct, avg_pnl_pct, trades, last_refreshed,
         perm_suppression_alerted_at
  FROM combo_performance WHERE combo_key=''gainers_early'' AND window=''30d'';
"' > .ge_combo.txt 2>&1
```

Then `Read` each file. Baseline (dark) state:
`signal_params.enabled=0, suspended_reason=hard_loss` **and**
`combo_performance.suppressed=1, parole_trades_remaining=0`.

Confirm the audit trail (was the original kill an auto action reversing an
operator state?):

```bash
ssh srilu-vps 'sqlite3 -header -column /root/gecko-alpha/scout.db "
  SELECT field_name, old_value, new_value, applied_by, reason, applied_at
  FROM signal_params_audit WHERE signal_type=''gainers_early''
  ORDER BY applied_at DESC LIMIT 10;
"' > .ge_audit.txt 2>&1
```

---

## 3. Confirm #424's nightly refresh is live (freshness precondition)

The evidence gate needs `combo_performance` kept fresh. #424 widened the nightly
refresh to include suppressed zero-trade combos, so `last_refreshed` must be
advancing daily even while the lane is dark:

```bash
ssh srilu-vps 'sqlite3 /root/gecko-alpha/scout.db "
  SELECT combo_key, last_refreshed FROM combo_performance
  WHERE combo_key=''gainers_early'' AND window=''30d'';
"' > .ge_fresh.txt 2>&1
```

`last_refreshed` older than ~24–48h ⇒ the nightly refresh is not running; fix
that (see `docs/runbook_deploy3_2026_07.md`) **before** relying on section 4.

---

## 4. Evidence gate (pre-registered — do not relax at evaluation time)

Revival is a two-phase commit against **fresh, forward** data:

**Phase 1 — open the bounded retest (section 5).** `revive_signal_with_baseline`
re-enables the signal and opens a `FEEDBACK_PAROLE_RETEST_TRADES`-trade parole on
the base combo. This produces fresh closed trades under the *current* market
regime.

**Phase 2 — evaluate the forward cohort at n ≥ 20.** Do **not** declare the lane
"back" on the 5 parole retests alone (the automated `combo_refresh` clear rule
fires on a low bar). The operator keep/kill decision is:

> Keep `gainers_early` enabled **iff** the post-revival cohort reaches
> **n ≥ 20 closed trades** with **net expectancy ≥ 0** (net PnL ≥ 0 and, as a
> secondary check, win-rate ≥ `FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT`).

If the cohort is negative at n ≥ 20 → re-suppress (or let auto-suspend /
combo_refresh do it) and record the fresh anti-evidence. If n < 20 by the review
date → extend, do not decide. Forward-cohort query (run after revival):

```bash
ssh srilu-vps 'sqlite3 -header -column /root/gecko-alpha/scout.db "
  SELECT COUNT(*) AS n,
         ROUND(SUM(pnl_usd),2) AS net_pnl_usd,
         ROUND(100.0*SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END)/COUNT(*),1) AS win_rate_pct
  FROM paper_trades
  WHERE signal_type=''gainers_early''
    AND status LIKE ''closed_%''
    AND COALESCE(exit_provenance,'''') != ''entry_fallback''
    AND COALESCE(exit_reason,'''') != ''expired_stale_no_price''
    AND opened_at >= ''<REVIVAL_TIMESTAMP_ISO>'';
"' > .ge_forward.txt 2>&1
```

(The two `exit_provenance` / `exit_reason` predicates mirror `_rolling_stats` /
`refresh_combo` so fabricated $0 closes don't dilute the number.)

---

## 5. Revival procedure

**Recommended — bounded retest via the helper** (clears gate B, opens parole on
gate A). Run on the VPS in the app venv so Settings/cool-off are honored:

```bash
ssh srilu-vps 'cd /root/gecko-alpha && uv run python -c "
import asyncio
from scout.db import Database
from scout.config import get_settings

async def main():
    s = get_settings()
    db = Database(s.DB_PATH)
    await db.initialize()
    await db.revive_signal_with_baseline(
        \"gainers_early\",
        reason=\"operator: SIG-07 revival after fresh-expectancy review\",
        settings=s,
    )
    await db.close()

asyncio.run(main())
"' > .ge_revive.txt 2>&1
```

Notes:
- **Cool-off:** a second operator revival within `SIGNAL_REVIVAL_MIN_SOAK_DAYS`
  (7d) of a prior one raises `ValueError`. Pass `force=True` only with a
  recorded reason (it stamps a bypass marker + `revive_signal_force_bypass`
  WARNING).
- **Scope:** the combo parole is scoped to the **base** combo
  (`combo_key == signal_type`). Multi-signal combos containing `gainers_early`
  (e.g. `gainers_early+volume_spike`) are **not** touched — they re-prove on
  their own merits.

**Full manual clear (only if you explicitly do NOT want a bounded retest).** Skip
the parole and clear gate A outright — riskier, no bounded-retest safety net:

```bash
ssh srilu-vps 'sqlite3 /root/gecko-alpha/scout.db "
  UPDATE combo_performance
     SET suppressed=0, suppressed_at=NULL, parole_at=NULL,
         parole_trades_remaining=NULL
   WHERE combo_key=''gainers_early'' AND window=''30d'';
"'
```

Prefer the helper. The manual clear removes the very retest gate that produces
the section-4 evidence.

---

## 6. Post-revival verification

Re-run the section-2 queries. Expected immediately after the helper:
- `signal_params.enabled=1`, `suspended_at`/`suspended_reason` NULL,
  `drawdown_baseline_at` ≈ now.
- `combo_performance`: `suppressed=1` (bounded retest), `parole_at` ≈ now (window
  open), `parole_trades_remaining = FEEDBACK_PAROLE_RETEST_TRADES`.

Then watch dispatch resume — `parole_retest` grants and non-zero opens:

```bash
ssh srilu-vps 'sqlite3 -header -column /root/gecko-alpha/scout.db "
  SELECT decision, reason, COUNT(*) FROM trade_decision_events
  WHERE signal_type=''gainers_early'' AND ts >= datetime(''now'',''-1 day'')
  GROUP BY decision, reason ORDER BY 3 DESC;
"' > .ge_dispatch.txt 2>&1
```

Expect `opened` and `parole_retest` rows to appear; `suppressed /
parole_exhausted` should stop dominating. Confirm the first new paper trade
opens (`SELECT MAX(opened_at) ... WHERE signal_type='gainers_early'`).

---

## 7. §12b expectations post-revival (what alerts to expect, and when)

Every automated reversal of the now-operator-revived state fires a plain-text
(`parse_mode=None`) Telegram alert with `*_alert_dispatched` / `*_alert_delivered`
structured logs. After revival, expect **one** of:

| Outcome | Alert | Source `event` | Trigger |
|---|---|---|---|
| Retest **fails** (WR < threshold on real trades), parole exhausted, re-latches | `combo gainers_early failed its parole retest … re-suppressed …` | `suppression_reversal_alert_dispatched/_delivered` (transition `parole_exhausted_resuppressed`) | `combo_refresh._process_suppression_reversals` |
| Signal-level auto-suspend re-fires (hard_loss / pnl_threshold) | `signal gainers_early auto-suspended (hard_loss): …` | `auto_suspend_alert_dispatched/_delivered` | `auto_suspend._send_suspend_alert` |
| Combo goes dark again with **no** trades in the window | `signal gainers_early is in permanent-suppression state …` | `permanent_suppression_alert_dispatched/_delivered` | `combo_refresh._process_permanent_suppression` (#424) |
| Retest **recovers** (WR ≥ threshold) | *(no reversal alert — `combo_refresh` silently clears `suppressed`)* | — | `refresh_combo` clear branch |

If the lane goes dark again and you receive **no** alert, that is itself a §12b
regression — check `journalctl -u gecko-pipeline | grep -E
'suppression_reversal_alert|auto_suspend_alert|permanent_suppression_alert'`
for a `*_failed` event (delivery outage) or a missing dispatched/delivered pair
(callsite skipped).

Verify the alert paths are wired (structured-log presence over a window):

```bash
ssh srilu-vps 'journalctl -u gecko-pipeline --since "-30 days" \
  | grep -c "suppression_reversal_alert_dispatched"' > .ge_alert_logs.txt 2>&1
```

---

## Audit summary — §12b coverage of every gainers_early reversal write site

| Write site | Reverses operator-favorable state | Operator alert | Status |
|---|---|---|---|
| `auto_suspend._suspend` (signal `enabled 1→0`, both hard_loss & pnl_threshold) | yes | `auto_suspend_alert_dispatched/_delivered`, `parse_mode=None`, names signal+reason+stats | **pre-existing (verified §12b-compliant)** |
| `combo_refresh` initial suppression (`suppressed 0→1`) | yes | `suppression_reversal_alert_*` (transition `newly_suppressed`) | **added (SIG-07 residual)** |
| `combo_refresh` parole-exhausted re-suppression (fresh parole) | yes | `suppression_reversal_alert_*` (transition `parole_exhausted_resuppressed`) | **added (SIG-07 residual)** |
| `combo_refresh._process_permanent_suppression` (aged-out latch) | yes | `permanent_suppression_alert_dispatched/_delivered`, names combo + `revive_signal_with_baseline` | **pre-existing (#424, verified §12b-compliant)** |
