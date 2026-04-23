import pytest
from datetime import datetime, timezone
from scout.db import Database
from scout.trading.evaluator import _load_bl061_cutover_ts


async def test_cutover_ts_returns_iso_timestamp(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ts = await _load_bl061_cutover_ts(db._conn)
    assert ts is not None
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None
    await db.close()
