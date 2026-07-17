"""ALR-02 detection-time alert lane tests.

The detection lane fires an "early candidate detected" Telegram alert on the
SCORING pass (before the paper dispatch gate), keyed on candidate freshness +
absence of a CG trending reference. Default OFF. Reuses tg_alert_log
(signal_type='detection_lane', detail='detection_lane[:reason]') — no schema
change. See tasks/design_detection_time_alert_lane.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.config import Settings
from scout.db import Database
from scout.models import CandidateToken
from scout.trading.detection_alert import (
    _detection_trigger,
    _passes_quality_gate,
    format_detection_alert,
    notify_early_detections,
)

_REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "x",
    "TELEGRAM_CHAT_ID": "x",
    "ANTHROPIC_API_KEY": "x",
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**_REQUIRED, **overrides})


def _cand(
    token_id: str,
    *,
    symbol: str = "WIF",
    name: str = "dogwifhat",
    chain: str = "coingecko",
    quant_score: int | None = 8,
    signals_fired: list[str] | None = None,
) -> CandidateToken:
    # Defaults clear the ALR-02 quality gate (a fired signal + non-zero score)
    # so pre-gate tests that expect a send keep passing. Pass quant_score=0 /
    # signals_fired=[] to model a score-0 candidate the gate must exclude.
    return CandidateToken(
        contract_address=token_id,
        chain=chain,
        token_name=name,
        ticker=symbol,
        quant_score=quant_score,
        signals_fired=(
            ["cg_trending_rank"] if signals_fired is None else signals_fired
        ),
    )


async def _insert_candidate(
    db: Database,
    token_id: str,
    *,
    mcap: float = 45_000_000.0,
    first_seen_min_ago: float = 8.0,
    symbol: str = "WIF",
    name: str = "dogwifhat",
) -> None:
    fs = (
        datetime.now(timezone.utc) - timedelta(minutes=first_seen_min_ago)
    ).isoformat()
    await db._conn.execute(
        "INSERT INTO candidates "
        "(contract_address, chain, token_name, ticker, market_cap_usd, "
        " first_seen_at) VALUES (?, 'coingecko', ?, ?, ?, ?)",
        (token_id, name, symbol, mcap, fs),
    )
    await db._conn.commit()


async def _insert_price(
    db: Database, token_id: str, *, price: float = 0.0234, mcap: float = 45_000_000.0
) -> None:
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache "
        "(coin_id, current_price, market_cap, updated_at) VALUES (?, ?, ?, ?)",
        (token_id, price, mcap, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()


async def _insert_trending(
    db: Database, token_id: str, *, snapshot_min_ago: float
) -> None:
    snap = (
        datetime.now(timezone.utc) - timedelta(minutes=snapshot_min_ago)
    ).isoformat()
    await db._conn.execute(
        "INSERT INTO trending_snapshots "
        "(coin_id, symbol, name, snapshot_at, created_at) VALUES (?, ?, ?, ?, ?)",
        (token_id, "WIF", "dogwifhat", snap, snap),
    )
    await db._conn.commit()


def _capture_send(monkeypatch):
    sent: list[str] = []

    async def _fake_send(text, session, settings, **kwargs):
        sent.append(text)

    monkeypatch.setattr("scout.alerter.send_telegram_message", _fake_send)
    return sent


def _block_send(monkeypatch):
    async def _no_send(*args, **kwargs):
        raise AssertionError("blocked candidate must not send")

    monkeypatch.setattr("scout.alerter.send_telegram_message", _no_send)


# ---------- _detection_trigger (pure predicate) ----------


def test_trigger_no_reference_fires():
    assert _detection_trigger(None, "no_reference") is True


def test_trigger_ok_negative_lead_fires():
    """Negative lead_time = detected BEFORE trending crossover = early."""
    assert _detection_trigger(-42.0, "ok") is True


def test_trigger_ok_zero_lead_does_not_fire():
    assert _detection_trigger(0.0, "ok") is False


def test_trigger_ok_positive_lead_does_not_fire():
    """Positive lead_time = already trending / late = not an early detection."""
    assert _detection_trigger(120.0, "ok") is False


def test_trigger_error_does_not_fire():
    assert _detection_trigger(None, "error") is False


def test_trigger_ok_none_lead_does_not_fire():
    assert _detection_trigger(None, "ok") is False


# ---------- _passes_quality_gate (pure predicate) ----------


def test_gate_passes_signal_and_score():
    assert (
        _passes_quality_gate(
            _cand("x", quant_score=8, signals_fired=["cg_trending_rank"]), _settings()
        )
        is True
    )


def test_gate_blocks_empty_signals():
    assert (
        _passes_quality_gate(_cand("x", quant_score=0, signals_fired=[]), _settings())
        is False
    )


def test_gate_blocks_score_below_bar():
    s = _settings(DETECTION_ALERT_MIN_QUANT_SCORE=5)
    assert (
        _passes_quality_gate(
            _cand("x", quant_score=4, signals_fired=["market_cap_range"]), s
        )
        is False
    )


def test_gate_min_score_zero_disables_gate():
    """MIN_QUANT_SCORE=0 is the single-knob off switch: a zero-score candidate
    passes (score 0 >= 0)."""
    s = _settings(DETECTION_ALERT_MIN_QUANT_SCORE=0)
    assert _passes_quality_gate(_cand("x", quant_score=0, signals_fired=[]), s) is True


def test_gate_handles_none_fields():
    """Model defaults (quant_score=None, signals_fired=None) never crash the
    gate — they read as an un-scored candidate and are blocked."""
    c = CandidateToken(
        contract_address="x", chain="coingecko", token_name="n", ticker="T"
    )
    assert _passes_quality_gate(c, _settings()) is False


# ---------- format_detection_alert (golden file) ----------


def test_format_detection_alert_golden_no_reference():
    body = format_detection_alert(
        symbol="WIF",
        coin_id="dogwifhat",
        price=0.0234,
        mcap=45_000_000.0,
        first_seen_min_ago=8.0,
        lead_time_min=None,
        lead_time_status="no_reference",
        dashboard_base_url="http://89.167.116.187:8000",
    )
    assert body == (
        "🔎 EARLY DETECT · WIF · $0.0234 · $45.0M\n"
        "first seen 8 min ago · not yet on CG trending\n"
        "coingecko.com/en/coins/dogwifhat\n"
        "Dashboard: http://89.167.116.187:8000/#/token/dogwifhat"
    )


def test_format_detection_alert_ahead_of_trending():
    body = format_detection_alert(
        symbol="WIF",
        coin_id="dogwifhat",
        price=0.0234,
        mcap=45_000_000.0,
        first_seen_min_ago=3.0,
        lead_time_min=-15.0,
        lead_time_status="ok",
        dashboard_base_url="",
    )
    # ok+negative → "N min ahead"; empty dashboard base → no Dashboard line.
    assert body == (
        "🔎 EARLY DETECT · WIF · $0.0234 · $45.0M\n"
        "first seen 3 min ago · 15 min ahead of CG trending\n"
        "coingecko.com/en/coins/dogwifhat"
    )


# ---------- notify_early_detections integration ----------


@pytest.mark.asyncio
async def test_flag_off_is_inert(tmp_path, monkeypatch):
    """Default OFF: no send, no tg_alert_log rows."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings()  # DETECTION_ALERT_LANE_ENABLED defaults False
    await _insert_candidate(db, "dogwifhat")
    await _insert_price(db, "dogwifhat")
    _block_send(monkeypatch)

    await notify_early_detections(
        db, settings, session=None, candidates=[_cand("dogwifhat")]
    )
    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_alert_log")
    assert (await cur.fetchone())[0] == 0
    await db.close()


@pytest.mark.asyncio
async def test_happy_path_fires_and_logs(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(DETECTION_ALERT_LANE_ENABLED=True)
    await _insert_candidate(db, "dogwifhat")
    await _insert_price(db, "dogwifhat")
    sent = _capture_send(monkeypatch)

    await notify_early_detections(
        db, settings, session=None, candidates=[_cand("dogwifhat")]
    )
    assert len(sent) == 1
    assert sent[0].startswith("🔎 EARLY DETECT · WIF")
    cur = await db._conn.execute(
        "SELECT outcome, detail, signal_type, paper_trade_id "
        "FROM tg_alert_log WHERE token_id='dogwifhat'"
    )
    outcome, detail, signal_type, ptid = await cur.fetchone()
    assert outcome == "sent"
    assert detail == "detection_lane"
    assert signal_type == "detection_lane"
    assert ptid is None
    await db.close()


@pytest.mark.asyncio
async def test_non_cg_candidate_skipped(tmp_path, monkeypatch):
    """DEX-address candidates (chain != coingecko) never fire."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(DETECTION_ALERT_LANE_ENABLED=True)
    _block_send(monkeypatch)

    await notify_early_detections(
        db,
        settings,
        session=None,
        candidates=[_cand("0xdeadbeef", chain="solana")],
    )
    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_alert_log")
    assert (await cur.fetchone())[0] == 0
    await db.close()


@pytest.mark.asyncio
async def test_stale_candidate_skipped(tmp_path, monkeypatch):
    """A candidate older than DETECTION_ALERT_MAX_AGE_MIN is not surfaced."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(
        DETECTION_ALERT_LANE_ENABLED=True, DETECTION_ALERT_MAX_AGE_MIN=180
    )
    await _insert_candidate(db, "dogwifhat", first_seen_min_ago=600.0)
    await _insert_price(db, "dogwifhat")
    _block_send(monkeypatch)

    await notify_early_detections(
        db, settings, session=None, candidates=[_cand("dogwifhat")]
    )
    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_alert_log")
    assert (await cur.fetchone())[0] == 0
    await db.close()


@pytest.mark.asyncio
async def test_already_trending_does_not_fire(tmp_path, monkeypatch):
    """A candidate already on CG trending (positive lead) is late, not early."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(DETECTION_ALERT_LANE_ENABLED=True)
    await _insert_candidate(db, "dogwifhat", first_seen_min_ago=8.0)
    await _insert_price(db, "dogwifhat")
    # Trending crossover 30 min ago, candidate first seen 8 min ago → the coin
    # trended BEFORE this detection instant → lead_time positive → not early.
    await _insert_trending(db, "dogwifhat", snapshot_min_ago=30.0)
    _block_send(monkeypatch)

    await notify_early_detections(
        db, settings, session=None, candidates=[_cand("dogwifhat")]
    )
    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_alert_log")
    assert (await cur.fetchone())[0] == 0
    await db.close()


@pytest.mark.asyncio
async def test_universe_filter_blocks_tokenized(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(
        DETECTION_ALERT_LANE_ENABLED=True, ALERT_UNIVERSE_FILTER_ENABLED=True
    )
    await _insert_candidate(db, "spy-bstocks-tokenized-stock")
    await _insert_price(db, "spy-bstocks-tokenized-stock")
    _block_send(monkeypatch)

    await notify_early_detections(
        db,
        settings,
        session=None,
        candidates=[_cand("spy-bstocks-tokenized-stock", symbol="SPY")],
    )
    cur = await db._conn.execute(
        "SELECT outcome, detail FROM tg_alert_log "
        "WHERE token_id='spy-bstocks-tokenized-stock'"
    )
    outcome, detail = await cur.fetchone()
    assert outcome == "blocked_eligibility"
    assert detail == "detection_lane:universe_filter:-tokenized-"
    await db.close()


@pytest.mark.asyncio
async def test_daily_rate_limit(tmp_path, monkeypatch):
    """MAX_PER_DAY=1: first candidate sends, second is rate-limited."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(
        DETECTION_ALERT_LANE_ENABLED=True, DETECTION_ALERT_MAX_PER_DAY=1
    )
    await _insert_candidate(db, "coin-a", first_seen_min_ago=2.0)
    await _insert_candidate(db, "coin-b", first_seen_min_ago=9.0)
    await _insert_price(db, "coin-a")
    await _insert_price(db, "coin-b")
    sent = _capture_send(monkeypatch)

    await notify_early_detections(
        db,
        settings,
        session=None,
        candidates=[_cand("coin-a"), _cand("coin-b")],
    )
    # Only 1 sent; freshest-first → coin-a (2 min) wins the single slot.
    assert len(sent) == 1
    cur = await db._conn.execute(
        "SELECT token_id, outcome, detail FROM tg_alert_log ORDER BY token_id"
    )
    rows = await cur.fetchall()
    by_token = {r[0]: (r[1], r[2]) for r in rows}
    assert by_token["coin-a"] == ("sent", "detection_lane")
    assert by_token["coin-b"] == ("blocked_cooldown", "detection_lane:rate_limit")
    await db.close()


@pytest.mark.asyncio
async def test_daily_cap_counts_preexisting_sent_rows(tmp_path, monkeypatch):
    """Cap counts today's sent detection_lane rows already in the table."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(
        DETECTION_ALERT_LANE_ENABLED=True, DETECTION_ALERT_MAX_PER_DAY=1
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO tg_alert_log "
        "(paper_trade_id, signal_type, token_id, alerted_at, outcome, detail) "
        "VALUES (NULL, 'detection_lane', 'earlier-coin', ?, 'sent', 'detection_lane')",
        (now_iso,),
    )
    await db._conn.commit()
    await _insert_candidate(db, "dogwifhat")
    await _insert_price(db, "dogwifhat")
    _block_send(monkeypatch)

    await notify_early_detections(
        db, settings, session=None, candidates=[_cand("dogwifhat")]
    )
    # Budget already spent by the pre-existing row → new candidate not sent.
    cur = await db._conn.execute(
        "SELECT outcome, detail FROM tg_alert_log WHERE token_id='dogwifhat'"
    )
    outcome, detail = await cur.fetchone()
    assert outcome == "blocked_cooldown"
    assert detail == "detection_lane:rate_limit"
    await db.close()


@pytest.mark.asyncio
async def test_dedup_24h(tmp_path, monkeypatch):
    """A prior sent detection_lane row within 24h suppresses re-send."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(DETECTION_ALERT_LANE_ENABLED=True)
    prior = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await db._conn.execute(
        "INSERT INTO tg_alert_log "
        "(paper_trade_id, signal_type, token_id, alerted_at, outcome, detail) "
        "VALUES (NULL, 'detection_lane', 'dogwifhat', ?, 'sent', 'detection_lane')",
        (prior,),
    )
    await db._conn.commit()
    await _insert_candidate(db, "dogwifhat")
    await _insert_price(db, "dogwifhat")
    _block_send(monkeypatch)

    await notify_early_detections(
        db, settings, session=None, candidates=[_cand("dogwifhat")]
    )
    cur = await db._conn.execute(
        "SELECT outcome, detail FROM tg_alert_log "
        "WHERE token_id='dogwifhat' AND alerted_at > ?",
        (prior,),
    )
    outcome, detail = await cur.fetchone()
    assert outcome == "blocked_cooldown"
    assert detail == "detection_lane:dedup_24h"
    await db.close()


@pytest.mark.asyncio
async def test_dedup_disabled_window_zero(tmp_path, monkeypatch):
    """TG_ALERT_DEDUP_WINDOW_HOURS=0 disables dedup — a fresh candidate sends
    even with a prior sent row."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(
        DETECTION_ALERT_LANE_ENABLED=True, TG_ALERT_DEDUP_WINDOW_HOURS=0
    )
    prior = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await db._conn.execute(
        "INSERT INTO tg_alert_log "
        "(paper_trade_id, signal_type, token_id, alerted_at, outcome, detail) "
        "VALUES (NULL, 'detection_lane', 'dogwifhat', ?, 'sent', 'detection_lane')",
        (prior,),
    )
    await db._conn.commit()
    await _insert_candidate(db, "dogwifhat")
    await _insert_price(db, "dogwifhat")
    sent = _capture_send(monkeypatch)

    await notify_early_detections(
        db, settings, session=None, candidates=[_cand("dogwifhat")]
    )
    assert len(sent) == 1
    await db.close()


# ---------- ALR-02 quality gate + score-ordered slots ----------


@pytest.mark.asyncio
async def test_quality_gate_excludes_zero_score(tmp_path, monkeypatch):
    """A quant_score=0 / signals_fired=[] candidate is dropped upstream of the
    cap: no send, and (being a silent upstream skip) no tg_alert_log row."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(DETECTION_ALERT_LANE_ENABLED=True)
    await _insert_candidate(db, "dogwifhat")
    await _insert_price(db, "dogwifhat")
    _block_send(monkeypatch)

    await notify_early_detections(
        db,
        settings,
        session=None,
        candidates=[_cand("dogwifhat", quant_score=0, signals_fired=[])],
    )
    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_alert_log")
    assert (await cur.fetchone())[0] == 0
    await db.close()


@pytest.mark.asyncio
async def test_quality_gate_includes_qualifying(tmp_path, monkeypatch):
    """A candidate that fired a signal (non-zero score) clears the gate."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(DETECTION_ALERT_LANE_ENABLED=True)
    await _insert_candidate(db, "dogwifhat")
    await _insert_price(db, "dogwifhat")
    sent = _capture_send(monkeypatch)

    await notify_early_detections(
        db,
        settings,
        session=None,
        candidates=[
            _cand("dogwifhat", quant_score=8, signals_fired=["cg_trending_rank"])
        ],
    )
    assert len(sent) == 1
    await db.close()


@pytest.mark.asyncio
async def test_score_ordered_selection_beats_freshness(tmp_path, monkeypatch):
    """With one slot, the HIGHER-scoring candidate wins even when it is older
    than a fresher-but-lower-scoring one (score-desc, not age-asc)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(
        DETECTION_ALERT_LANE_ENABLED=True, DETECTION_ALERT_MAX_PER_DAY=1
    )
    # coin-hi: older (100 min) but higher score; coin-lo: fresher (2 min), lower.
    await _insert_candidate(db, "coin-hi", first_seen_min_ago=100.0)
    await _insert_candidate(db, "coin-lo", first_seen_min_ago=2.0)
    await _insert_price(db, "coin-hi")
    await _insert_price(db, "coin-lo")
    sent = _capture_send(monkeypatch)

    await notify_early_detections(
        db,
        settings,
        session=None,
        candidates=[
            _cand("coin-lo", quant_score=5, signals_fired=["market_cap_range"]),
            _cand("coin-hi", quant_score=20, signals_fired=["vol_acceleration"]),
        ],
    )
    assert len(sent) == 1
    cur = await db._conn.execute(
        "SELECT token_id, outcome, detail FROM tg_alert_log ORDER BY token_id"
    )
    by_token = {r[0]: (r[1], r[2]) for r in await cur.fetchall()}
    assert by_token["coin-hi"] == ("sent", "detection_lane")
    assert by_token["coin-lo"] == ("blocked_cooldown", "detection_lane:rate_limit")
    await db.close()


@pytest.mark.asyncio
async def test_cap_enforced_after_gating(tmp_path, monkeypatch):
    """The cap still binds AFTER gating: a zero-score candidate is gated out
    (never audited, never consumes a slot) while two qualifying candidates
    contend for the single slot."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(
        DETECTION_ALERT_LANE_ENABLED=True, DETECTION_ALERT_MAX_PER_DAY=1
    )
    await _insert_candidate(db, "coin-a", first_seen_min_ago=2.0)
    await _insert_candidate(db, "coin-b", first_seen_min_ago=9.0)
    await _insert_candidate(db, "coin-noise", first_seen_min_ago=1.0)
    for cid in ("coin-a", "coin-b", "coin-noise"):
        await _insert_price(db, cid)
    sent = _capture_send(monkeypatch)

    await notify_early_detections(
        db,
        settings,
        session=None,
        candidates=[
            _cand("coin-a", quant_score=8, signals_fired=["cg_trending_rank"]),
            _cand("coin-b", quant_score=8, signals_fired=["cg_trending_rank"]),
            _cand("coin-noise", quant_score=0, signals_fired=[]),
        ],
    )
    assert len(sent) == 1
    cur = await db._conn.execute(
        "SELECT token_id, outcome, detail FROM tg_alert_log ORDER BY token_id"
    )
    by_token = {r[0]: (r[1], r[2]) for r in await cur.fetchall()}
    # Equal score → freshest (coin-a, 2 min) wins the slot; coin-b rate-limited.
    assert by_token["coin-a"] == ("sent", "detection_lane")
    assert by_token["coin-b"] == ("blocked_cooldown", "detection_lane:rate_limit")
    # The gated-out zero-score candidate is never audited.
    assert "coin-noise" not in by_token
    await db.close()


@pytest.mark.asyncio
async def test_min_quant_score_threshold_excludes_below_bar(tmp_path, monkeypatch):
    """DETECTION_ALERT_MIN_QUANT_SCORE gates on the numeric bar independently:
    a signals-fired candidate scoring below the bar is excluded."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(
        DETECTION_ALERT_LANE_ENABLED=True, DETECTION_ALERT_MIN_QUANT_SCORE=5
    )
    await _insert_candidate(db, "dogwifhat")
    await _insert_price(db, "dogwifhat")
    _block_send(monkeypatch)

    await notify_early_detections(
        db,
        settings,
        session=None,
        # qs=4 fired a signal but is below the numeric bar of 5.
        candidates=[
            _cand("dogwifhat", quant_score=4, signals_fired=["market_cap_range"])
        ],
    )
    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_alert_log")
    assert (await cur.fetchone())[0] == 0
    await db.close()


@pytest.mark.asyncio
async def test_gate_disabled_sends_zero_score(tmp_path, monkeypatch):
    """MIN_QUANT_SCORE=0 (the single-knob off switch / rollback) restores the
    ungated behavior — a zero-score candidate sends."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(
        DETECTION_ALERT_LANE_ENABLED=True,
        DETECTION_ALERT_MIN_QUANT_SCORE=0,
    )
    await _insert_candidate(db, "dogwifhat")
    await _insert_price(db, "dogwifhat")
    sent = _capture_send(monkeypatch)

    await notify_early_detections(
        db,
        settings,
        session=None,
        candidates=[_cand("dogwifhat", quant_score=0, signals_fired=[])],
    )
    assert len(sent) == 1
    await db.close()
