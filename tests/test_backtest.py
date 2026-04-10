"""Tests for scout/backtest.py CLI."""
import sys
from io import StringIO
from contextlib import redirect_stdout

from scout.db import Database


async def test_backtest_empty_db(tmp_path, monkeypatch):
    """Backtest runs successfully on empty DB."""
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.initialize()
    await db.close()

    from scout import backtest
    monkeypatch.setattr(sys, "argv", ["backtest", "--db", str(db_path), "--days", "30"])

    buf = StringIO()
    with redirect_stdout(buf):
        await backtest.main()

    output = buf.getvalue()
    assert "Backtest Analysis" in output
    assert "Narrative Agent Predictions" in output


async def test_backtest_with_predictions(tmp_path, monkeypatch):
    """Backtest computes hit rates from sample data."""
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.initialize()
    conn = db._conn
    assert conn is not None

    # Insert sample predictions: 2 agent HITs, 1 agent MISS, 1 control HIT
    for i, (cls, is_ctrl) in enumerate([("HIT", 0), ("HIT", 0), ("MISS", 0), ("HIT", 1)]):
        await conn.execute(
            """INSERT INTO predictions
               (category_id, category_name, coin_id, symbol, name,
                market_cap_at_prediction, price_at_prediction,
                narrative_fit_score, staying_power, confidence, reasoning,
                market_regime, trigger_count, is_control, is_holdout,
                strategy_snapshot, predicted_at, outcome_class, evaluated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("ai", "AI", f"coin-{i}", f"S{i}", f"N{i}", 50e6, 1.0,
             75, "High", "Med", "r", "BULL", 1, is_ctrl, 0,
             "{}", "2026-04-09T00:00:00Z", cls, "2026-04-09T12:00:00Z"),
        )
    await conn.commit()
    await db.close()

    from scout import backtest
    monkeypatch.setattr(sys, "argv", ["backtest", "--db", str(db_path), "--days", "3650"])

    buf = StringIO()
    with redirect_stdout(buf):
        await backtest.main()

    output = buf.getvalue()
    # 2/3 agent hits = 66.7%
    assert "66.7%" in output or "66.6%" in output or "2/3" in output
