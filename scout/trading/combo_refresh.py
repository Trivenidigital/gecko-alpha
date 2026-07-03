"""Nightly combo refresh (spec §5.3)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import aiosqlite
import structlog

from scout.db import Database
from scout.trading.paper import CLOSED_COUNTABLE_STATUSES

log = structlog.get_logger()


async def refresh_combo(db: Database, combo_key: str, settings) -> bool:
    """Recompute 7d + 30d rows for `combo_key`. Apply suppression rule to 30d.
    Returns True on success, False otherwise.
    """
    # Acquire the shared asyncio.Lock so the multi-statement read→write
    # sequence here cannot interleave with should_open's BEGIN...COMMIT block
    # across asyncio suspend points within the same event loop.
    if db._txn_lock is None:
        raise RuntimeError(
            "Database._txn_lock is None — Database.initialize() was not awaited "
            "before refresh_combo(). A fresh ephemeral Lock here would silently "
            "break mutual exclusion across concurrent callers."
        )
    async with db._txn_lock:
        return await _refresh_combo_locked(db, combo_key, settings)


async def _refresh_combo_locked(db: Database, combo_key: str, settings) -> bool:
    """Inner implementation — called with db._txn_lock already held."""
    try:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        stats = {}
        status_placeholders = ",".join("?" * len(CLOSED_COUNTABLE_STATUSES))
        for window, days in (("7d", 7), ("30d", 30)):
            cur = await db._conn.execute(
                f"""SELECT
                     COUNT(*) AS trades,
                     SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                     SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses,
                     COALESCE(SUM(pnl_usd), 0) AS total_pnl_usd,
                     COALESCE(AVG(pnl_pct), 0) AS avg_pnl_pct
                   FROM paper_trades
                   WHERE signal_combo = ?
                     AND status IN ({status_placeholders})
                     -- GA-01 / Phase 6 slice 2: exclude fabricated $0
                     -- closes (unpriceable token_id force-closed at
                     -- entry_price) from combo rollups — they dilute
                     -- total/avg PnL toward zero. Keyed on
                     -- exit_provenance (durable label); the GA-01
                     -- exit_reason predicate stays as OR-fallback.
                     AND COALESCE(exit_provenance, '') != 'entry_fallback'
                     AND COALESCE(exit_reason, '') != 'expired_stale_no_price'
                     AND closed_at >= ?""",
                (
                    combo_key,
                    *CLOSED_COUNTABLE_STATUSES,
                    (now - timedelta(days=days)).isoformat(),
                ),
            )
            row = await cur.fetchone()
            trades = row["trades"] or 0
            wins = row["wins"] or 0
            losses = row["losses"] or 0
            total_pnl = float(row["total_pnl_usd"] or 0)
            avg_pct = float(row["avg_pnl_pct"] or 0)
            wr = (100.0 * wins / trades) if trades else 0.0
            stats[window] = dict(
                trades=trades,
                wins=wins,
                losses=losses,
                total_pnl=total_pnl,
                avg_pct=avg_pct,
                wr=wr,
            )

        # 7d row: plain UPSERT.
        w7 = stats["7d"]
        await db._conn.execute(
            "INSERT INTO combo_performance "
            "(combo_key, window, trades, wins, losses, total_pnl_usd, "
            " avg_pnl_pct, win_rate_pct, suppressed, last_refreshed) "
            "VALUES (?, '7d', ?, ?, ?, ?, ?, ?, 0, ?) "
            "ON CONFLICT(combo_key, window) DO UPDATE SET "
            " trades=excluded.trades, wins=excluded.wins, losses=excluded.losses, "
            " total_pnl_usd=excluded.total_pnl_usd, avg_pnl_pct=excluded.avg_pnl_pct, "
            " win_rate_pct=excluded.win_rate_pct, last_refreshed=excluded.last_refreshed, "
            " refresh_failures=0",
            (
                combo_key,
                w7["trades"],
                w7["wins"],
                w7["losses"],
                w7["total_pnl"],
                w7["avg_pct"],
                w7["wr"],
                now_iso,
            ),
        )

        # 30d row: apply suppression rule.
        w30 = stats["30d"]
        cur = await db._conn.execute(
            "SELECT suppressed, parole_trades_remaining, suppressed_at "
            "FROM combo_performance WHERE combo_key = ? AND window = '30d'",
            (combo_key,),
        )
        existing = await cur.fetchone()

        min_trades = settings.FEEDBACK_SUPPRESSION_MIN_TRADES
        wr_thresh = settings.FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT
        parole_days = settings.FEEDBACK_PAROLE_DAYS
        retest = settings.FEEDBACK_PAROLE_RETEST_TRADES

        new_suppressed = 0
        new_suppressed_at = None
        new_parole_at = None
        new_parole_remaining = None

        if existing is None:
            # First write — maybe suppress immediately if bad enough.
            if w30["trades"] >= min_trades and w30["wr"] < wr_thresh:
                new_suppressed = 1
                new_suppressed_at = now_iso
                new_parole_at = (now + timedelta(days=parole_days)).isoformat()
                new_parole_remaining = retest
        else:
            was_suppressed = bool(existing["suppressed"])
            remaining = existing["parole_trades_remaining"]
            if not was_suppressed:
                if w30["trades"] >= min_trades and w30["wr"] < wr_thresh:
                    new_suppressed = 1
                    new_suppressed_at = now_iso
                    new_parole_at = (now + timedelta(days=parole_days)).isoformat()
                    new_parole_remaining = retest
            else:
                # Frozen-lock guard (fix/frozen-suppression-lock): a currently-
                # suppressed combo with ZERO trades in the 30d window is being
                # refreshed only to keep its state live/alertable (see the
                # widened selection in refresh_all). Recomputing a re-suppression
                # here would hand a permanently-locked combo a fresh parole
                # window + retest allowance — auto-revival the operator never
                # approved (constraint a). So zero-trade suppressed combos route
                # to the preserve branch; only combos with REAL retest data
                # (trades > 0) can clear or re-arm parole.
                zero_trade = w30["trades"] == 0
                exhausted = remaining is not None and remaining <= 0
                if not zero_trade and exhausted and w30["wr"] >= wr_thresh:
                    # Retest recovered on real data — clear suppression.
                    new_suppressed = 0
                    new_suppressed_at = None
                    new_parole_at = None
                    new_parole_remaining = None
                elif not zero_trade and exhausted:
                    # Retest failed on real data — re-suppress with fresh parole.
                    new_suppressed = 1
                    new_suppressed_at = now_iso
                    new_parole_at = (now + timedelta(days=parole_days)).isoformat()
                    new_parole_remaining = retest
                else:
                    # Preserve existing suppression state verbatim. Covers both
                    # mid-parole (remaining > 0 — parole timing must not reset
                    # every nightly refresh) and the frozen-lock zero-trade case
                    # (keep the lock exactly as-is; no revival).
                    new_suppressed = 1
                    new_suppressed_at = existing["suppressed_at"]
                    cur2 = await db._conn.execute(
                        "SELECT parole_at FROM combo_performance "
                        "WHERE combo_key = ? AND window = '30d'",
                        (combo_key,),
                    )
                    new_parole_at = (await cur2.fetchone())[0]
                    new_parole_remaining = remaining

        await db._conn.execute(
            "INSERT INTO combo_performance "
            "(combo_key, window, trades, wins, losses, total_pnl_usd, "
            " avg_pnl_pct, win_rate_pct, suppressed, suppressed_at, parole_at, "
            " parole_trades_remaining, refresh_failures, last_refreshed) "
            "VALUES (?, '30d', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?) "
            "ON CONFLICT(combo_key, window) DO UPDATE SET "
            " trades=excluded.trades, wins=excluded.wins, losses=excluded.losses, "
            " total_pnl_usd=excluded.total_pnl_usd, avg_pnl_pct=excluded.avg_pnl_pct, "
            " win_rate_pct=excluded.win_rate_pct, "
            " suppressed=excluded.suppressed, suppressed_at=excluded.suppressed_at, "
            " parole_at=excluded.parole_at, "
            " parole_trades_remaining=excluded.parole_trades_remaining, "
            " refresh_failures=0, last_refreshed=excluded.last_refreshed",
            (
                combo_key,
                w30["trades"],
                w30["wins"],
                w30["losses"],
                w30["total_pnl"],
                w30["avg_pct"],
                w30["wr"],
                new_suppressed,
                new_suppressed_at,
                new_parole_at,
                new_parole_remaining,
                now_iso,
            ),
        )
        await db._conn.commit()
        return True
    except (aiosqlite.Error, ValueError) as e:
        log.error(
            "combo_refresh_error",
            combo_key=combo_key,
            err=str(e),
            err_id="COMBO_REFRESH",
        )
        try:
            # Scope failure increment to the 30d window only: the 30d row drives
            # suppression decisions and the chronic-failure alert. Incrementing
            # both windows caused the alert to fire at half the expected day count
            # (each single refresh failure would increment two rows, so the
            # chronic threshold appeared reached after threshold/2 days).
            await db._conn.execute(
                "UPDATE combo_performance SET refresh_failures = refresh_failures + 1 "
                "WHERE combo_key = ? AND window = '30d'",
                (combo_key,),
            )
            await db._conn.commit()
        except Exception as counter_err:
            # The chronic-failure surfacing in weekly_digest depends on this
            # counter incrementing — if the counter write itself fails, log
            # loudly so the operator notices the counter is blind, not silent.
            log.exception(
                "combo_refresh_failure_counter_update_failed",
                combo_key=combo_key,
                err=str(counter_err),
                err_id="COMBO_REFRESH_COUNTER",
            )
        return False


async def refresh_all(db: Database, settings) -> dict:
    """Rebuild `combo_performance` for every combo that traded recently OR is
    currently suppressed.

    The recent-trade window comes from ``FEEDBACK_REFRESH_WINDOW_DAYS``. A
    currently-suppressed combo is refreshed even with NO trade in that window
    (fix/frozen-suppression-lock): a suppressed combo blocks its own trades, so
    it stops trading, so under a trade-only refresh set it would fall out of the
    set, never be refreshed again, and latch at ``parole_exhausted`` forever
    with no operator notification. Widening the selection keeps it live +
    alertable; ``refresh_combo`` preserves its suppressed state verbatim for
    zero-trade combos (no auto-revival — constraint a).

    Returns ``{"refreshed": N, "failed": M, "chronic_failures": [keys],
    "permanent_suppression": [keys]}`` where ``permanent_suppression`` lists the
    combos newly alerted this run as entering permanent-suppression state.
    """
    window_days = settings.FEEDBACK_REFRESH_WINDOW_DAYS
    # SQLite accepts the datetime() modifier as a bound parameter, so the
    # window stays Settings-driven with no hardcoded 30 in the query.
    window_modifier = f"-{window_days} days"
    cur = await db._conn.execute(
        "SELECT DISTINCT signal_combo AS combo FROM paper_trades "
        "WHERE signal_combo IS NOT NULL "
        "  AND opened_at >= datetime('now', ?) "
        "UNION "
        "SELECT combo_key AS combo FROM combo_performance "
        "WHERE window = '30d' AND suppressed = 1",
        (window_modifier,),
    )
    rows = await cur.fetchall()
    combos = [r[0] for r in rows if r[0]]

    refreshed = 0
    failed = 0
    for combo in combos:
        ok = await refresh_combo(db, combo, settings)
        if ok:
            refreshed += 1
        else:
            failed += 1

    cur = await db._conn.execute(
        "SELECT combo_key FROM combo_performance "
        "WHERE window = '30d' AND refresh_failures >= ?",
        (settings.FEEDBACK_CHRONIC_FAILURE_THRESHOLD,),
    )
    chronic = [r[0] for r in await cur.fetchall()]
    for key in chronic:
        log.warning(
            "combo_refresh_chronic_failure",
            combo_key=key,
        )

    permanent = await _process_permanent_suppression(db, settings, window_modifier)

    log.info(
        "combo_refresh_summary",
        refreshed=refreshed,
        failed=failed,
        chronic=len(chronic),
        permanent_suppression=len(permanent),
    )
    return {
        "refreshed": refreshed,
        "failed": failed,
        "chronic_failures": chronic,
        "permanent_suppression": permanent,
    }


async def _process_permanent_suppression(
    db: Database, settings, window_modifier: str
) -> list[str]:
    """Detect combos entering permanent-suppression state and fire the §12b
    operator alert once per entry.

    A combo is in *permanent-suppression* state when its 30d row is
    ``suppressed = 1`` AND it has NO trade opened within the refresh window —
    i.e. it survives in the refresh set only because of the widening in
    ``refresh_all``. That is a permanent, operator-invisible state change, so
    §12b requires an operator alert.

    Dedup via ``perm_suppression_alerted_at``: fire once, re-arm only when the
    combo leaves the state (becomes unsuppressed OR trades again inside the
    window). Alert-delivery failures never break refresh — the dedup marker is
    set only after a confirmed send, so a transient Telegram outage re-attempts
    on the next run rather than silently dropping the notification.

    Returns the combos newly alerted this run.
    """
    conn = db._conn
    now_iso = datetime.now(timezone.utc).isoformat()

    # Re-arm: clear the dedup marker for any combo that has LEFT the
    # permanent-suppression state since it was last alerted, so a future
    # re-entry alerts again.
    await conn.execute(
        "UPDATE combo_performance "
        "SET perm_suppression_alerted_at = NULL "
        "WHERE window = '30d' "
        "  AND perm_suppression_alerted_at IS NOT NULL "
        "  AND (suppressed = 0 OR EXISTS ("
        "        SELECT 1 FROM paper_trades pt "
        "        WHERE pt.signal_combo = combo_performance.combo_key "
        "          AND pt.opened_at >= datetime('now', ?)))",
        (window_modifier,),
    )

    # Pending = suppressed, no recent trade, not yet alerted for this entry.
    cur = await conn.execute(
        "SELECT combo_key FROM combo_performance cp "
        "WHERE cp.window = '30d' "
        "  AND cp.suppressed = 1 "
        "  AND cp.perm_suppression_alerted_at IS NULL "
        "  AND NOT EXISTS ("
        "        SELECT 1 FROM paper_trades pt "
        "        WHERE pt.signal_combo = cp.combo_key "
        "          AND pt.opened_at >= datetime('now', ?))",
        (window_modifier,),
    )
    pending = [r[0] for r in await cur.fetchall()]
    await conn.commit()

    window_days = settings.FEEDBACK_REFRESH_WINDOW_DAYS
    alerted: list[str] = []
    for combo in pending:
        message = (
            f"signal {combo} is in permanent-suppression state (no trades in "
            f">{window_days}d, still suppressed); revival requires explicit "
            f"operator action via revive_signal_with_baseline"
        )
        # §12b: dispatched/delivered/failed logs bracket the send so a
        # successful delivery is NOT silent. parse_mode=None is set inside the
        # sender — the body carries signal names + revive_signal_with_baseline
        # whose underscores MarkdownV1 would mangle without an error.
        log.info("permanent_suppression_alert_dispatched", combo_key=combo)
        try:
            await _send_permanent_suppression_alert(settings, message)
        except Exception as exc:
            # Alert failure must never break refresh. Leave the dedup marker
            # NULL so the next run re-attempts — the operator MUST eventually be
            # told; the failed log surfaces the outage in the meantime.
            log.exception(
                "permanent_suppression_alert_failed",
                combo_key=combo,
                err=str(exc),
                err_type=type(exc).__name__,
            )
            continue
        log.info("permanent_suppression_alert_delivered", combo_key=combo)

        # Set the dedup marker only after a confirmed send.
        try:
            await conn.execute(
                "UPDATE combo_performance SET perm_suppression_alerted_at = ? "
                "WHERE combo_key = ? AND window = '30d'",
                (now_iso, combo),
            )
            await conn.commit()
        except aiosqlite.Error as exc:
            log.exception(
                "permanent_suppression_marker_update_failed",
                combo_key=combo,
                err=str(exc),
            )
        alerted.append(combo)

    return alerted


async def _send_permanent_suppression_alert(settings, message: str) -> None:
    """Deliver the §12b permanent-suppression alert via a one-shot Telegram send.

    ``aiohttp`` + ``scout.alerter`` are imported lazily: a module-level
    ``import aiohttp`` aborts the interpreter on Windows dev boxes (OpenSSL
    Applink), and importing it is pointless on any run with no
    permanent-suppression combo. Tests monkeypatch this function so the real
    aiohttp import never runs.
    """
    import aiohttp  # deferred — module-level import aborts on Windows

    from scout import alerter  # deferred — pulls aiohttp at import time

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15)
    ) as session:
        await alerter.send_telegram_message(
            message,
            session,
            settings,
            parse_mode=None,
            source="combo_refresh_permanent_suppression",
        )
