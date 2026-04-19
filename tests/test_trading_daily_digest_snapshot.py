"""Byte-identical regression gate for the daily digest (spec §12)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scout.db import Database

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "daily_digest_snapshot.txt"


async def _seed_deterministic_fixture(db):
    """Seed a known, deterministic set of trades so the digest is reproducible."""
    now = datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        (
            "deterministic_a",
            "A",
            "Apple",
            "cg",
            "volume_spike",
            15.0,
            12.0,
            "closed_tp",
        ),
        (
            "deterministic_b",
            "B",
            "Banana",
            "cg",
            "gainers_early",
            -8.0,
            -5.0,
            "closed_sl",
        ),
        (
            "deterministic_c",
            "C",
            "Cherry",
            "cg",
            "volume_spike",
            25.0,
            20.0,
            "closed_tp",
        ),
    ]
    for i, (tid, sym, name, chain, sig, pnl_usd, pnl_pct, status) in enumerate(rows):
        await db._conn.execute(
            "INSERT INTO paper_trades "
            "(token_id, symbol, name, chain, signal_type, signal_data, "
            " entry_price, amount_usd, quantity, tp_pct, sl_pct, "
            " tp_price, sl_price, status, pnl_usd, pnl_pct, "
            " opened_at, closed_at, signal_combo) "
            "VALUES (?, ?, ?, ?, ?, '{}', 1.0, 100.0, 100.0, 20, 10, 1.2, 0.9, "
            " ?, ?, ?, ?, ?, ?)",
            (
                tid,
                sym,
                name,
                chain,
                sig,
                status,
                pnl_usd,
                pnl_pct,
                (now - timedelta(hours=6 + i)).isoformat(),
                (now - timedelta(hours=2 + i)).isoformat(),
                sig,
            ),
        )
    await db._conn.commit()


async def test_daily_digest_byte_identical_against_master_snapshot(
    tmp_path, settings_factory
):
    """Daily digest format must not change due to additive feedback-loop schema.

    HOW TO CAPTURE THE SNAPSHOT (one-time, run on master before this PR):

        uv run python - <<'EOF'
        import asyncio
        from pathlib import Path
        from datetime import datetime, timedelta, timezone
        from scout.db import Database

        async def main():
            db = Database("/tmp/snapshot_seed.db")
            await db.initialize()
            now = datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)
            rows = [
                ("deterministic_a", "A", "Apple",  "cg", "volume_spike",  15.0,  12.0, "closed_tp"),
                ("deterministic_b", "B", "Banana", "cg", "gainers_early", -8.0,  -5.0, "closed_sl"),
                ("deterministic_c", "C", "Cherry", "cg", "volume_spike",  25.0,  20.0, "closed_tp"),
            ]
            for i, (tid, sym, name, chain, sig, pnl_usd, pnl_pct, status) in enumerate(rows):
                await db._conn.execute(
                    "INSERT INTO paper_trades "
                    "(token_id, symbol, name, chain, signal_type, signal_data, "
                    " entry_price, amount_usd, quantity, tp_pct, sl_pct, "
                    " tp_price, sl_price, status, pnl_usd, pnl_pct, "
                    " opened_at, closed_at, signal_combo) "
                    "VALUES (?, ?, ?, ?, ?, '{}', 1.0, 100.0, 100.0, 20, 10, 1.2, 0.9, "
                    " ?, ?, ?, ?, ?, ?)",
                    (tid, sym, name, chain, sig, status, pnl_usd, pnl_pct,
                     (now - timedelta(hours=6 + i)).isoformat(),
                     (now - timedelta(hours=2 + i)).isoformat(),
                     sig),
                )
            await db._conn.commit()
            from scout.trading.digest import build_paper_digest
            result = await build_paper_digest(db, "2026-04-18")
            Path("tests/fixtures/daily_digest_snapshot.txt").write_text(result or "", encoding="utf-8")
            await db.close()

        asyncio.run(main())
        EOF

    Then commit tests/fixtures/daily_digest_snapshot.txt.
    """
    if not SNAPSHOT_PATH.exists():
        pytest.skip(
            "daily_digest_snapshot.txt missing — capture from master before running. "
            "See docstring for instructions."
        )
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_deterministic_fixture(db)

    from scout.trading.digest import build_paper_digest

    actual = await build_paper_digest(db, "2026-04-18")
    expected = SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert actual == expected, (
        "Daily digest output drifted — either the feedback-loop work "
        "accidentally perturbed the existing digest, or the snapshot is stale. "
        "If intentional, regenerate the snapshot."
    )
    await db.close()
