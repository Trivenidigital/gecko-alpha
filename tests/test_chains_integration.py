"""End-to-end conviction chain integration test."""

from datetime import datetime, timezone

import pytest

from scout.chains.events import emit_event
from scout.chains.patterns import seed_built_in_patterns
from scout.chains.tracker import check_chains, get_active_boosts
from scout.db import Database

_CHAIN_DEFAULTS = dict(
    CHAIN_CHECK_INTERVAL_SEC=300,
    CHAIN_MAX_WINDOW_HOURS=24.0,
    CHAIN_COOLDOWN_HOURS=12.0,
    CHAIN_EVENT_RETENTION_DAYS=14,
    CHAIN_ACTIVE_RETENTION_DAYS=7,
    CHAIN_ALERT_ON_COMPLETE=False,
    CHAIN_TOTAL_BOOST_CAP=30,
    CHAINS_ENABLED=True,
)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    await seed_built_in_patterns(d)
    yield d
    await d.close()


@pytest.fixture
def settings(settings_factory):
    return settings_factory(**_CHAIN_DEFAULTS)


@pytest.fixture(autouse=True)
def _patch_get_settings(monkeypatch, settings_factory):
    s = settings_factory(**_CHAIN_DEFAULTS)
    monkeypatch.setattr("scout.config.get_settings", lambda: s)


async def test_full_conviction_e2e(db, settings):
    await emit_event(
        db,
        "cat-ai",
        "narrative",
        "category_heating",
        {"acceleration": 8.0, "volume_growth_pct": 40.0, "market_regime": "BULL"},
        "narrative.observer",
    )
    await emit_event(
        db,
        "cat-ai",
        "narrative",
        "laggard_picked",
        {"narrative_fit_score": 80, "confidence": "High", "trigger_count": 2},
        "narrative.predictor",
    )
    await emit_event(
        db,
        "cat-ai",
        "narrative",
        "counter_scored",
        {
            "risk_score": 20,
            "flag_count": 0,
            "high_severity_count": 0,
            "data_completeness": "full",
        },
        "counter.scorer",
    )
    await check_chains(db, settings)

    async with db._conn.execute(
        "SELECT COUNT(*) FROM chain_matches WHERE token_id='cat-ai'"
    ) as cur:
        row = await cur.fetchone()
    assert row[0] >= 1

    boost = await get_active_boosts(db, "cat-ai", "narrative", settings)
    assert boost >= 15


async def test_gate_applies_chain_boost(db, settings_factory, monkeypatch):
    """gate.evaluate adds get_active_boosts to the conviction score."""
    import scout.gate as gate_mod
    from scout.models import CandidateToken

    async with db._conn.execute(
        "SELECT id FROM chain_patterns WHERE name='full_conviction'"
    ) as cur:
        pid = (await cur.fetchone())[0]
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("0xabc", "memecoin", pid, "full_conviction", 3, 4, now_iso, now_iso, 2.0, 25),
    )
    await db._conn.commit()

    token = CandidateToken(
        contract_address="0xabc",
        chain="ethereum",
        token_name="Test",
        ticker="TST",
        quant_score=50,
        first_seen_at=datetime.now(timezone.utc),
    )

    async def _no_narrative(*args, **kwargs):
        return None

    monkeypatch.setattr(gate_mod, "_get_narrative_score", _no_narrative)

    local_settings = settings_factory(
        **_CHAIN_DEFAULTS,
        CONVICTION_THRESHOLD=70,
        MIN_SCORE=999,
    )

    import aiohttp

    async with aiohttp.ClientSession() as session:
        should_alert, conviction, updated = await gate_mod.evaluate(
            token, db, session, local_settings, signals_fired=[]
        )

    assert conviction == 75.0  # 50 base + 25 boost
    assert should_alert is True
    assert updated.conviction_score == 75.0
