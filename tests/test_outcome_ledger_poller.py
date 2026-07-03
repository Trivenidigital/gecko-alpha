"""Enrollment forward-poller for the signal outcome ledger (P0, edge-audit).

HTTP-facing tests (aiohttp + aioresponses) for scout.outcome_ledger's
poll_enrollments: the per-cycle lane that prices enrolled tokens so in-DB
labeling can reach tokens the tracked lanes never carry (gated-out
micro-caps, dex:-namespace ids).

Lives separately from tests/test_outcome_ledger.py because importing aiohttp
aborts on Windows dev boxes (OPENSSL_Applink); CI runs this file on Linux.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from scout.db import Database
from scout.outcome_ledger import (
    active_enrollments,
    label_pending,
    poll_enrollments,
    price_from_cache,
    record_emission,
)
from scout.ratelimit import coingecko_limiter

SIMPLE_PRICE_PATTERN = re.compile(r"https://api\.coingecko\.com/api/v3/simple/price")


@pytest.fixture(autouse=True)
async def _clear_rate_limit():
    await coingecko_limiter.reset()
    yield
    await coingecko_limiter.reset()


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "poller.db"))
    await database.initialize()
    yield database
    await database.close()


def _ledger_settings(settings_factory, **overrides):
    defaults = dict(
        LEDGER_ENABLED=True,
        LEDGER_GATED_OUT_SAMPLE_RATE=25,
    )
    defaults.update(overrides)
    return settings_factory(**defaults)


async def _enroll_via_gated_out(
    db, settings, token_id: str, *, price=None, emitted_at: str | None = None
) -> None:
    """Record a gated_out_sample. NOTE (LIVENESS coverage,
    fix/ledger-coverage-gated-enrollment): this ENROLLS *token_id* whenever it
    has NO FRESH price observation (stale/missing price_cache AND stale/missing
    volume_history_cg) — regardless of shape. So an untracked dex: id AND a
    dead/untracked CG-slug both enroll. Only tokens with a fresh in-DB price are
    'covered' and skip enrollment; tests that need a CG-namespace enrollment for
    a token that WOULD be fresh-covered seed it via :func:`_seed_enrollment`."""
    row_id = await record_emission(
        db,
        settings,
        kind="gated_out_sample",
        token_id=token_id,
        surface="gainers_early",
        price=price,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts={"reason": "below_min_mcap"},
        emitted_at=emitted_at,
    )
    assert row_id is not None


async def _seed_enrollment(db, token_id: str, namespace: str = "cg") -> None:
    """Insert a ledger_enrollments row directly (bypassing the coverage-gated
    record_emission decision) so the poller mechanism can be exercised for the
    CG namespace, which no longer auto-enrolls under coverage-gated
    enrollment."""
    now = datetime.now(timezone.utc)
    await db._conn.execute(
        "INSERT INTO ledger_enrollments (token_id, namespace, enrolled_at, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (token_id, namespace, now.isoformat(), (now + timedelta(days=7)).isoformat()),
    )
    await db._conn.commit()


async def test_poll_enrollments_cg_batch_call_shape(db, settings_factory):
    """All enrolled CG ids ride ONE batched /simple/price call per cycle.

    Coverage-gated enrollment (fix/ledger-coverage-gated-enrollment) stops
    CG-slug tokens from auto-enrolling, so seed the CG poll set directly to
    exercise the (unchanged) batched /simple/price poller mechanism."""
    settings = _ledger_settings(settings_factory)
    await _seed_enrollment(db, "micro-alpha", "cg")
    await _seed_enrollment(db, "micro-beta", "cg")

    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(
                SIMPLE_PRICE_PATTERN,
                payload={
                    "micro-alpha": {"usd": 0.5, "usd_market_cap": 1_000_000},
                    "micro-beta": {"usd": 2.0},
                },
            )
            stats = await poll_enrollments(db, session, settings)

            # Exactly one CG call, carrying both ids comma-joined.
            cg_calls = [
                (key, reqs)
                for key, reqs in m.requests.items()
                if "simple/price" in str(key[1])
            ]
            assert len(cg_calls) == 1
            assert len(cg_calls[0][1]) == 1
            ids_param = URL(str(cg_calls[0][0][1])).query.get("ids", "")
            request_kwargs = cg_calls[0][1][0].kwargs
            params = request_kwargs.get("params") or {}
            sent_ids = params.get("ids", ids_param)
            assert set(sent_ids.split(",")) == {"micro-alpha", "micro-beta"}

    assert stats["n_cg"] == 2
    assert stats["n_priced"] == 2
    # Prices landed in price_cache (the labeler's in-DB source).
    assert await price_from_cache(db, "micro-alpha") == pytest.approx(0.5)
    assert await price_from_cache(db, "micro-beta") == pytest.approx(2.0)


async def test_poll_dex_enrollment_writes_readable_price(db, settings_factory):
    """dex:{chain}:{addr} enrollments are priced via the DexScreener tokens
    endpoint and written to price_cache keyed by the FULL dex token_id —
    the namespace's first price writer (labeling only)."""
    settings = _ledger_settings(settings_factory)
    addr = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    token_id = f"dex:solana:{addr}"
    await _enroll_via_gated_out(db, settings, token_id)

    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(
                f"https://api.dexscreener.com/tokens/v1/solana/{addr}",
                payload=[
                    {
                        "baseToken": {"address": addr, "symbol": "WIF"},
                        "priceUsd": "0.75",
                        "marketCap": 5_000_000,
                    }
                ],
            )
            stats = await poll_enrollments(db, session, settings)

    assert stats["n_dex"] == 1
    assert stats["n_priced"] == 1
    assert await price_from_cache(db, token_id) == pytest.approx(0.75)


async def test_poller_then_labeler_labels_enrolled_only_token(db, settings_factory):
    """End-to-end: an enrolled token is priced by the poller and labeled by
    the hourly pass — the missed-winner recall lane becomes measurable.

    LIVENESS coverage (fix/ledger-coverage-gated-enrollment): 'orphan-coin' is
    an UNTRACKED CG-slug (no fresh price_cache / volume_history_cg observation),
    so the gated_out emission AUTO-ENROLLS it into the 'cg' poll namespace — no
    direct seed needed (and seeding it too would double-insert). This exercises
    the real auto-enroll path for a dead-but-CG-shaped token. Labeling is
    independent of enrollment either way — label_pending resolves prices for any
    pending row keyed on its token_id."""
    settings = _ledger_settings(settings_factory)
    emitted = datetime.now(timezone.utc) - timedelta(minutes=20)
    await _enroll_via_gated_out(
        db, settings, "orphan-coin", price=1.0, emitted_at=emitted.isoformat()
    )
    # Auto-enrolled by the emission above (untracked -> no fresh price ->
    # enrolled, namespace 'cg'); confirm before poll/label.
    assert [t for t, _ in await active_enrollments(db)] == ["orphan-coin"]

    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(SIMPLE_PRICE_PATTERN, payload={"orphan-coin": {"usd": 1.30}})
            await poll_enrollments(db, session, settings)

    stats = await label_pending(db, settings)
    assert stats["n_labeled"] == 1

    cur = await db._conn.execute("SELECT r15m, label_status FROM signal_outcome_ledger")
    row = await cur.fetchone()
    assert row["r15m"] == pytest.approx(0.30)
    assert row["label_status"] == "partial"


async def test_poll_enrollments_kill_switch_no_http(db, settings_factory):
    settings = _ledger_settings(settings_factory)
    await _seed_enrollment(db, "micro-alpha", "cg")  # coverage-gated: seed directly

    disabled = _ledger_settings(settings_factory, LEDGER_ENABLED=False)
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:  # no mocks registered: any HTTP would error
            stats = await poll_enrollments(db, session, disabled)
            assert m.requests == {}
    assert stats["enabled"] is False
    assert stats["n_active"] == 0


async def test_poll_enrollments_purges_expired_rows(db, settings_factory):
    settings = _ledger_settings(settings_factory)
    now = datetime.now(timezone.utc)
    await db._conn.execute(
        "INSERT INTO ledger_enrollments (token_id, namespace, enrolled_at, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (
            "expired-tok",
            "cg",
            (now - timedelta(days=9)).isoformat(),
            (now - timedelta(days=2)).isoformat(),
        ),
    )
    await db._conn.commit()

    async with aiohttp.ClientSession() as session:
        with aioresponses():
            stats = await poll_enrollments(db, session, settings)

    assert stats["n_expired_purged"] == 1
    assert stats["n_active"] == 0
    cur = await db._conn.execute("SELECT COUNT(*) FROM ledger_enrollments")
    assert (await cur.fetchone())[0] == 0


async def test_poll_enrollments_http_failure_never_raises(db, settings_factory):
    settings = _ledger_settings(settings_factory)
    await _seed_enrollment(db, "micro-alpha", "cg")  # coverage-gated: seed directly
    addr = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    await _enroll_via_gated_out(db, settings, f"dex:solana:{addr}")

    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(SIMPLE_PRICE_PATTERN, exception=aiohttp.ClientError("cg down"))
            m.get(
                f"https://api.dexscreener.com/tokens/v1/solana/{addr}",
                status=500,
            )
            stats = await poll_enrollments(db, session, settings)  # no raise

    assert stats["n_priced"] == 0
