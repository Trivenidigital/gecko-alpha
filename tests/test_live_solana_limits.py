"""PR-S4: the lane's execution envelope, in one object.

Two things are being pinned down here.

**Every limit is mechanically enforced**, not documented. Each check gets a
test that drives the engine past the limit and asserts a NAMED failure, so a
limit that silently stops applying fails a test rather than quietly widening
the envelope.

**BOUNDED_AUTONOMOUS inherits the identical envelope.** The engine is built
from Settings and never sees the mode. That is the load-bearing claim of the
whole autonomy design — if the autonomous mode could carry its own limits,
"the transition is policy and configuration only" would be false — so it is
asserted directly by evaluating the same inputs under both modes and comparing
the reports.

Fail-closed is tested as its own axis: an input the engine could not read
produces a FAILED check, never a skipped one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from scout.config import Settings
from scout.live.solana.constants import SOL_MINT, USDC_MINT
from scout.live.solana.exceptions import SolanaLimitBreached
from scout.live.solana.jupiter_client import RouteStep, SolanaQuote
from scout.live.solana.limits import (
    FeeCeilings,
    LaneExposure,
    LimitsEngine,
    notional_usd_today,
)
from scout.live.solana.tx_inspector import VerificationReport

_LAMPORTS = 50_000_000
_OUT_RAW = 8_528_730  # 8.52873 USDC, inside the default [5, 10] band
_MIN_OUT_RAW = 8_443_443


def _quote(**overrides) -> SolanaQuote:
    fields = dict(
        input_mint=SOL_MINT,
        output_mint=USDC_MINT,
        in_amount=_LAMPORTS,
        out_amount=_OUT_RAW,
        other_amount_threshold=_MIN_OUT_RAW,
        slippage_bps=100,
        price_impact_pct="0.0001",
        swap_mode="ExactIn",
        route_plan=(
            RouteStep(
                label="Raydium",
                amm_key="58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2",
                input_mint=SOL_MINT,
                output_mint=USDC_MINT,
                in_amount=_LAMPORTS,
                out_amount=_OUT_RAW,
            ),
        ),
        context_slot=283_000_111,
        raw={},
    )
    fields.update(overrides)
    return SolanaQuote(**fields)


def _verification(**overrides) -> VerificationReport:
    fields = dict(
        passed=True,
        checks=(),
        message_sha256="0" * 64,
        fee_payer="7v54NWdBtkjuAFJrLGsS2SXnuk8nKam81mZJeeYxVFi9",
        num_required_signatures=1,
        recent_blockhash="11111111111111111111111111111111",
        jito_tip_lamports=100_000,
        jito_tip_destination="96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
        priority_fee_lamports=200,
        compute_unit_limit=200_000,
        compute_unit_price_micro_lamports=1_000,
        ata_rent_lamports=2_039_280,
        ata_create_count=1,
        total_fee_lamports=2_144_480,
        program_ids=(
            "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
            "11111111111111111111111111111111",
        ),
    )
    fields.update(overrides)
    return VerificationReport(**fields)


@pytest.fixture
def engine(settings_factory):
    def _make(**overrides) -> LimitsEngine:
        return LimitsEngine(settings_factory(**overrides))

    return _make


def _quote_report(engine_, *, quote=None, impact="0.01", exposure=None):
    return engine_.check_quote(
        quote or _quote(),
        amount_lamports=_LAMPORTS,
        price_impact_pct=Decimal(impact),
        exposure=exposure or LaneExposure(),
    )


def _failed(report) -> list[str]:
    return [c.name for c in report.failures]


# ======================================================================
# The baseline: a legitimate trade clears every check
# ======================================================================
def test_a_legitimate_trade_passes_every_check(engine):
    report = _quote_report(engine())
    assert report.passed, _failed(report)
    # A floor, not an exact count: the engine gains limits over time and this
    # test is about them all being evaluated, not about how many there are.
    assert len(report.checks) >= 11
    assert all(c.detail for c in report.checks), "every check must say what it proved"


def test_the_report_names_the_stage_and_the_failure(engine):
    report = _quote_report(engine(SOLANA_PILOT_MAX_ORDER_USD=6.0))
    assert report.stage == "quote"
    assert report.passed is False
    with pytest.raises(SolanaLimitBreached, match="per_trade_notional_within_band"):
        report.raise_if_failed()
    evidence = report.as_evidence()
    assert evidence["failed_checks"] == ["per_trade_notional_within_band"]
    # Passing checks are recorded too — the evidence shows what was PROVED,
    # not only what broke.
    assert len(evidence["checks"]) == len(report.checks)


# ======================================================================
# Capital: per-trade band and the daily cap
# ======================================================================
@pytest.mark.parametrize("out_raw", [4_990_000, 10_010_000])
def test_the_per_trade_band_binds_on_the_usdc_output(engine, out_raw):
    report = _quote_report(engine(), quote=_quote(out_amount=out_raw))
    assert _failed(report) == ["per_trade_notional_within_band"]


def test_the_daily_cap_counts_what_is_already_authorized(engine):
    """*** The limit that bounds a runaway rather than a single trade. ***

    Per-trade caps bound one mistake. The daily cap is what bounds a loop, and
    it has to count AUTHORIZED notional — a cap that waited for settlement
    would let a burst of in-flight trades straight through.
    """
    eng = engine(SOLANA_MAX_DAILY_NOTIONAL_USD=20.0)
    # 8.52 already on the book leaves room for one more.
    ok = _quote_report(eng, exposure=LaneExposure(notional_usd_today=Decimal("8.52")))
    assert ok.passed, _failed(ok)

    # 12.00 does not.
    breached = _quote_report(
        eng, exposure=LaneExposure(notional_usd_today=Decimal("12.00"))
    )
    assert _failed(breached) == ["daily_notional_within_cap"]
    detail = next(
        c.detail for c in breached.checks if c.name == "daily_notional_within_cap"
    )
    assert "12.00 already authorized today" in detail
    assert "cap 20.0 USD" in detail


def test_a_daily_cap_below_one_trade_is_refused_at_config_time(settings_factory):
    """A lane that refuses everything for a reason no message explains."""
    with pytest.raises(ValueError, match="SOLANA_MAX_DAILY_NOTIONAL_USD"):
        settings_factory(
            SOLANA_MAX_DAILY_NOTIONAL_USD=4.0, SOLANA_PILOT_MAX_ORDER_USD=10.0
        )


def test_notional_today_ignores_yesterday_and_keeps_unreadable_timestamps(engine):
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    rows = [
        ("5.00", now.isoformat()),
        ("7.00", (now - timedelta(days=1)).isoformat()),  # yesterday: excluded
        ("3.00", "not-a-timestamp"),  # unreadable stamp: COUNTS
    ]
    # 5 + 3, not 5 and not 15. An unreadable row that were dropped would raise
    # the amount the lane may still spend, and a cap that loosens itself on bad
    # data is not a cap.
    assert notional_usd_today(
        rows, unreadable_size_usd=Decimal("10"), now=now
    ) == Decimal("8.00")


def test_an_unreadable_size_counts_at_the_per_trade_maximum(engine):
    """*** The documented-vs-actual mismatch on a money cap. ***

    An unreadable SIZE used to contribute zero, which silently created
    headroom that had already been spent: two $40 authorizations total $80,
    but corrupt the second row and the day reads $40. It now counts at the
    largest the row could legitimately have been.
    """
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    rows = [("40.00", now.isoformat()), ("!!corrupt!!", now.isoformat())]

    assert notional_usd_today(
        rows, unreadable_size_usd=Decimal("40"), now=now
    ) == Decimal("80.00")
    # And NOT the old behaviour, which is the whole point of the test.
    assert notional_usd_today(
        rows, unreadable_size_usd=Decimal("40"), now=now
    ) != Decimal("40.00")


def test_a_corrupt_row_narrows_the_cap_rather_than_wedging_the_lane(engine):
    """Substitution, not refusal.

    Refusing outright on unreadable data would let one corrupt row block the
    lane until somebody edited the database. The conservative substitution
    keeps the cap meaningful AND the lane recoverable — there is still room
    under the cap for a trade the budget genuinely allows.
    """
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    eng = engine(SOLANA_MAX_DAILY_NOTIONAL_USD=100.0, SOLANA_PILOT_MAX_ORDER_USD=10.0)
    spent = notional_usd_today(
        [(None, now.isoformat()), ("garbage", now.isoformat())],
        unreadable_size_usd=Decimal("10"),
        now=now,
    )
    # None is a legitimate zero (NOT NULL is on size_usd, but be explicit);
    # only the unparseable string is substituted.
    assert spent == Decimal("10")
    assert _quote_report(eng, exposure=LaneExposure(notional_usd_today=spent)).passed


# ======================================================================
# Concurrency
# ======================================================================
def test_the_open_position_limit_counts_this_trade_as_one_more(engine):
    eng = engine(SOLANA_MAX_OPEN_POSITIONS=1)
    assert _quote_report(eng, exposure=LaneExposure(open_positions=0)).passed
    breached = _quote_report(eng, exposure=LaneExposure(open_positions=1))
    assert _failed(breached) == ["open_positions_within_limit"]


def test_the_concurrent_execution_limit_binds(engine):
    eng = engine(SOLANA_MAX_CONCURRENT_EXECUTIONS=1)
    breached = _quote_report(eng, exposure=LaneExposure(active_executions=1))
    assert _failed(breached) == ["concurrent_executions_within_limit"]


def test_a_zero_concurrency_limit_is_refused_at_config_time(settings_factory):
    """Zero is not 'unlimited'; DISABLED is how you stop the lane."""
    with pytest.raises(ValueError, match="SOLANA_MAX_OPEN_POSITIONS"):
        settings_factory(SOLANA_MAX_OPEN_POSITIONS=0)


# ======================================================================
# Allowlists: mint, route, program
# ======================================================================
def test_an_unlisted_output_mint_is_refused(engine):
    stranger = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"  # RAY, not USDC
    report = _quote_report(engine(), quote=_quote(output_mint=stranger))
    assert _failed(report) == ["output_mint_allowed"]


def test_an_unlisted_input_mint_is_refused(engine):
    report = _quote_report(engine(), quote=_quote(input_mint=USDC_MINT))
    assert "input_mint_allowed" in _failed(report)


def test_an_empty_mint_allowlist_is_refused_at_config_time(settings_factory):
    """Unlike the route allowlist, empty is NOT 'unrestricted' here.

    Reading it that way would turn a config typo into an any-token lane.
    """
    with pytest.raises(ValueError, match="SOLANA_ALLOWED_OUTPUT_MINTS"):
        settings_factory(SOLANA_ALLOWED_OUTPUT_MINTS="")


def test_the_route_allowlist_is_unrestricted_by_default_and_says_so(engine):
    """An evidence file must never imply a restriction that is not configured."""
    report = _quote_report(engine())
    detail = next(c.detail for c in report.checks if c.name == "route_labels_allowed")
    assert detail.startswith("unrestricted")
    assert "Raydium" in detail


def test_a_configured_route_allowlist_refuses_an_unlisted_amm(engine):
    eng = engine(SOLANA_ALLOWED_ROUTE_LABELS="Orca,Whirlpool")
    report = _quote_report(eng)  # the fixture routes through Raydium
    assert _failed(report) == ["route_labels_allowed"]
    detail = next(c.detail for c in report.checks if c.name == "route_labels_allowed")
    assert "NOT allowed: ['Raydium']" in detail


def test_a_configured_allowlist_refuses_a_route_it_cannot_see(engine):
    """An allowlist that passes what it cannot read is not an allowlist."""
    eng = engine(SOLANA_ALLOWED_ROUTE_LABELS="Raydium")
    report = _quote_report(eng, quote=_quote(route_plan=()))
    assert _failed(report) == ["route_labels_allowed"]


def test_an_unknown_program_id_is_refused(engine):
    report = engine().check_transaction(
        _verification(program_ids=("StrangeProgram1111111111111111111111111111",))
    )
    assert _failed(report) == ["programs_allowlisted"]


# ======================================================================
# Quote terms
# ======================================================================
def test_looser_slippage_than_approved_is_refused(engine):
    report = _quote_report(engine(), quote=_quote(slippage_bps=500))
    assert _failed(report) == ["slippage_within_ceiling"]


def test_price_impact_over_the_ceiling_is_refused(engine):
    report = _quote_report(engine(SOLANA_PILOT_MAX_PRICE_IMPACT_PCT=1.0), impact="2.0")
    assert _failed(report) == ["price_impact_within_ceiling"]


def test_a_quote_that_is_not_exact_in_is_refused(engine):
    """otherAmountThreshold is a minimum-output bound only under ExactIn."""
    report = _quote_report(engine(), quote=_quote(swap_mode="ExactOut"))
    assert "swap_mode_is_exact_in" in _failed(report)


def test_a_quote_for_a_different_amount_is_refused(engine):
    report = _quote_report(engine(), quote=_quote(in_amount=_LAMPORTS + 1))
    assert _failed(report) == ["quote_matches_requested_amount"]


# ======================================================================
# Fee ceilings, applied to what the BYTES carry
# ======================================================================
def test_a_tip_over_the_ceiling_is_refused(engine):
    # The REQUESTED tip has to come down with the ceiling or Settings refuses
    # the pair outright — Jupiter would be asked for a tip it could never pass.
    eng = engine(
        SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS=50_000,
        SOLANA_PILOT_JITO_TIP_LAMPORTS=50_000,
    )
    # Jupiter built a 100_000 tip regardless of what was requested.
    report = eng.check_transaction(_verification(jito_tip_lamports=100_000))
    assert "jito_tip_within_ceiling" in _failed(report)


def test_a_priority_fee_over_the_ceiling_is_refused(engine):
    eng = engine(SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS=100)
    report = eng.check_transaction(_verification(priority_fee_lamports=200))
    assert "priority_fee_within_ceiling" in _failed(report)


def test_the_total_fee_ceiling_catches_components_that_stack(engine):
    """Each component inside its own ceiling, the sum over the aggregate."""
    eng = engine(
        SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS=700_000,
        SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS=700_000,
        SOLANA_PILOT_JITO_TIP_LAMPORTS=700_000,
        # The lowest aggregate Settings will accept for those components.
        SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS=1_405_000,
        SOLANA_PILOT_MAX_ATA_CREATES=0,
    )
    report = eng.check_transaction(
        _verification(
            priority_fee_lamports=700_000,  # exactly at its ceiling
            jito_tip_lamports=700_000,  # exactly at its ceiling
            total_fee_lamports=1_500_000,  # over the aggregate
            ata_create_count=0,
            ata_rent_lamports=0,
        )
    )
    assert _failed(report) == ["total_fee_within_ceiling"]


def test_too_many_account_creates_are_refused(engine):
    """ATA rent is real money and each create is another rent deposit."""
    eng = engine(SOLANA_PILOT_MAX_ATA_CREATES=1)
    report = eng.check_transaction(_verification(ata_create_count=2))
    assert "ata_creates_within_limit" in _failed(report)


def test_the_engine_and_the_inspector_agree_on_fee_ceilings(settings_factory):
    """*** The anti-drift test. ***

    ``tx_inspector`` enforces the same three ceilings and must keep doing so —
    it is the gate between remotely-built bytes and the signing key, and it has
    to be safe standalone. Both read them through ``FeeCeilings.from_settings``
    so they cannot be changed in one place only.
    """
    settings = settings_factory(
        SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS=123_456,
        SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS=234_567,
        SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS=9_000_000,
    )
    ceilings = FeeCeilings.from_settings(settings)
    assert (
        ceilings.priority_fee_lamports
        == settings.SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS
    )
    assert ceilings.jito_tip_lamports == settings.SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS
    assert ceilings.total_fee_lamports == settings.SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS
    assert LimitsEngine(settings).fee_ceilings == ceilings


# ======================================================================
# Balance cover
# ======================================================================
def test_balance_must_cover_the_swap_the_fees_and_the_headroom(engine):
    eng = engine(SOLANA_BALANCE_HEADROOM_PCT=10.0)
    required, with_headroom = eng.required_lamports(
        amount_lamports=_LAMPORTS, total_fee_lamports=2_144_480
    )
    assert required == _LAMPORTS + 2_144_480
    assert with_headroom == int(required * Decimal("1.10"))

    assert eng.check_balance(
        sol_lamports=with_headroom,
        amount_lamports=_LAMPORTS,
        total_fee_lamports=2_144_480,
    ).passed
    short = eng.check_balance(
        sol_lamports=with_headroom - 1,
        amount_lamports=_LAMPORTS,
        total_fee_lamports=2_144_480,
    )
    assert _failed(short) == ["sol_covers_swap_fees_and_headroom"]


def test_the_headroom_percentage_is_configuration_not_a_constant(engine):
    """It was an inline 1.10. A threshold in code cannot be tuned per venue."""
    _, tight = engine(SOLANA_BALANCE_HEADROOM_PCT=0.0).required_lamports(
        amount_lamports=1_000, total_fee_lamports=0
    )
    _, loose = engine(SOLANA_BALANCE_HEADROOM_PCT=50.0).required_lamports(
        amount_lamports=1_000, total_fee_lamports=0
    )
    assert (tight, loose) == (1_000, 1_500)


# ======================================================================
# Freshness, re-asked after the approval prompt
# ======================================================================
def test_a_stale_quote_is_refused_even_though_the_blockhash_is_fine(engine):
    """The on-chain minimum-output bound protects the trade. This protects
    the operator's INTENT: they authorized a price, not just a transaction."""
    report = engine(SOLANA_MAX_QUOTE_AGE_SEC=60.0).check_freshness(
        quote_age_sec=61.0,
        current_block_height=283_000_100,
        last_valid_block_height=283_000_500,
    )
    assert _failed(report) == ["quote_age_within_limit"]


def test_a_blockhash_inside_the_safety_margin_is_refused(engine):
    eng = engine(SOLANA_PILOT_BLOCKHASH_SAFETY_MARGIN_BLOCKS=15)
    # 10 blocks left, inside the 15-block margin.
    report = eng.check_freshness(
        quote_age_sec=1.0,
        current_block_height=283_000_490,
        last_valid_block_height=283_000_500,
    )
    assert _failed(report) == ["blockhash_within_safety_margin"]


# ======================================================================
# Fail-closed: unknown is a FAILURE, never a skip
# ======================================================================
@pytest.mark.parametrize(
    "kwargs,expected",
    [
        (
            {
                "quote_age_sec": None,
                "current_block_height": 1,
                "last_valid_block_height": 2,
            },
            "quote_age_within_limit",
        ),
        (
            {
                "quote_age_sec": 1.0,
                "current_block_height": None,
                "last_valid_block_height": 2,
            },
            "blockhash_within_safety_margin",
        ),
        (
            {
                "quote_age_sec": 1.0,
                "current_block_height": 1,
                "last_valid_block_height": None,
            },
            "blockhash_within_safety_margin",
        ),
    ],
)
def test_an_unreadable_freshness_input_fails_closed(engine, kwargs, expected):
    report = engine().check_freshness(**kwargs)
    assert expected in _failed(report)


def test_an_unreadable_balance_fails_closed(engine):
    report = engine().check_balance(
        sol_lamports=None, amount_lamports=_LAMPORTS, total_fee_lamports=0
    )
    assert _failed(report) == ["sol_covers_swap_fees_and_headroom"]
    assert "could not be read" in report.failures[0].detail


def test_an_unparseable_price_impact_fails_closed(engine):
    """The lane's converter returns a 100% sentinel for a field it cannot read.

    A field we could not parse is not a zero-impact route.
    """
    from scout.live.solana_lane import quote_price_impact_pct

    sentinel = quote_price_impact_pct(_quote(price_impact_pct="not-a-number"))
    assert sentinel == Decimal("100")
    report = _quote_report(engine(), impact=str(sentinel))
    assert _failed(report) == ["price_impact_within_ceiling"]


# ======================================================================
# *** BOUNDED_AUTONOMOUS INHERITS THE IDENTICAL ENVELOPE ***
# ======================================================================
def _both_modes(**overrides) -> tuple[Settings, Settings]:
    base = dict(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        **overrides,
    )
    return (
        Settings(**base, SOLANA_MODE="SUPERVISED_LIVE"),
        Settings(
            **base,
            SOLANA_MODE="BOUNDED_AUTONOMOUS",
            SOLANA_BOUNDED_AUTONOMOUS_ENABLED=True,
        ),
    )


def test_the_declared_envelope_is_identical_across_modes():
    supervised, autonomous = _both_modes()
    assert (
        LimitsEngine(supervised).declared_limits()
        == LimitsEngine(autonomous).declared_limits()
    )


@pytest.mark.parametrize(
    "quote_kwargs,exposure",
    [
        ({}, LaneExposure()),  # a clean trade
        ({"out_amount": 10_010_000}, LaneExposure()),  # over the band
        ({"slippage_bps": 500}, LaneExposure()),  # over slippage
        ({}, LaneExposure(open_positions=1)),  # at the position limit
        ({}, LaneExposure(notional_usd_today=Decimal("24"))),  # near the daily cap
    ],
)
def test_every_quote_verdict_is_identical_across_modes(quote_kwargs, exposure):
    """*** The claim the whole autonomy design rests on. ***

    If the autonomous mode could carry its own limits, "the transition is
    policy and configuration only" would be false. Same inputs, same checks,
    same verdicts — the engine never sees the mode.
    """
    supervised, autonomous = _both_modes()
    quote = _quote(**quote_kwargs)
    args = dict(
        amount_lamports=_LAMPORTS,
        price_impact_pct=Decimal("0.01"),
        exposure=exposure,
    )
    a = LimitsEngine(supervised).check_quote(quote, **args)
    b = LimitsEngine(autonomous).check_quote(quote, **args)
    assert a == b


def test_every_transaction_verdict_is_identical_across_modes():
    supervised, autonomous = _both_modes(
        SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS=50_000,
        SOLANA_PILOT_JITO_TIP_LAMPORTS=50_000,
    )
    report = _verification(jito_tip_lamports=100_000)
    a = LimitsEngine(supervised).check_transaction(report)
    b = LimitsEngine(autonomous).check_transaction(report)
    assert a == b
    assert a.passed is False  # and the shared verdict is the refusing one


def test_the_engine_holds_no_reference_to_the_mode():
    """Structural, not behavioural: there is nothing here to branch on.

    An engine that never receives the mode cannot diverge on it, whatever a
    future edit does inside a check.
    """
    import ast
    import inspect

    import scout.live.solana.limits as module

    tree = ast.parse(inspect.getsource(module))
    referenced = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert "SOLANA_MODE" not in referenced
    assert "SOLANA_BOUNDED_AUTONOMOUS_ENABLED" not in referenced
