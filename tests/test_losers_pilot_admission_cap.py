"""Bounded-pilot admission cap for losers_contrarian.

A cap enforced by an operator noticing "entry 200 landed" and then disabling the
signal is a monitored cutoff, not a cap: `trade_losers` iterates the whole fresh
-losers batch, so further entries can be admitted before the disable write
lands. If a plan calls a number a hard bound, the code must be able to refuse the
entry that would exceed it.
"""

from __future__ import annotations

import pytest

from scout.db import Database
from scout.trading.signals import _pilot_admission_allowed


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    await d.initialize()
    yield d
    await d.close()


async def _set_baseline(db: Database, ts: str | None) -> None:
    await db._conn.execute(
        "UPDATE signal_params SET drawdown_baseline_at = ? "
        "WHERE signal_type = 'losers_contrarian'",
        (ts,),
    )
    await db._conn.commit()


_SEQ = {"n": 0}


async def _add_entries(db: Database, n: int, *, opened_at: str) -> None:
    """Insert `n` cohort entries. Token ids are globally unique across calls —
    (token_id, signal_type, opened_at) carries a UNIQUE constraint, so reusing
    ids across two calls with the same timestamp collides."""
    for _ in range(n):
        i = _SEQ["n"]
        _SEQ["n"] += 1
        await db._conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity, tp_pct, sl_pct,
                tp_price, sl_price, status, opened_at, created_at)
               VALUES (?, 'T', 'T', 'coingecko', 'losers_contrarian', '{}',
                       100.0, 150.0, 1.5, 50.0, 25.0, 150.0, 75.0,
                       'open', ?, ?)""",
            (f"tok-{opened_at}-{i}", opened_at, opened_at),
        )
    await db._conn.commit()


class TestCapDisabledByDefault:
    async def test_zero_means_no_cap(self, db, settings_factory):
        """Default must not change behaviour for any existing deployment."""
        s = settings_factory()
        assert s.PAPER_LOSERS_PILOT_MAX_ENTRIES == 0
        await _set_baseline(db, "2026-08-08T00:00:00+00:00")
        await _add_entries(db, 500, opened_at="2026-08-08T01:00:00+00:00")
        assert await _pilot_admission_allowed(db, s) is True


class TestCapEnforcement:
    async def test_it_refuses_the_entry_that_would_exceed_the_cap(
        self, db, settings_factory
    ):
        """*** THE POINT. *** Entry N+1 must be refused, not merely noticed."""
        s = settings_factory(PAPER_LOSERS_PILOT_MAX_ENTRIES=5)
        await _set_baseline(db, "2026-08-08T00:00:00+00:00")

        await _add_entries(db, 4, opened_at="2026-08-08T01:00:00+00:00")
        assert await _pilot_admission_allowed(db, s) is True, "4 < 5 — room for one"

        await _add_entries(db, 1, opened_at="2026-08-08T01:00:00+00:00")
        assert await _pilot_admission_allowed(db, s) is False, (
            "at exactly the cap, the NEXT entry must be refused"
        )

    async def test_it_stays_refused_past_the_cap(self, db, settings_factory):
        s = settings_factory(PAPER_LOSERS_PILOT_MAX_ENTRIES=5)
        await _set_baseline(db, "2026-08-08T00:00:00+00:00")
        await _add_entries(db, 9, opened_at="2026-08-08T01:00:00+00:00")
        assert await _pilot_admission_allowed(db, s) is False


class TestCohortAnchoring:
    async def test_pre_baseline_trades_do_not_consume_the_cap(
        self, db, settings_factory
    ):
        """*** THE COHORT BOUNDARY. ***

        Historical losers_contrarian trades (330 of them in prod) must not
        instantly exhaust a 200-entry pilot cap. Counting from
        drawdown_baseline_at is what makes the cap measure THIS revival.
        """
        s = settings_factory(PAPER_LOSERS_PILOT_MAX_ENTRIES=5)
        await _set_baseline(db, "2026-08-08T00:00:00+00:00")
        await _add_entries(db, 50, opened_at="2026-05-01T00:00:00+00:00")  # old
        assert await _pilot_admission_allowed(db, s) is True, (
            "pre-revival trades belong to an earlier cohort"
        )

    async def test_the_anchor_is_the_same_one_auto_suspend_uses(self, db):
        """Cap and auto-suspend must not disagree about cohort membership.

        Both read signal_params.drawdown_baseline_at; this pins that they do,
        so a future change to one surfaces here rather than silently splitting
        the two definitions of 'this pilot'.
        """
        from pathlib import Path

        import scout.trading.auto_suspend as a
        import scout.trading.signals as sg

        assert "drawdown_baseline_at" in Path(sg.__file__).read_text("utf-8")
        assert "drawdown_baseline_at" in Path(a.__file__).read_text("utf-8")


class TestFailClosed:
    async def test_a_cap_with_no_baseline_refuses_admission(
        self, db, settings_factory
    ):
        """A cap configured with no baseline means the pilot was never properly
        activated. Admitting uncapped trades in that state is the unbounded run
        the cap exists to prevent, so this must fail CLOSED."""
        s = settings_factory(PAPER_LOSERS_PILOT_MAX_ENTRIES=200)
        await _set_baseline(db, None)
        assert await _pilot_admission_allowed(db, s) is False

    async def test_no_baseline_is_still_fine_when_uncapped(
        self, db, settings_factory
    ):
        """Fail-closed applies only when a cap is actually configured — an
        unset baseline must not block every non-pilot deployment."""
        s = settings_factory()
        await _set_baseline(db, None)
        assert await _pilot_admission_allowed(db, s) is True


class TestOvershootDetection:
    """The cap is check-then-act, not an atomic reservation. Concurrent writers
    can push the cohort past the cap. That must SURFACE, because a
    silently-oversized cohort invalidates the n the analysis is registered
    against — it must not be quietly absorbed."""

    async def test_an_over_cap_cohort_is_reported_not_silently_accepted(
        self, db, settings_factory
    ):
        from structlog.testing import capture_logs

        s = settings_factory(PAPER_LOSERS_PILOT_MAX_ENTRIES=5)
        await _set_baseline(db, "2026-08-08T00:00:00+00:00")
        await _add_entries(db, 7, opened_at="2026-08-08T01:00:00+00:00")  # 2 over

        with capture_logs() as logs:
            assert await _pilot_admission_allowed(db, s) is False

        exceeded = [e for e in logs if e.get("event") == "pilot_entry_cap_exceeded"]
        assert len(exceeded) == 1, (
            "an over-cap cohort must be reported exactly once at error level, "
            "not merely refused — refusal alone is indistinguishable from a "
            f"normal stop. got: {[e.get('event') for e in logs]}"
        )
        assert exceeded[0]["log_level"] == "error"
        assert exceeded[0]["overshoot"] == 2
        assert exceeded[0]["entries"] == 7

    async def test_exactly_at_cap_is_NOT_reported_as_overshoot(
        self, db, settings_factory
    ):
        """Discriminating control: at the cap is the normal terminal state and
        must not raise a false alarm. Without this, the test above passes even
        if the error fires on every refusal."""
        from structlog.testing import capture_logs

        s = settings_factory(PAPER_LOSERS_PILOT_MAX_ENTRIES=5)
        await _set_baseline(db, "2026-08-08T00:00:00+00:00")
        await _add_entries(db, 5, opened_at="2026-08-08T01:00:00+00:00")

        with capture_logs() as logs:
            assert await _pilot_admission_allowed(db, s) is False

        events = [e.get("event") for e in logs]
        assert "pilot_entry_cap_exceeded" not in events
        assert "pilot_entry_cap_reached" in events


class TestWiring:
    def test_the_guard_runs_before_open_trade_in_trade_losers(self):
        """Structural: the check is worthless if it sits after the open."""
        from pathlib import Path

        import scout.trading.signals as sg

        src = Path(sg.__file__).read_text("utf-8")
        start = src.index("async def trade_losers(")
        body = src[start : src.index("logger.info(\n            \"trade_losers_filtered\"", start)]
        guard = body.index("_pilot_admission_allowed")
        opened = body.index("await engine.open_trade(")
        assert guard < opened, "admission cap must be checked BEFORE opening"
