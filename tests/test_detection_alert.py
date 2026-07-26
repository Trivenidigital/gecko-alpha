"""ALR-02 detection-time alert lane tests.

The detection lane fires an "early candidate detected" Telegram alert on the
SCORING pass (before the paper dispatch gate), keyed on candidate freshness +
absence of a CG trending reference. Default OFF. Reuses tg_alert_log
(signal_type='detection_lane', detail='detection_lane[:reason]') — no schema
change. See tasks/design_detection_time_alert_lane.md.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from scout.config import Settings
from scout.db import Database
from scout.models import CandidateToken
from scout.trading.detection_alert import (
    DETECTION_CODE_VERSION,
    DETECTION_GATE_COMPARATOR,
    DETECTION_GATE_VERSION,
    _detection_trigger,
    _passes_quality_gate,
    _receipt_idempotency_key,
    _write_detection_receipt,
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
    # rate-limit overflow is LOG-ONLY since the audit-flood fix: no DB row.
    assert "coin-b" not in by_token
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
    # LOG-ONLY rate limiting: budget exhaustion writes no audit row (flood fix).
    assert await cur.fetchone() is None
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
    # rate-limit overflow is LOG-ONLY since the audit-flood fix: no DB row.
    assert "coin-lo" not in by_token
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
    # rate-limit overflow is LOG-ONLY since the audit-flood fix: no DB row.
    assert "coin-b" not in by_token
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


# ==================================================================
# Decision-receipt audit (feat/detection-decision-receipts) — one test
# per reviewer lock/correction. See tasks/prereg_detection_gate_enrichment_cohort.md.
# ==================================================================


async def _fetch_receipts(db: Database, token_id: str | None = None) -> list[dict]:
    q = "SELECT * FROM detection_decision_receipts"
    params: tuple = ()
    if token_id is not None:
        q += " WHERE token_id = ?"
        params = (token_id,)
    q += " ORDER BY id"
    cur = await db._conn.execute(q, params)
    return [dict(r) for r in await cur.fetchall()]


def _receipt_kwargs(**overrides) -> dict:
    """Full arg set for db.insert_detection_decision_receipt with sane defaults."""
    d = dict(
        token_id="tok",
        decided_at="2026-07-20T12:00:00+00:00",
        outcome="gate_fail_quality",
        reason=None,
        source_observation_ts="2026-07-20T11:55:00+00:00",
        gate_version=DETECTION_GATE_VERSION,
        code_version=DETECTION_CODE_VERSION,
        score_before=0,
        score_after=0,
        comparator=DETECTION_GATE_COMPARATOR,
        threshold_value=1,
        signals_fired=None,
        raw_inputs=None,
        idempotency_key="key-default",
    )
    d.update(overrides)
    return d


# ---------- LOCK 3 idempotency recipe (documented sha256 recipe) ----------


def test_idempotency_key_recipe_matches_sha256():
    import hashlib

    key = _receipt_idempotency_key("tok", "sent", "2026-07-20T11:00:00+00:00", "466.1")
    raw = "tok|sent|2026-07-20T11:00:00+00:00|466.1"
    assert key == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    # A None source-observation renders as the empty string.
    key2 = _receipt_idempotency_key("tok", "too_old", None, "466.1")
    assert key2 == hashlib.sha256("tok|too_old||466.1".encode("utf-8")).hexdigest()


# ---------- correction 1: both arms share the index_decision_at anchor ----------


@pytest.mark.asyncio
async def test_receipt_index_anchor_shared_across_arms(tmp_path, monkeypatch):
    """A gate-FAILER and a gate-PASSER evaluated in the SAME cycle get index
    receipts with the SAME anchor (decided_at) — outcome windows are measured
    identically for both arms (reviewer correction 1)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(DETECTION_ALERT_LANE_ENABLED=True)
    await _insert_candidate(db, "passer", first_seen_min_ago=5.0)
    await _insert_price(db, "passer")
    await _insert_candidate(db, "failer", first_seen_min_ago=6.0)
    await _insert_price(db, "failer")
    _capture_send(monkeypatch)

    await notify_early_detections(
        db,
        settings,
        session=None,
        candidates=[
            _cand("passer", quant_score=8, signals_fired=["cg_trending_rank"]),
            _cand("failer", quant_score=0, signals_fired=[]),
        ],
    )
    passer = await _fetch_receipts(db, "passer")
    failer = await _fetch_receipts(db, "failer")
    assert len(passer) == 1 and len(failer) == 1
    assert passer[0]["outcome"] == "sent"
    assert failer[0]["outcome"] == "gate_fail_quality"
    # Identical same-cycle index anchor across both arms.
    assert passer[0]["decided_at"] == failer[0]["decided_at"]
    await db.close()


# ---------- correction 2: raw inputs + applied decision logic persisted ----------


@pytest.mark.asyncio
async def test_receipt_fields_persisted_gate_fail(tmp_path, monkeypatch):
    """A gate-FAIL receipt carries every LOCK-2 field + the raw decision inputs:
    score before/after, comparator, threshold, versions, raw_inputs blob."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(
        DETECTION_ALERT_LANE_ENABLED=True, DETECTION_ALERT_MIN_QUANT_SCORE=5
    )
    await _insert_candidate(db, "failer", first_seen_min_ago=6.0)
    await _insert_price(db, "failer")
    _block_send(monkeypatch)

    await notify_early_detections(
        db,
        settings,
        session=None,
        candidates=[_cand("failer", quant_score=4, signals_fired=["market_cap_range"])],
    )
    rows = await _fetch_receipts(db, "failer")
    assert len(rows) == 1
    r = rows[0]
    assert r["token_id"] == "failer"
    assert r["decided_at"]  # UTC decision timestamp, non-null
    assert r["outcome"] == "gate_fail_quality"
    assert r["gate_version"] == DETECTION_GATE_VERSION
    assert r["code_version"] == DETECTION_CODE_VERSION
    assert r["score_before"] == 4  # raw, pre-clip
    assert r["score_after"] == 4  # operand actually compared
    assert r["comparator"] == ">="
    assert r["threshold_value"] == 5
    assert json.loads(r["signals_fired"]) == ["market_cap_range"]
    assert r["source_observation_ts"]  # first_seen the decision was based on
    assert len(r["idempotency_key"]) == 64
    raw = json.loads(r["raw_inputs"])
    assert raw["quant_score_raw"] == 4
    assert raw["quant_score_missing"] is False
    assert raw["score_before"] == 4 and raw["score_after"] == 4
    assert raw["comparator"] == ">=" and raw["threshold"] == 5
    assert raw["gate_expr"] == "score_after >= 5"
    # Idempotency key reproduces from the documented recipe.
    assert r["idempotency_key"] == _receipt_idempotency_key(
        "failer",
        "gate_fail_quality",
        r["source_observation_ts"],
        DETECTION_GATE_VERSION,
    )
    await db.close()


@pytest.mark.asyncio
async def test_receipt_fields_persisted_sent(tmp_path, monkeypatch):
    """A SENT receipt carries the same LOCK-2 fields + raw inputs on the pass
    arm (reviewer correction 2)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(DETECTION_ALERT_LANE_ENABLED=True)
    await _insert_candidate(db, "dogwifhat", first_seen_min_ago=5.0)
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
    rows = await _fetch_receipts(db, "dogwifhat")
    assert len(rows) == 1
    r = rows[0]
    assert r["outcome"] == "sent"
    assert r["gate_version"] == DETECTION_GATE_VERSION
    assert r["code_version"] == DETECTION_CODE_VERSION
    assert r["score_before"] == 8 and r["score_after"] == 8
    assert r["comparator"] == ">=" and r["threshold_value"] == 1
    assert json.loads(r["signals_fired"]) == ["cg_trending_rank"]
    assert r["source_observation_ts"]
    assert len(r["idempotency_key"]) == 64
    raw = json.loads(r["raw_inputs"])
    assert raw["lead_time_status"] in ("no_reference", "ok")
    await db.close()


@pytest.mark.asyncio
async def test_receipt_score_before_after_clip_on_unscored(tmp_path, monkeypatch):
    """score_before (raw None) vs score_after (clipped 0) distinguishes an
    unscored candidate from a genuine 0 (reviewer correction 2)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(DETECTION_ALERT_LANE_ENABLED=True)
    await _insert_candidate(db, "unscored", first_seen_min_ago=5.0)
    await _insert_price(db, "unscored")
    _block_send(monkeypatch)
    # quant_score=None, signals_fired=None (model defaults) → unscored.
    c = CandidateToken(
        contract_address="unscored", chain="coingecko", token_name="n", ticker="T"
    )

    await notify_early_detections(db, settings, session=None, candidates=[c])
    rows = await _fetch_receipts(db, "unscored")
    assert len(rows) == 1 and rows[0]["outcome"] == "gate_fail_quality"
    assert rows[0]["score_before"] is None  # raw None preserved as NULL
    assert rows[0]["score_after"] == 0  # clipped operand
    assert rows[0]["signals_fired"] is None
    raw = json.loads(rows[0]["raw_inputs"])
    assert raw["quant_score_missing"] is True
    assert raw["signals_fired_missing"] is True
    await db.close()


# ---------- correction 3: idempotent replay vs conflicting duplicate ----------


@pytest.mark.asyncio
async def test_receipt_idempotent_replay_counted_as_replay(tmp_path):
    """Same key + same payload (different decided_at) → idempotent_replay, one
    row, FIRST decided_at preserved."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    r1 = await db.insert_detection_decision_receipt(
        **_receipt_kwargs(idempotency_key="k1")
    )
    assert r1 == "inserted"
    r2 = await db.insert_detection_decision_receipt(
        **_receipt_kwargs(idempotency_key="k1", decided_at="2026-07-20T13:00:00+00:00")
    )
    assert r2 == "idempotent_replay"
    rows = await _fetch_receipts(db, "tok")
    assert len(rows) == 1
    assert rows[0]["decided_at"] == "2026-07-20T12:00:00+00:00"  # first write kept
    await db.close()


@pytest.mark.asyncio
async def test_receipt_conflicting_duplicate_detected(tmp_path):
    """Same key + materially different payload → conflict; original preserved."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    r1 = await db.insert_detection_decision_receipt(
        **_receipt_kwargs(idempotency_key="k2", score_after=0)
    )
    assert r1 == "inserted"
    r2 = await db.insert_detection_decision_receipt(
        **_receipt_kwargs(idempotency_key="k2", score_after=99)
    )
    assert r2 == "conflict"
    rows = await _fetch_receipts(db, "tok")
    assert len(rows) == 1 and rows[0]["score_after"] == 0  # original untouched
    await db.close()


@pytest.mark.asyncio
async def test_receipt_conflict_counted_and_warned():
    """When the DB reports a conflict, the writer counts it AND warns (never
    hides it) — reviewer correction 3."""

    class _StubDB:
        async def insert_detection_decision_receipt(self, **kw):
            return "conflict"

    settings = _settings()
    counters = {
        "evaluated": 0,
        "receipts_written": 0,
        "exact_replays": 0,
        "write_failures": 0,
        "conflicts": 0,
    }
    with capture_logs() as logs:
        await _write_detection_receipt(
            _StubDB(),
            settings,
            counters,
            token_id="t",
            outcome="sent",
            reason=None,
            cand=_cand("t"),
            source_observation_ts="2026-07-20T11:00:00+00:00",
            decided_at="2026-07-20T12:00:00+00:00",
        )
    # A conflict is evaluated + counted as a conflict, but is NOT a new insert.
    assert counters["evaluated"] == 1
    assert counters["conflicts"] == 1
    assert counters["receipts_written"] == 0
    assert counters["exact_replays"] == 0
    assert counters["write_failures"] == 0
    assert any(e["event"] == "detection_receipt_conflict" for e in logs)


# ---------- correction 3 / LOCK 4: failures AND conflicts in the summary ----------


@pytest.mark.asyncio
async def test_receipt_summary_counts_write_failures(tmp_path, monkeypatch):
    """A receipt-write failure is caught, logged, counted, and reconciled in the
    per-cycle detection_receipt_summary (evaluated == written + failures)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(DETECTION_ALERT_LANE_ENABLED=True)
    await _insert_candidate(db, "failer", first_seen_min_ago=6.0)
    await _insert_price(db, "failer")
    _block_send(monkeypatch)

    async def _raise(**kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(db, "insert_detection_decision_receipt", _raise)
    with capture_logs() as logs:
        await notify_early_detections(
            db,
            settings,
            session=None,
            candidates=[_cand("failer", quant_score=0, signals_fired=[])],
        )
    summ = [e for e in logs if e["event"] == "detection_receipt_summary"][-1]
    assert summ["evaluated_n"] == 1
    assert summ["write_failures_n"] == 1
    assert summ["receipts_written_n"] == 0
    assert summ["exact_replays_n"] == 0
    assert summ["conflicting_duplicates_n"] == 0
    # Reconciliation identity holds.
    assert summ["evaluated_n"] == (
        summ["receipts_written_n"]
        + summ["exact_replays_n"]
        + summ["write_failures_n"]
        + summ["conflicting_duplicates_n"]
    )
    assert any(e["event"] == "detection_receipt_write_failed" for e in logs)
    await db.close()


@pytest.mark.asyncio
async def test_receipt_summary_counts_conflicts(tmp_path, monkeypatch):
    """conflicting_duplicates_n surfaces in the per-cycle summary so a conflict
    can never pass as healthy coverage (reviewer correction 3). A conflict is
    NOT a new insert, so receipts_written_n stays 0."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(DETECTION_ALERT_LANE_ENABLED=True)
    await _insert_candidate(db, "failer", first_seen_min_ago=6.0)
    await _insert_price(db, "failer")
    _block_send(monkeypatch)

    async def _conflict(**kw):
        return "conflict"

    monkeypatch.setattr(db, "insert_detection_decision_receipt", _conflict)
    with capture_logs() as logs:
        await notify_early_detections(
            db,
            settings,
            session=None,
            candidates=[_cand("failer", quant_score=0, signals_fired=[])],
        )
    summ = [e for e in logs if e["event"] == "detection_receipt_summary"][-1]
    assert summ["evaluated_n"] == 1
    assert summ["conflicting_duplicates_n"] == 1
    assert summ["receipts_written_n"] == 0
    assert summ["exact_replays_n"] == 0
    assert summ["write_failures_n"] == 0
    assert summ["evaluated_n"] == (
        summ["receipts_written_n"]
        + summ["exact_replays_n"]
        + summ["write_failures_n"]
        + summ["conflicting_duplicates_n"]
    )
    assert any(e["event"] == "detection_receipt_conflict" for e in logs)
    await db.close()


# ---------- amendment 1: not_early preserves the pre-index trending timestamp --


@pytest.mark.asyncio
async def test_not_early_receipt_preserves_pre_index_trending_at(tmp_path, monkeypatch):
    """A gate-PASSER that CG had already trended past gets a not_early receipt
    carrying pre_index_trending_at = the trending snapshot that drove the
    classification (reviewer amendment 1). It is flow attrition, never a
    primary-arm hit/miss."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(DETECTION_ALERT_LANE_ENABLED=True)
    await _insert_candidate(db, "dogwifhat", first_seen_min_ago=8.0)
    await _insert_price(db, "dogwifhat")
    # Trending crossover 30 min ago vs first-seen 8 min ago → positive lead →
    # already trending → not_early.
    await _insert_trending(db, "dogwifhat", snapshot_min_ago=30.0)
    _block_send(monkeypatch)

    await notify_early_detections(
        db,
        settings,
        session=None,
        candidates=[
            _cand("dogwifhat", quant_score=8, signals_fired=["cg_trending_rank"])
        ],
    )
    rows = await _fetch_receipts(db, "dogwifhat")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "not_early"
    raw = json.loads(rows[0]["raw_inputs"])
    # The pre-index trending timestamp is preserved and matches the stored snapshot.
    assert raw["pre_index_trending_at"] is not None
    cur = await db._conn.execute(
        "SELECT MIN(snapshot_at) FROM trending_snapshots WHERE coin_id = 'dogwifhat'"
    )
    assert raw["pre_index_trending_at"] == (await cur.fetchone())[0]
    await db.close()


# ---------- amendment 2: filtered inputs never reach the gate; counted by reason --


@pytest.mark.asyncio
async def test_filtered_inputs_never_reach_gate_and_counted_by_reason(
    tmp_path, monkeypatch
):
    """Pre-boundary rejects (non-CG source, missing id, malformed) never reach the
    gate/score/arm logic, emit NO receipt, and are counted SEPARATELY BY REASON —
    excluded from evaluated_n (reviewer amendment 2)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(DETECTION_ALERT_LANE_ENABLED=True)
    _block_send(monkeypatch)

    # Spy: the gate must receive ZERO calls for filtered inputs.
    gate_calls: list = []

    def _spy_gate(cand, settings):
        gate_calls.append(getattr(cand, "contract_address", "?"))
        return False

    monkeypatch.setattr("scout.trading.detection_alert._passes_quality_gate", _spy_gate)

    class _Malformed:
        chain = "coingecko"

        @property
        def contract_address(self):
            raise RuntimeError("boom")

    with capture_logs() as logs:
        await notify_early_detections(
            db,
            settings,
            session=None,
            candidates=[
                _cand("x", chain="solana"),  # non-CG source
                _cand(""),  # missing id (empty contract_address)
                _Malformed(),  # unreadable attributes
            ],
        )
    # The gate (first substantive decision) was never reached.
    assert gate_calls == []
    # No receipts written for filtered inputs.
    assert await _fetch_receipts(db) == []
    summ = [e for e in logs if e["event"] == "detection_receipt_summary"][-1]
    assert summ["filtered_non_cg_source_n"] == 1
    assert summ["filtered_missing_id_n"] == 1
    assert summ["filtered_malformed_n"] == 1
    # Filtered inputs never inflate the evaluated cohort; identity holds at 0.
    assert summ["evaluated_n"] == 0
    assert summ["evaluated_n"] == (
        summ["receipts_written_n"]
        + summ["exact_replays_n"]
        + summ["write_failures_n"]
        + summ["conflicting_duplicates_n"]
    )
    await db.close()


# ---------- LOCK 3 (lane-level idempotency): repeated cycles → one row ----------


@pytest.mark.asyncio
async def test_repeated_cycles_single_receipt_row(tmp_path, monkeypatch):
    """Re-evaluating the same token in the same state across cycles collapses to
    a SINGLE receipt row (no analytical-unit inflation)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(
        DETECTION_ALERT_LANE_ENABLED=True, DETECTION_ALERT_MIN_QUANT_SCORE=5
    )
    await _insert_candidate(db, "failer", first_seen_min_ago=6.0)
    await _insert_price(db, "failer")
    _block_send(monkeypatch)
    cands = [_cand("failer", quant_score=4, signals_fired=["market_cap_range"])]

    for _ in range(3):
        await notify_early_detections(db, settings, session=None, candidates=cands)
    assert len(await _fetch_receipts(db, "failer")) == 1
    await db.close()


# ---------- correction e: retention floor + cohort-completeness prune guard ----------


def test_retention_floor_rejects_below_120():
    """The retention floor (ge=120) makes an under-lifecycle value unconstructable."""
    with pytest.raises(ValidationError):
        _settings(DETECTION_DECISION_RECEIPTS_RETENTION_DAYS=60)


@pytest.mark.asyncio
async def test_prune_blocked_until_cohort_closed(tmp_path):
    """No cohort-close marker → NOTHING is pruned, even a 200d-old row. Setting
    the marker unblocks pruning of rows older than both floor and marker."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    await db.insert_detection_decision_receipt(
        **_receipt_kwargs(idempotency_key="old", decided_at=old)
    )
    # Cohort open → guard blocks pruning.
    n = await db.prune_detection_decision_receipts(keep_days=120, cohort_closed_at=None)
    assert n == 0
    assert len(await _fetch_receipts(db)) == 1
    # Close the cohort at 150d ago → the 200d-old row is now prunable.
    closed = (datetime.now(timezone.utc) - timedelta(days=150)).isoformat()
    n2 = await db.prune_detection_decision_receipts(
        keep_days=120, cohort_closed_at=closed
    )
    assert n2 == 1
    assert len(await _fetch_receipts(db)) == 0
    await db.close()


@pytest.mark.asyncio
async def test_prune_respects_cohort_close_marker(tmp_path):
    """Rows NEWER than the close marker (a still-open cohort) survive pruning even
    when older than the retention floor."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    a = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()  # prunable
    b = (
        datetime.now(timezone.utc) - timedelta(days=130)
    ).isoformat()  # newer than marker
    await db.insert_detection_decision_receipt(
        **_receipt_kwargs(idempotency_key="A", token_id="A", decided_at=a)
    )
    await db.insert_detection_decision_receipt(
        **_receipt_kwargs(idempotency_key="B", token_id="B", decided_at=b)
    )
    marker = (datetime.now(timezone.utc) - timedelta(days=140)).isoformat()
    n = await db.prune_detection_decision_receipts(
        keep_days=120, cohort_closed_at=marker
    )
    assert n == 1
    remaining = {r["token_id"] for r in await _fetch_receipts(db)}
    assert remaining == {"B"}  # B is newer than the 140d marker → survives
    await db.close()


# ---------- correction f: no behavior change with a failing receipt stub ----------


async def _run_receipt_scenario(db, settings, monkeypatch, *, fail_receipts: bool):
    await _insert_candidate(db, "sendme", first_seen_min_ago=5.0)
    await _insert_price(db, "sendme")
    await _insert_candidate(db, "spy-bstocks-tokenized-stock", first_seen_min_ago=6.0)
    await _insert_price(db, "spy-bstocks-tokenized-stock")
    await _insert_candidate(db, "lowscore", first_seen_min_ago=7.0)
    await _insert_price(db, "lowscore")
    sent = _capture_send(monkeypatch)
    if fail_receipts:

        async def _raise(**kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(db, "insert_detection_decision_receipt", _raise)
    await notify_early_detections(
        db,
        settings,
        session=None,
        candidates=[
            _cand(
                "sendme",
                symbol="SEND",
                quant_score=8,
                signals_fired=["cg_trending_rank"],
            ),
            _cand(
                "spy-bstocks-tokenized-stock",
                symbol="SPY",
                quant_score=8,
                signals_fired=["cg_trending_rank"],
            ),
            _cand("lowscore", symbol="LOW", quant_score=0, signals_fired=[]),
        ],
    )
    cur = await db._conn.execute(
        "SELECT token_id, outcome, detail, signal_type, paper_trade_id "
        "FROM tg_alert_log ORDER BY token_id, alerted_at"
    )
    rows = [tuple(r) for r in await cur.fetchall()]
    return sent, rows


@pytest.mark.asyncio
async def test_no_behavior_change_with_failing_receipt_stub(tmp_path, monkeypatch):
    """The send path (sent messages + tg_alert_log rows) is byte-identical whether
    receipts write cleanly or a receipt-write stub ALWAYS fails (LOCK 4)."""
    settings = _settings(
        DETECTION_ALERT_LANE_ENABLED=True, ALERT_UNIVERSE_FILTER_ENABLED=True
    )
    dba = Database(tmp_path / "a.db")
    await dba.initialize()
    sent_a, rows_a = await _run_receipt_scenario(
        dba, settings, monkeypatch, fail_receipts=False
    )
    await dba.close()

    dbb = Database(tmp_path / "b.db")
    await dbb.initialize()
    sent_b, rows_b = await _run_receipt_scenario(
        dbb, settings, monkeypatch, fail_receipts=True
    )
    await dbb.close()

    assert sent_a == sent_b
    assert rows_a == rows_b
    # Sanity: the scenario actually exercised the send path.
    assert len(sent_a) == 1 and sent_a[0].startswith("🔎 EARLY DETECT · SEND")


# ---------- migration idempotency ----------


@pytest.mark.asyncio
async def test_migration_idempotent(tmp_path):
    """Re-running the receipts migration is a no-op: no error, one schema_version
    row, working UNIQUE-index dedup."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._migrate_detection_decision_receipts_v1()
    await db._migrate_detection_decision_receipts_v1()
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM schema_version WHERE version = 20260726"
    )
    assert (await cur.fetchone())[0] == 1
    r1 = await db.insert_detection_decision_receipt(
        **_receipt_kwargs(idempotency_key="mig")
    )
    r2 = await db.insert_detection_decision_receipt(
        **_receipt_kwargs(idempotency_key="mig")
    )
    assert r1 == "inserted" and r2 == "idempotent_replay"
    assert len(await _fetch_receipts(db, "tok")) == 1
    await db.close()
