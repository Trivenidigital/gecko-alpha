"""Signal outcome ledger (P0, edge-audit 2026-07-02).

The 2026-07-02 edge audit (tasks/gecko-alpha-fable-review_2026_07.md Phase 3)
found historical forward-returns uncomputable: the alerts table had 33 lifetime
rows with no usable price, so gate counterfactuals were impossible. The ledger
makes every future emission (candidate alert, paper-trade dispatch, sampled
gate-block) self-label with forward returns resolved from IN-DB price sources
only (volume_history_cg history + price_cache) — zero external API budget.

Covered here (runnable locally on Windows — no aiohttp import chain):
- migration: table + indexes, idempotency, schema_version registration
- record_emission: happy path, kill switch, fail-soft isolation, CHECK kinds
- GatedOutSampler: 1-in-N rate, 0=off
- label_pending: per-horizon labeling from synthetic volume_history_cg rows,
  price_cache fallback + lateness bound, unlabelable-after-7d, peak7d window,
  batch cap, kill switch, per-pass stats
- engine wiring: dispatch record on open, blocked-path sampling, kill switch,
  host-path isolation when the ledger write raises

scout.main wiring (candidate-alert site) lives in
tests/test_outcome_ledger_main_wiring.py — scout.main imports aiohttp, which
aborts locally on Windows (OPENSSL_Applink); CI runs it on Linux.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import structlog

from scout.db import Database
from scout.outcome_ledger import (
    GatedOutSampler,
    label_pending,
    price_and_age_from_cache,
    price_from_cache,
    record_emission,
)


def _events(logs) -> list[str]:
    """Event names from a structlog.testing.capture_logs() capture."""
    return [entry["event"] for entry in logs]


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ledger_settings(**overrides) -> SimpleNamespace:
    base = dict(
        LEDGER_ENABLED=True,
        LEDGER_GATED_OUT_SAMPLE_RATE=25,
        LEDGER_LABEL_BATCH_MAX=500,
        LEDGER_PRICE_CACHE_MAX_LATENESS_MINUTES=120,
        LEDGER_ENROLLMENT_TTL_DAYS=7,
        LEDGER_ENROLLMENT_MAX_ACTIVE=200,
        LEDGER_COVERAGE_FRESHNESS_MIN=60,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "ledger.db"))
    await database.initialize()
    yield database
    await database.close()


async def _insert_vhc(db, coin_id: str, price: float, recorded_at: datetime) -> None:
    await db._conn.execute(
        "INSERT INTO volume_history_cg "
        "(coin_id, symbol, name, volume_24h, price, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (coin_id, coin_id.upper(), coin_id, 1000.0, price, _iso(recorded_at)),
    )
    await db._conn.commit()


async def _insert_price_cache(
    db, coin_id: str, price: float, updated_at: datetime
) -> None:
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        (coin_id, price, _iso(updated_at)),
    )
    await db._conn.commit()


async def _fetch_rows(db):
    cur = await db._conn.execute("SELECT * FROM signal_outcome_ledger ORDER BY id ASC")
    return await cur.fetchall()


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


async def test_migration_creates_table_and_indexes(db):
    cur = await db._conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='signal_outcome_ledger'"
    )
    row = await cur.fetchone()
    assert row is not None
    ddl = row["sql"]
    for col in (
        "kind",
        "token_id",
        "surface",
        "price_at_emission",
        "anchor_cache_age_seconds",
        "liquidity_at_emission",
        "liquidity_source",
        "gate_verdicts",
        "enrollment_status",
        "emitted_at",
        "r15m",
        "r1h",
        "r4h",
        "r24h",
        "r7d",
        "peak7d",
        "label_status",
        "labeled_at",
    ):
        assert col in ddl, col

    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='signal_outcome_ledger'"
    )
    index_names = {r["name"] for r in await cur.fetchall()}
    assert "idx_sol_status_emitted" in index_names
    assert "idx_sol_token_emitted" in index_names


async def test_migration_registers_schema_version(db):
    cur = await db._conn.execute(
        "SELECT description FROM schema_version WHERE version = 20260704"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row["description"] == "signal_outcome_ledger_v1"


async def test_migration_idempotent_across_reinitialize(tmp_path):
    path = str(tmp_path / "idempotent.db")
    db1 = Database(path)
    await db1.initialize()
    await record_emission(
        db1,
        _ledger_settings(),
        kind="alert",
        token_id="tok",
        surface="candidate_alert",
        price=1.0,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts=None,
    )
    await db1.close()

    # Simulated restart: fresh Database object, same file. Migration must
    # neither error nor clobber existing rows.
    db2 = Database(path)
    await db2.initialize()
    cur = await db2._conn.execute("SELECT COUNT(*) AS n FROM signal_outcome_ledger")
    assert (await cur.fetchone())["n"] == 1
    await db2.close()


# ---------------------------------------------------------------------------
# record_emission
# ---------------------------------------------------------------------------


async def test_record_emission_inserts_full_row(db):
    verdicts = {"reason": "conviction", "score": 81.5}
    row_id = await record_emission(
        db,
        _ledger_settings(),
        kind="dispatch",
        token_id="dogwifhat",
        surface="gainers_early",
        price=2.5,
        liquidity=55_000.0,
        liquidity_source="signal_data",
        gate_verdicts=verdicts,
    )
    assert isinstance(row_id, int)

    rows = await _fetch_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "dispatch"
    assert row["token_id"] == "dogwifhat"
    assert row["surface"] == "gainers_early"
    assert row["price_at_emission"] == pytest.approx(2.5)
    assert row["liquidity_at_emission"] == pytest.approx(55_000.0)
    assert row["liquidity_source"] == "signal_data"
    assert json.loads(row["gate_verdicts"]) == verdicts
    assert row["label_status"] == "pending"
    assert row["labeled_at"] is None
    # c1: caller-supplied (live) anchor without explicit cache age -> 0.0.
    assert row["anchor_cache_age_seconds"] == pytest.approx(0.0)
    # c2: priced non-gated emission -> in-DB coverage -> no enrollment.
    assert row["enrollment_status"] == "not_needed"
    # emitted_at defaults to now (UTC ISO)
    emitted = datetime.fromisoformat(row["emitted_at"])
    assert abs((_now() - emitted).total_seconds()) < 60


async def test_record_emission_accepts_explicit_emitted_at(db):
    ts = _now() - timedelta(hours=3)
    await record_emission(
        db,
        _ledger_settings(),
        kind="alert",
        token_id="tok",
        surface="candidate_alert",
        price=None,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts=None,
        emitted_at=_iso(ts),
    )
    rows = await _fetch_rows(db)
    assert rows[0]["emitted_at"] == _iso(ts)
    assert rows[0]["price_at_emission"] is None
    # c1: no anchor -> age is NULL, never 0.0.
    assert rows[0]["anchor_cache_age_seconds"] is None


async def test_record_emission_alert_kind_stores_cache_age(db):
    """c1: an alert whose anchor came from an AGED price_cache row records
    the age alongside the price (price_and_age_from_cache round-trip)."""
    aged_at = _now() - timedelta(seconds=300)
    await _insert_price_cache(db, "tok", 1.5, aged_at)

    price, age = await price_and_age_from_cache(db, "tok")
    assert price == pytest.approx(1.5)
    assert age is not None and 295 <= age <= 360  # ~300s, small runtime slack

    await record_emission(
        db,
        _ledger_settings(),
        kind="alert",
        token_id="tok",
        surface="candidate_alert",
        price=price,
        anchor_cache_age_seconds=age,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts=None,
    )
    row = (await _fetch_rows(db))[0]
    assert row["anchor_cache_age_seconds"] == pytest.approx(age)
    assert row["anchor_cache_age_seconds"] > 0


async def test_price_and_age_from_cache_missing_row(db):
    assert await price_and_age_from_cache(db, "ghost") == (None, None)


async def test_record_emission_kill_switch_blocks_write(db):
    row_id = await record_emission(
        db,
        _ledger_settings(LEDGER_ENABLED=False),
        kind="alert",
        token_id="tok",
        surface="candidate_alert",
        price=1.0,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts=None,
    )
    assert row_id is None
    assert await _fetch_rows(db) == []


async def test_record_emission_invalid_kind_fails_soft(db):
    """CHECK(kind IN ...) violation must be swallowed + logged, never raised."""
    row_id = await record_emission(
        db,
        _ledger_settings(),
        kind="bogus_kind",
        token_id="tok",
        surface="x",
        price=None,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts=None,
    )
    assert row_id is None
    assert await _fetch_rows(db) == []


async def test_record_emission_never_raises_when_db_broken(db, monkeypatch):
    """Host paths (alert send, trade open) must survive any ledger failure."""

    async def _boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(db._conn, "execute", _boom)
    row_id = await record_emission(
        db,
        _ledger_settings(),
        kind="alert",
        token_id="tok",
        surface="candidate_alert",
        price=1.0,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts={"a": 1},
    )
    assert row_id is None  # no exception escaped


async def test_record_emission_on_closed_db_is_noop():
    database = Database(":memory:")  # never initialized -> _conn is None
    row_id = await record_emission(
        database,
        _ledger_settings(),
        kind="alert",
        token_id="tok",
        surface="candidate_alert",
        price=None,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts=None,
    )
    assert row_id is None


# ---------------------------------------------------------------------------
# GatedOutSampler
# ---------------------------------------------------------------------------


def test_sampler_records_one_in_n():
    sampler = GatedOutSampler()
    results = [sampler.should_record(3) for _ in range(9)]
    assert results == [False, False, True] * 3


def test_sampler_rate_zero_is_off():
    sampler = GatedOutSampler()
    assert not any(sampler.should_record(0) for _ in range(50))


def test_sampler_rate_one_records_every_time():
    sampler = GatedOutSampler()
    assert all(sampler.should_record(1) for _ in range(5))


# ---------------------------------------------------------------------------
# price_from_cache
# ---------------------------------------------------------------------------


async def test_price_from_cache_returns_price(db):
    await _insert_price_cache(db, "tok", 3.21, _now())
    assert await price_from_cache(db, "tok") == pytest.approx(3.21)


async def test_price_from_cache_missing_returns_none(db):
    assert await price_from_cache(db, "ghost") is None


async def test_price_from_cache_fails_soft(db, monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(db._conn, "execute", _boom)
    assert await price_from_cache(db, "tok") is None


# ---------------------------------------------------------------------------
# label_pending
# ---------------------------------------------------------------------------


async def _seed_ledger_row(
    db,
    *,
    token_id: str = "tok",
    price: float | None = 1.0,
    emitted_at: datetime,
    kind: str = "alert",
) -> int:
    row_id = await record_emission(
        db,
        _ledger_settings(),
        kind=kind,
        token_id=token_id,
        surface="candidate_alert",
        price=price,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts=None,
        emitted_at=_iso(emitted_at),
    )
    assert row_id is not None
    return row_id


async def test_label_pending_labels_due_horizons_partial(db):
    emitted = _now() - timedelta(hours=2)
    await _seed_ledger_row(db, emitted_at=emitted, price=1.0)
    # First price row at/after each due horizon.
    await _insert_vhc(db, "tok", 1.10, emitted + timedelta(minutes=16))
    await _insert_vhc(db, "tok", 1.20, emitted + timedelta(minutes=61))

    stats = await label_pending(db, _ledger_settings())
    assert stats["n_labeled"] == 1

    row = (await _fetch_rows(db))[0]
    assert row["r15m"] == pytest.approx(0.10)
    assert row["r1h"] == pytest.approx(0.20)
    assert row["r4h"] is None  # not due yet
    assert row["r24h"] is None
    assert row["r7d"] is None
    assert row["label_status"] == "partial"
    assert row["labeled_at"] is None  # terminal only


async def test_label_pending_uses_first_row_at_or_after_horizon(db):
    """A pre-horizon row must NOT satisfy the horizon; the first row at/after
    the deadline does, even if a later row also exists."""
    emitted = _now() - timedelta(hours=2)
    await _seed_ledger_row(db, emitted_at=emitted, price=1.0)
    await _insert_vhc(db, "tok", 9.99, emitted + timedelta(minutes=10))  # pre-15m
    await _insert_vhc(db, "tok", 1.50, emitted + timedelta(minutes=20))  # first after
    await _insert_vhc(db, "tok", 3.00, emitted + timedelta(minutes=40))  # later

    await label_pending(db, _ledger_settings())
    row = (await _fetch_rows(db))[0]
    assert row["r15m"] == pytest.approx(0.50)


async def test_label_pending_nothing_due_stays_pending(db):
    await _seed_ledger_row(db, emitted_at=_now(), price=1.0)
    stats = await label_pending(db, _ledger_settings())
    assert stats["n_labeled"] == 0
    row = (await _fetch_rows(db))[0]
    assert row["label_status"] == "pending"
    assert row["r15m"] is None


async def test_label_pending_price_cache_fallback(db):
    """No volume_history_cg rows: price_cache observed just after the horizon
    (within the lateness bound) resolves the label."""
    emitted = _now() - timedelta(minutes=20)
    await _seed_ledger_row(db, emitted_at=emitted, price=2.0)
    await _insert_price_cache(db, "tok", 3.0, _now())  # ~5 min after 15m deadline

    await label_pending(db, _ledger_settings())
    row = (await _fetch_rows(db))[0]
    assert row["r15m"] == pytest.approx(0.50)
    assert row["label_status"] == "partial"


async def test_label_pending_price_cache_too_late_is_rejected(db):
    """price_cache observed far after the horizon (beyond the lateness bound)
    must not be used as that horizon's price."""
    emitted = _now() - timedelta(hours=4, minutes=1)
    await _seed_ledger_row(db, emitted_at=emitted, price=2.0)
    await _insert_price_cache(db, "tok", 3.0, _now())

    await label_pending(
        db, _ledger_settings(LEDGER_PRICE_CACHE_MAX_LATENESS_MINUTES=120)
    )
    row = (await _fetch_rows(db))[0]
    # 15m + 1h horizons: cache is >120 min late -> rejected.
    assert row["r15m"] is None
    assert row["r1h"] is None
    # 4h horizon due ~1 min ago: cache is ~1 min late -> accepted.
    assert row["r4h"] == pytest.approx(0.50)


async def test_label_pending_complete_after_7d_with_full_history(db):
    emitted = _now() - timedelta(days=8)
    await _seed_ledger_row(db, emitted_at=emitted, price=1.0)
    for offset, price in [
        (timedelta(minutes=15), 1.05),
        (timedelta(hours=1), 1.10),
        (timedelta(hours=4), 1.30),
        (timedelta(hours=24), 0.90),
        (timedelta(days=3), 2.50),  # in-window peak
        (timedelta(days=7), 1.75),
    ]:
        await _insert_vhc(db, "tok", price, emitted + offset)
    # A post-window spike must NOT count toward peak7d.
    await _insert_vhc(db, "tok", 99.0, emitted + timedelta(days=7, hours=6))

    stats = await label_pending(db, _ledger_settings())
    assert stats["n_labeled"] == 1

    row = (await _fetch_rows(db))[0]
    assert row["r15m"] == pytest.approx(0.05)
    assert row["r1h"] == pytest.approx(0.10)
    assert row["r4h"] == pytest.approx(0.30)
    assert row["r24h"] == pytest.approx(-0.10)
    assert row["r7d"] == pytest.approx(0.75)
    assert row["peak7d"] == pytest.approx(2.50)  # MAX(price) in window, raw
    assert row["label_status"] == "complete"
    assert row["labeled_at"] is not None


async def test_label_pending_unlabelable_after_7d_without_sources(db):
    """No in-DB price source after 7d -> 'unlabelable'. This cohort is itself
    signal (liquidity-death tokens the CG surfaces stopped tracking)."""
    emitted = _now() - timedelta(days=8)
    await _seed_ledger_row(db, emitted_at=emitted, price=1.0)

    stats = await label_pending(db, _ledger_settings())
    assert stats["n_unlabelable"] == 1

    row = (await _fetch_rows(db))[0]
    assert row["label_status"] == "unlabelable"
    assert row["labeled_at"] is not None
    assert row["r7d"] is None
    assert row["peak7d"] is None


async def test_label_pending_no_base_price_can_still_complete_via_peak(db):
    """price_at_emission NULL: returns are uncomputable, but peak7d (raw MAX
    price) still labels, so the row terminates 'complete', not 'unlabelable'."""
    emitted = _now() - timedelta(days=8)
    await _seed_ledger_row(db, emitted_at=emitted, price=None)
    await _insert_vhc(db, "tok", 5.0, emitted + timedelta(days=2))

    await label_pending(db, _ledger_settings())
    row = (await _fetch_rows(db))[0]
    assert row["r7d"] is None
    assert row["peak7d"] == pytest.approx(5.0)
    assert row["label_status"] == "complete"


async def test_label_pending_respects_batch_max(db):
    emitted = _now() - timedelta(days=8)
    for i in range(3):
        await _seed_ledger_row(
            db, token_id=f"tok{i}", emitted_at=emitted + timedelta(minutes=i)
        )

    stats = await label_pending(db, _ledger_settings(LEDGER_LABEL_BATCH_MAX=2))
    assert stats["n_examined"] == 2
    assert stats["n_unlabelable"] == 2

    rows = await _fetch_rows(db)
    statuses = [r["label_status"] for r in rows]
    # Oldest-first ordering: the third (newest) row is untouched this pass.
    assert statuses == ["unlabelable", "unlabelable", "pending"]


async def test_label_pending_kill_switch(db):
    emitted = _now() - timedelta(days=8)
    await _seed_ledger_row(db, emitted_at=emitted)

    stats = await label_pending(db, _ledger_settings(LEDGER_ENABLED=False))
    assert stats["enabled"] is False
    assert stats["n_examined"] == 0
    row = (await _fetch_rows(db))[0]
    assert row["label_status"] == "pending"


async def test_label_pending_stats_shape(db):
    stats = await label_pending(db, _ledger_settings())
    for key in ("enabled", "n_examined", "n_labeled", "n_pending", "n_unlabelable"):
        assert key in stats, key


async def test_label_pending_fails_soft_on_broken_db(db, monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(db._conn, "execute", _boom)
    stats = await label_pending(db, _ledger_settings())
    assert stats["n_examined"] == 0  # no exception escaped


# ---------------------------------------------------------------------------
# Liveness heartbeat — alive-empty must be distinguishable from dead labeler.
# A pass with nothing pending must still emit exactly one ledger_label_heartbeat
# so "ran but empty" (heartbeat present) diverges from "never ran" (silence).
# ---------------------------------------------------------------------------


async def test_label_pending_empty_still_emits_heartbeat(db):
    """Divergence proof: an enabled pass over an EMPTY ledger still emits one
    ledger_label_heartbeat — silence now means dead, not merely idle."""
    with structlog.testing.capture_logs() as logs:
        stats = await label_pending(db, _ledger_settings())
    assert stats["n_examined"] == 0
    beats = [e for e in logs if e["event"] == "ledger_label_heartbeat"]
    assert len(beats) == 1
    beat = beats[0]
    assert beat["enabled"] is True
    assert beat["n_labeled"] == 0
    assert beat["n_pending"] == 0
    assert beat["n_unlabelable"] == 0


async def test_label_pending_disabled_emits_heartbeat_enabled_false(db):
    """Kill-switched pass still emits exactly one heartbeat with enabled=False —
    the disabled path is alive, not dead."""
    with structlog.testing.capture_logs() as logs:
        await label_pending(db, _ledger_settings(LEDGER_ENABLED=False))
    beats = [e for e in logs if e["event"] == "ledger_label_heartbeat"]
    assert len(beats) == 1
    assert beats[0]["enabled"] is False


async def test_label_pending_emits_exactly_one_heartbeat_when_working(db):
    """Even on the working path (rows labeled) exactly one heartbeat fires —
    one line per pass, no per-item spam."""
    emitted = _now() - timedelta(hours=2)
    await _seed_ledger_row(db, emitted_at=emitted, price=1.0)
    await _insert_vhc(db, "tok", 1.10, emitted + timedelta(minutes=16))

    with structlog.testing.capture_logs() as logs:
        stats = await label_pending(db, _ledger_settings())
    assert stats["n_labeled"] == 1
    assert _events(logs).count("ledger_label_heartbeat") == 1


# ---------------------------------------------------------------------------
# Engine wiring (dispatch + gated_out_sample)
# ---------------------------------------------------------------------------


def _engine_settings(settings_factory, **overrides):
    defaults = dict(
        PAPER_STARTUP_WARMUP_SECONDS=0,
        PAPER_TRADE_AMOUNT_USD=100.0,
        PAPER_MAX_EXPOSURE_USD=10_000.0,
        PAPER_TP_PCT=20.0,
        LEDGER_ENABLED=True,
        LEDGER_GATED_OUT_SAMPLE_RATE=25,
    )
    defaults.update(overrides)
    return settings_factory(**defaults)


@pytest.fixture
def _quiet_tg(monkeypatch):
    """Neutralize the post-open TG alert spawn: its lazy aiohttp import aborts
    on Windows (OPENSSL_Applink) and is irrelevant to ledger wiring."""
    from scout.trading.engine import TradingEngine

    monkeypatch.setattr(TradingEngine, "_spawn_tg_alert", AsyncMock())


async def test_engine_open_trade_records_dispatch(db, settings_factory, _quiet_tg):
    from scout.trading.engine import TradingEngine
    from scout.trading.params import clear_cache_for_tests

    clear_cache_for_tests()
    await _insert_price_cache(db, "tok", 1.0, _now())
    settings = _engine_settings(settings_factory)
    engine = TradingEngine(mode="paper", db=db, settings=settings)

    trade_id = await engine.open_trade(
        token_id="tok",
        symbol="TOK",
        name="Tok",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={"liquidity_usd": 42_000.0},
        signal_combo="gainers_early",
    )
    assert trade_id is not None

    rows = await _fetch_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "dispatch"
    assert row["token_id"] == "tok"
    assert row["surface"] == "gainers_early"
    # Market price at dispatch (pre-slippage), the forward-return anchor.
    assert row["price_at_emission"] == pytest.approx(1.0)
    # c1: dispatch anchor is live at emission -> age 0.0.
    assert row["anchor_cache_age_seconds"] == pytest.approx(0.0)
    assert row["liquidity_at_emission"] == pytest.approx(42_000.0)
    assert row["liquidity_source"] == "signal_data"
    verdicts = json.loads(row["gate_verdicts"])
    assert verdicts["paper_trade_id"] == trade_id
    assert verdicts["signal_combo"] == "gainers_early"
    clear_cache_for_tests()


async def test_engine_blocked_path_samples_gated_out(db, settings_factory):
    """Warmup-blocked opens (no DB seeds needed) sample at 1-in-N."""
    from scout.trading.engine import TradingEngine

    settings = _engine_settings(
        settings_factory,
        PAPER_STARTUP_WARMUP_SECONDS=9_999,
        LEDGER_GATED_OUT_SAMPLE_RATE=3,
    )
    # Mirror prod: a CG-sourced dispatch signal suppresses a token the CG lanes
    # are ACTIVELY tracking -> a FRESH in-DB price exists. Under liveness
    # coverage that makes the row 'not_needed' (no 200-cap churn). Without this
    # seed the token would be feed-dead and correctly ENROLL.
    await _insert_price_cache(db, "tok", 1.0, _now())
    engine = TradingEngine(mode="paper", db=db, settings=settings)

    for _ in range(6):
        result = await engine.open_trade(
            token_id="tok",
            symbol="TOK",
            chain="coingecko",
            signal_type="gainers_early",
            signal_data={},
            signal_combo="gainers_early",
        )
        assert result is None  # warmup-blocked

    rows = await _fetch_rows(db)
    assert len(rows) == 2  # 6 blocks / rate 3
    for row in rows:
        assert row["kind"] == "gated_out_sample"
        assert row["surface"] == "gainers_early"
        verdicts = json.loads(row["gate_verdicts"])
        assert verdicts["reason"] == "warmup"
        assert verdicts["sample_rate"] == 3
        # c1: no anchor price for this blocked path -> age NULL.
        assert row["anchor_cache_age_seconds"] is None
        # coverage = LIVENESS (fix/ledger-coverage-gated-enrollment): token_id
        # "tok" has a FRESH price_cache row (seeded above), so it is actively
        # served by the CG lanes and labelable WITHOUT the poller -> records
        # the gated_out row but does NOT enroll and stamps 'not_needed'. This
        # mirrors prod: the CG-sourced dispatch signals (chain_completed/
        # gainers_early/losers_contrarian/volume_spike) suppress tokens the CG
        # lanes actively price, so they must not churn the 200-cap. NOTE: shape
        # (is_cg_coin_id) alone no longer implies coverage — a DEAD slug with no
        # fresh price would ENROLL (see
        # test_gated_out_cg_slug_no_price_data_enrolls).
        assert row["enrollment_status"] == "not_needed"


async def test_engine_blocked_sampler_rate_zero_records_nothing(db, settings_factory):
    from scout.trading.engine import TradingEngine

    settings = _engine_settings(
        settings_factory,
        PAPER_STARTUP_WARMUP_SECONDS=9_999,
        LEDGER_GATED_OUT_SAMPLE_RATE=0,
    )
    engine = TradingEngine(mode="paper", db=db, settings=settings)
    for _ in range(10):
        await engine.open_trade(
            token_id="tok",
            symbol="TOK",
            chain="coingecko",
            signal_type="gainers_early",
            signal_data={},
            signal_combo="gainers_early",
        )
    assert await _fetch_rows(db) == []


async def test_engine_blocked_sampler_respects_kill_switch(db, settings_factory):
    from scout.trading.engine import TradingEngine

    settings = _engine_settings(
        settings_factory,
        PAPER_STARTUP_WARMUP_SECONDS=9_999,
        LEDGER_GATED_OUT_SAMPLE_RATE=1,
        LEDGER_ENABLED=False,
    )
    engine = TradingEngine(mode="paper", db=db, settings=settings)
    for _ in range(5):
        await engine.open_trade(
            token_id="tok",
            symbol="TOK",
            chain="coingecko",
            signal_type="gainers_early",
            signal_data={},
            signal_combo="gainers_early",
        )
    assert await _fetch_rows(db) == []


async def test_engine_open_survives_ledger_raise(
    db, settings_factory, _quiet_tg, monkeypatch
):
    """Belt-and-braces: even if record_emission itself raises (it shouldn't),
    the trade-open host path must complete."""
    import scout.trading.engine as engine_mod
    from scout.trading.engine import TradingEngine
    from scout.trading.params import clear_cache_for_tests

    clear_cache_for_tests()

    async def _boom(*args, **kwargs):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(engine_mod, "_ledger_record_emission", _boom)
    await _insert_price_cache(db, "tok", 1.0, _now())
    settings = _engine_settings(settings_factory)
    engine = TradingEngine(mode="paper", db=db, settings=settings)

    trade_id = await engine.open_trade(
        token_id="tok",
        symbol="TOK",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={},
        signal_combo="gainers_early",
    )
    assert trade_id is not None  # host path unaffected
    clear_cache_for_tests()


# ---------------------------------------------------------------------------
# Enrollment-at-emission (forward-polling set for untracked tokens)
# ---------------------------------------------------------------------------


async def _fetch_enrollments(db):
    cur = await db._conn.execute(
        "SELECT token_id, namespace, enrolled_at, expires_at "
        "FROM ledger_enrollments ORDER BY enrolled_at ASC, token_id ASC"
    )
    return await cur.fetchall()


async def test_migration_creates_ledger_enrollments(db):
    cur = await db._conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='ledger_enrollments'"
    )
    row = await cur.fetchone()
    assert row is not None
    for col in ("token_id", "namespace", "enrolled_at", "expires_at"):
        assert col in row["sql"], col


async def test_gated_out_untracked_enrolls_token(db):
    """coverage-gated enrollment (fix/ledger-coverage-gated-enrollment):
    an UNTRACKED token (no in-DB coverage — here a dex: id) still enrolls at
    TTL and stamps 'enrolled'. CG-slug tokens no longer enroll (they are
    in-DB-covered); the CG-slug negative case is
    test_gated_out_cg_slug_covered_does_not_enroll below."""
    token_id = "dex:ethereum:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    await record_emission(
        db,
        _ledger_settings(),
        kind="gated_out_sample",
        token_id=token_id,
        surface="tg_social",
        price=None,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts={"reason": "below_min_mcap"},
    )
    rows = await _fetch_enrollments(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["token_id"] == token_id
    assert row["namespace"] == "dex"
    expires = datetime.fromisoformat(row["expires_at"])
    enrolled = datetime.fromisoformat(row["enrolled_at"])
    assert abs((expires - enrolled) - timedelta(days=7)).total_seconds() < 60
    # c2: enrollment outcome stamped on the ledger row itself.
    ledger_row = (await _fetch_rows(db))[0]
    assert ledger_row["enrollment_status"] == "enrolled"


async def test_gated_out_cg_slug_no_price_data_enrolls(db):
    """CORE of the liveness fix (fix/ledger-coverage-gated-enrollment): coverage
    is LIVENESS, not SHAPE. A valid CG-slug token with NO in-DB price data (a
    DEAD/delisted slug, or one dropped from the tracked top-N) no longer reads
    as "covered" just because is_cg_coin_id passes — it has no fresh price
    observation, so it ENROLLS and stamps 'enrolled'.

    This inverts the prior #423 assertion for the SAME token ('micro-cap-coin'
    stamped 'not_needed' under the shape heuristic). Enrolling the dead-slug
    cohort is exactly the point: otherwise it reads covered -> never re-priced
    -> unlabelable-but-unflagged, undercounting dead suppressed tokens and
    biasing the suppressed cohort's returns upward."""
    await record_emission(
        db,
        _ledger_settings(),
        kind="gated_out_sample",
        token_id="micro-cap-coin",  # CG-slug shape, but NO fresh price -> not covered
        surface="gainers_early",
        price=None,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts={"reason": "below_min_mcap"},
    )
    rows = await _fetch_enrollments(db)
    assert [r["token_id"] for r in rows] == ["micro-cap-coin"]
    assert rows[0]["namespace"] == "cg"  # CG-shaped id still polls via the CG lane
    ledger_row = (await _fetch_rows(db))[0]
    assert ledger_row["kind"] == "gated_out_sample"
    assert ledger_row["enrollment_status"] == "enrolled"


async def test_gated_out_fresh_price_cache_does_not_enroll(db):
    """Liveness-covered (test 1): a FRESH price_cache row (updated_at = now)
    means the token is actively served -> no enrollment, stamp 'not_needed'.
    Adjusted from the prior #423 test_gated_out_price_cache_covered_does_not_
    enroll: 'covered' now requires FRESHNESS, so the freshness of the seeded
    row (not merely its existence) is what makes this pass."""
    token_id = "dex:base:0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    await _insert_price_cache(db, token_id, 0.42, _now())  # FRESH
    await record_emission(
        db,
        _ledger_settings(),
        kind="gated_out_sample",
        token_id=token_id,
        surface="tg_social",
        price=None,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts={"reason": "below_min_mcap"},
    )
    assert await _fetch_enrollments(db) == []
    assert (await _fetch_rows(db))[0]["enrollment_status"] == "not_needed"


async def test_gated_out_stale_price_cache_enrolls(db):
    """CORE of the liveness fix (test 2): a STALE price_cache row (updated_at 3h
    ago, older than LEDGER_COVERAGE_FRESHNESS_MIN=60) is FEED-DEAD -> NOT
    covered -> the token ENROLLS. Under the prior existence-only check this row
    read as 'covered' (no freshness filter) and the token was never re-priced.
    Stale != covered is the whole fix."""
    token_id = "dex:base:0xstalestalestalestalestalestalestalestal"
    await _insert_price_cache(db, token_id, 0.42, _now() - timedelta(hours=3))  # STALE
    await record_emission(
        db,
        _ledger_settings(),
        kind="gated_out_sample",
        token_id=token_id,
        surface="tg_social",
        price=None,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts={"reason": "below_min_mcap"},
    )
    rows = await _fetch_enrollments(db)
    assert [r["token_id"] for r in rows] == [token_id]
    assert (await _fetch_rows(db))[0]["enrollment_status"] == "enrolled"


async def test_gated_out_fresh_volume_history_does_not_enroll(db):
    """Liveness-covered via the OTHER lane (test 3): a fresh volume_history_cg
    row (latest recorded_at within the window) also counts as coverage -> no
    enrollment, stamp 'not_needed'. Exercises the volume_history_cg branch of
    _has_fresh_price_observation independently of price_cache."""
    token_id = "fresh-vhc-coin"
    await _insert_vhc(db, token_id, 1.0, _now() - timedelta(minutes=10))  # FRESH
    await record_emission(
        db,
        _ledger_settings(),
        kind="gated_out_sample",
        token_id=token_id,
        surface="gainers_early",
        price=None,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts={"reason": "below_min_mcap"},
    )
    assert await _fetch_enrollments(db) == []
    assert (await _fetch_rows(db))[0]["enrollment_status"] == "not_needed"


async def test_gated_out_stale_volume_history_enrolls(db):
    """Symmetric to the stale-price_cache case: a volume_history_cg row whose
    latest recorded_at is older than the window is feed-dead -> NOT covered ->
    the token ENROLLS. Guards against a MAX(recorded_at) regression that would
    treat any historical row as coverage regardless of age."""
    token_id = "stale-vhc-coin"
    await _insert_vhc(db, token_id, 1.0, _now() - timedelta(hours=5))  # STALE
    await record_emission(
        db,
        _ledger_settings(),
        kind="gated_out_sample",
        token_id=token_id,
        surface="gainers_early",
        price=None,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts={"reason": "below_min_mcap"},
    )
    rows = await _fetch_enrollments(db)
    assert [r["token_id"] for r in rows] == [token_id]
    assert (await _fetch_rows(db))[0]["enrollment_status"] == "enrolled"


async def test_labeler_labels_fresh_covered_row_without_enrollment(db):
    """Operator condition (fix/ledger-coverage-gated-enrollment): a LIVENESS-
    covered (non-enrolled) gated_out row is STILL labeled — label_pending
    resolves prices from volume_history_cg for any pending row keyed on its
    token_id, independent of ledger_enrollments. A liveness-covered token by
    definition HAS a fresh in-DB price, so it labels trivially from that same
    in-DB history. Enrollment only drives the poller for the untracked cohort;
    it is never a precondition for labeling.

    Adjusted from the prior CG-slug variant: coverage now requires a fresh
    price row, so we seed one at emission time to make the row 'not_needed'."""
    emitted = _now() - timedelta(hours=2)
    # Fresh observation (updated_at = real now) -> liveness-covered at the
    # moment record_emission runs its coverage check (which always uses
    # wall-clock now, independent of the backdated emitted_at) -> not enrolled.
    await _insert_price_cache(db, "covered-coin", 1.0, _now())
    await record_emission(
        db,
        _ledger_settings(),
        kind="gated_out_sample",
        token_id="covered-coin",
        surface="gainers_early",
        price=1.0,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts={"reason": "below_min_mcap"},
        emitted_at=_iso(emitted),
    )
    # Not enrolled (liveness-covered), yet labelable from in-DB history.
    assert await _fetch_enrollments(db) == []
    await _insert_vhc(db, "covered-coin", 1.30, emitted + timedelta(minutes=16))
    await _insert_vhc(db, "covered-coin", 1.50, emitted + timedelta(minutes=61))

    stats = await label_pending(db, _ledger_settings())
    assert stats["n_labeled"] == 1
    row = (await _fetch_rows(db))[0]
    assert row["enrollment_status"] == "not_needed"
    assert row["r15m"] == pytest.approx(0.30)
    assert row["r1h"] == pytest.approx(0.50)
    assert row["label_status"] == "partial"


async def test_dex_namespace_classified(db):
    await record_emission(
        db,
        _ledger_settings(),
        kind="gated_out_sample",
        token_id="dex:solana:EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        surface="tg_social",
        price=None,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts={"reason": "unpriceable_token_id"},
    )
    rows = await _fetch_enrollments(db)
    assert rows[0]["namespace"] == "dex"


async def test_priceless_alert_enrolls_priced_alert_does_not(db):
    await record_emission(
        db,
        _ledger_settings(),
        kind="alert",
        token_id="0xdeadbeef",
        surface="candidate_alert",
        price=None,  # no in-DB coverage -> enroll
        liquidity=None,
        liquidity_source="none",
        gate_verdicts=None,
    )
    await record_emission(
        db,
        _ledger_settings(),
        kind="alert",
        token_id="tracked-coin",
        surface="candidate_alert",
        price=1.23,  # price in hand -> tracked lane covers it -> no enroll
        liquidity=None,
        liquidity_source="none",
        gate_verdicts=None,
    )
    rows = await _fetch_enrollments(db)
    assert [r["token_id"] for r in rows] == ["0xdeadbeef"]
    assert rows[0]["namespace"] == "other"
    # c2: stamps distinguish "enrolled" from "coverage already exists."
    ledger_rows = await _fetch_rows(db)
    by_token = {r["token_id"]: r["enrollment_status"] for r in ledger_rows}
    assert by_token == {"0xdeadbeef": "enrolled", "tracked-coin": "not_needed"}


async def test_priced_dispatch_does_not_enroll(db):
    await record_emission(
        db,
        _ledger_settings(),
        kind="dispatch",
        token_id="tok",
        surface="gainers_early",
        price=2.0,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts=None,
    )
    assert await _fetch_enrollments(db) == []
    assert (await _fetch_rows(db))[0]["enrollment_status"] == "not_needed"


async def test_reenrollment_refreshes_ttl_preserves_enrolled_at(db):
    """UPSERT (not REPLACE, #325): re-emission extends expires_at only.

    Uses an untracked (dex:) token — coverage-gated enrollment means CG-slug
    tokens no longer enroll (fix/ledger-coverage-gated-enrollment)."""
    settings = _ledger_settings(LEDGER_ENROLLMENT_TTL_DAYS=1)
    for _ in range(2):
        await record_emission(
            db,
            settings,
            kind="gated_out_sample",
            token_id="dex:ethereum:0xreenroll",
            surface="s",
            price=None,
            liquidity=None,
            liquidity_source="none",
            gate_verdicts=None,
        )
    rows = await _fetch_enrollments(db)
    assert len(rows) == 1
    enrolled = datetime.fromisoformat(rows[0]["enrolled_at"])
    expires = datetime.fromisoformat(rows[0]["expires_at"])
    # expires_at tracks the SECOND emission, enrolled_at the first, so the
    # gap is >= TTL (strictly greater whenever any time elapsed between).
    assert expires - enrolled >= timedelta(days=1)


async def test_enrollment_cap_evicts_oldest_expiring_first(db):
    # Untracked (dex:) tokens — coverage-gated enrollment means CG-slug tokens
    # no longer enroll (fix/ledger-coverage-gated-enrollment).
    settings = _ledger_settings(LEDGER_ENROLLMENT_MAX_ACTIVE=3)
    ids = [f"dex:ethereum:0xtok{i}" for i in range(4)]
    for tid in ids:
        await record_emission(
            db,
            settings,
            kind="gated_out_sample",
            token_id=tid,
            surface="s",
            price=None,
            liquidity=None,
            liquidity_source="none",
            gate_verdicts=None,
        )
    rows = await _fetch_enrollments(db)
    assert len(rows) == 3
    # tok0 enrolled first -> earliest expires_at -> evicted first.
    assert [r["token_id"] for r in rows] == ids[1:]
    # c2: the new enrollment always lands, so 'skipped_cap' is unreachable —
    # every row (including the evicted token's) is stamped 'enrolled'; the
    # ledger_enrollment_evicted log (asserted separately) is the censoring
    # record. No retroactive re-stamping.
    statuses = {r["enrollment_status"] for r in await _fetch_rows(db)}
    assert statuses == {"enrolled"}


async def test_enrollment_cap_eviction_emits_named_log(db, monkeypatch):
    """c2: every cap eviction fires a ledger_enrollment_evicted structured
    log NAMING the evicted token_id, so cap-censoring is never silent."""
    import scout.outcome_ledger as ol
    from unittest.mock import MagicMock

    capture = MagicMock()
    monkeypatch.setattr(ol, "log", capture)

    settings = _ledger_settings(LEDGER_ENROLLMENT_MAX_ACTIVE=1)
    # Untracked (dex:) tokens — coverage-gated enrollment means CG-slug tokens
    # no longer enroll (fix/ledger-coverage-gated-enrollment).
    tok_a, tok_b = "dex:ethereum:0xtoka", "dex:ethereum:0xtokb"
    for token in (tok_a, tok_b):
        await record_emission(
            db,
            settings,
            kind="gated_out_sample",
            token_id=token,
            surface="s",
            price=None,
            liquidity=None,
            liquidity_source="none",
            gate_verdicts=None,
        )

    evict_calls = [
        c
        for c in capture.info.call_args_list
        if c.args and c.args[0] == "ledger_enrollment_evicted"
    ]
    assert len(evict_calls) == 1
    kwargs = evict_calls[0].kwargs
    assert kwargs["evicted_token_ids"] == [tok_a]
    assert kwargs["n_evicted"] == 1
    assert kwargs["evicted_for"] == tok_b
    # Only tok-b remains enrolled.
    assert [r["token_id"] for r in await _fetch_enrollments(db)] == [tok_b]


async def test_purge_expired_enrollments_ttl(db):
    from scout.outcome_ledger import active_enrollments, purge_expired_enrollments

    now = _now()
    await db._conn.execute(
        "INSERT INTO ledger_enrollments (token_id, namespace, enrolled_at, expires_at) "
        "VALUES (?, ?, ?, ?), (?, ?, ?, ?)",
        (
            "expired-tok",
            "cg",
            _iso(now - timedelta(days=8)),
            _iso(now - timedelta(days=1)),
            "live-tok",
            "cg",
            _iso(now - timedelta(days=1)),
            _iso(now + timedelta(days=6)),
        ),
    )
    await db._conn.commit()

    purged = await purge_expired_enrollments(db)
    assert purged == 1
    active = await active_enrollments(db)
    assert [t for t, _ in active] == ["live-tok"]


async def test_enrollment_respects_kill_switch(db):
    await record_emission(
        db,
        _ledger_settings(LEDGER_ENABLED=False),
        kind="gated_out_sample",
        token_id="tok",
        surface="s",
        price=None,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts=None,
    )
    assert await _fetch_enrollments(db) == []


async def test_labeler_labels_enrolled_only_token_via_price_cache(db):
    """End-to-end (minus HTTP): an UNTRACKED gated-out token (dex: id, no
    in-DB coverage) gets enrolled, the poller's write lands in price_cache
    (written directly here — HTTP shape covered in
    tests/test_outcome_ledger_poller.py), and the labeler prices the horizon
    from that write. Coverage-gated enrollment
    (fix/ledger-coverage-gated-enrollment): only untracked tokens enroll, so
    the enrollment poller lane exists precisely for this cohort."""
    token_id = "dex:solana:EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    emitted = _now() - timedelta(minutes=20)
    await record_emission(
        db,
        _ledger_settings(),
        kind="gated_out_sample",
        token_id=token_id,
        surface="tg_social",
        price=1.0,
        liquidity=None,
        liquidity_source="none",
        gate_verdicts={"reason": "unpriceable_token_id"},
        emitted_at=_iso(emitted),
    )
    # Untracked -> enrolled even though a price was supplied (kind rule).
    assert [r["token_id"] for r in await _fetch_enrollments(db)] == [token_id]

    # Simulate the per-cycle poller write (db.cache_prices path).
    await db.cache_prices([{"id": token_id, "current_price": 1.4}])

    await label_pending(db, _ledger_settings())
    row = (await _fetch_rows(db))[0]
    assert row["r15m"] == pytest.approx(0.40)
    assert row["label_status"] == "partial"


# ---------------------------------------------------------------------------
# Settings defaults (operator-approved: writer default-on, observe-only)
# ---------------------------------------------------------------------------


def test_settings_ledger_defaults(settings_factory):
    settings = settings_factory()
    assert settings.LEDGER_ENABLED is True
    assert settings.LEDGER_GATED_OUT_SAMPLE_RATE == 25
    assert settings.LEDGER_LABEL_BATCH_MAX == 500
    assert settings.LEDGER_PRICE_CACHE_MAX_LATENESS_MINUTES == 120
    assert settings.LEDGER_ENROLLMENT_TTL_DAYS == 7
    assert settings.LEDGER_ENROLLMENT_MAX_ACTIVE == 200
    assert settings.LEDGER_COVERAGE_FRESHNESS_MIN == 60
