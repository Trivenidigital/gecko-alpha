import asyncio
import subprocess
import sys
from pathlib import Path


def _seed_db(db_path: str) -> None:
    """Seed a test DB with one slow_burn detection + one volume_history_cg row.

    R1-M1 amendment: use Database.initialize() to build the schema rather than
    raw CREATE TABLE statements. Couples the CLI integration test to the real
    migration chain.
    """
    from scout.db import Database

    async def _seed():
        db = Database(db_path)
        await db.connect()
        await db._conn.execute(
            "INSERT INTO slow_burn_candidates "
            "(coin_id, symbol, name, price_change_7d, price_change_1h, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("test-coin", "TEST", "Test", 75.0, 0.0, "2026-05-10T03:50:00+00:00"),
        )
        await db._conn.execute(
            "INSERT INTO volume_history_cg "
            "(coin_id, symbol, name, volume_24h, market_cap, price, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("test-coin", "TEST", "Test", 1000.0, 5e6, 0.1, "2026-05-10T04:00:00+00:00"),
        )
        await db._conn.commit()
        await db.close()

    asyncio.run(_seed())


def test_cli_runs_and_writes_heartbeat(tmp_path):
    """CLI invocation captures rows + writes atomic heartbeat file."""
    db_path = tmp_path / "test.db"
    hb_path = tmp_path / "snapshot-last-ok"
    _seed_db(str(db_path))

    script = Path(__file__).parent.parent.parent / "scripts" / "gecko_audit_snapshot.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--db-path", str(db_path),
            "--soak-start", "2026-05-10T00:00:00+00:00",
            "--soak-end", "2026-05-25T00:00:00+00:00",
            "--heartbeat-file", str(hb_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    assert hb_path.exists()
    hb_content = hb_path.read_text().strip()
    assert hb_content.isdigit()  # unix timestamp
    assert int(hb_content) > 1_700_000_000  # post-2023 sanity


def test_cli_exits_2_on_missing_db(tmp_path):
    """Missing DB path returns exit code 2 (misconfiguration)."""
    hb_path = tmp_path / "hb"
    script = Path(__file__).parent.parent.parent / "scripts" / "gecko_audit_snapshot.py"
    result = subprocess.run(
        [
            sys.executable, str(script),
            "--db-path", str(tmp_path / "nonexistent.db"),
            "--soak-start", "2026-05-10T00:00:00+00:00",
            "--soak-end", "2026-05-25T00:00:00+00:00",
            "--heartbeat-file", str(hb_path),
        ],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2
    assert not hb_path.exists()


def test_cli_exits_2_on_bad_iso(tmp_path):
    """Invalid ISO timestamp returns exit code 2."""
    db_path = tmp_path / "test.db"
    hb_path = tmp_path / "hb"
    _seed_db(str(db_path))
    script = Path(__file__).parent.parent.parent / "scripts" / "gecko_audit_snapshot.py"
    result = subprocess.run(
        [
            sys.executable, str(script),
            "--db-path", str(db_path),
            "--soak-start", "not-an-iso-timestamp",
            "--soak-end", "2026-05-25T00:00:00+00:00",
            "--heartbeat-file", str(hb_path),
        ],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2
