"""Tier 1b auto-suspension: flips ``signal_params.enabled=0`` on dud signals.

Scheduled in-loop on a daily hour-gate from
``scout.main._run_feedback_schedulers`` — matches the ``FEEDBACK_*`` cron-style
pattern, NOT an external cron job. Idempotent within a day via
``_suspension_ran_today`` sentinel managed by the caller.

Triggers (any one is sufficient, in priority order):

1. ``hard_loss``      — Combined gate (BL-NEW-AUTOSUSPEND-FIX 2026-05-06):
                          ``net_pnl <= SIGNAL_SUSPEND_HARD_LOSS_USD``
                          OR (``max_drawdown <= SIGNAL_SUSPEND_HARD_LOSS_USD``
                              AND ``net_pnl < SIGNAL_SUSPEND_PNL_THRESHOLD_USD``).
                        First disjunct catches catastrophic net bleed (no
                        MIN_TRADES floor). Second disjunct catches
                        pump-then-crash (drew up then fell below zero with
                        deep peak-to-trough). Profitable signals with normal
                        volatility (drew up, gave some back, still net
                        positive) do NOT fire — the prior drawdown-only rule
                        produced false positives killing winners.
2. ``pnl_threshold``  — ``net_pnl < SIGNAL_SUSPEND_PNL_THRESHOLD_USD``
                        AND ``n_trades >= SIGNAL_SUSPEND_MIN_TRADES``.

Auto-suspension is ONE-WAY — the job NEVER sets ``enabled=1``. Re-enable
requires operator action via :meth:`Database.revive_signal_with_baseline`,
which atomically flips ``enabled=1`` AND stamps ``drawdown_baseline_at`` so
historical drawdown isn't carried into the new rolling window.

Window: trades closed since ``MAX(signal_params.last_calibration_at,
signal_params.drawdown_baseline_at, last 30d)`` — last_calibration_at protects
against killing a signal for losses incurred under stale params (per
adversarial review §2); drawdown_baseline_at protects against carrying
historical drawdown across operator revival (BL-NEW-AUTOSUSPEND-FIX).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import structlog

from scout.config import Settings
from scout.db import Database
from scout.trading.params import (
    CALIBRATION_EXCLUDE_SIGNALS,
    DEFAULT_SIGNAL_TYPES,
    bump_cache_version,
)

# aiohttp pulls OpenSSL Applink on Windows — defer import to call sites.
if TYPE_CHECKING:
    import aiohttp

log = structlog.get_logger(__name__)


async def _rolling_stats(
    conn,
    signal_type: str,
    since_iso: str,
) -> tuple[int, float, float]:
    """Return (n_trades, net_pnl_usd, max_drawdown_usd) for the window.

    ``max_drawdown_usd`` is **peak-to-trough** — the deepest cumulative-pnl
    drop from any prior peak in the window, returned as a non-positive
    number. A signal that ran +$1,000 and then bled to +$1 has drawdown
    -$999. The earlier ``min(running, 0)`` formulation missed this case
    entirely (it never went negative). Comparison ``<= hard_loss``
    (e.g. -500.0) fires when the trough is at least that deep.
    """
    cur = await conn.execute(
        """SELECT COALESCE(pnl_usd, 0) AS pnl
           FROM paper_trades
           WHERE signal_type = ?
             AND status LIKE 'closed_%'
             AND datetime(closed_at) >= datetime(?)
           ORDER BY closed_at ASC""",
        (signal_type, since_iso),
    )
    rows = await cur.fetchall()
    n = len(rows)
    if n == 0:
        return 0, 0.0, 0.0
    running = 0.0
    peak = 0.0
    max_drawdown = 0.0  # most-negative running-minus-peak we've seen
    for row in rows:
        running += float(row[0])
        if running > peak:
            peak = running
        drop = running - peak  # always <= 0
        if drop < max_drawdown:
            max_drawdown = drop
    return n, round(running, 2), round(max_drawdown, 2)


async def _active_signal_types(conn) -> list[str]:
    """Currently-enabled signal_params rows. Excludes already-suspended rows."""
    cur = await conn.execute(
        "SELECT signal_type FROM signal_params WHERE enabled = 1 ORDER BY signal_type"
    )
    rows = await cur.fetchall()
    return [r[0] for r in rows]


async def _suspend(
    conn,
    signal_type: str,
    *,
    reason: str,
    detail: str,
    now_iso: str,
) -> None:
    """Atomic suspend: flip enabled, write reason, append audit row.

    Caller is responsible for the surrounding ``BEGIN EXCLUSIVE`` so the
    suspend + audit + Telegram are one transaction.
    """
    # V1-I1 PR-stage fold: read pre-update tg_alert_eligible.
    cur = await conn.execute(
        "SELECT tg_alert_eligible FROM signal_params WHERE signal_type = ?",
        (signal_type,),
    )
    pre_row = await cur.fetchone()
    pre_eligible = str(pre_row[0]) if pre_row and pre_row[0] is not None else "0"
    # R2-I1 design fold: clear tg_alert_eligible jointly with enabled so
    # auto-suspended signals don't continue alerting after they stop
    # producing paper trades. Revive helper restores =1 if signal in
    # DEFAULT_ALLOW_SIGNALS.
    await conn.execute(
        """UPDATE signal_params
           SET enabled = 0,
               tg_alert_eligible = 0,
               suspended_at = ?,
               suspended_reason = ?,
               updated_at = ?,
               updated_by = 'auto_suspend'
           WHERE signal_type = ?""",
        (now_iso, reason, now_iso, signal_type),
    )
    await conn.execute(
        """INSERT INTO signal_params_audit
           (signal_type, field_name, old_value, new_value,
            reason, applied_by, applied_at)
           VALUES (?, 'enabled', '1', '0', ?, 'auto_suspend', ?)""",
        (signal_type, f"{reason}: {detail}", now_iso),
    )
    # V1-I1 fold: only emit audit row if value actually changed.
    if pre_eligible != "0":
        await conn.execute(
            """INSERT INTO signal_params_audit
               (signal_type, field_name, old_value, new_value,
                reason, applied_by, applied_at)
               VALUES (?, 'tg_alert_eligible', ?, '0', ?, 'auto_suspend', ?)""",
            (signal_type, pre_eligible, f"joint suspend: {reason}", now_iso),
        )


async def maybe_suspend_signals(
    db: Database,
    settings: Settings,
    *,
    session=None,  # aiohttp.ClientSession | None
) -> list[dict]:
    """Run one suspension pass. Returns list of suspended signals (may be empty)."""
    if not getattr(settings, "SIGNAL_PARAMS_ENABLED", False):
        # Flag-off — same gate as calibrate.py. Never auto-modify until
        # operator opts in.
        return []

    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")

    # Defer alerter import to the call sites — `scout.alerter` imports
    # aiohttp at module level, which triggers Windows OpenSSL Applink
    # loading even when no Telegram delivery actually happens. We import
    # right where it's needed (both branches) — the previous "hoist to top
    # of function" fix accidentally re-introduced the cost. The
    # NameError-on-pnl_threshold-path bug is solved by importing in BOTH
    # branches, not by hoisting.

    pnl_threshold = settings.SIGNAL_SUSPEND_PNL_THRESHOLD_USD
    hard_loss = settings.SIGNAL_SUSPEND_HARD_LOSS_USD
    min_trades = settings.SIGNAL_SUSPEND_MIN_TRADES

    fixed_window_iso = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()

    suspended: list[dict] = []
    candidates = await _active_signal_types(conn)
    for signal_type in candidates:
        if signal_type in CALIBRATION_EXCLUDE_SIGNALS:
            # Excluded from calibration → also excluded from auto-suspend
            # (we don't tune them, so we shouldn't auto-kill them either).
            continue
        if signal_type not in DEFAULT_SIGNAL_TYPES:
            # Orphan row — log but skip (per design doc: operator drops manually).
            log.warning(
                "auto_suspend_orphan_row",
                signal_type=signal_type,
            )
            continue

        cur = await conn.execute(
            "SELECT last_calibration_at, drawdown_baseline_at "
            "FROM signal_params WHERE signal_type = ?",
            (signal_type,),
        )
        row = await cur.fetchone()
        last_cal = row[0] if row else None
        baseline = row[1] if row else None
        # Window floor = MAX(last_cal, baseline, 30d_default).
        # ISO-8601 lex-sort matches chronological order for any consistent
        # timezone offset; all writes use UTC isoformat() so this is safe.
        since_iso = max(iso for iso in (last_cal, baseline, fixed_window_iso) if iso)

        n, net_pnl, max_drawdown = await _rolling_stats(conn, signal_type, since_iso)

        # Hard-loss combined gate (BL-NEW-AUTOSUSPEND-FIX): fires on either
        #   (a) catastrophic net bleed: net_pnl <= hard_loss (no MIN_TRADES floor)
        #   (b) pump-then-crash: deep peak-to-trough AND net < pnl_threshold
        # Profitable signals with normal volatility (drew up, gave back, still
        # net positive) do NOT fire. The previous drawdown-only rule killed
        # losers_contrarian (+$635 net, -$857 dd) and gainers_early (+$120 net,
        # -$1640 dd) on 2026-05-02 / 2026-05-04 — both false positives.
        #
        # Second disjunct uses ``net_pnl < pnl_threshold`` (NOT ``<= 0``) to
        # close the no-MIN_TRADES-floor gap: a sparse-data signal at
        # (n=2, net=-$10, dd=-$510) would otherwise hard-kill via this branch
        # while the proper pnl_threshold rule defers it (n < MIN_TRADES). The
        # tightened threshold preserves the pump-then-crash kill on real
        # losses (e.g., n=9, net=-$300, dd=-$600 still fires) while letting
        # borderline-negative low-data signals route to pnl_threshold.
        fires_hard_loss = net_pnl <= hard_loss or (
            max_drawdown <= hard_loss and net_pnl < pnl_threshold
        )
        if fires_hard_loss:
            try:
                await conn.execute("BEGIN EXCLUSIVE")
                await _suspend(
                    conn,
                    signal_type,
                    reason="hard_loss",
                    detail=(
                        f"net ${net_pnl:.0f}, drawdown ${max_drawdown:.0f} " f"(n={n})"
                    ),
                    now_iso=now_iso,
                )
                if session is not None:
                    from scout import alerter  # local import (Windows OpenSSL)

                    await alerter.send_telegram_message(
                        f"⚠ signal {signal_type} auto-suspended (hard_loss): "
                        f"net ${net_pnl:.0f}, drawdown ${max_drawdown:.0f}, "
                        f"n={n}",
                        session,
                        settings,
                    )
                await conn.commit()
            except Exception:
                try:
                    await conn.execute("ROLLBACK")
                except Exception as rb_err:
                    log.exception("auto_suspend_rollback_failed", err=str(rb_err))
                raise
            suspended.append(
                {
                    "signal_type": signal_type,
                    "reason": "hard_loss",
                    "n_trades": n,
                    "net_pnl": net_pnl,
                    "max_drawdown": max_drawdown,
                }
            )
            continue

        # Threshold-based suspension — needs MIN_TRADES floor.
        if n < min_trades:
            continue
        if net_pnl >= pnl_threshold:
            continue

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await _suspend(
                conn,
                signal_type,
                reason="pnl_threshold",
                detail=f"net_pnl ${net_pnl:.0f} (n={n})",
                now_iso=now_iso,
            )
            if session is not None:
                from scout import alerter  # local import (Windows OpenSSL)

                await alerter.send_telegram_message(
                    f"⚠ signal {signal_type} auto-suspended (pnl_threshold): "
                    f"net ${net_pnl:.0f}, n={n}",
                    session,
                    settings,
                )
            await conn.commit()
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                log.exception("auto_suspend_rollback_failed", err=str(rb_err))
            raise
        suspended.append(
            {
                "signal_type": signal_type,
                "reason": "pnl_threshold",
                "n_trades": n,
                "net_pnl": net_pnl,
                "max_drawdown": max_drawdown,
            }
        )

    if suspended:
        bump_cache_version()
        log.info("auto_suspend_done", suspended=suspended)
    return suspended
