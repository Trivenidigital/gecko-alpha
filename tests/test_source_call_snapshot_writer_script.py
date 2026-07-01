"""Tests for the C2 snapshot-writer cron entrypoint + config knobs (#392 C2).

The DISABLED path is aiohttp-free by design (deploy-without-activate), so it
runs on Windows via subprocess. The enabled path (real aiohttp + C0) is
exercised on CI/VPS; write_price_snapshots' logic is unit-tested separately in
test_source_call_snapshot_writer.py.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "source_call_price_snapshots_writer.py"


def test_script_disabled_is_inert_and_needs_no_db(tmp_path):
    # Default (no --enabled): no-op exit 0, DB absence irrelevant, no network.
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(tmp_path / "absent.db")],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    body = json.loads(res.stdout)
    assert body["ok"] is True
    assert body["skipped"] == "writer_disabled"


def test_script_disabled_via_explicit_false(tmp_path):
    res = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--db",
            str(tmp_path / "x.db"),
            "--enabled",
            "false",
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout)["skipped"] == "writer_disabled"


def test_snapshot_writer_settings_default_off(settings_factory):
    s = settings_factory()
    assert s.SOURCE_CALL_SNAPSHOT_WRITER_ENABLED is False
    assert s.SOURCE_CALL_SNAPSHOT_HORIZON_HOURS == 28


def test_snapshot_writer_settings_accept_activation_override(settings_factory):
    s = settings_factory(
        SOURCE_CALL_SNAPSHOT_WRITER_ENABLED=True,
        SOURCE_CALL_SNAPSHOT_HORIZON_HOURS=48,
    )
    assert s.SOURCE_CALL_SNAPSHOT_WRITER_ENABLED is True
    assert s.SOURCE_CALL_SNAPSHOT_HORIZON_HOURS == 48
