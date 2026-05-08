"""Per-signal trading parameters (Tier 1a).

Replaces global ``PAPER_LADDER_*`` / ``PAPER_SL_PCT`` / ``PAPER_MAX_DURATION_HOURS``
with a per-signal-type table that the calibrator can recalibrate weekly.

Read path:
    1. ``SIGNAL_PARAMS_ENABLED=False``  → always Settings (source='settings')
    2. signal_type in DEFAULT_SIGNAL_TYPES AND row exists → table (source='table')
    3. signal_type in DEFAULT_SIGNAL_TYPES AND row missing → log error, return Settings
    4. signal_type NOT in DEFAULT_SIGNAL_TYPES → raise UnknownSignalType (typo guard)

The cache is module-level dict with a TTL + version int that ``--apply`` bumps.
The dashboard process has its own cache, so the apply runbook restarts both.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

import structlog

from scout.db import Database

log = structlog.get_logger(__name__)


# Known signal_types stored in paper_trades.signal_type. Membership gates
# get_params() to catch typos rather than silently falling back to globals.
# moonshot is intentionally absent — it is a trail modifier (BL-063), not a
# stored signal_type.
DEFAULT_SIGNAL_TYPES: frozenset[str] = frozenset(
    {
        "gainers_early",
        "losers_contrarian",
        "trending_catch",
        "first_signal",
        "narrative_prediction",
        "volume_spike",
        "chain_completed",
        "tg_social",
    }
)

# Signals excluded from auto-calibration in v1.
# narrative_prediction has known token_id divergence (32/56 stale-young rows
# in late-April audit) — outcomes are partly noise from upstream, calibrating
# on them tunes the wrong thing.
CALIBRATION_EXCLUDE_SIGNALS: frozenset[str] = frozenset({"narrative_prediction"})


class UnknownSignalType(Exception):
    """Raised when get_params() is called with a signal_type not in
    DEFAULT_SIGNAL_TYPES. Catches typos at the call site rather than letting
    them silently inherit global Settings."""


@dataclass(frozen=True)
class SignalParams:
    """Resolved parameters for one signal_type at one point in time.

    ``source`` indicates whether the values came from the DB row or fell
    back to global Settings. The dashboard surfaces this so operators can
    tell at a glance whether per-signal calibration is in effect.
    """

    signal_type: str
    leg_1_pct: float
    leg_1_qty_frac: float
    leg_2_pct: float
    leg_2_qty_frac: float
    trail_pct: float
    trail_pct_low_peak: float
    low_peak_threshold_pct: float
    sl_pct: float
    max_duration_hours: int
    enabled: bool
    source: Literal["table", "settings"]
    # BL-067 conviction-lock per-signal opt-in. Defaults False (fail-closed)
    # both in the Settings fallback path and in the table path. Default
    # value placed last to preserve existing positional construction sites.
    conviction_lock_enabled: bool = False
    # BL-NEW-HPF high-peak fade per-signal opt-in. Default False (fail-closed).
    # Requires signal_params.high_peak_fade_enabled=1 when master
    # PAPER_HIGH_PEAK_FADE_PER_SIGNAL_OPT_IN=True (the default).
    high_peak_fade_enabled: bool = False
    # BL-NEW-MOONSHOT-OPT-OUT per-signal opt-out from the moonshot regime
    # floor. Default True preserves current behavior:
    #     effective_trail_pct = max(MOONSHOT_TRAIL_DRAWDOWN_PCT, sp.trail_pct)
    # When False, the evaluator uses sp.trail_pct directly in the moonshot
    # regime (peak >= 40%), letting calibration / conviction-lock fully
    # control the trail width. Default placed last to preserve positional
    # construction sites in _settings_params and the row-reader. Default
    # True is load-bearing: without it, the Settings-fallback path in
    # _settings_params (which omits the kwarg) would TypeError.
    moonshot_enabled: bool = True
    # BL-NEW-LIVE-HYBRID M1 — Layer 3 per-signal opt-in. Default 0 fail-closed.
    live_eligible: bool = False


# Module-level cache. Keyed by signal_type, value = (params, expires_at).
# `_cache_version` is bumped by calibrate.py / auto_suspend.py after writes,
# so the next get_params() call within the same process gets fresh data
# without waiting for the TTL.
_CACHE_TTL_SEC = 300
_cache: dict[str, tuple[SignalParams, float, int]] = {}
_cache_version: int = 0


def bump_cache_version() -> None:
    """Invalidate this process's cache. Cross-process invalidation requires
    a service restart — documented in the apply runbook."""
    global _cache_version
    _cache_version += 1


def _settings_params(signal_type: str, settings) -> SignalParams:
    """Build SignalParams from global Settings (the v0 / fallback path)."""
    return SignalParams(
        signal_type=signal_type,
        leg_1_pct=settings.PAPER_LADDER_LEG_1_PCT,
        leg_1_qty_frac=settings.PAPER_LADDER_LEG_1_QTY_FRAC,
        leg_2_pct=settings.PAPER_LADDER_LEG_2_PCT,
        leg_2_qty_frac=settings.PAPER_LADDER_LEG_2_QTY_FRAC,
        trail_pct=settings.PAPER_LADDER_TRAIL_PCT,
        trail_pct_low_peak=settings.PAPER_LADDER_TRAIL_PCT_LOW_PEAK,
        low_peak_threshold_pct=settings.PAPER_LADDER_LOW_PEAK_THRESHOLD_PCT,
        sl_pct=settings.PAPER_SL_PCT,
        max_duration_hours=settings.PAPER_MAX_DURATION_HOURS,
        enabled=True,
        source="settings",
    )


async def get_params(
    db: Database,
    signal_type: str,
    settings,
) -> SignalParams:
    """Return per-signal params, falling back to Settings when off / missing.

    Raises ``UnknownSignalType`` when ``signal_type`` is not in
    ``DEFAULT_SIGNAL_TYPES`` — this catches caller typos.
    """
    if signal_type not in DEFAULT_SIGNAL_TYPES:
        raise UnknownSignalType(
            f"signal_type={signal_type!r} not in DEFAULT_SIGNAL_TYPES; "
            f"add it to scout/trading/params.py if it is a real signal."
        )

    if not getattr(settings, "SIGNAL_PARAMS_ENABLED", False):
        return _settings_params(signal_type, settings)

    now = time.monotonic()
    cached = _cache.get(signal_type)
    if cached is not None:
        params, expires_at, version = cached
        if expires_at > now and version == _cache_version:
            return params

    if db._conn is None:
        # DB closed mid-call (shutdown race, restart). Returning Settings
        # instead of crashing the eval loop is the lesser evil, but it
        # silently bypasses any suspended/calibrated row — log loudly so
        # the operator can correlate with restart events.
        log.error(
            "signal_params_db_closed",
            err_id="SIGNAL_PARAMS_DB_CLOSED",
            signal_type=signal_type,
        )
        return _settings_params(signal_type, settings)

    # BL-067: conviction_lock_enabled is row[10]. BL-NEW-HPF:
    # high_peak_fade_enabled is row[11]. BL-NEW-MOONSHOT-OPT-OUT:
    # moonshot_enabled is row[12]. BL-NEW-LIVE-HYBRID M1:
    # live_eligible is row[13]. Per design-v2 adv-N1, signal_type is
    # NOT in the SELECT — caller passes it as the function argument.
    cursor = await db._conn.execute(
        """SELECT leg_1_pct, leg_1_qty_frac, leg_2_pct, leg_2_qty_frac,
                  trail_pct, trail_pct_low_peak, low_peak_threshold_pct,
                  sl_pct, max_duration_hours, enabled,
                  conviction_lock_enabled,
                  high_peak_fade_enabled,
                  moonshot_enabled,
                  live_eligible
           FROM signal_params WHERE signal_type = ?""",
        (signal_type,),
    )
    row = await cursor.fetchone()
    if row is None:
        # Known-good signal_type, but no row — calibrate.py never wrote one,
        # or the migration was skipped. Log so the operator notices, return
        # Settings so the pipeline keeps moving.
        log.error(
            "signal_params_missing_row",
            err_id="SIGNAL_PARAMS_MISSING_ROW",
            signal_type=signal_type,
        )
        params = _settings_params(signal_type, settings)
    else:
        params = SignalParams(
            signal_type=signal_type,
            leg_1_pct=float(row[0]),
            leg_1_qty_frac=float(row[1]),
            leg_2_pct=float(row[2]),
            leg_2_qty_frac=float(row[3]),
            trail_pct=float(row[4]),
            trail_pct_low_peak=float(row[5]),
            low_peak_threshold_pct=float(row[6]),
            sl_pct=float(row[7]),
            max_duration_hours=int(row[8]),
            enabled=bool(row[9]),
            source="table",
            conviction_lock_enabled=bool(row[10]),
            high_peak_fade_enabled=bool(row[11]),
            moonshot_enabled=bool(row[12]),
            live_eligible=bool(row[13]),
        )

    _cache[signal_type] = (params, now + _CACHE_TTL_SEC, _cache_version)
    return params


async def params_for_signal(
    db: Database,
    signal_type: str,
    settings,
) -> SignalParams:
    """Lenient variant of :func:`get_params` for the evaluator hot path.

    The evaluator processes historical trades whose ``signal_type`` may
    pre-date the current ``DEFAULT_SIGNAL_TYPES`` set (e.g. legacy
    ``momentum_7d`` or ``long_hold``). Raising on those would force every
    eval cycle to skip those rows. Instead, fall back to Settings — the
    legacy params are still good enough to wind down old positions.

    New ``open_trade`` calls go through the strict :func:`get_params`,
    so typos in *new* code still raise.
    """
    if signal_type not in DEFAULT_SIGNAL_TYPES:
        return _settings_params(signal_type, settings)
    return await get_params(db, signal_type, settings)


def clear_cache_for_tests() -> None:
    """Test helper — wipe cache between tests so they don't see each other's writes."""
    global _cache_version
    _cache.clear()
    _cache_version = 0
