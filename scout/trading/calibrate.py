"""Tier 1a calibration: recommend per-signal param updates from trade history.

Heuristics are intentionally conservative (point-estimate triggers, ±2pp
step size, hard floors/ceilings). The job is "absorb the manual tuning
loop"; outright optimisation is gated on Tier 3 ML.

Default mode is dry-run — prints a diff. ``--apply`` writes the
``signal_params`` rows AND ``signal_params_audit`` rows in a single
transaction, then sends a Telegram summary. If the Telegram token is a
known placeholder, ``--apply`` refuses unless ``--force-no-alert`` is set
(operator visibility is the load-bearing safety control here).
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
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

# aiohttp is heavy on Windows (loads OpenSSL Applink) and is only used inside
# the CLI _main_async — guard the import so test collection doesn't trip it.
if TYPE_CHECKING:
    import aiohttp

log = structlog.get_logger(__name__)


# Heuristic guard rails (per design doc §Heuristics).
_TRAIL_FLOOR_PCT = 5.0
_TRAIL_CEILING_PCT = 30.0
_SL_FLOOR_PCT = 5.0
_SL_CEILING_PCT = 30.0


@dataclass(frozen=True)
class SignalStats:
    """Rolling stats for one signal_type over the calibration window."""

    signal_type: str
    n_trades: int
    win_rate_pct: float
    expired_pct: float
    avg_loss_pct: float | None  # None when no losers
    avg_winner_peak_pct: float | None  # None when no winners


@dataclass
class FieldChange:
    field: str
    old: float
    new: float


@dataclass
class SignalDiff:
    signal_type: str
    stats: SignalStats
    changes: list[FieldChange]
    reason_parts: list[str]
    skipped_reason: str | None = None

    @property
    def reason(self) -> str:
        return "; ".join(self.reason_parts) if self.reason_parts else "no_change"


# ---------------------------------------------------------------------------
# Stats fetch
# ---------------------------------------------------------------------------


async def _stats_for_signal(
    conn,
    signal_type: str,
    since_iso: str,
) -> SignalStats:
    """Aggregate paper_trades GROUP BY signal_type for the given window.

    Reads ``paper_trades`` directly (not ``combo_performance``, which is
    keyed by ``signal_combo`` and would require an unintuitive sub-aggregate).
    """
    cur = await conn.execute(
        """SELECT
            COUNT(*)                                                 AS n,
            SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)             AS wins,
            SUM(CASE WHEN status = 'closed_expired' THEN 1 ELSE 0 END) AS expired,
            AVG(CASE WHEN pnl_usd <  0 THEN pnl_pct  END)            AS avg_loss_pct,
            AVG(CASE WHEN pnl_usd >  0 THEN peak_pct END)            AS avg_winner_peak
           FROM paper_trades
           WHERE signal_type = ?
             AND status LIKE 'closed_%'
             AND datetime(closed_at) >= datetime(?)""",
        (signal_type, since_iso),
    )
    row = await cur.fetchone()
    n = row[0] or 0
    wins = row[1] or 0
    expired = row[2] or 0
    avg_loss = row[3]
    avg_peak = row[4]
    return SignalStats(
        signal_type=signal_type,
        n_trades=n,
        win_rate_pct=round(100.0 * wins / n, 1) if n > 0 else 0.0,
        expired_pct=round(100.0 * expired / n, 1) if n > 0 else 0.0,
        avg_loss_pct=float(avg_loss) if avg_loss is not None else None,
        avg_winner_peak_pct=float(avg_peak) if avg_peak is not None else None,
    )


async def _current_params(conn, signal_type: str) -> dict | None:
    cur = await conn.execute(
        """SELECT trail_pct, sl_pct, last_calibration_at
           FROM signal_params WHERE signal_type = ?""",
        (signal_type,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return {
        "trail_pct": float(row[0]),
        "sl_pct": float(row[1]),
        "last_calibration_at": row[2],
    }


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------


def _propose_changes(
    stats: SignalStats,
    current: dict,
    step: float,
) -> tuple[list[FieldChange], list[str]]:
    """Apply the v1 heuristic table. Returns (changes, reason_parts).

    Both rules evaluate independently and can both fire in one run — they
    mutate different fields (sl_pct vs trail_pct) so there is no conflict.
    The reason-list ordering puts SL first for audit-log readability only.

    Each value is rounded to 1 decimal place to prevent float-precision flap
    on idempotent re-runs.

    v1 heuristics only touch ``sl_pct`` and ``trail_pct``. All other fields
    (leg_*, low_peak_*, max_duration_hours, qty fractions) are operator-only —
    schema carries them; calibrator does not.
    """
    changes: list[FieldChange] = []
    reasons: list[str] = []

    # Rule: win_rate < 40% AND avg_loss < -20% → widen SL
    if (
        stats.win_rate_pct < 40.0
        and stats.avg_loss_pct is not None
        and stats.avg_loss_pct < -20.0
    ):
        new_sl = round(min(current["sl_pct"] + step, _SL_CEILING_PCT), 1)
        if new_sl != round(current["sl_pct"], 1):
            changes.append(FieldChange("sl_pct", current["sl_pct"], new_sl))
            reasons.append(
                f"win_rate {stats.win_rate_pct}% < 40 AND avg_loss "
                f"{stats.avg_loss_pct:.1f}% < -20 → widen sl"
            )

    # Rule: expired > 30% → tighten trail
    if stats.expired_pct > 30.0:
        new_trail = round(max(current["trail_pct"] - step, _TRAIL_FLOOR_PCT), 1)
        if new_trail != round(current["trail_pct"], 1):
            changes.append(FieldChange("trail_pct", current["trail_pct"], new_trail))
            reasons.append(f"expired {stats.expired_pct}% > 30 → tighten trail")

    return changes, reasons


# ---------------------------------------------------------------------------
# Diff builder
# ---------------------------------------------------------------------------


async def build_diffs(
    db: Database,
    settings: Settings,
    *,
    window_days: int,
    min_trades: int,
    step: float,
    signal_filter: str | None = None,
    since_deploy: bool = False,
) -> list[SignalDiff]:
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")

    diffs: list[SignalDiff] = []
    targets = (
        [signal_filter]
        if signal_filter
        else sorted(DEFAULT_SIGNAL_TYPES - CALIBRATION_EXCLUDE_SIGNALS)
    )

    fixed_window_iso = (
        datetime.now(timezone.utc) - timedelta(days=window_days)
    ).isoformat()

    for signal_type in targets:
        cur = await _current_params(conn, signal_type)
        if cur is None:
            diffs.append(
                SignalDiff(
                    signal_type=signal_type,
                    stats=SignalStats(signal_type, 0, 0, 0, None, None),
                    changes=[],
                    reason_parts=[],
                    skipped_reason="no_signal_params_row",
                )
            )
            continue

        # --since-deploy uses last_calibration_at (or seed) as the cutoff so
        # the first calibration after a strategy change isn't tuned on
        # contaminated history. Falls back to the fixed window when there's
        # no calibration record yet.
        if since_deploy and cur["last_calibration_at"]:
            since_iso = cur["last_calibration_at"]
        else:
            since_iso = fixed_window_iso

        stats = await _stats_for_signal(conn, signal_type, since_iso)
        if stats.n_trades < min_trades:
            diffs.append(
                SignalDiff(
                    signal_type=signal_type,
                    stats=stats,
                    changes=[],
                    reason_parts=[],
                    skipped_reason=f"n_trades {stats.n_trades} < min {min_trades}",
                )
            )
            continue

        changes, reasons = _propose_changes(stats, cur, step)
        diffs.append(
            SignalDiff(
                signal_type=signal_type,
                stats=stats,
                changes=changes,
                reason_parts=reasons,
            )
        )

    return diffs


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


async def apply_diffs(
    db: Database,
    diffs: list[SignalDiff],
    settings: Settings,
    *,
    session,  # aiohttp.ClientSession | None — typed loosely to avoid import
    force_no_alert: bool,
) -> int:
    """Write changes inside a single transaction. Returns number of rows touched.

    Telegram is dispatched inside the transaction so the txn-boundary is
    *one* unit. **Important caveat:** ``alerter.send_telegram_message``
    swallows aiohttp errors and only logs a warning — it does NOT raise.
    So a Telegram delivery failure (network, 401, wrong chat_id) will NOT
    roll back the params write. The ``_telegram_token_looks_real`` gate at
    CLI entry is what protects against the *known* placeholder failure
    mode; runtime-only delivery failures fall through silently. If
    detection of those becomes important, swap ``send_telegram_message``
    for a strict variant that raises on non-200.
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")

    actionable = [d for d in diffs if d.changes]
    if not actionable:
        log.info("calibrate_apply_no_changes")
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        await conn.execute("BEGIN EXCLUSIVE")
        for diff in actionable:
            set_clauses = ", ".join(f"{c.field} = ?" for c in diff.changes)
            values = [c.new for c in diff.changes]
            values.extend(
                [
                    now_iso,
                    "calibration",
                    now_iso,
                    diff.reason,
                    diff.signal_type,
                ]
            )
            await conn.execute(
                f"""UPDATE signal_params
                    SET {set_clauses},
                        updated_at = ?,
                        updated_by = ?,
                        last_calibration_at = ?,
                        last_calibration_reason = ?
                    WHERE signal_type = ?""",
                values,
            )
            for change in diff.changes:
                await conn.execute(
                    """INSERT INTO signal_params_audit
                       (signal_type, field_name, old_value, new_value,
                        reason, applied_by, applied_at)
                       VALUES (?, ?, ?, ?, ?, 'calibration', ?)""",
                    (
                        diff.signal_type,
                        change.field,
                        f"{change.old:.1f}",
                        f"{change.new:.1f}",
                        diff.reason,
                        now_iso,
                    ),
                )

        # Telegram inside the txn — failure aborts the writes.
        if session is not None and not force_no_alert:
            from scout import alerter  # local import — avoids aiohttp at collection

            summary = "\n".join(
                f"  {d.signal_type}: "
                + ", ".join(f"{c.field} {c.old:.1f}→{c.new:.1f}" for c in d.changes)
                + f" [{d.reason}]"
                for d in actionable
            )
            await alerter.send_telegram_message(
                f"calibration applied:\n{summary}",
                session,
                settings,
            )

        await conn.commit()
    except Exception:
        try:
            await conn.execute("ROLLBACK")
        except Exception as rb_err:
            log.exception("calibrate_apply_rollback_failed", err=str(rb_err))
        log.error("CALIBRATE_APPLY_FAILED")
        raise

    # AFTER commit only — bumping before commit would expose other readers
    # to uncommitted state if a rollback fired. The bump invalidates the
    # in-process cache so the next get_params() re-reads the table.
    bump_cache_version()
    return sum(len(d.changes) for d in actionable)


# ---------------------------------------------------------------------------
# Telegram health check (guards --apply)
# ---------------------------------------------------------------------------


_TELEGRAM_PLACEHOLDER_TOKENS = frozenset(
    {
        "placeholder",
        "todo",
        "changeme",
        "replace_me",
        "your_token_here",
        "xxx",
        "test",
        "none",
        "null",
        "0",
    }
)


def _telegram_token_looks_real(settings: Settings) -> bool:
    """Returns True for any well-formed-looking bot token.

    Does NOT prove deliverability — a syntactically valid but stale or
    revoked token still passes here and will silently fail at runtime
    (alerter swallows aiohttp errors). This guards against the known
    placeholder failure mode (project memory: BL-064 — token has been
    literally "placeholder" in prod for weeks) and against obvious
    malformed strings, not against deliverability.

    Real Telegram bot tokens have shape ``<bot_id>:<hash>`` where
    ``bot_id`` is digits and the suffix is base64-ish — we require the
    colon and a numeric prefix.
    """
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
    if token is None:
        return False
    raw = token.get_secret_value() if hasattr(token, "get_secret_value") else str(token)
    if not raw or len(raw) < 20:
        return False
    if raw.lower() in _TELEGRAM_PLACEHOLDER_TOKENS:
        return False
    # Real bot token shape: digits + ":" + suffix. Reject hex/random
    # strings that happen to be ≥20 chars but lack the format.
    if ":" not in raw:
        return False
    bot_id, _, _ = raw.partition(":")
    return bot_id.isdigit() and len(bot_id) >= 6


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _format_diff(diff: SignalDiff) -> str:
    if diff.skipped_reason:
        return f"  {diff.signal_type:24s} SKIPPED ({diff.skipped_reason})"
    s = diff.stats
    head = (
        f"  {diff.signal_type:24s} "
        f"n={s.n_trades:3d} "
        f"win={s.win_rate_pct:5.1f}% "
        f"expired={s.expired_pct:5.1f}% "
    )
    if not diff.changes:
        return head + "→ no change"
    body = ", ".join(f"{c.field} {c.old:.1f}→{c.new:.1f}" for c in diff.changes)
    return head + f"→ {body} [{diff.reason}]"


def format_dryrun_telegram_message(
    diffs: list[SignalDiff],
    actionable: int,
    *,
    window_days: int,
) -> str:
    """Build the Telegram message body for the weekly calibration dry-run.

    Truncates if total length exceeds Telegram's 4096-char ceiling.
    Returns plain text (NOT Markdown) — `_format_diff` includes
    `[reason]` brackets that Telegram's Markdown parser would mis-handle
    as link anchors. Caller MUST send with `parse_mode=None` (or no
    parse_mode kwarg).

    Used by scout/main.py:_run_feedback_schedulers calibration_dryrun
    hook (PR-review arch-Issue1 — public function so cross-module
    private-name import is avoided).
    """
    header = (
        f"📊 Weekly calibration dry-run (window={window_days}d, since-deploy)\n"
        f"{actionable} of {len(diffs)} signal(s) would change.\n"
    )
    body = "\n".join(_format_diff(d) for d in diffs)
    footer = (
        "\n\nTo apply: ssh root@<vps> 'cd /root/gecko-alpha && "
        "uv run python -m scout.trading.calibrate --apply'"
    )
    full = header + "\n" + body + footer
    if len(full) > 4090:  # leave headroom under 4096 Telegram cap
        full = full[:4087] + "..."
    return full


def telegram_token_looks_real(settings: Settings) -> bool:
    """Public alias for the private guard. Used by scout/main.py
    calibration_dryrun hook to gate Telegram emission (avoids attempting
    a 401/404 on placeholder tokens)."""
    return _telegram_token_looks_real(settings)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def _main_async(args: argparse.Namespace) -> int:
    settings = Settings()

    if not settings.SIGNAL_PARAMS_ENABLED and not args.force:
        print(
            "[CALIBRATE] SIGNAL_PARAMS_ENABLED=False — table values are NOT in "
            "effect (evaluator reads Settings). Calibration would be cosmetic. "
            "Re-run with --force to ignore."
        )
        return 2

    if (
        args.apply
        and not _telegram_token_looks_real(settings)
        and not args.force_no_alert
    ):
        print(
            "[CALIBRATE] TELEGRAM_BOT_TOKEN is missing/placeholder. "
            "Operator visibility is the load-bearing safety control here. "
            "Fix the token or pass --force-no-alert to acknowledge silent apply."
        )
        return 3

    db = Database(settings.DB_PATH)
    await db.initialize()

    diffs = await build_diffs(
        db,
        settings,
        window_days=args.window,
        min_trades=settings.CALIBRATION_MIN_TRADES,
        step=settings.CALIBRATION_STEP_SIZE_PCT,
        signal_filter=args.signal,
        since_deploy=args.since_deploy,
    )

    print(
        f"[CALIBRATE] window={args.window}d "
        f"{'(since-deploy)' if args.since_deploy else ''} "
        f"min_trades={settings.CALIBRATION_MIN_TRADES} "
        f"excluded={sorted(CALIBRATION_EXCLUDE_SIGNALS)}"
    )
    for d in diffs:
        print(_format_diff(d))

    actionable = sum(1 for d in diffs if d.changes)
    print(f"[CALIBRATE] {actionable} signal(s) would change.")

    if not args.apply:
        print("[CALIBRATE] dry-run. Re-run with --apply to persist.")
        await db.close()
        return 0

    import aiohttp  # local — avoids OpenSSL Applink at module-import time

    async with aiohttp.ClientSession() as session:
        n_writes = await apply_diffs(
            db,
            diffs,
            settings,
            session=session if not args.force_no_alert else None,
            force_no_alert=args.force_no_alert,
        )
    print(f"[CALIBRATE] applied {n_writes} field change(s).")
    await db.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Tier 1a calibration")
    p.add_argument("--apply", action="store_true", help="persist changes")
    p.add_argument("--signal", help="only this signal_type")
    p.add_argument(
        "--window",
        type=int,
        default=30,
        help="rolling window in days (default 30)",
    )
    p.add_argument(
        "--since-deploy",
        action="store_true",
        help="window starts at last_calibration_at (or seed) — avoids "
        "tuning on pre-strategy-change history",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="run even when SIGNAL_PARAMS_ENABLED=False",
    )
    p.add_argument(
        "--force-no-alert",
        action="store_true",
        help="permit --apply when Telegram is misconfigured",
    )
    args = p.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
