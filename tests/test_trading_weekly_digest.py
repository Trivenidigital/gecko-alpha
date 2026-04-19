"""Tests for weekly digest (spec §5.4)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading import weekly_digest


async def test_build_digest_returns_none_on_empty_week(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    result = await weekly_digest.build_weekly_digest(
        db,
        end_date=date.today(),
        settings=s,
    )
    assert result is None
    await db.close()


async def test_build_digest_renders_core_sections(tmp_path, settings_factory):
    """With fallback counter == 0 the Fallback section is elided."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()

    # Seed a trade + a combo_performance row so digest has content.
    now = datetime.now(timezone.utc)
    await db._conn.execute(
        "INSERT INTO paper_trades "
        "(token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price, "
        " status, opened_at, closed_at, pnl_usd, pnl_pct, signal_combo, "
        " lead_time_vs_trending_min, lead_time_vs_trending_status) "
        "VALUES ('c', 'C', 'C', 'coingecko', 'volume_spike', '{}', "
        " 1.0, 100.0, 100.0, 20, 10, 1.2, 0.9, 'closed_tp', ?, ?, 15.0, 12.0, "
        " 'volume_spike', -10.0, 'ok')",
        ((now - timedelta(days=3)).isoformat(), (now - timedelta(days=2)).isoformat()),
    )
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, "
        " avg_pnl_pct, win_rate_pct, suppressed, refresh_failures, last_refreshed) "
        "VALUES ('volume_spike', '30d', 12, 7, 5, 42, 3.5, 58.3, 0, 0, ?)",
        (now.isoformat(),),
    )
    await db._conn.commit()

    result = await weekly_digest.build_weekly_digest(
        db,
        end_date=date.today(),
        settings=s,
    )
    assert result is not None
    for header in (
        "Weekly Feedback",
        "Combo leaderboard",
        "Missed winners",
        "Lead-time",
        "Suppression log",
        "Chronic refresh failures",
    ):
        assert header in result
    # Fallback section elided when counter == 0.
    assert "Fallback counters" not in result
    await db.close()


async def test_fallback_section_rendered_when_nonzero(
    tmp_path, settings_factory, monkeypatch
):
    """When the in-memory fallback ring has entries, [Fallback counters]
    section is rendered."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    # Minimal seed so digest doesn't short-circuit empty.
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, "
        " avg_pnl_pct, win_rate_pct, suppressed, refresh_failures, last_refreshed) "
        "VALUES ('x', '30d', 10, 5, 5, 0, 0, 50.0, 0, 0, ?)",
        (now.isoformat(),),
    )
    await db._conn.commit()

    # Prime fallback ring.
    from scout.trading import suppression as _supp

    monkeypatch.setattr(
        _supp,
        "_fallback_timestamps",
        [now.isoformat(), now.isoformat()],
        raising=False,
    )

    result = await weekly_digest.build_weekly_digest(
        db,
        end_date=date.today(),
        settings=s,
    )
    assert result is not None
    assert "Fallback counters" in result
    assert "Suppression fail-opens: 2" in result
    await db.close()


async def test_section_failure_does_not_kill_entire_digest(
    tmp_path, settings_factory, monkeypatch
):
    """If one analytics helper raises, other sections still render + the
    failing section is replaced by an '(error)' marker."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, "
        " avg_pnl_pct, win_rate_pct, suppressed, refresh_failures, last_refreshed) "
        "VALUES ('x', '30d', 10, 5, 5, 0, 0, 50.0, 0, 0, ?)",
        (now.isoformat(),),
    )
    await db._conn.commit()

    from scout.trading import analytics as _analytics

    async def _boom(*a, **k):
        raise RuntimeError("lead-time crash")

    monkeypatch.setattr(_analytics, "lead_time_breakdown", _boom)

    result = await weekly_digest.build_weekly_digest(
        db,
        end_date=date.today(),
        settings=s,
    )
    assert result is not None
    assert "Combo leaderboard" in result
    assert "Missed winners" in result
    # The failing section should be annotated (error: RuntimeError), not missing.
    assert "Lead-time" in result
    assert "(error: RuntimeError)" in result
    await db.close()


async def test_telegram_split_at_4096_preserves_line_integrity(
    tmp_path,
    settings_factory,
):
    """_split_for_telegram must split on newline boundaries, never mid-line."""
    long_lines = "\n".join(f"line-{i}" * 20 for i in range(500))
    chunks = weekly_digest._split_for_telegram(long_lines, 4000)
    assert len(chunks) > 1
    # Every chunk <= limit.
    for c in chunks:
        assert len(c) <= 4000
    # Rejoining chunks with "\n" recovers the original (all lines present).
    recovered = "\n".join(chunks)
    for line in long_lines.split("\n"):
        assert line in recovered


async def test_telegram_split_hard_truncates_long_lines(tmp_path, settings_factory):
    """A single line > limit is hard-truncated to the limit so Telegram accepts it."""
    monster = "X" * 5000
    text = "header\n" + monster + "\nfooter"
    chunks = weekly_digest._split_for_telegram(text, 4000)
    for c in chunks:
        assert len(c) <= 4000, f"chunk {len(c)}B exceeds 4000"
    joined = "".join(chunks)
    assert "header" in joined
    assert "footer" in joined


async def test_send_weekly_digest_empty_skips_telegram(
    tmp_path,
    settings_factory,
    monkeypatch,
):
    """Empty week → build returns None → send must NOT call telegram."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()

    sent: list = []

    async def _capture(text, session, settings):
        sent.append(text)

    monkeypatch.setattr(
        "scout.trading.weekly_digest.alerter.send_telegram_message",
        _capture,
    )
    await weekly_digest.send_weekly_digest(db, s)
    assert sent == []
    await db.close()


async def test_send_weekly_digest_fallback_on_error(
    tmp_path, settings_factory, monkeypatch
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()

    sent: list = []

    async def _capture(text, session, settings):
        sent.append(text)

    monkeypatch.setattr(
        "scout.trading.weekly_digest.alerter.send_telegram_message",
        _capture,
    )

    async def _boom(*a, **k):
        raise RuntimeError("digest broken")

    monkeypatch.setattr(weekly_digest, "build_weekly_digest", _boom)

    await weekly_digest.send_weekly_digest(db, s)
    assert any("Weekly digest failed" in m for m in sent)
    assert any("ref=wd-" in m for m in sent)
    await db.close()
