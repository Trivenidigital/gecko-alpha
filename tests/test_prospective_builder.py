"""Prospective watchlist builder (Task 4)."""

from datetime import datetime, timedelta, timezone

from scout.conviction.prospective import build_prospective_watchlist
from scout.db import Database

NOW = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)
T30H = (NOW - timedelta(hours=30)).isoformat()  # sustained (>=24h)
T2H = (NOW - timedelta(hours=2)).isoformat()  # fresh (<24h)
MCAP_FRESH = (NOW - timedelta(minutes=10)).isoformat()


async def _db(tmp_path):
    db = Database(str(tmp_path / "c.db"))
    await db.initialize()
    return db


async def _accel(db, coin_id, at):
    await db._conn.execute(
        "INSERT INTO gainer_acceleration (coin_id, symbol, name, detected_at) VALUES (?,?,?,?)",
        (coin_id, coin_id.upper(), coin_id, at),
    )


async def _momentum(db, coin_id, at):
    await db._conn.execute(
        "INSERT INTO momentum_7d (coin_id, symbol, name, price_change_7d, detected_at) VALUES (?,?,?,?,?)",
        (coin_id, coin_id.upper(), coin_id, 50.0, at),
    )


async def _spike(db, coin_id, at):
    await db._conn.execute(
        "INSERT INTO volume_spikes (coin_id, symbol, name, current_volume, avg_volume_7d, spike_ratio, detected_at) VALUES (?,?,?,?,?,?,?)",
        (coin_id, coin_id.upper(), coin_id, 1000.0, 100.0, 10.0, at),
    )


async def _chain(db, token_id, at):
    await db._conn.execute(
        "INSERT INTO signal_events (token_id, pipeline, event_type, event_data, source_module, created_at) VALUES (?,?,?,?,?,?)",
        (token_id, "chains", "detected", "{}", "test", at),
    )


async def _gainer_snapshot(db, coin_id, at):
    await db._conn.execute(
        "INSERT INTO gainers_snapshots (coin_id, symbol, name, price_change_24h, snapshot_at) VALUES (?,?,?,?,?)",
        (coin_id, coin_id.upper(), coin_id, 25.0, at),
    )


async def _price(db, coin_id, mcap, at):
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, market_cap, updated_at) VALUES (?,?,?)",
        (coin_id, mcap, at),
    )


async def _candidate(db, contract, chain, ticker, at):
    await db._conn.execute(
        "INSERT INTO candidates (contract_address, chain, token_name, ticker, first_seen_at) VALUES (?,?,?,?,?)",
        (contract, chain, ticker, ticker, at),
    )


async def test_builder_core_fold1_fold2_and_mcap(tmp_path):
    db = await _db(tmp_path)
    # "pepe": 4 sustained surfaces -> high; small-cap fresh mcap
    await _accel(db, "pepe", T30H)
    await _momentum(db, "pepe", T30H)
    await _spike(db, "pepe", T30H)
    await _chain(db, "pepe", T30H)  # exact token_id == coin_id
    await _price(db, "pepe", 12_000_000.0, MCAP_FRESH)
    # Fold 2: a base-chain candidate sharing the PEPE ticker must NOT add "pipeline"
    await _candidate(db, "0xbasecontract", "base", "PEPE", T30H)
    # "wassie": 2 sustained surfaces -> watch (full denominator, snapshotted)
    await _accel(db, "wassie", T30H)
    await _momentum(db, "wassie", T30H)
    # "dump": 2 sustained surfaces BUT already on gainers (Fold 1) -> excluded
    await _accel(db, "dump", T30H)
    await _momentum(db, "dump", T30H)
    await _gainer_snapshot(db, "dump", T2H)
    # "emerg": 2 FRESH surfaces (<24h) -> early_count 0 -> low -> not snapshotted
    await _accel(db, "emerg", T2H)
    await _momentum(db, "emerg", T2H)
    await db._conn.commit()

    settings = _make_settings()
    summary = await build_prospective_watchlist(db, settings, now=NOW)

    assert (
        summary["rows_written"] == 2
    )  # pepe (high) + wassie (watch); dump/emerg excluded
    assert summary["high_tier"] == 1
    assert summary["sub30m_high_fresh"] == 1  # pepe is <$30M with fresh mcap

    rows = {r["coin_id"]: r for r in await db.get_latest_conviction_watchlist()}
    assert set(rows) == {"pepe", "wassie"}
    pepe = rows["pepe"]
    assert pepe["tier"] == "high" and pepe["early_count"] == 4
    assert set(pepe["contributing_surfaces"]) == {
        "acceleration",
        "momentum",
        "spikes",
        "chains",
    }
    assert "pipeline" not in pepe["contributing_surfaces"]  # Fold 2: no symbol merge
    assert pepe["market_cap"] == 12_000_000.0
    assert pepe["mcap_age_minutes"] is not None and pepe["mcap_age_minutes"] < 60
    assert rows["wassie"]["tier"] == "watch"
    await db.close()


async def test_empty_universe_writes_run_heartbeat(tmp_path):
    """Fold A: a 0-row run still records a heartbeat (ran, found nothing)."""
    db = await _db(tmp_path)
    summary = await build_prospective_watchlist(db, _make_settings(), now=NOW)
    assert summary["rows_written"] == 0
    assert summary["status"] == "ok"
    assert await db.latest_conviction_watchlist_run_at() == NOW.isoformat()
    assert await db.get_latest_conviction_watchlist() == []
    await db.close()


async def test_exclusion_failure_fails_closed(tmp_path):
    """Fold B: if the pumped-exclusion query fails, do NOT write the snapshot
    (already-pumped coins must not leak), but DO record a run heartbeat."""
    db = await _db(tmp_path)
    await _accel(db, "pepe", T30H)
    await _momentum(db, "pepe", T30H)
    await _spike(db, "pepe", T30H)
    await _chain(db, "pepe", T30H)
    await db._conn.execute("DROP TABLE gainers_snapshots")  # exclusion query will raise
    await db._conn.commit()
    summary = await build_prospective_watchlist(db, _make_settings(), now=NOW)
    assert summary["status"] == "skipped_exclusion_failed"
    assert summary["rows_written"] == 0
    assert await db.get_latest_conviction_watchlist() == []  # no snapshot written
    assert await db.latest_conviction_watchlist_run_at() == NOW.isoformat()  # heartbeat
    await db.close()


async def test_surface_query_failure_marks_run_degraded(tmp_path):
    """P1 fold (silent recall hole): a per-surface query failure (-1 sentinel)
    under-counts the cohort, so the run is flagged non-ok (degraded_surface_failed)
    — letting the watchdog's status!=ok branch alert — while the surviving
    true-positive rows are still written (better degraded data than a silent hole)."""
    db = await _db(tmp_path)
    await _accel(db, "pepe", T30H)
    await _momentum(db, "pepe", T30H)
    await _spike(db, "pepe", T30H)
    await _chain(db, "pepe", T30H)  # 4 surviving surfaces -> still high
    await db._conn.execute("DROP TABLE velocity_alerts")  # one surface query raises
    await db._conn.commit()
    summary = await build_prospective_watchlist(db, _make_settings(), now=NOW)
    assert summary["status"] == "degraded_surface_failed"
    assert summary["per_surface_contrib"]["velocity"] == -1
    assert summary["rows_written"] == 1  # pepe still written (true positive)
    rows = await db.get_latest_conviction_watchlist()
    assert [r["coin_id"] for r in rows] == ["pepe"]
    # heartbeat recorded with the degraded status (watchdog reads this)
    run = await db.latest_conviction_watchlist_run()
    assert run["status"] == "degraded_surface_failed"
    await db.close()


def _make_settings():
    from scout.config import Settings

    return Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
