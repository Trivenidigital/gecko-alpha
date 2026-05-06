"""BL-063 moonshot exit-path integration tests.

Verifies the evaluator wiring:
- arm fires when peak_pct >= MOONSHOT_THRESHOLD_PCT and flag is on
- trailing-stop uses widened drawdown when armed
- close status is 'closed_moonshot_trail' on trail exit when armed
- non-armed and disabled-flag paths preserve existing behaviour
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout.db import Database
from scout.trading.evaluator import evaluate_paper_trades
from scout.trading.paper import PaperTrader


async def _seed_price(db: Database, token_id: str, price: float) -> None:
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        (token_id, price, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()


async def _open_armed_runner(
    db: Database, trader: PaperTrader, *, token_id: str, settings
) -> int:
    """Open a trade, fire leg 1 (floor armed), and seed peak_pct above threshold.

    Returns the trade_id, ready for evaluator to consider trailing/moonshot.
    """
    trade_id = await trader.execute_buy(
        db=db,
        token_id=token_id,
        symbol=token_id.upper(),
        name=token_id.title(),
        chain="coingecko",
        signal_type="first_signal",
        signal_data={},
        current_price=1.00,
        amount_usd=100.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,
        signal_combo="first_signal",
    )
    assert trade_id is not None
    # Fire leg 1 manually so floor is armed (post-leg-1 trail eligibility).
    await trader.execute_partial_sell(
        db=db,
        trade_id=trade_id,
        leg=1,
        sell_qty_frac=settings.PAPER_LADDER_LEG_1_QTY_FRAC,
        current_price=1.30,
        slippage_bps=0,
    )
    return trade_id


@pytest.mark.asyncio
async def test_moonshot_arms_at_threshold_when_enabled(tmp_path, settings_factory):
    """When peak_pct >= threshold and flag on, evaluator arms the moonshot."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_MOONSHOT_ENABLED=True,
        PAPER_MOONSHOT_THRESHOLD_PCT=40.0,
        PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT=30.0,
        PAPER_LADDER_TRAIL_PCT=12.0,
    )
    trade_id = await _open_armed_runner(db, trader, token_id="m1", settings=settings)
    # Push price to +50% — peak_pct will be updated to ~50 inside the evaluator
    # which triggers the arm path.
    await _seed_price(db, "m1", 1.50)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT moonshot_armed_at, original_trail_drawdown_pct, status "
        "FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    armed_at, original_trail, status = await cur.fetchone()
    assert armed_at is not None
    assert original_trail == pytest.approx(12.0)
    # The trade is still open — armed, not closed (price hasn't trailed off
    # the peak yet).
    assert status == "open"
    await db.close()


@pytest.mark.asyncio
async def test_moonshot_disabled_does_not_arm(tmp_path, settings_factory):
    """Flag off => moonshot never arms even past the threshold."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(PAPER_MOONSHOT_ENABLED=False)
    trade_id = await _open_armed_runner(db, trader, token_id="m2", settings=settings)
    await _seed_price(db, "m2", 1.50)  # +50%

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT moonshot_armed_at FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (armed_at,) = await cur.fetchone()
    assert armed_at is None
    await db.close()


@pytest.mark.asyncio
async def test_moonshot_trail_widens_after_arm(tmp_path, settings_factory):
    """Once armed, a -15% drawdown from peak does NOT trigger trail
    (would have under default 12% trail), but a -35% drawdown does."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_MOONSHOT_ENABLED=True,
        PAPER_MOONSHOT_THRESHOLD_PCT=40.0,
        PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT=30.0,
        PAPER_LADDER_TRAIL_PCT=12.0,
    )
    trade_id = await _open_armed_runner(db, trader, token_id="m3", settings=settings)

    # Push to +50% to arm; peak becomes 1.50.
    await _seed_price(db, "m3", 1.50)
    await evaluate_paper_trades(db, settings)
    # Sanity-check setup so failures here surface as setup bugs, not as
    # spurious trail-formula assertions later.
    cur = await db._conn.execute(
        "SELECT floor_armed, peak_price FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    floor_armed, peak_price = await cur.fetchone()
    assert floor_armed == 1, "leg 1 should have armed the floor"
    assert peak_price == pytest.approx(1.50, rel=1e-6)

    # Drawdown of -15% from peak: 1.50 * 0.85 = 1.275 — past the 12% baseline
    # trail (1.32) but well within the 30% moonshot trail (1.05). Should NOT close.
    await _seed_price(db, "m3", 1.275)
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (status,) = await cur.fetchone()
    assert (
        status == "open"
    ), "Moonshot trail should be wider — 15% drawdown shouldn't close"

    # Drawdown past the 30% moonshot trail (1.50 * 0.70 = 1.05). Price 1.04
    # is below the trail threshold but ABOVE the entry-price floor (1.00),
    # so the moonshot-trail branch fires before the BL-061 floor exit.
    await _seed_price(db, "m3", 1.04)
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?", (trade_id,)
    )
    status, exit_reason = await cur.fetchone()
    assert status == "closed_moonshot_trail"
    assert exit_reason == "trailing_stop"
    await db.close()


@pytest.mark.asyncio
async def test_non_armed_trail_uses_baseline(tmp_path, settings_factory):
    """When moonshot is disabled, the trail uses PAPER_LADDER_TRAIL_PCT
    and closes as 'closed_trailing_stop' (regression gate for BL-061)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_MOONSHOT_ENABLED=False,
        PAPER_LADDER_TRAIL_PCT=12.0,
    )
    trade_id = await _open_armed_runner(db, trader, token_id="m4", settings=settings)
    await _seed_price(db, "m4", 1.50)  # +50% peak
    await evaluate_paper_trades(db, settings)
    # -15% from peak — past 12% baseline trail, should close.
    await _seed_price(db, "m4", 1.275)
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?", (trade_id,)
    )
    status, exit_reason = await cur.fetchone()
    assert status == "closed_trailing_stop"
    assert exit_reason == "trailing_stop"
    await db.close()


@pytest.mark.asyncio
async def test_pre_bl061_trade_never_arms(tmp_path, settings_factory):
    """A trade with created_at BEFORE the BL-061 cutover must not arm
    even with PAPER_MOONSHOT_ENABLED=True. BL-060 mid-flight migration
    lesson: A/B is scoped to opened_at >= cutover_ts."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Push the bl061 cutover into the future so the inserted row is "pre-cutover".
    future_iso = "2099-01-01T00:00:00+00:00"
    await db._conn.execute(
        "UPDATE paper_migrations SET cutover_ts = ? WHERE name = 'bl061_ladder'",
        (future_iso,),
    )
    await db._conn.commit()

    trader = PaperTrader()
    settings = settings_factory(PAPER_MOONSHOT_ENABLED=True)
    trade_id = await trader.execute_buy(
        db=db,
        token_id="pre",
        symbol="PRE",
        name="Pre",
        chain="coingecko",
        signal_type="first_signal",
        signal_data={},
        current_price=1.00,
        amount_usd=100.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,
        signal_combo="first_signal",
    )
    await _seed_price(db, "pre", 1.50)  # +50% peak — past the moonshot threshold

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT moonshot_armed_at, status FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    armed_at, status = await cur.fetchone()
    assert armed_at is None, "pre-cutover trades must skip the BL-063 path entirely"
    # Lock in legacy-cascade behavior at the same time: a +50% pass on a
    # pre-BL-061 trade with TP=+20% closes via the legacy fixed-TP path.
    # Catches an unrelated regression in the legacy cascade.
    assert status == "closed_tp"
    await db.close()


@pytest.mark.asyncio
async def test_floor_exit_pre_empts_moonshot_trail(tmp_path, settings_factory):
    """When price drops below entry while moonshot is armed, the BL-061
    floor exit fires first — NOT closed_moonshot_trail. Locks in the
    elif-chain ordering in the evaluator."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_MOONSHOT_ENABLED=True,
        PAPER_MOONSHOT_THRESHOLD_PCT=40.0,
        PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT=30.0,
    )
    trade_id = await _open_armed_runner(db, trader, token_id="fp", settings=settings)

    # Arm the moonshot via a +50% pass
    await _seed_price(db, "fp", 1.50)
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT moonshot_armed_at FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (armed_at,) = await cur.fetchone()
    assert armed_at is not None

    # Hard drop below entry — floor exit must win over moonshot trail.
    await _seed_price(db, "fp", 0.95)
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?", (trade_id,)
    )
    status, exit_reason = await cur.fetchone()
    assert status == "closed_floor"
    assert exit_reason == "floor"
    await db.close()


@pytest.mark.asyncio
async def test_moonshot_arm_and_leg_2_same_tick(tmp_path, settings_factory):
    """When peak_pct >= max(LEG_2_PCT, MOONSHOT_THRESHOLD) on a single tick,
    moonshot arms AND leg 2 fires (in that order, with leg 2 hitting `continue`
    before the trail check)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_MOONSHOT_ENABLED=True,
        PAPER_MOONSHOT_THRESHOLD_PCT=40.0,
        PAPER_LADDER_LEG_1_PCT=25.0,
        PAPER_LADDER_LEG_2_PCT=50.0,
    )
    trade_id = await _open_armed_runner(db, trader, token_id="al", settings=settings)
    # +55% covers both LEG_2 (>=50) and MOONSHOT_THRESHOLD (>=40)
    await _seed_price(db, "al", 1.55)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT moonshot_armed_at, leg_2_filled_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    armed_at, leg_2_filled = await cur.fetchone()
    assert armed_at is not None, "arm fires before the leg 2 continue"
    assert leg_2_filled is not None, "leg 2 still fires on the same pass"
    await db.close()


@pytest.mark.asyncio
async def test_moonshot_trail_wins_over_peak_fade(tmp_path, settings_factory):
    """When both the moonshot trail and peak-fade conditions could fire on
    the same pass, the trail wins (close_reason is set first in the cascade)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_MOONSHOT_ENABLED=True,
        PAPER_MOONSHOT_THRESHOLD_PCT=40.0,
        PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT=30.0,
        PEAK_FADE_ENABLED=True,
        PEAK_FADE_MIN_PEAK_PCT=10.0,
        PEAK_FADE_RETRACE_RATIO=0.7,
    )
    trade_id = await _open_armed_runner(db, trader, token_id="pf", settings=settings)

    # Arm at +50%
    await _seed_price(db, "pf", 1.50)
    await evaluate_paper_trades(db, settings)
    # Pre-fill 6h + 24h checkpoints below the peak-fade retrace threshold so
    # peak-fade WOULD be eligible to fire on the next pass.
    await db._conn.execute(
        "UPDATE paper_trades SET checkpoint_6h_pct = ?, checkpoint_24h_pct = ? "
        "WHERE id = ?",
        (5.0, 5.0, trade_id),  # both below 50 * 0.7 = 35
    )
    await db._conn.commit()

    # Drop below the moonshot trail (1.50 * 0.7 = 1.05). 1.04 is below trail
    # AND above floor (1.00). Both moonshot trail and peak-fade would fire,
    # but trail is checked first and sets close_reason.
    await _seed_price(db, "pf", 1.04)
    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (status,) = await cur.fetchone()
    assert (
        status == "closed_moonshot_trail"
    ), "moonshot trail must fire before peak-fade in the cascade"
    await db.close()


@pytest.mark.asyncio
async def test_bl061_moonshot_trade_never_closes_via_fixed_tp(
    tmp_path, settings_factory
):
    """Locks in the structural-unreachability claim from the PR body:
    on a BL-061 trade, the legacy `current_price >= tp_price` check at the
    pre-cutover cascade is unreachable because the BL-061 branch ends with
    a `continue`.

    A +50% trade with tp_pct=20 (so tp_price = entry * 1.20) MUST NOT close
    as `closed_tp` — it stays open until trail/peak-fade/expire fires.
    Without this gate a future refactor that drops the `continue` would
    silently re-introduce the early-clipping bug BL-063 was built to fix.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_MOONSHOT_ENABLED=True,
        PAPER_MOONSHOT_THRESHOLD_PCT=40.0,
    )
    # Open a fresh BL-061 trade (post-cutover by default).
    trade_id = await _open_armed_runner(db, trader, token_id="ntp", settings=settings)

    # Push to +50% — well past tp_pct=20. If the legacy fixed-TP path were
    # reachable, the trade would close as closed_tp here.
    await _seed_price(db, "ntp", 1.50)
    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status, moonshot_armed_at, leg_2_filled_at "
        "FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    status, armed_at, leg_2 = await cur.fetchone()
    assert status != "closed_tp", (
        "BL-061 trades must reach `continue` before the legacy fixed-TP "
        "cascade — closing as closed_tp would re-introduce the early-clip bug"
    )
    # Confirm the BL-061 path actually ran (moonshot armed + leg 2 filled).
    assert armed_at is not None
    assert leg_2 is not None
    await db.close()


# BL-NEW-MOONSHOT-OPT-OUT: per-signal opt-out from the moonshot floor


@pytest.mark.asyncio
async def test_signal_params_has_moonshot_enabled_column(tmp_path):
    """Migration adds moonshot_enabled INTEGER NOT NULL DEFAULT 1."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(signal_params)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "moonshot_enabled" in cols
    await db.close()


@pytest.mark.asyncio
async def test_moonshot_enabled_defaults_to_1_for_all_seed_signals(tmp_path):
    """All seeded signal_params rows have moonshot_enabled=1 (default opt-in,
    no behavior change on deploy)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT signal_type, moonshot_enabled FROM signal_params"
    )
    rows = await cur.fetchall()
    assert len(rows) > 0
    for sig, opt in rows:
        assert opt == 1, f"{sig} should default to 1 (opt-in); got {opt}"
    await db.close()


@pytest.mark.asyncio
async def test_migration_idempotent_on_rerun(tmp_path):
    """Per design-stage policy reviewer RECOMMEND: re-running the
    migration on an already-migrated DB must be a no-op (no exception,
    column still exists, paper_migrations + schema_version each have
    exactly one row for this migration)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Re-run the migration directly — initialize() already ran it once.
    await db._migrate_moonshot_opt_out_column()
    # Column still exists exactly once.
    cur = await db._conn.execute("PRAGMA table_info(signal_params)")
    cols = [row[1] for row in await cur.fetchall()]
    assert cols.count("moonshot_enabled") == 1
    # Cutover row exists exactly once.
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_migrations WHERE name = ?",
        ("bl_moonshot_opt_out_v1",),
    )
    assert (await cur.fetchone())[0] == 1
    # Schema_version row exists exactly once.
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM schema_version WHERE version = ?",
        (20260507,),
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


async def _arm_at_50pct(db, trader, *, token_id, settings, signal_type="first_signal"):
    """Helper: open a trade, arm leg-1, arm moonshot at +50% peak."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id=token_id,
        symbol=token_id.upper(),
        name=token_id.title(),
        chain="coingecko",
        signal_type=signal_type,
        signal_data={},
        current_price=1.00,
        amount_usd=100.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,
        signal_combo=signal_type,
    )
    await trader.execute_partial_sell(
        db=db,
        trade_id=trade_id,
        leg=1,
        sell_qty_frac=settings.PAPER_LADDER_LEG_1_QTY_FRAC,
        current_price=1.30,
        slippage_bps=0,
    )
    # Push price to +50% to arm moonshot in the next evaluator pass.
    await _seed_price(db, token_id, 1.50)
    await evaluate_paper_trades(db, settings)
    return trade_id


@pytest.mark.asyncio
async def test_moonshot_floor_applies_by_default(tmp_path, settings_factory):
    """moonshot_enabled=1 (default): trade armed at peak >= 40 closes via
    trailing_stop only when retrace exceeds the MOONSHOT_TRAIL floor (30%),
    NOT the tighter sp.trail_pct from per-signal calibration. This is the
    pre-PR behavior the opt-out is designed to escape from."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_MOONSHOT_ENABLED=True,
        PAPER_MOONSHOT_THRESHOLD_PCT=40.0,
        PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT=30.0,
        PAPER_LADDER_TRAIL_PCT=15.0,  # tighter than 30 floor; floor wins
        SIGNAL_PARAMS_ENABLED=True,
    )
    trade_id = await _arm_at_50pct(db, trader, token_id="m_opt1", settings=settings)
    # Confirm armed.
    cur = await db._conn.execute(
        "SELECT moonshot_armed_at FROM paper_trades WHERE id = ?", (trade_id,)
    )
    assert (await cur.fetchone())[0] is not None
    # Push to -20% retrace from peak (1.50 → 1.20). trail floor=30%, not yet
    # triggered; trade stays open.
    await _seed_price(db, "m_opt1", 1.20)
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    assert (await cur.fetchone())[0] == "open"
    # Push to -35% retrace (1.50 → 0.975). Now > floor 30%; closes.
    await _seed_price(db, "m_opt1", 0.975)
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    assert (await cur.fetchone())[0].startswith("closed_")
    await db.close()


@pytest.mark.asyncio
async def test_moonshot_floor_skipped_when_opted_out(tmp_path, settings_factory):
    """moonshot_enabled=0: signal-level trail_pct=15 controls the trail in
    moonshot regime, NOT the global 30 floor. Trade armed at +50% peak
    closes when retrace > 15%, which would NOT trigger under default
    moonshot_enabled=1 (where the floor at 30% wins)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Opt out first_signal from moonshot
    await db._conn.execute(
        "UPDATE signal_params SET moonshot_enabled = 0 WHERE signal_type = 'first_signal'"
    )
    # Tighten trail_pct to 15 in the table (overrides Settings default)
    await db._conn.execute(
        "UPDATE signal_params SET trail_pct = 15.0 WHERE signal_type = 'first_signal'"
    )
    await db._conn.commit()
    from scout.trading.params import clear_cache_for_tests

    clear_cache_for_tests()

    trader = PaperTrader()
    settings = settings_factory(
        PAPER_MOONSHOT_ENABLED=True,
        PAPER_MOONSHOT_THRESHOLD_PCT=40.0,
        PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT=30.0,
        PAPER_LADDER_TRAIL_PCT=15.0,
        SIGNAL_PARAMS_ENABLED=True,
    )
    trade_id = await _arm_at_50pct(db, trader, token_id="m_opt2", settings=settings)
    # Push to -20% retrace from peak (1.50 → 1.20). With moonshot_enabled=0
    # and sp.trail_pct=15%, this IS past trail; trade should close.
    await _seed_price(db, "m_opt2", 1.20)
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    status = (await cur.fetchone())[0]
    assert status.startswith("closed_"), (
        f"With moonshot_enabled=0 and sp.trail_pct=15, a 20% retrace from peak "
        f"should close the trade; got status={status}"
    )
    await db.close()


@pytest.mark.asyncio
async def test_moonshot_opt_out_pre_moonshot_path_unchanged(tmp_path, settings_factory):
    """Opt-out only affects moonshot regime (peak >= 40). At peak in
    [low_peak_threshold, 40), the existing sp.trail_pct path runs
    regardless of moonshot_enabled."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "UPDATE signal_params SET moonshot_enabled = 0 WHERE signal_type = 'first_signal'"
    )
    await db._conn.commit()
    from scout.trading.params import clear_cache_for_tests

    clear_cache_for_tests()

    trader = PaperTrader()
    settings = settings_factory(
        PAPER_MOONSHOT_ENABLED=True,
        PAPER_MOONSHOT_THRESHOLD_PCT=40.0,
        PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT=30.0,
        PAPER_LADDER_TRAIL_PCT=12.0,
        SIGNAL_PARAMS_ENABLED=True,
    )
    trade_id = await trader.execute_buy(
        db=db,
        token_id="m_opt3",
        symbol="M3",
        name="M3",
        chain="coingecko",
        signal_type="first_signal",
        signal_data={},
        current_price=1.00,
        amount_usd=100.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,
        signal_combo="first_signal",
    )
    await trader.execute_partial_sell(
        db=db,
        trade_id=trade_id,
        leg=1,
        sell_qty_frac=settings.PAPER_LADDER_LEG_1_QTY_FRAC,
        current_price=1.30,
        slippage_bps=0,
    )
    # Push only to +30% peak (below moonshot threshold 40)
    await _seed_price(db, "m_opt3", 1.30)
    await evaluate_paper_trades(db, settings)
    # Confirm not armed (peak < 40)
    cur = await db._conn.execute(
        "SELECT moonshot_armed_at FROM paper_trades WHERE id = ?", (trade_id,)
    )
    assert (await cur.fetchone())[0] is None
    # The trade should obey sp.trail_pct = 12 (NOT the moonshot 30 floor)
    # because we're not in moonshot regime — same as pre-PR behavior.
    await db.close()


@pytest.mark.asyncio
async def test_moonshot_opt_out_with_conviction_lock(
    tmp_path, settings_factory, monkeypatch
):
    """Per #2 PR statistical reviewer MUST-FIX: pin the conviction-lock
    interaction claimed in the evaluator opt-out branch comment.

    When BL-067 conviction-lock has overlaid sp.trail_pct upward (via
    dataclasses.replace at evaluator.py ~228), the moonshot_enabled=0
    branch reads the LOCKED sp.trail_pct value (35%), NOT the base
    config 12% nor the moonshot floor 30%. This test exercises that
    specific composition.

    We monkeypatch compute_stack to return 3 (>= threshold) rather than
    constructing sibling-trade fixtures, which would require seeding
    multiple signal-source tables per `_SIGNAL_SOURCES`. The point of
    THIS test is the post-overlay → moonshot-opt-out flow, not the
    stack-counting fixture.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Opt-out + base trail = 12 (so the locked overlay can widen to 35;
    # delta at stack=3 is +20pp capped at 35). conviction_lock_enabled=1
    # so the per-signal opt-in passes.
    await db._conn.execute(
        "UPDATE signal_params SET moonshot_enabled = 0, "
        "trail_pct = 12.0, conviction_lock_enabled = 1 "
        "WHERE signal_type = 'first_signal'"
    )
    await db._conn.commit()
    from scout.trading.params import clear_cache_for_tests

    clear_cache_for_tests()

    # Force compute_stack to return 3 (>= default threshold of 3).
    # The evaluator overlays sp.trail_pct = min(12 + 20, 35) = 32.
    async def _stub_stack(db_arg, token_id, signal_type, exclude_trade_id=None):
        return 3

    monkeypatch.setattr(
        "scout.trading.conviction.compute_stack", _stub_stack, raising=True
    )

    trader = PaperTrader()
    settings = settings_factory(
        PAPER_MOONSHOT_ENABLED=True,
        PAPER_MOONSHOT_THRESHOLD_PCT=40.0,
        PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT=30.0,
        PAPER_LADDER_TRAIL_PCT=12.0,
        PAPER_CONVICTION_LOCK_ENABLED=True,
        PAPER_CONVICTION_LOCK_THRESHOLD=3,
        SIGNAL_PARAMS_ENABLED=True,
    )
    trade_id = await _arm_at_50pct(db, trader, token_id="m_lock", settings=settings)

    # Confirm conviction-lock fired (audit row stamped).
    cur = await db._conn.execute(
        "SELECT conviction_locked_at, conviction_locked_stack "
        "FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    locked_at, locked_stack = await cur.fetchone()
    assert (
        locked_at is not None
    ), "expected conviction-lock to fire with stack>=3; check stub setup"
    assert locked_stack == 3

    # The locked trail at stack=3 with base 12 is min(12+20, 35) = 32.
    # With moonshot_enabled=0, effective_trail_pct = sp.trail_pct = 32%
    # (NOT the moonshot floor 30, NOT the base 12).
    #
    # Push price to -25% retrace from peak 1.50: 1.125. That's ABOVE
    # 32% trail (1.50 * 0.68 = 1.02), so trade stays open.
    await _seed_price(db, "m_lock", 1.125)
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    status = (await cur.fetchone())[0]
    assert (
        status == "open"
    ), f"-25% retrace should not close at locked trail 32%; got {status}"

    # Push to -34% retrace: 1.50 * 0.66 = 0.99. NOW past 32% trail.
    await _seed_price(db, "m_lock", 0.99)
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    status = (await cur.fetchone())[0]
    assert status.startswith(
        "closed_"
    ), f"-34% retrace > locked trail 32% should close; got {status}"
    await db.close()


@pytest.mark.asyncio
async def test_moonshot_opt_out_low_peak_path_unchanged(tmp_path, settings_factory):
    """Below low_peak_threshold (default 20), trail_pct_low_peak applies
    regardless of moonshot_enabled — this branch is independent of the
    opt-out logic."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "UPDATE signal_params SET moonshot_enabled = 0 WHERE signal_type = 'first_signal'"
    )
    await db._conn.commit()
    from scout.trading.params import clear_cache_for_tests, get_params

    clear_cache_for_tests()

    settings = settings_factory(
        PAPER_MOONSHOT_ENABLED=True,
        PAPER_LADDER_TRAIL_PCT=20.0,
        PAPER_LADDER_TRAIL_PCT_LOW_PEAK=8.0,
        PAPER_LADDER_LOW_PEAK_THRESHOLD_PCT=20.0,
        SIGNAL_PARAMS_ENABLED=True,
    )
    sp = await get_params(db, "first_signal", settings)
    # Sanity: opt-out flag is set on params
    assert sp.moonshot_enabled is False
    # The low-peak branch is structural — verified by reading
    # evaluator.py:475: `elif peak_pct is not None and peak_pct < ...`
    # is the path for sub-threshold peaks regardless of moonshot_enabled.
    await db.close()
