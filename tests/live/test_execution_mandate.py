"""The cross-venue execution mandate.

Each lock is asserted to refuse INDEPENDENTLY — with every other lock satisfied —
because a gate that only refuses when several things are wrong at once is a gate
that permits when one thing is wrong on its own.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from scout.config import Settings
from scout.db import Database
from scout.live.intent import TradeIntent
from scout.live.mandate import (
    EXECUTING_MODES,
    ExecutionMandate,
    MandateRefused,
)

_T0 = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)

#: Every lock satisfied. Individual tests override ONE key, so a failure names
#: exactly which lock did the refusing.
_OPEN = dict(
    LIVE_EXECUTION_MANDATE_MODE="SUPERVISED_LIVE",
    LIVE_EXECUTION_MANDATE_ENABLED=True,
    LIVE_EXECUTION_MANDATE_FAMILIES="cex",
    LIVE_EXECUTION_MANDATE_VENUES="binance,kraken",
    LIVE_EXECUTION_MANDATE_PER_TRADE_MAX_USD=D("100"),
    LIVE_EXECUTION_MANDATE_DAILY_MAX_USD=D("500"),
    LIVE_EXECUTION_MANDATE_MAX_OPEN_POSITIONS=3,
)


def _settings(**overrides) -> Settings:
    base = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        **_OPEN,
    )
    base.update(overrides)
    return Settings(**base)


def _intent(**overrides) -> TradeIntent:
    kw = dict(
        strategy_id="first_signal",
        decision_id="paper-1",
        created_at=_T0,
        expires_at=_T0 + timedelta(minutes=5),
        execution_deadline=_T0 + timedelta(minutes=5),
        mode="SUPERVISED_LIVE",
        policy_version="engine-dispatch-v1",
        venue_family="cex",
        preferred_venue="binance",
        base_asset="BTC",
        quote_asset="USDT",
        side="buy",
        exact_quantity=D("100"),
        quantity_denomination="quote",
        maximum_notional=D("100"),
        order_type="market",
        maximum_slippage_bps=50,
        maximum_price_impact_bps=50,
    )
    kw.update(overrides)
    return TradeIntent(**kw)


def _mandate(db=None, **overrides) -> ExecutionMandate:
    return ExecutionMandate(settings=_settings(**overrides), db=db)


# ---------------------------------------------------------------------------


class TestDefaultsRefuse:
    def test_an_unconfigured_mandate_permits_nothing(self):
        """*** THE MOST IMPORTANT TEST IN THE FILE. ***

        Constructed from stock Settings — no mandate keys set at all — the gate
        must be closed. 'Nothing was restricted' must never mean 'everything is
        permitted'.
        """
        s = Settings(
            TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k"
        )
        mandate = ExecutionMandate(settings=s)
        assert mandate.mode == "DISABLED"
        assert mandate.is_active is False
        with pytest.raises(MandateRefused) as exc:
            mandate.precheck()
        assert exc.value.gate == "mode"

    def test_a_mandate_with_no_settings_object_at_all_refuses(self):
        """`LiveEngine` default-constructs the mandate from `config._s`, which can
        be None. Every getattr default must land on the refusing value."""
        mandate = ExecutionMandate(settings=None)
        assert mandate.is_active is False
        with pytest.raises(MandateRefused):
            mandate.precheck()

    @pytest.mark.parametrize("mode", ["DISABLED", "SIMULATION_ONLY"])
    def test_non_executing_modes_are_refused(self, mode):
        with pytest.raises(MandateRefused) as exc:
            _mandate(LIVE_EXECUTION_MANDATE_MODE=mode).precheck()
        assert exc.value.gate == "mode"

    def test_the_executing_modes_are_exactly_two(self):
        """Membership, not `!= DISABLED`. A mode added to the Literal later is
        refused until someone deliberately adds it here too."""
        assert EXECUTING_MODES == ("SUPERVISED_LIVE", "BOUNDED_AUTONOMOUS")

    def test_the_mode_alone_does_not_authorize(self):
        with pytest.raises(MandateRefused) as exc:
            _mandate(LIVE_EXECUTION_MANDATE_ENABLED=False).precheck()
        assert exc.value.gate == "enable_flag"


class TestEnvelope:
    @pytest.mark.parametrize(
        "key",
        [
            "LIVE_EXECUTION_MANDATE_PER_TRADE_MAX_USD",
            "LIVE_EXECUTION_MANDATE_DAILY_MAX_USD",
            "LIVE_EXECUTION_MANDATE_MAX_OPEN_POSITIONS",
        ],
    )
    def test_each_limit_must_be_set(self, key):
        with pytest.raises(MandateRefused) as exc:
            _mandate(**{key: 0}).precheck()
        assert exc.value.gate == "envelope"
        assert key in exc.value.message

    def test_a_negative_limit_is_refused(self):
        with pytest.raises(MandateRefused) as exc:
            _mandate(LIVE_EXECUTION_MANDATE_PER_TRADE_MAX_USD=D("-1")).precheck()
        assert exc.value.gate == "envelope"

    def test_pydantic_already_refuses_a_non_finite_limit_in_settings(self):
        """The outer layer. Recorded so the inner guard below is understood as
        defence-in-depth rather than as the only thing standing there."""
        import pydantic

        for key in (
            "LIVE_EXECUTION_MANDATE_PER_TRADE_MAX_USD",
            "LIVE_EXECUTION_MANDATE_DAILY_MAX_USD",
        ):
            for bad in (D("Infinity"), D("NaN")):
                with pytest.raises(pydantic.ValidationError):
                    _settings(**{key: bad})

    @pytest.mark.parametrize("bad", [D("Infinity"), D("-Infinity"), D("NaN")])
    @pytest.mark.parametrize(
        "key",
        [
            "LIVE_EXECUTION_MANDATE_PER_TRADE_MAX_USD",
            "LIVE_EXECUTION_MANDATE_DAILY_MAX_USD",
        ],
    )
    def test_a_non_finite_limit_is_refused_even_from_a_non_settings_object(
        self, key, bad
    ):
        """*** Decimal("Infinity") > 0 is True, and NaN raises on comparison. ***

        An infinite 'maximum' satisfies every positivity guard and then compares
        greater than any size — a field named maximum that imposes no maximum.
        NaN raises InvalidOperation, an ArithmeticError rather than a ValueError,
        so it escapes a caller catching (ValueError, TypeError).

        `ExecutionMandate` accepts ANY settings object — `LiveEngine` passes
        `config._s`, tests pass namespaces, and a subclass could override a
        field — so pydantic's validation is not reachable on every path and the
        mandate does its own check.
        """
        cfg = SimpleNamespace(**{**_OPEN, key: bad})
        with pytest.raises(MandateRefused) as exc:
            ExecutionMandate(settings=cfg).precheck()
        assert exc.value.gate == "envelope"
        assert key in exc.value.message

    def test_an_unparseable_limit_is_refused_rather_than_crashing(self):
        cfg = SimpleNamespace(
            **{**_OPEN, "LIVE_EXECUTION_MANDATE_DAILY_MAX_USD": "not-a-number"}
        )
        with pytest.raises(MandateRefused) as exc:
            ExecutionMandate(settings=cfg).precheck()
        assert exc.value.gate == "envelope"

    def test_a_per_trade_cap_above_the_daily_cap_is_refused(self):
        """One trade able to exhaust more than a day's budget means the daily cap
        bounds nothing."""
        with pytest.raises(MandateRefused) as exc:
            _mandate(
                LIVE_EXECUTION_MANDATE_PER_TRADE_MAX_USD=D("600"),
                LIVE_EXECUTION_MANDATE_DAILY_MAX_USD=D("500"),
            ).precheck()
        assert exc.value.gate == "envelope"

    def test_a_configured_envelope_is_returned(self):
        env = _mandate().precheck()
        assert env.per_trade_max_notional_usd == D("100")
        assert env.daily_max_notional_usd == D("500")
        assert env.max_open_positions == 3


class TestAuthorize:
    async def test_the_happy_path_returns_a_decision(self):
        decision = await _mandate().authorize(_intent(), at=_T0)
        assert decision.mode == "SUPERVISED_LIVE"
        assert decision.venue == "binance"
        assert decision.intent_hash == _intent().intent_hash
        # SUPERVISED_LIVE does not consult the ledger, so there is no count.
        assert decision.supervised_reconciled is None

    async def test_precheck_is_a_strict_subset_of_authorize(self):
        """precheck() exists so an inactive mandate refuses before the caller
        does anything with a side effect. If it could pass where authorize()
        fails on the same gates, that would be a second, weaker gate."""
        mandate = _mandate(LIVE_EXECUTION_MANDATE_ENABLED=False)
        with pytest.raises(MandateRefused) as pre:
            mandate.precheck()
        with pytest.raises(MandateRefused) as full:
            await mandate.authorize(_intent(), at=_T0)
        assert pre.value.gate == full.value.gate == "enable_flag"

    async def test_a_tampered_intent_is_refused_before_any_other_gate(self):
        """Every gate below reasons about the intent's terms. If the hash does
        not match them, those terms are unauthenticated and no gate result means
        anything."""
        intent = _intent()
        object.__setattr__(intent, "maximum_notional", D("999999"))
        with pytest.raises(MandateRefused) as exc:
            await _mandate().authorize(intent, at=_T0)
        assert exc.value.gate == "integrity"

    async def test_an_exit_is_refused_rather_than_approved(self):
        """*** The mandate bounds OPENING exposure and never closing it. ***

        Returning permitted=True for a reduce_only intent would read as though
        the mandate authorized exits, and the next person would wire the closer
        through it — recreating at runtime the buy-only state
        `LiveEngine.__init__` already refuses to boot into.
        """
        with pytest.raises(MandateRefused) as exc:
            await _mandate().authorize(
                _intent(reduce_only=True, position_id="lt-1"), at=_T0
            )
        assert exc.value.gate == "scope"

    async def test_an_unlisted_venue_family_is_refused(self):
        with pytest.raises(MandateRefused) as exc:
            await _mandate(LIVE_EXECUTION_MANDATE_FAMILIES="dex").authorize(
                _intent(), at=_T0
            )
        assert exc.value.gate == "venue"

    async def test_an_unlisted_venue_is_refused(self):
        with pytest.raises(MandateRefused) as exc:
            await _mandate(LIVE_EXECUTION_MANDATE_VENUES="kraken").authorize(
                _intent(), at=_T0
            )
        assert exc.value.gate == "venue"

    async def test_an_empty_allowlist_permits_nothing(self):
        """Empty means nothing is allowed, never everything. A settings typo
        closes the gate rather than opening it."""
        with pytest.raises(MandateRefused) as exc:
            await _mandate(LIVE_EXECUTION_MANDATE_VENUES="").authorize(
                _intent(), at=_T0
            )
        assert exc.value.gate == "venue"

    async def test_whitespace_and_casing_in_the_allowlist_are_handled(self):
        await _mandate(LIVE_EXECUTION_MANDATE_VENUES=" binance , kraken ").authorize(
            _intent(), at=_T0
        )

    async def test_an_expired_intent_is_refused(self):
        with pytest.raises(MandateRefused) as exc:
            await _mandate().authorize(_intent(), at=_T0 + timedelta(minutes=6))
        assert exc.value.gate == "expiry"

    async def test_an_intent_minted_under_a_different_mode_is_refused(self):
        """The mode is one of the intent's hashed terms. Executing a
        SUPERVISED_LIVE intent under BOUNDED_AUTONOMOUS would be executing
        something nobody authorized in that regime."""
        with pytest.raises(MandateRefused) as exc:
            await _mandate(LIVE_EXECUTION_MANDATE_MODE="BOUNDED_AUTONOMOUS").authorize(
                _intent(mode="SUPERVISED_LIVE"), at=_T0
            )
        assert exc.value.gate == "mode"

    async def test_an_intent_larger_than_the_per_trade_cap_is_refused(self):
        with pytest.raises(MandateRefused) as exc:
            await _mandate().authorize(
                _intent(exact_quantity=D("101"), maximum_notional=D("101")), at=_T0
            )
        assert exc.value.gate == "envelope"

    async def test_a_naive_timestamp_is_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            await _mandate().authorize(_intent(), at=datetime(2026, 8, 2, 12, 0))


class TestSupervisedHistory:
    """BOUNDED_AUTONOMOUS is the mode a flag cannot reach."""

    _AUTO = dict(
        LIVE_EXECUTION_MANDATE_MODE="BOUNDED_AUTONOMOUS",
        LIVE_EXECUTION_MANDATE_MIN_SUPERVISED_EXECUTIONS=2,
    )

    async def test_the_ledger_settles_the_claim_not_the_config(self, tmp_path):
        db = Database(str(tmp_path / "s.db"))
        await db.initialize()
        try:
            mandate = _mandate(db, **self._AUTO)
            intent = _intent(mode="BOUNDED_AUTONOMOUS")
            with pytest.raises(MandateRefused) as exc:
                await mandate.authorize(intent, at=_T0)
            assert exc.value.gate == "supervised_history"
            assert "there are 0" in exc.value.message
            assert "cannot be satisfied by configuration" in exc.value.message
        finally:
            await db.close()

    async def test_recorded_supervised_rows_satisfy_it(self, tmp_path):
        db = Database(str(tmp_path / "s.db"))
        await db.initialize()
        try:
            await _seed_supervised_cex(db, 2)
            decision = await _mandate(db, **self._AUTO).authorize(
                _intent(mode="BOUNDED_AUTONOMOUS"), at=_T0
            )
            assert decision.supervised_reconciled == 2
        finally:
            await db.close()

    async def test_rows_without_a_mandate_mode_do_not_count(self, tmp_path):
        """*** Pre-mandate history must not satisfy the promotion bar. ***

        `mandate_mode` is NULL on every row written before this gate existed. If
        those counted, the bar would be met by executions that were never
        mandate-gated — which is precisely the claim the count exists to refuse.
        """
        db = Database(str(tmp_path / "s.db"))
        await db.initialize()
        try:
            await _seed_supervised_cex(db, 5, mandate_mode=None)
            with pytest.raises(MandateRefused) as exc:
                await _mandate(db, **self._AUTO).authorize(
                    _intent(mode="BOUNDED_AUTONOMOUS"), at=_T0
                )
            assert "there are 0" in exc.value.message
        finally:
            await db.close()

    async def test_rows_that_never_completed_do_not_count(self, tmp_path):
        """*** A POSITION WHOSE ACCOUNTING IS UNRESOLVED IS NOT SUPERVISED HISTORY. ***

        `entry_fill_qty IS NOT NULL` alone counts a row whose ENTRY filled and whose
        EXIT then failed: `live_evaluator` sets such a row to
        `needs_manual_review` and leaves `entry_fill_qty` and `mandate_mode` intact.
        That is a trade that already paged the operator — the CEX analogue of a
        Solana swap stuck at `finalized`, which the dex branch has always refused to
        count. Reachable with no DB surgery at all.
        """
        db = Database(str(tmp_path / "s.db"))
        await db.initialize()
        try:
            await _seed_supervised_cex(db, 3, status="needs_manual_review")
            with pytest.raises(MandateRefused) as exc:
                await _mandate(db, **self._AUTO).authorize(
                    _intent(mode="BOUNDED_AUTONOMOUS"), at=_T0
                )
            assert "there are 0" in exc.value.message
        finally:
            await db.close()

    async def test_open_rows_do_not_count(self, tmp_path):
        db = Database(str(tmp_path / "s.db"))
        await db.initialize()
        try:
            await _seed_supervised_cex(db, 3, status="open")
            with pytest.raises(MandateRefused) as exc:
                await _mandate(db, **self._AUTO).authorize(
                    _intent(mode="BOUNDED_AUTONOMOUS"), at=_T0
                )
            assert "there are 0" in exc.value.message
        finally:
            await db.close()

    async def test_history_earned_on_one_venue_does_not_promote_another(self, tmp_path):
        """*** A FLAG MUST NOT MOVE EARNED AUTONOMY ONTO UNEARNED GROUND. ***

        Counting per FAMILY means three supervised Binance fills promote Kraken —
        a venue with no supervised history whatsoever — the moment an operator adds
        "kraken" to the allowlist. The count would then be settled by the ledger for
        a venue nobody traded, which is the precondition being satisfiable by
        configuration after all.
        """
        db = Database(str(tmp_path / "s.db"))
        await db.initialize()
        try:
            await _seed_supervised_cex(db, 3, venue="binance")
            # Binance is promotable...
            decision = await _mandate(db, **self._AUTO).authorize(
                _intent(mode="BOUNDED_AUTONOMOUS", preferred_venue="binance"), at=_T0
            )
            assert decision.supervised_reconciled == 3
            # ...and Kraken, on the same allowlist and the same family, is not.
            with pytest.raises(MandateRefused) as exc:
                await _mandate(db, **self._AUTO).authorize(
                    _intent(mode="BOUNDED_AUTONOMOUS", preferred_venue="kraken"),
                    at=_T0,
                )
            assert exc.value.gate == "supervised_history"
            assert "there are 0" in exc.value.message
            assert "'kraken'" in exc.value.message
        finally:
            await db.close()

    async def test_rows_with_no_entry_fill_do_not_count(self, tmp_path):
        """An order that never filled is not a completed supervised execution."""
        db = Database(str(tmp_path / "s.db"))
        await db.initialize()
        try:
            await _seed_supervised_cex(db, 3, entry_fill_qty=None)
            with pytest.raises(MandateRefused) as exc:
                await _mandate(db, **self._AUTO).authorize(
                    _intent(mode="BOUNDED_AUTONOMOUS"), at=_T0
                )
            assert "there are 0" in exc.value.message
        finally:
            await db.close()

    @pytest.mark.parametrize("bad", [float("inf"), D("NaN"), "three", None, [3]])
    async def test_an_unreadable_threshold_refuses_rather_than_escaping(
        self, tmp_path, bad
    ):
        """*** THE SIBLING OF THE ENVELOPE FIX, WHICH THE ENVELOPE FIX MISSED. ***

        `int()` raises OverflowError on inf and InvalidOperation on NaN — both
        ArithmeticError, neither a ValueError — and plain ValueError on a
        non-numeric string, which is not exotic. `_dispatch_live` catches only
        `MandateRefused` around `authorize`, so any of these escapes
        `on_paper_trade_opened` with no reject row and no log: the gate reads as a
        crash rather than a refusal.
        """
        db = Database(str(tmp_path / "s.db"))
        await db.initialize()
        try:
            cfg = SimpleNamespace(
                **{
                    **_OPEN,
                    "LIVE_EXECUTION_MANDATE_MODE": "BOUNDED_AUTONOMOUS",
                    "LIVE_EXECUTION_MANDATE_MIN_SUPERVISED_EXECUTIONS": bad,
                }
            )
            mandate = ExecutionMandate(settings=cfg, db=db)
            mandate.precheck()  # the envelope is fine; only the bar is unreadable
            with pytest.raises(MandateRefused) as exc:
                await mandate.authorize(_intent(mode="BOUNDED_AUTONOMOUS"), at=_T0)
            assert exc.value.gate == "supervised_history"
        finally:
            await db.close()

    async def test_a_threshold_below_one_is_refused(self, tmp_path):
        """Autonomous promotion with no recorded history at all is not a
        configuration anyone may choose."""
        db = Database(str(tmp_path / "s.db"))
        await db.initialize()
        try:
            with pytest.raises(MandateRefused) as exc:
                await _mandate(
                    db,
                    LIVE_EXECUTION_MANDATE_MODE="BOUNDED_AUTONOMOUS",
                    LIVE_EXECUTION_MANDATE_MIN_SUPERVISED_EXECUTIONS=0,
                ).authorize(_intent(mode="BOUNDED_AUTONOMOUS"), at=_T0)
            assert exc.value.gate == "supervised_history"
        finally:
            await db.close()

    async def test_no_database_refuses_rather_than_counting_zero_silently(self):
        with pytest.raises(MandateRefused) as exc:
            await _mandate(None, **self._AUTO).authorize(
                _intent(mode="BOUNDED_AUTONOMOUS"), at=_T0
            )
        assert exc.value.gate == "supervised_history"
        assert "no database" in exc.value.message

    async def test_an_unreadable_ledger_refuses_rather_than_reading_as_empty(
        self, tmp_path
    ):
        """*** 'The database could not answer' and 'the answer is none' are the
        same verdict but not the same fact. ***

        Conflating them is how a promotion is granted against a ledger nobody
        read. Here the table is dropped out from under the query.
        """
        db = Database(str(tmp_path / "s.db"))
        await db.initialize()
        try:
            await db._conn.execute("DROP TABLE live_trades")
            await db._conn.commit()
            with pytest.raises(MandateRefused) as exc:
                await _mandate(db, **self._AUTO).authorize(
                    _intent(mode="BOUNDED_AUTONOMOUS"), at=_T0
                )
            assert exc.value.gate == "supervised_history"
            assert "Refusing rather than" in exc.value.message
        finally:
            await db.close()

    async def test_a_family_with_no_ledger_cannot_reach_autonomy(self, tmp_path):
        db = Database(str(tmp_path / "s.db"))
        await db.initialize()
        try:
            mandate = ExecutionMandate(
                settings=_settings(
                    LIVE_EXECUTION_MANDATE_FAMILIES="cex,futures",
                    LIVE_EXECUTION_MANDATE_VENUES="binance",
                    **self._AUTO,
                ),
                db=db,
            )
            # `futures` is not a TradeIntent venue_family, so the refusal is
            # reachable only through the private counter — asserted directly
            # rather than through a family the type system forbids.
            with pytest.raises(MandateRefused) as exc:
                await mandate._count_supervised("futures", venue="binance")
            assert exc.value.gate == "supervised_history"
        finally:
            await db.close()


class TestPinnedToTheSolanaLane:
    def test_the_solana_constants_match_the_lanes_own(self):
        """The dex-side count is REIMPLEMENTED here rather than imported, because
        nothing in scout/ may import scout.live.solana_lane. Duplication is the
        price of that guarantee; this test is what stops the two drifting."""
        from scout.live import mandate as m
        from scout.live import solana_lane as lane

        assert m.SOLANA_STATE_RECONCILED == lane.STATE_RECONCILED
        assert m.MODE_SUPERVISED_LIVE == lane.MODE_SUPERVISED_LIVE
        assert m.MODE_BOUNDED_AUTONOMOUS == lane.MODE_BOUNDED_AUTONOMOUS


# ---------------------------------------------------------------------------


async def _seed_supervised_cex(
    db: Database,
    count: int,
    *,
    mandate_mode: str | None = "SUPERVISED_LIVE",
    entry_fill_qty: str | None = "1.0",
    status: str = "closed_tp",
    venue: str = "binance",
) -> None:
    """Seed `count` completed supervised CEX executions.

    `live_trades.paper_trade_id` is NOT NULL REFERENCES paper_trades(id), so each
    row needs a real parent.
    """
    assert db._conn is not None
    for i in range(count):
        # paper_trades is UNIQUE on (token_id, signal_type, opened_at), so each
        # seeded parent needs a distinct opened_at.
        await db._conn.execute(
            "INSERT INTO paper_trades (token_id, symbol, name, chain, signal_type, "
            "signal_data, entry_price, amount_usd, quantity, tp_pct, sl_pct, "
            "tp_price, sl_price, status, opened_at) "
            "VALUES ('c','SYM','N','eth','first_signal','{}',1.0,100.0,100.0,"
            "20.0,10.0,1.2,0.9,'open',?)",
            (f"2026-08-01T00:00:{i:02d}",),
        )
        cur = await db._conn.execute("SELECT last_insert_rowid()")
        pt_id = (await cur.fetchone())[0]
        await db._conn.execute(
            "INSERT INTO live_trades (paper_trade_id, coin_id, symbol, venue, pair, "
            "signal_type, size_usd, status, created_at, client_order_id, "
            "entry_fill_qty, mandate_mode) "
            "VALUES (?, 'c', 'SYM', ?, 'SYMUSDT', 'first_signal', '100', "
            "?, '2026-08-01T00:00:00', ?, ?, ?)",
            (pt_id, venue, status, f"cid-{venue}-{i}", entry_fill_qty, mandate_mode),
        )
    await db._conn.commit()
