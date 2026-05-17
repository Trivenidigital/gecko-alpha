"""Revival-criteria evaluator (BL-NEW-LOSERS-CONTRARIAN-REVIVAL-CRITERIA-TIGHTENING).

Pure read-only evaluator. Reads paper_trades + signal_params_audit + signal_params.
On PASS verdict, CLI emits SQL the operator pastes to write a
``keep_on_provisional_until_<iso>`` (default 30d expiry) audit row.

Note on ``Database._conn`` private access (per design-review fold C#15): this
module reaches into ``db._conn`` directly via ``fetch_closed_trades``,
``find_latest_regime_cutover``, ``signal_type_exists``,
``find_existing_keep_verdict``, ``compute_recent_trade_rate``. This mirrors
the existing project convention (see ``scout/trading/calibrate.py``). If
``Database._conn`` is renamed in a future refactor, this module breaks loudly
with ``AttributeError`` on first DB-call — deliberate, cheap to detect.

NULL ``peak_pct`` is treated as no-breakout (conservative — penalizes the
signal). This matches reality: ``peak_pct`` is set on every tick once the
position is open, so NULL implies the position never received a tick OR
pre-existed before the ``peak_pct`` column landed.

See plan: ``tasks/plan_lc_revival_criteria_tightening.md`` (v3)
See design: ``tasks/design_lc_revival_criteria_tightening.md``
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import re as _re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

import structlog


# --------------------------------------------------------------------------
# Dataclasses + verdict enum (plan Task 1)
# --------------------------------------------------------------------------


class RevivalVerdict(Enum):
    PASS = "pass"
    FAIL = "fail"
    BELOW_MIN_TRADES = "below_min_trades"
    STRATIFICATION_INFEASIBLE = "stratification_infeasible"


@dataclass(frozen=True)
class ClosedTrade:
    id: int
    signal_type: str
    pnl_usd: float
    pnl_pct: float
    peak_pct: float | None
    exit_reason: str | None
    closed_at: datetime


@dataclass(frozen=True)
class WindowDiagnostics:
    start_at: datetime
    end_at: datetime
    n: int
    net_pnl_usd: float
    per_trade_usd: float
    win_pct: float
    win_pct_wilson_lb: float           # percent (0-100)
    per_trade_bootstrap_lb: float      # dollars
    no_breakout_and_loss_rate: float
    stop_loss_frequency: float
    expired_loss_frequency: float
    exit_machinery_contribution: float


@dataclass(frozen=True)
class RevivalCriteriaResult:
    signal_type: str
    verdict: RevivalVerdict
    n_trades: int
    cutover_at: datetime | None
    cutover_source: str
    cutover_age_days: int | None
    window_a: WindowDiagnostics | None
    window_b: WindowDiagnostics | None
    failure_reasons: list[str] = field(default_factory=list)
    evaluated_at: datetime | None = None


# --------------------------------------------------------------------------
# Pure-function diagnostics (plan Tasks 2-6)
# --------------------------------------------------------------------------


def compute_no_breakout_and_loss_rate(
    trades: list[ClosedTrade], *, threshold_pct: float
) -> float:
    """Fraction of trades where position failed to break out AND lost money.

    Predicate: ``(peak_pct <= threshold_pct OR peak_pct IS NULL) AND pnl_usd < 0``.

    Per Reviewer A finding #5: the v1 ``peak_pct <= threshold_pct`` predicate
    alone counted legitimate tight-trail winners as failures. The correct
    failure mode is "couldn't break out AND lost money," not "didn't break out."
    NULL peak treated conservatively as no-breakout (penalizes the signal).
    """
    if not trades:
        return 0.0
    failures = sum(
        1
        for t in trades
        if (t.peak_pct is None or t.peak_pct <= threshold_pct) and t.pnl_usd < 0
    )
    return failures / len(trades)


def compute_stop_loss_frequency(trades: list[ClosedTrade]) -> float:
    """Fraction of trades exited via ``exit_reason='stop_loss'``."""
    if not trades:
        return 0.0
    sl_count = sum(1 for t in trades if t.exit_reason == "stop_loss")
    return sl_count / len(trades)


_EXPIRED_REASONS = frozenset({"expired", "expired_stale_price"})


def compute_expired_loss_frequency(trades: list[ClosedTrade]) -> float:
    """Fraction of trades with expired-class exit AND negative pnl."""
    if not trades:
        return 0.0
    losses = sum(
        1
        for t in trades
        if t.exit_reason in _EXPIRED_REASONS and t.pnl_usd < 0
    )
    return losses / len(trades)


_EXIT_MACHINERY_REASONS = frozenset(
    {"peak_fade", "trailing_stop", "moonshot_trail"}
)


def compute_exit_machinery_contribution(trades: list[ClosedTrade]) -> float:
    """Ratio of positive (peak_fade + trailing_stop + moonshot_trail) pnl over all positive pnl.

    Sanity check that exit machinery is doing the work, not entry luck. Per
    Reviewer A finding #6: v1 ``peak_fade``-only numerator punished signals
    whose winners exit via ``trailing_stop`` (cycle-9 first_signal had 35
    trailing-stop wins vs 2 peak_fade wins — that pattern would have failed
    the v1 gate).
    """
    if not trades:
        return 0.0
    positive_total = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    if positive_total <= 0:
        return 0.0
    machinery_positive = sum(
        t.pnl_usd
        for t in trades
        if t.exit_reason in _EXIT_MACHINERY_REASONS and t.pnl_usd > 0
    )
    return machinery_positive / positive_total


def compute_wilson_lb(*, wins: int, n: int, z: float = 1.96) -> float:
    """Wilson score interval lower bound on win-rate (default z=1.96 → 95%).

    Formula:  (p + z²/(2n) − z·√(p(1-p)/n + z²/(4n²))) / (1 + z²/n)

    Returns 0.0 on n<=0 (sentinel — undefined CI).
    CLAUDE.md §11b mandate: project standard for win-rate gates.
    """
    if n <= 0:
        return 0.0
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p + z2 / (2.0 * n)
    margin = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    lb = (center - margin) / denom
    return max(0.0, lb)


def compute_bootstrap_lb_per_trade(
    pnls: list[float],
    *,
    n_resamples: int = 10_000,
    seed: int = 42,
    alpha: float = 0.05,
) -> float:
    """Bootstrap CI lower bound on per-trade mean PnL.

    Distribution-free CI lower bound via percentile bootstrap. Default 95% CI
    (alpha=0.05). Stdlib-only (random.choices) to avoid the Windows OpenSSL
    workaround per memory ``reference_windows_openssl_workaround.md``.

    CLAUDE.md §11b mandate: project standard for per-trade EV gates.
    """
    if not pnls:
        return 0.0
    n = len(pnls)
    rng = random.Random(seed)
    means = []
    for _ in range(n_resamples):
        sample = rng.choices(pnls, k=n)
        means.append(sum(sample) / n)
    means.sort()
    lb_idx = int(alpha / 2.0 * n_resamples)
    return means[lb_idx]


# --------------------------------------------------------------------------
# Cutover split (plan Task 7)
# --------------------------------------------------------------------------

# Per design-review fold C#1: DENYLIST (not allowlist) — accept all
# signal_params_audit field_names EXCEPT consequence rows that aren't
# regime triggers. ``calibrate.py`` writes dynamic field_names like
# ``leg_1_pct``/``trail_pct``/etc.; an allowlist would miss them silently.
_REGIME_NON_BOUNDARY_FIELDS = frozenset({"soak_verdict", "last_calibration_at"})


def _is_operator_revival_row(
    applied_by: str, field_name: str, old_value: str | None, new_value: str | None
) -> bool:
    """Per design-review fold C#6: operator-revival shape — the OUTCOME of a
    regime decision (auto_suspend, calibrate), NOT the regime decision itself.
    The triggering event is the actual cutover; the operator flip-back is
    follow-up state-restoration.
    """
    return (
        applied_by == "operator"
        and field_name == "enabled"
        and (old_value or "") == "0"
        and (new_value or "") == "1"
    )


def split_at_cutover_boundary(
    trades: list[ClosedTrade],
    *,
    cutover_at: datetime,
    min_window_days: int,
    min_window_trades: int,
) -> tuple[list[ClosedTrade], list[ClosedTrade]] | None:
    """Split sorted trades at ``cutover_at`` into (before, after) windows.

    Returns ``(window_a, window_b)`` where both windows individually span
    ``>= min_window_days`` AND contain ``>= min_window_trades``. Returns
    ``None`` when either constraint fails.

    Per Reviewer A finding #2: cutover-anchored split (regime stratification)
    replaces v1 plan's median-split (which did NOT stratify regimes).
    """
    sorted_trades = sorted(trades, key=lambda t: t.closed_at)
    a = [t for t in sorted_trades if t.closed_at < cutover_at]
    b = [t for t in sorted_trades if t.closed_at >= cutover_at]
    if len(a) < min_window_trades or len(b) < min_window_trades:
        return None
    if (a[-1].closed_at - a[0].closed_at) < timedelta(days=min_window_days):
        return None
    if (b[-1].closed_at - b[0].closed_at) < timedelta(days=min_window_days):
        return None
    return a, b


# --------------------------------------------------------------------------
# Async DB layer (plan Task 9)
# --------------------------------------------------------------------------


async def fetch_closed_trades(
    db, signal_type: str, *, since: datetime | None = None
) -> list[ClosedTrade]:
    """Fetch closed paper trades for one signal_type with NOT-NULL pnl values.

    Per Reviewer B finding #12: filters ``pnl_pct IS NOT NULL`` in addition to
    ``pnl_usd IS NOT NULL`` to exclude mid-fill / ladder-partial rows.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    sql = (
        "SELECT id, signal_type, pnl_usd, pnl_pct, peak_pct, exit_reason, closed_at "
        "FROM paper_trades "
        "WHERE signal_type = ? AND status LIKE 'closed_%' "
        "AND pnl_usd IS NOT NULL AND pnl_pct IS NOT NULL "
    )
    params: list = [signal_type]
    if since is not None:
        sql += "AND datetime(closed_at) >= datetime(?) "
        params.append(since.isoformat())
    sql += "ORDER BY closed_at"
    cur = await db._conn.execute(sql, params)
    rows = await cur.fetchall()
    return [
        ClosedTrade(
            id=r[0],
            signal_type=r[1],
            pnl_usd=float(r[2]),
            pnl_pct=float(r[3]),
            peak_pct=float(r[4]) if r[4] is not None else None,
            exit_reason=r[5],
            closed_at=datetime.fromisoformat(r[6].replace("Z", "+00:00")),
        )
        for r in rows
    ]


async def find_latest_regime_cutover(
    db, signal_type: str
) -> tuple[datetime | None, str]:
    """Return ``(cutover_at, source)`` of latest regime-changing audit row.

    Iterates audit rows newest→oldest, skipping (a) rows whose ``field_name``
    is in ``_REGIME_NON_BOUNDARY_FIELDS`` and (b) operator-revival shape rows.
    Returns first match or ``(None, 'no_audit_events')``.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    sql = (
        "SELECT applied_at, applied_by, field_name, old_value, new_value "
        "FROM signal_params_audit "
        "WHERE signal_type = ? "
        "ORDER BY applied_at DESC"
    )
    cur = await db._conn.execute(sql, (signal_type,))
    rows = await cur.fetchall()
    for iso, by, fld, old_val, new_val in rows:
        if fld in _REGIME_NON_BOUNDARY_FIELDS:
            continue
        if _is_operator_revival_row(by, fld, old_val, new_val):
            continue
        cutover = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return cutover, f"signal_params_audit:{by}:{fld}"
    return None, "no_audit_events"


async def signal_type_exists(db, signal_type: str) -> bool:
    """Per design-review fold C#10: guard against signal_type typos."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    cur = await db._conn.execute(
        "SELECT 1 FROM signal_params WHERE signal_type = ? LIMIT 1",
        (signal_type,),
    )
    row = await cur.fetchone()
    return row is not None


async def find_existing_keep_verdict(
    db, signal_type: str
) -> tuple[str, str] | None:
    """Per design-review fold D#7: return most-recent soak_verdict audit row.

    Returns ``(applied_at_iso, new_value)`` or ``None``.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    cur = await db._conn.execute(
        "SELECT applied_at, new_value FROM signal_params_audit "
        "WHERE signal_type = ? AND field_name = 'soak_verdict' "
        "ORDER BY applied_at DESC LIMIT 1",
        (signal_type,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return row[0], row[1]


async def compute_recent_trade_rate(
    db, signal_type: str, *, lookback_days: int = 7
) -> float:
    """Per design-review fold D#6: trades-per-day rate for BELOW_MIN_TRADES projection."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades "
        "WHERE signal_type = ? AND status LIKE 'closed_%' "
        "AND datetime(closed_at) >= datetime('now', ?)",
        (signal_type, f"-{lookback_days} days"),
    )
    row = await cur.fetchone()
    n = int(row[0]) if row else 0
    return n / lookback_days if lookback_days > 0 else 0.0


# --------------------------------------------------------------------------
# Orchestrator (plan Task 10)
# --------------------------------------------------------------------------


def _window_diagnostics(
    trades: list[ClosedTrade], *, settings
) -> WindowDiagnostics:
    n = len(trades)
    net = sum(t.pnl_usd for t in trades)
    wins = sum(1 for t in trades if t.pnl_usd > 0)
    pnls = [t.pnl_usd for t in trades]
    return WindowDiagnostics(
        start_at=trades[0].closed_at,
        end_at=trades[-1].closed_at,
        n=n,
        net_pnl_usd=net,
        per_trade_usd=net / n if n else 0.0,
        win_pct=100.0 * wins / n if n else 0.0,
        win_pct_wilson_lb=100.0 * compute_wilson_lb(wins=wins, n=n),
        per_trade_bootstrap_lb=compute_bootstrap_lb_per_trade(
            pnls, n_resamples=settings.REVIVAL_CRITERIA_BOOTSTRAP_RESAMPLES
        ),
        no_breakout_and_loss_rate=compute_no_breakout_and_loss_rate(
            trades, threshold_pct=settings.REVIVAL_CRITERIA_NO_BREAKOUT_PEAK_PCT
        ),
        stop_loss_frequency=compute_stop_loss_frequency(trades),
        expired_loss_frequency=compute_expired_loss_frequency(trades),
        exit_machinery_contribution=compute_exit_machinery_contribution(trades),
    )


def _evaluate_window_gates(label: str, w: WindowDiagnostics, settings) -> list[str]:
    failures: list[str] = []
    if w.per_trade_bootstrap_lb <= 0:
        failures.append(
            f"window_{label}.per_trade_bootstrap_lb=${w.per_trade_bootstrap_lb:.2f} <= 0"
        )
    if w.win_pct_wilson_lb < settings.REVIVAL_CRITERIA_WIN_WILSON_LB_MIN * 100:
        failures.append(
            f"window_{label}.win_pct_wilson_lb={w.win_pct_wilson_lb:.1f}% < "
            f"{settings.REVIVAL_CRITERIA_WIN_WILSON_LB_MIN * 100:.1f}%"
        )
    if w.no_breakout_and_loss_rate > settings.REVIVAL_CRITERIA_MAX_NO_BREAKOUT_AND_LOSS:
        failures.append(
            f"window_{label}.no_breakout_and_loss_rate={w.no_breakout_and_loss_rate:.2f} > "
            f"{settings.REVIVAL_CRITERIA_MAX_NO_BREAKOUT_AND_LOSS}"
        )
    if w.exit_machinery_contribution < settings.REVIVAL_CRITERIA_EXIT_MACHINERY_MIN:
        failures.append(
            f"window_{label}.exit_machinery_contribution={w.exit_machinery_contribution:.2f} < "
            f"{settings.REVIVAL_CRITERIA_EXIT_MACHINERY_MIN}"
        )
    return failures


async def evaluate_revival_criteria(
    db, signal_type: str, settings, *, cutover_override: datetime | None = None
) -> RevivalCriteriaResult:
    """Evaluate revival criteria for one signal_type. Pure read-only."""
    trades = await fetch_closed_trades(db, signal_type)
    now = datetime.now(timezone.utc)
    n = len(trades)

    if n < settings.REVIVAL_CRITERIA_MIN_TRADES:
        return RevivalCriteriaResult(
            signal_type=signal_type,
            verdict=RevivalVerdict.BELOW_MIN_TRADES,
            n_trades=n,
            cutover_at=None,
            cutover_source="not_evaluated",
            cutover_age_days=None,
            window_a=None,
            window_b=None,
            failure_reasons=[
                f"n_trades={n} < REVIVAL_CRITERIA_MIN_TRADES={settings.REVIVAL_CRITERIA_MIN_TRADES}"
            ],
            evaluated_at=now,
        )

    if cutover_override is not None:
        cutover_at, cutover_source = cutover_override, "operator_override"
    else:
        cutover_at, cutover_source = await find_latest_regime_cutover(db, signal_type)

    if cutover_at is None:
        return RevivalCriteriaResult(
            signal_type=signal_type,
            verdict=RevivalVerdict.STRATIFICATION_INFEASIBLE,
            n_trades=n,
            cutover_at=None,
            cutover_source=cutover_source,
            cutover_age_days=None,
            window_a=None,
            window_b=None,
            failure_reasons=[
                f"no regime cutover found in signal_params_audit for {signal_type}; "
                "pass --cutover-iso to override"
            ],
            evaluated_at=now,
        )

    cutover_age_days = (now - cutover_at).days

    split = split_at_cutover_boundary(
        trades,
        cutover_at=cutover_at,
        min_window_days=settings.REVIVAL_CRITERIA_MIN_WINDOW_DAYS,
        min_window_trades=settings.REVIVAL_CRITERIA_MIN_WINDOW_TRADES,
    )
    if split is None:
        return RevivalCriteriaResult(
            signal_type=signal_type,
            verdict=RevivalVerdict.STRATIFICATION_INFEASIBLE,
            n_trades=n,
            cutover_at=cutover_at,
            cutover_source=cutover_source,
            cutover_age_days=cutover_age_days,
            window_a=None,
            window_b=None,
            failure_reasons=[
                f"cutover at {cutover_at.isoformat()} cannot split into two "
                f">= {settings.REVIVAL_CRITERIA_MIN_WINDOW_DAYS}d / "
                f">= {settings.REVIVAL_CRITERIA_MIN_WINDOW_TRADES}-trade windows"
            ],
            evaluated_at=now,
        )

    a_trades, b_trades = split
    a = _window_diagnostics(a_trades, settings=settings)
    b = _window_diagnostics(b_trades, settings=settings)
    failures = _evaluate_window_gates("a", a, settings) + _evaluate_window_gates(
        "b", b, settings
    )
    verdict = RevivalVerdict.PASS if not failures else RevivalVerdict.FAIL
    return RevivalCriteriaResult(
        signal_type=signal_type,
        verdict=verdict,
        n_trades=n,
        cutover_at=cutover_at,
        cutover_source=cutover_source,
        cutover_age_days=cutover_age_days,
        window_a=a,
        window_b=b,
        failure_reasons=failures,
        evaluated_at=now,
    )


# --------------------------------------------------------------------------
# CLI helpers (plan Task 11)
# --------------------------------------------------------------------------

log = structlog.get_logger(__name__)

_SIGNAL_TYPE_RE = _re.compile(r"^[a-z_][a-z0-9_]*$")


def _validate_signal_type(s: str) -> None:
    """Per design-review fold B#1: prevent SQL injection via input validation."""
    if not _SIGNAL_TYPE_RE.match(s):
        raise ValueError(
            f"signal_type must match {_SIGNAL_TYPE_RE.pattern}; got={s!r}"
        )


def _sql_escape(s: str) -> str:
    """Double single-quotes for SQL literal interpolation."""
    return s.replace("'", "''")


def _parse_cutover_iso(s: str) -> datetime:
    """Per design-review fold C#14: argparse type validator with friendly error."""
    if not s:
        raise argparse.ArgumentTypeError("--cutover-iso cannot be empty")
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"--cutover-iso must be a valid ISO 8601 timestamp; got={s!r} ({e})"
        )


async def _query_cool_off_status(
    db, signal_type: str, settings
) -> datetime | None:
    """Return timestamp until which cool-off is active, or None if cleared."""
    if db._conn is None:
        return None
    cur = await db._conn.execute(
        "SELECT applied_at FROM signal_params_audit "
        "WHERE signal_type = ? AND field_name = 'enabled' "
        "AND old_value = '0' AND new_value = '1' AND applied_by = 'operator' "
        "ORDER BY applied_at DESC LIMIT 1",
        (signal_type,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    last_revival = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
    expires = last_revival + timedelta(days=settings.SIGNAL_REVIVAL_MIN_SOAK_DAYS)
    if expires > datetime.now(timezone.utc):
        return expires
    return None


def _print_verdict(
    result: RevivalCriteriaResult,
    *,
    existing_keep: tuple[str, str] | None,
    cool_off_active_until: datetime | None,
    recent_trade_rate: float | None,
    settings,
) -> None:
    print(f"\n=== Revival criteria evaluation: {result.signal_type} ===")
    print(f"Evaluated at: {result.evaluated_at}")
    print(f"Total closed trades: {result.n_trades}")
    if result.cutover_at is not None:
        age = result.cutover_age_days
        warn = " WARN: stale cutover" if (age is not None and age > 30) else ""
        print(
            f"Cutover: {result.cutover_at.isoformat()} "
            f"({age}d ago) [source: {result.cutover_source}]{warn}"
        )
    else:
        print(f"Cutover: NOT FOUND [source: {result.cutover_source}]")
    if cool_off_active_until is not None:
        print(f">>> Cool-off status: ACTIVE until {cool_off_active_until.isoformat()}")
    else:
        print(">>> Cool-off status: CLEAR")
    print(f"Verdict: {result.verdict.value.upper()}")

    if result.failure_reasons:
        print("\nFailure reasons:")
        for r in result.failure_reasons:
            print(f"  - {r}")

    if (
        result.verdict is RevivalVerdict.BELOW_MIN_TRADES
        and recent_trade_rate is not None
        and recent_trade_rate > 0
    ):
        needed = settings.REVIVAL_CRITERIA_MIN_TRADES - result.n_trades
        days = needed / recent_trade_rate
        print(
            f"\n>>> Estimated re-evaluable in ~{days:.1f} days "
            f"(need {needed} more trades; recent rate = {recent_trade_rate:.2f}/day)"
        )
        print(
            f">>> Note: PASS additionally requires >= "
            f"{settings.REVIVAL_CRITERIA_MIN_WINDOW_DAYS}d AND >= "
            f"{settings.REVIVAL_CRITERIA_MIN_WINDOW_TRADES} trades on BOTH sides of cutover."
        )

    if result.verdict is RevivalVerdict.FAIL and existing_keep is not None:
        keep_iso, keep_value = existing_keep
        print(
            f"\n>>> ATTENTION: existing soak_verdict={keep_value!r} at {keep_iso} "
            f"is CONTRADICTED by current FAIL."
        )
        print(">>> To revoke, run:")
        revoke_sql = (
            f"sqlite3 <db> \"INSERT INTO signal_params_audit"
            f"(signal_type, field_name, old_value, new_value, reason, applied_by, applied_at) "
            f"VALUES('{_sql_escape(result.signal_type)}', 'soak_verdict', "
            f"'{_sql_escape(keep_value)}', 'revoked', "
            f"'revoke: revival_criteria FAIL at {result.evaluated_at.isoformat()}', "
            f"'operator', '{result.evaluated_at.isoformat()}');\""
        )
        print(revoke_sql)

    if result.window_a is not None and result.window_b is not None:
        for label, w in (("A", result.window_a), ("B", result.window_b)):
            print(
                f"\nWindow {label}: {w.start_at.date()} → {w.end_at.date()} (n={w.n})"
            )
            print(
                f"  net=${w.net_pnl_usd:.2f}  per_trade=${w.per_trade_usd:.2f}  "
                f"win%={w.win_pct:.1f}"
            )
            print(
                f"  win_pct_wilson_lb={w.win_pct_wilson_lb:.1f}%  "
                f"per_trade_bootstrap_lb=${w.per_trade_bootstrap_lb:.2f}"
            )
            print(f"  no_breakout_and_loss_rate={w.no_breakout_and_loss_rate:.2f}")
            print(f"  stop_loss_frequency={w.stop_loss_frequency:.2f}")
            print(f"  expired_loss_frequency={w.expired_loss_frequency:.2f}")
            print(f"  exit_machinery_contribution={w.exit_machinery_contribution:.2f}")


def _emit_soak_verdict_sql(
    result: RevivalCriteriaResult, *, operator: str, settings
) -> str | None:
    """Return SQL operator may paste to write the soak_verdict audit row.

    Returns None when verdict != PASS. Wraps INSERT in BEGIN IMMEDIATE / COMMIT.
    All interpolated values are sql-escaped.

    Per design-review fold D#2: verdict is ``keep_on_provisional_until_<iso>``,
    NOT ``keep_on_permanent``, embedding a time-boxed expiry. The active
    watchdog enforcement is filed as follow-up ``BL-NEW-REVIVAL-VERDICT-WATCHDOG``.
    """
    if result.verdict is not RevivalVerdict.PASS:
        return None
    _validate_signal_type(result.signal_type)
    _validate_signal_type(operator)
    sig = _sql_escape(result.signal_type)
    op = _sql_escape(operator)
    expiry_at = result.evaluated_at + timedelta(
        days=settings.REVIVAL_CRITERIA_VERDICT_EXPIRY_DAYS
    )
    verdict_str = f"keep_on_provisional_until_{expiry_at.isoformat()}"
    reason = _sql_escape(
        f"PASS: n={result.n_trades}, cutover={result.cutover_at.isoformat()} "
        f"({result.cutover_age_days}d ago), source={result.cutover_source}, "
        f"window_a per_trade=${result.window_a.per_trade_usd:.2f} "
        f"bootstrap_lb=${result.window_a.per_trade_bootstrap_lb:.2f}, "
        f"window_b per_trade=${result.window_b.per_trade_usd:.2f} "
        f"bootstrap_lb=${result.window_b.per_trade_bootstrap_lb:.2f}"
    )
    ts = _sql_escape(result.evaluated_at.isoformat())
    verdict_value = _sql_escape(verdict_str)
    return (
        f"-- Generated by scout.trading.revival_criteria at {result.evaluated_at.isoformat()}\n"
        f"-- NOTE: PASS does not bypass BL-NEW-REVIVAL-COOLOFF; check cool-off\n"
        f"--   before calling Database.revive_signal_with_baseline.\n"
        f"-- VERDICT EXPIRY: {expiry_at.isoformat()} "
        f"({settings.REVIVAL_CRITERIA_VERDICT_EXPIRY_DAYS}d)\n"
        f"PRAGMA busy_timeout=30000;\n"
        f"BEGIN IMMEDIATE;\n"
        f"INSERT INTO signal_params_audit\n"
        f"  (signal_type, field_name, old_value, new_value, reason, applied_by, applied_at)\n"
        f"VALUES\n"
        f"  ('{sig}', 'soak_verdict', NULL, '{verdict_value}',\n"
        f"   '{reason}',\n"
        f"   '{op}', '{ts}');\n"
        f"COMMIT;\n"
    )


async def _main_async(args: argparse.Namespace) -> int:
    from scout.config import get_settings
    from scout.db import Database

    settings = get_settings()
    db = Database(args.db)
    await db.connect()
    try:
        if not await signal_type_exists(db, args.signal_type):
            print(
                f"ERROR: signal_type={args.signal_type!r} not found in signal_params table",
                file=sys.stderr,
            )
            return 3

        result = await evaluate_revival_criteria(
            db, args.signal_type, settings, cutover_override=args.cutover_iso
        )

        if args.cutover_iso is not None:
            audit_cutover, _ = await find_latest_regime_cutover(db, args.signal_type)
            if audit_cutover is not None and audit_cutover != args.cutover_iso:
                delta = (args.cutover_iso - audit_cutover).days
                print(
                    f">>> OVERRIDE WARNING: audit-derived cutover was "
                    f"{audit_cutover.isoformat()}; operator override is "
                    f"{args.cutover_iso.isoformat()} (delta={delta}d)",
                    file=sys.stderr,
                )

        existing_keep = await find_existing_keep_verdict(db, args.signal_type)
        cool_off_until = await _query_cool_off_status(db, args.signal_type, settings)
        recent_rate = await compute_recent_trade_rate(db, args.signal_type)
    finally:
        await db.close()

    sql = _emit_soak_verdict_sql(result, operator=args.operator, settings=settings)

    if args.emit_sql_only:
        if sql is not None:
            print(sql)
    else:
        _print_verdict(
            result,
            existing_keep=existing_keep,
            cool_off_active_until=cool_off_until,
            recent_trade_rate=recent_rate,
            settings=settings,
        )
        if sql is not None:
            print("\n--- Operator may paste the following SQL to write the audit row ---")
            print(sql)

    log.info(
        "revival_criteria_evaluated",
        signal_type=args.signal_type,
        verdict=result.verdict.value,
        n_trades=result.n_trades,
        cutover_source=result.cutover_source,
        cutover_age_days=result.cutover_age_days,
        cutover_override_used=args.cutover_iso is not None,
        failures=result.failure_reasons,
    )

    if result.verdict is RevivalVerdict.PASS:
        return 0
    if result.verdict is RevivalVerdict.FAIL:
        return 1
    return 2


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="revival_criteria")
    p.add_argument("signal_type", help="signal_type to evaluate")
    p.add_argument("--db", default="scout.db", help="path to scout.db")
    p.add_argument(
        "--operator", default="operator", help="applied_by value for emitted SQL"
    )
    p.add_argument(
        "--cutover-iso",
        default=None,
        type=_parse_cutover_iso,
        help="explicit cutover ISO timestamp; overrides audit-derived cutover",
    )
    p.add_argument(
        "--emit-sql-only",
        action="store_true",
        help="suppress diagnostic prose; print SQL only (redirect-pipeable)",
    )
    args = p.parse_args(argv)
    _validate_signal_type(args.signal_type)
    _validate_signal_type(args.operator)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
