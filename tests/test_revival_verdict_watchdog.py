"""Tests for scripts/revival-verdict-watchdog.sh.

Mirrors the shell-watchdog test pattern from tests/test_cron_drift_watchdog.py
(stub `curl`/`python3` via PATH prepend; fixture sqlite DB; subprocess.run).

The watchdog monitors signal_params_audit rows of the form:
    field_name = 'soak_verdict'
    new_value  = 'keep_on_provisional_until_<ISO-8601>'

…and alerts (Telegram, plain-text, curl-direct) when the parsed ISO is in
the past relative to NOW.

NOW is normally `date -u +%Y-%m-%dT%H:%M:%SZ`. For deterministic tests the
script honors `REVIVAL_VERDICT_WATCHDOG_NOW_OVERRIDE` if set.

Skipped on Windows; bash + sqlite3 semantics are Linux-specific.
"""

from __future__ import annotations

import os
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash + sqlite3 semantics are Linux-specific",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHDOG_SCRIPT = REPO_ROOT / "scripts" / "revival-verdict-watchdog.sh"


# ---------------------------------------------------------------------------
# Stubs / fixtures
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    """Create a fixture scout.db with signal_params_audit rows.

    rows: list of (signal_type, new_value, applied_at)
    """
    db = tmp_path / "scout.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS signal_params_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_type TEXT NOT NULL,
            field_name TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            reason TEXT NOT NULL,
            applied_by TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_signal_params_audit_signal_at
            ON signal_params_audit(signal_type, applied_at);
        """
    )
    for signal_type, new_value, applied_at in rows:
        conn.execute(
            "INSERT INTO signal_params_audit "
            "(signal_type, field_name, old_value, new_value, reason, applied_by, applied_at) "
            "VALUES (?, 'soak_verdict', NULL, ?, 'test', 'operator', ?)",
            (signal_type, new_value, applied_at),
        )
    conn.commit()
    conn.close()
    return db


def _make_env_file(tmp_path: Path, *, token: str = "FAKE-BOT-TOKEN", chat: str = "12345") -> Path:
    env = tmp_path / ".env"
    env.write_text(f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={chat}\n")
    return env


def _make_curl_stub(tmp_path: Path) -> tuple[Path, Path]:
    """curl stub that records the JSON payload + returns HTTP 200."""
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "curl"
    marker = tmp_path / "curl_payload.json"
    qm = shlex.quote(str(marker))
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "out=''\n"
        "payload=''\n"
        "while [[ $# -gt 0 ]]; do\n"
        '  case "$1" in\n'
        '    -o) out="$2"; shift 2 ;;\n'
        '    -d) payload="$2"; shift 2 ;;\n'
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        f"printf '%s\\n' \"$payload\" > {qm}\n"
        'if [[ -n "$out" ]]; then printf \'{"ok":true}\' > "$out"; fi\n'
        "printf '200'\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    return stub, marker


def _run(
    tmp_path: Path,
    *,
    db: Path,
    env_file: Path,
    now: str | None = None,
    realert_hours: int | None = None,
    state_dir: Path | None = None,
    extra_path: Path | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GECKO_DB_PATH"] = str(db)
    env["GECKO_ENV_FILE"] = str(env_file)
    env["REVIVAL_VERDICT_WATCHDOG_STATE_DIR"] = str(
        state_dir if state_dir else (tmp_path / "state")
    )
    if now is not None:
        env["REVIVAL_VERDICT_WATCHDOG_NOW_OVERRIDE"] = now
    if realert_hours is not None:
        env["REVIVAL_VERDICT_WATCHDOG_REALERT_HOURS"] = str(realert_hours)
    if extra_path is not None:
        env["PATH"] = f"{extra_path}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        ["bash", str(WATCHDOG_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Criterion 1 — empty input (0 provisional rows)
# ---------------------------------------------------------------------------


def test_criterion_1_empty_input(tmp_path):
    """0 keep_on_provisional_until_* rows → exit 0, no alert."""
    db = _make_db(tmp_path, rows=[])
    env_file = _make_env_file(tmp_path)
    curl_stub, marker = _make_curl_stub(tmp_path)

    r = _run(tmp_path, db=db, env_file=env_file, extra_path=curl_stub.parent)

    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert not marker.exists(), "curl should NOT have been called on empty input"
    assert "expired_count=0" in r.stdout
    assert "revival_verdict_watchdog_run" in r.stdout


# ---------------------------------------------------------------------------
# Criterion 6 — legacy keep_on_permanent / dry_run_continued ignored
# ---------------------------------------------------------------------------


def test_criterion_6_legacy_verdicts_ignored(tmp_path):
    db = _make_db(
        tmp_path,
        rows=[
            ("losers_contrarian", "keep_on_permanent", "2026-05-13T04:05:02Z"),
            ("gainers_early", "keep_on_permanent", "2026-05-13T04:05:02Z"),
            ("__hpf__", "dry_run_continued", "2026-05-13T04:05:02Z"),
        ],
    )
    env_file = _make_env_file(tmp_path)
    curl_stub, marker = _make_curl_stub(tmp_path)

    r = _run(tmp_path, db=db, env_file=env_file, extra_path=curl_stub.parent)

    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert not marker.exists()
    assert "expired_count=0" in r.stdout


# ---------------------------------------------------------------------------
# Criterion 5 — future expiry → no alert
# ---------------------------------------------------------------------------


def test_criterion_5_future_expiry_no_alert(tmp_path):
    db = _make_db(
        tmp_path,
        rows=[
            (
                "gainers_early",
                "keep_on_provisional_until_2026-12-31T00:00:00",
                "2026-05-19T00:00:00",
            ),
        ],
    )
    env_file = _make_env_file(tmp_path)
    curl_stub, marker = _make_curl_stub(tmp_path)

    r = _run(
        tmp_path,
        db=db,
        env_file=env_file,
        now="2026-06-01T00:00:00Z",
        extra_path=curl_stub.parent,
    )

    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert not marker.exists()


# ---------------------------------------------------------------------------
# Criterion 2 — single expired row, clean state dir → alert
# ---------------------------------------------------------------------------


def test_criterion_2_single_expired_alerts(tmp_path):
    db = _make_db(
        tmp_path,
        rows=[
            (
                "gainers_early",
                "keep_on_provisional_until_2026-05-01T00:00:00",
                "2026-04-01T00:00:00",
            ),
        ],
    )
    env_file = _make_env_file(tmp_path)
    curl_stub, marker = _make_curl_stub(tmp_path)
    state_dir = tmp_path / "state"

    r = _run(
        tmp_path,
        db=db,
        env_file=env_file,
        now="2026-05-19T16:00:00Z",
        state_dir=state_dir,
        extra_path=curl_stub.parent,
    )

    assert r.returncode == 1, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert marker.exists(), "curl should have been called"
    payload = marker.read_text()
    assert "gainers_early" in payload
    assert "revival-verdict-watchdog" in payload
    # State file written
    assert (state_dir / "last_alert_gainers_early").exists()


# ---------------------------------------------------------------------------
# Criterion 2a — multi-row first-run summary alert
# ---------------------------------------------------------------------------


def test_criterion_2a_multi_row_first_run_summary(tmp_path):
    db = _make_db(
        tmp_path,
        rows=[
            (
                "gainers_early",
                "keep_on_provisional_until_2026-05-01T00:00:00",
                "2026-04-01T00:00:00",
            ),
            (
                "losers_contrarian",
                "keep_on_provisional_until_2026-05-02T00:00:00",
                "2026-04-02T00:00:00",
            ),
            (
                "chain_completed",
                "keep_on_provisional_until_2026-05-03T00:00:00",
                "2026-04-03T00:00:00",
            ),
        ],
    )
    env_file = _make_env_file(tmp_path)
    curl_stub, marker = _make_curl_stub(tmp_path)
    state_dir = tmp_path / "state"

    r = _run(
        tmp_path,
        db=db,
        env_file=env_file,
        now="2026-05-19T16:00:00Z",
        state_dir=state_dir,
        extra_path=curl_stub.parent,
    )

    assert r.returncode == 1, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert marker.exists()
    # Exactly ONE alert
    n_calls = len(marker.read_text().splitlines())
    assert n_calls == 1, f"expected 1 summary alert, got {n_calls}"
    payload = marker.read_text()
    assert "gainers_early" in payload
    assert "losers_contrarian" in payload
    assert "chain_completed" in payload
    # All 3 state files written
    assert (state_dir / "last_alert_gainers_early").exists()
    assert (state_dir / "last_alert_losers_contrarian").exists()
    assert (state_dir / "last_alert_chain_completed").exists()


# ---------------------------------------------------------------------------
# Criterion 3 — re-alert window suppresses spam
# ---------------------------------------------------------------------------


def test_criterion_3_realert_window_suppresses(tmp_path):
    db = _make_db(
        tmp_path,
        rows=[
            (
                "gainers_early",
                "keep_on_provisional_until_2026-05-01T00:00:00",
                "2026-04-01T00:00:00",
            ),
        ],
    )
    env_file = _make_env_file(tmp_path)
    curl_stub, marker = _make_curl_stub(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # Last alert sent recently (within 168h window)
    (state_dir / "last_alert_gainers_early").write_text("2026-05-19T10:00:00Z\n")

    r = _run(
        tmp_path,
        db=db,
        env_file=env_file,
        now="2026-05-19T16:00:00Z",  # 6 hours later
        state_dir=state_dir,
        extra_path=curl_stub.parent,
    )

    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert not marker.exists(), "should NOT alert within re-alert window"
    assert "realert_skipped" in r.stdout


# ---------------------------------------------------------------------------
# Criterion 4 — outside re-alert window → alert
# ---------------------------------------------------------------------------


def test_criterion_4_outside_realert_window_alerts(tmp_path):
    db = _make_db(
        tmp_path,
        rows=[
            (
                "gainers_early",
                "keep_on_provisional_until_2026-05-01T00:00:00",
                "2026-04-01T00:00:00",
            ),
        ],
    )
    env_file = _make_env_file(tmp_path)
    curl_stub, marker = _make_curl_stub(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # Last alert 200 hours ago (default re-alert window is 168h)
    (state_dir / "last_alert_gainers_early").write_text("2026-05-11T08:00:00Z\n")

    r = _run(
        tmp_path,
        db=db,
        env_file=env_file,
        now="2026-05-19T16:00:00Z",  # 200h later
        state_dir=state_dir,
        extra_path=curl_stub.parent,
    )

    assert r.returncode == 1, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert marker.exists()
    # State file updated
    assert "2026-05-19T16:00:00Z" in (state_dir / "last_alert_gainers_early").read_text()


# ---------------------------------------------------------------------------
# Criterion 7 — malformed ISO → exit 4
# ---------------------------------------------------------------------------


def test_criterion_7_malformed_iso_exits_4(tmp_path):
    db = _make_db(
        tmp_path,
        rows=[
            (
                "gainers_early",
                "keep_on_provisional_until_GARBLED-NOT-A-DATE",
                "2026-04-01T00:00:00",
            ),
        ],
    )
    env_file = _make_env_file(tmp_path)
    curl_stub, marker = _make_curl_stub(tmp_path)

    r = _run(
        tmp_path,
        db=db,
        env_file=env_file,
        now="2026-05-19T16:00:00Z",
        extra_path=curl_stub.parent,
    )

    assert r.returncode == 4, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "malformed" in (r.stdout + r.stderr).lower()
    assert not marker.exists()


# ---------------------------------------------------------------------------
# Criterion 7a — ISO-shape tolerance matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "iso_suffix,label",
    [
        ("2026-05-01T00:00:00", "naive-no-ms"),
        ("2026-05-01T00:00:00Z", "naive-z"),
        ("2026-05-01T00:00:00+00:00", "naive-plus0000"),
        ("2026-05-01T00:00:00.123456", "ms-naive"),
        ("2026-05-01T00:00:00.123456Z", "ms-z"),
    ],
)
def test_criterion_7a_iso_shape_tolerance(tmp_path, iso_suffix, label):
    db = _make_db(
        tmp_path,
        rows=[
            (
                "gainers_early",
                f"keep_on_provisional_until_{iso_suffix}",
                "2026-04-01T00:00:00",
            ),
        ],
    )
    env_file = _make_env_file(tmp_path)
    curl_stub, marker = _make_curl_stub(tmp_path)

    r = _run(
        tmp_path,
        db=db,
        env_file=env_file,
        now="2026-05-19T16:00:00Z",
        extra_path=curl_stub.parent,
    )

    # All 5 shapes should parse and trigger an alert (all are 2026-05-01,
    # which is before 2026-05-19).
    assert r.returncode == 1, (
        f"shape={label} should parse + alert; "
        f"stdout={r.stdout!r} stderr={r.stderr!r}"
    )
    assert marker.exists()


def test_criterion_7a_alt_timezone_rejected(tmp_path):
    """Alternate timezone (+05:30) is in the malformed path per criterion 7."""
    db = _make_db(
        tmp_path,
        rows=[
            (
                "gainers_early",
                "keep_on_provisional_until_2026-05-01T00:00:00+05:30",
                "2026-04-01T00:00:00",
            ),
        ],
    )
    env_file = _make_env_file(tmp_path)
    curl_stub, marker = _make_curl_stub(tmp_path)

    r = _run(
        tmp_path,
        db=db,
        env_file=env_file,
        now="2026-05-19T16:00:00Z",
        extra_path=curl_stub.parent,
    )

    assert r.returncode == 4
    assert not marker.exists()


# ---------------------------------------------------------------------------
# Criterion 8 — fresh verdict resets idempotency
# ---------------------------------------------------------------------------


def test_criterion_8_fresh_verdict_resets_idempotency(tmp_path):
    """After operator emits a NEW row whose applied_at > last_alert, the new
    expiry should re-alert immediately on its own expiry — the previous
    state file does NOT suppress the new event."""
    db = _make_db(
        tmp_path,
        rows=[
            # Old provisional that fired and was alerted
            (
                "gainers_early",
                "keep_on_provisional_until_2026-05-01T00:00:00",
                "2026-04-01T00:00:00",
            ),
            # Fresh operator-emitted row, applied_at AFTER the previous alert
            (
                "gainers_early",
                "keep_on_provisional_until_2026-05-15T00:00:00",
                "2026-04-15T00:00:00",
            ),
        ],
    )
    env_file = _make_env_file(tmp_path)
    curl_stub, marker = _make_curl_stub(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # Last alert at 2026-04-01T01:00:00Z (after old applied_at, BEFORE fresh applied_at)
    (state_dir / "last_alert_gainers_early").write_text("2026-04-01T01:00:00Z\n")

    r = _run(
        tmp_path,
        db=db,
        env_file=env_file,
        now="2026-05-19T16:00:00Z",  # both expired by now, fresh is the most recent
        state_dir=state_dir,
        extra_path=curl_stub.parent,
    )

    # The watchdog's "most recent row per signal_type" rule picks the fresh
    # one; fresh expiry is 2026-05-15, which is now expired. last_alert is
    # 2026-04-01 (before fresh.applied_at=2026-04-15), so idempotency
    # resets and a new alert fires.
    assert r.returncode == 1, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert marker.exists()


# ---------------------------------------------------------------------------
# Criterion 11 — plain-text Telegram render with underscored signal_type
# ---------------------------------------------------------------------------


def test_criterion_11_plain_text_no_markdown_parse(tmp_path):
    """parse_mode must NOT be Markdown — signal_type underscores would
    render as italics and damage the message (CLAUDE.md §12b Class-3)."""
    db = _make_db(
        tmp_path,
        rows=[
            (
                "losers_contrarian",
                "keep_on_provisional_until_2026-05-01T00:00:00",
                "2026-04-01T00:00:00",
            ),
        ],
    )
    env_file = _make_env_file(tmp_path)
    curl_stub, marker = _make_curl_stub(tmp_path)

    r = _run(
        tmp_path,
        db=db,
        env_file=env_file,
        now="2026-05-19T16:00:00Z",
        extra_path=curl_stub.parent,
    )

    assert r.returncode == 1
    payload = marker.read_text()
    # Should NOT contain parse_mode: any value
    assert "parse_mode" not in payload, (
        "parse_mode should be omitted (None) — present would risk "
        "Markdown rendering and underscore corruption per §12b"
    )
    # Signal name with underscore must appear literally
    assert "losers_contrarian" in payload


# ---------------------------------------------------------------------------
# §12b log triplet — dispatched + delivered structured logs (criterion 10)
# ---------------------------------------------------------------------------


def test_criterion_10_log_triplet_on_alert(tmp_path):
    db = _make_db(
        tmp_path,
        rows=[
            (
                "gainers_early",
                "keep_on_provisional_until_2026-05-01T00:00:00",
                "2026-04-01T00:00:00",
            ),
        ],
    )
    env_file = _make_env_file(tmp_path)
    curl_stub, _ = _make_curl_stub(tmp_path)

    r = _run(
        tmp_path,
        db=db,
        env_file=env_file,
        now="2026-05-19T16:00:00Z",
        extra_path=curl_stub.parent,
    )

    assert r.returncode == 1
    out = r.stdout
    assert "revival_verdict_watchdog_alert_dispatched" in out
    assert "revival_verdict_watchdog_alert_delivered" in out


# ---------------------------------------------------------------------------
# §12b "no state mutation" — script must not touch DB
# ---------------------------------------------------------------------------


def test_no_state_mutation_on_alert(tmp_path):
    """Watchdog reads signal_params_audit but never writes. Verify
    row count + content unchanged after alert."""
    db = _make_db(
        tmp_path,
        rows=[
            (
                "gainers_early",
                "keep_on_provisional_until_2026-05-01T00:00:00",
                "2026-04-01T00:00:00",
            ),
        ],
    )
    env_file = _make_env_file(tmp_path)
    curl_stub, _ = _make_curl_stub(tmp_path)

    # Snapshot
    conn = sqlite3.connect(db)
    before = list(conn.execute("SELECT * FROM signal_params_audit ORDER BY id"))
    conn.close()

    r = _run(
        tmp_path,
        db=db,
        env_file=env_file,
        now="2026-05-19T16:00:00Z",
        extra_path=curl_stub.parent,
    )

    assert r.returncode == 1

    conn = sqlite3.connect(db)
    after = list(conn.execute("SELECT * FROM signal_params_audit ORDER BY id"))
    conn.close()
    assert before == after, "watchdog must not mutate signal_params_audit"
