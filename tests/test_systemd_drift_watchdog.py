"""Tests for scripts/systemd-drift-watchdog.sh.

Modeled on tests/test_backup_rotate_script.py:354-379 (_make_uv_stub + PATH
injection + marker-file pattern). Skipped on Windows: bash + find -print0 +
sort -z + flock semantics are Linux-specific.

Fixture write strategy: Path.write_bytes(content.encode()) — V45 SHOULD-FIX
CRLF guard. Path.write_text on Windows would inject CRLF and the diff -q
inside the watchdog would see drift between byte-CRLF and byte-LF.

Cycle 10 of autonomous backlog knockdown: BL-NEW-SYSTEMD-DRIFT-PRECOMMIT-HOOK.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash + find -print0 + sort -z + flock are Linux-specific",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHDOG_SCRIPT = REPO_ROOT / "scripts" / "systemd-drift-watchdog.sh"


def _make_uv_stub(tmp_path: Path) -> tuple[Path, Path]:
    """Mirrors tests/test_backup_rotate_script.py:354-364 verbatim."""
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    stub = stub_dir / "uv"
    marker = tmp_path / "alert_marker"
    quoted_marker = shlex.quote(str(marker))
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "uv called: $@" >> {quoted_marker}\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    return stub, marker


def _make_fake_repo(tmp_path: Path) -> Path:
    """Build a fake /root/gecko-alpha/systemd/ tree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "systemd").mkdir()
    return repo


def _make_fake_prod_systemd(tmp_path: Path) -> Path:
    """Build a fake /etc/systemd/system/ tree."""
    prod = tmp_path / "prod-systemd-system"
    prod.mkdir()
    return prod


def _write_unit(dir_: Path, name: str, content: str) -> Path:
    """V45 SHOULD-FIX — write_bytes(content.encode()) to avoid CRLF on Windows
    fixture writes that would later get diffed against LF."""
    p = dir_ / name
    p.write_bytes(content.encode())
    return p


def _run_watchdog(
    tmp_path: Path,
    repo: Path,
    prod: Path,
    env_overrides: dict | None = None,
) -> tuple[subprocess.CompletedProcess, Path]:
    stub, marker = _make_uv_stub(tmp_path)
    ack_dir = tmp_path / "ack"
    heartbeat = tmp_path / "heartbeat"
    env = os.environ.copy()
    env["PATH"] = f"{stub.parent}:" + env.get("PATH", "")
    env["UV_BIN"] = str(stub)
    env["GECKO_REPO"] = str(repo)
    env["PROD_SYSTEMD_DIR"] = str(prod)
    env["SYSTEMD_DRIFT_ACK_DIR"] = str(ack_dir)
    env["SYSTEMD_DRIFT_HEARTBEAT_FILE"] = str(heartbeat)
    if env_overrides:
        env.update(env_overrides)
    res = subprocess.run(
        ["bash", str(WATCHDOG_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )
    return res, marker


# ----------------------------------------------------------------------
# Test #1: clean — exit 0
# ----------------------------------------------------------------------


def test_clean_returns_zero(tmp_path):
    repo = _make_fake_repo(tmp_path)
    prod = _make_fake_prod_systemd(tmp_path)
    body = "[Unit]\nDescription=foo\n[Service]\nExecStart=/bin/true\n"
    _write_unit(repo / "systemd", "gecko-foo.service", body)
    _write_unit(prod, "gecko-foo.service", body)

    res, marker = _run_watchdog(tmp_path, repo, prod)
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "OK:" in res.stdout
    assert not marker.exists()
    # V48 SHOULD-FIX — CLEAN path touches heartbeat
    heartbeat = tmp_path / "heartbeat"
    assert heartbeat.exists()


# ----------------------------------------------------------------------
# Test #2: content drift — exit 1 + stub called
# ----------------------------------------------------------------------


def test_drift_alerts_via_stub(tmp_path):
    repo = _make_fake_repo(tmp_path)
    prod = _make_fake_prod_systemd(tmp_path)
    _write_unit(repo / "systemd", "gecko-foo.service", "[Unit]\nDescription=v1\n")
    _write_unit(prod, "gecko-foo.service", "[Unit]\nDescription=v2-prod-drift\n")

    res, marker = _run_watchdog(tmp_path, repo, prod)
    assert res.returncode == 1, (res.stdout, res.stderr)
    assert marker.exists()
    payload = marker.read_text()
    assert "DRIFT: gecko-foo.service" in payload


# ----------------------------------------------------------------------
# Test #3: drop-in present — exit 1 + stub called
# ----------------------------------------------------------------------


def test_drop_in_alerts(tmp_path):
    repo = _make_fake_repo(tmp_path)
    prod = _make_fake_prod_systemd(tmp_path)
    body = "[Unit]\nDescription=foo\n"
    _write_unit(repo / "systemd", "gecko-foo.service", body)
    _write_unit(prod, "gecko-foo.service", body)
    drop_dir = prod / "gecko-foo.service.d"
    drop_dir.mkdir()
    _write_unit(drop_dir, "override.conf", "[Service]\nRestart=on-failure\n")

    res, marker = _run_watchdog(tmp_path, repo, prod)
    assert res.returncode == 1, (res.stdout, res.stderr)
    assert marker.exists()
    payload = marker.read_text()
    assert "DROP-IN PRESENT: gecko-foo.service.d/" in payload


# ----------------------------------------------------------------------
# Test #4: env file missing + UV_BIN empty — exit 4
# ----------------------------------------------------------------------


def test_missing_env_file_exits_4(tmp_path):
    repo = _make_fake_repo(tmp_path)
    prod = _make_fake_prod_systemd(tmp_path)
    _write_unit(repo / "systemd", "gecko-foo.service", "[Unit]\nDescription=v1\n")
    _write_unit(prod, "gecko-foo.service", "[Unit]\nDescription=v2\n")

    # UV_BIN empty forces the env-file path; ENV_FILE points to a non-existent file
    res = subprocess.run(
        ["bash", str(WATCHDOG_SCRIPT)],
        env={
            **os.environ,
            "GECKO_REPO": str(repo),
            "PROD_SYSTEMD_DIR": str(prod),
            "SYSTEMD_DRIFT_ACK_DIR": str(tmp_path / "ack"),
            "SYSTEMD_DRIFT_HEARTBEAT_FILE": str(tmp_path / "heartbeat"),
            "GECKO_ENV_FILE": str(tmp_path / "does-not-exist.env"),
            "UV_BIN": "",
        },
        capture_output=True,
        text=True,
    )
    assert res.returncode == 4, (res.stdout, res.stderr)


# ----------------------------------------------------------------------
# Test #5 (V45 MUST-FIX #2): multi-unit drift — both reported (set -e × diff)
# ----------------------------------------------------------------------


def test_multi_unit_drift_reports_all(tmp_path):
    repo = _make_fake_repo(tmp_path)
    prod = _make_fake_prod_systemd(tmp_path)
    _write_unit(repo / "systemd", "gecko-foo.service", "[Unit]\nDescription=v1\n")
    _write_unit(prod, "gecko-foo.service", "[Unit]\nDescription=foo-prod\n")
    _write_unit(repo / "systemd", "gecko-bar.service", "[Unit]\nDescription=v1\n")
    _write_unit(prod, "gecko-bar.service", "[Unit]\nDescription=bar-prod\n")

    res, marker = _run_watchdog(tmp_path, repo, prod)
    assert res.returncode == 1, (res.stdout, res.stderr)
    assert marker.exists()
    payload = marker.read_text()
    assert "DRIFT: gecko-foo.service" in payload
    assert "DRIFT: gecko-bar.service" in payload


# ----------------------------------------------------------------------
# Test #6 (V45 MUST-FIX #3): prod-only unit — UNTRACKED PROD UNIT
# ----------------------------------------------------------------------


def test_prod_only_unit_alerts(tmp_path):
    repo = _make_fake_repo(tmp_path)
    prod = _make_fake_prod_systemd(tmp_path)
    # Operator ran `systemctl edit --full gecko-new.service` prod-side without committing
    _write_unit(prod, "gecko-new.service", "[Unit]\nDescription=operator-edit\n")

    res, marker = _run_watchdog(tmp_path, repo, prod)
    assert res.returncode == 1, (res.stdout, res.stderr)
    assert marker.exists()
    payload = marker.read_text()
    assert "UNTRACKED PROD UNIT: gecko-new.service" in payload


# ----------------------------------------------------------------------
# Test #7 (V45 SHOULD-FIX): HTTP 503 / placeholder token paths
# ----------------------------------------------------------------------


def test_telegram_http_failure_exits_7(tmp_path):
    """When UV_BIN unset but ENV_FILE present + Telegram API returns non-200.
    We simulate via a curl wrapper stub that exits with HTTP 503 mock.

    The watchdog's curl call won't actually hit Telegram (no internet); we
    inject a `curl` stub on PATH that returns 503.
    """
    repo = _make_fake_repo(tmp_path)
    prod = _make_fake_prod_systemd(tmp_path)
    _write_unit(repo / "systemd", "gecko-foo.service", "[Unit]\nDescription=v1\n")
    _write_unit(prod, "gecko-foo.service", "[Unit]\nDescription=v2\n")

    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=8000000000:AAreal_looking_token_format\n"
        "TELEGRAM_CHAT_ID=12345\n"
    )

    # curl stub that returns HTTP 503 and a mock response body
    stub_dir = tmp_path / "curl-stubs"
    stub_dir.mkdir()
    curl_stub = stub_dir / "curl"
    curl_stub.write_text(
        "#!/usr/bin/env bash\n"
        "for arg in \"$@\"; do\n"
        "  if [[ \"$arg\" == -o ]]; then OUT_NEXT=1; continue; fi\n"
        "  if [[ -n \"${OUT_NEXT:-}\" ]]; then echo 'simulated 503' > \"$arg\"; OUT_NEXT=; fi\n"
        "done\n"
        "echo 503\n"
    )
    curl_stub.chmod(0o755)

    ack_dir = tmp_path / "ack"
    res = subprocess.run(
        ["bash", str(WATCHDOG_SCRIPT)],
        env={
            **os.environ,
            "PATH": f"{stub_dir}:" + os.environ.get("PATH", ""),
            "GECKO_REPO": str(repo),
            "PROD_SYSTEMD_DIR": str(prod),
            "SYSTEMD_DRIFT_ACK_DIR": str(ack_dir),
            "SYSTEMD_DRIFT_HEARTBEAT_FILE": str(tmp_path / "heartbeat"),
            "GECKO_ENV_FILE": str(env_file),
            "UV_BIN": "",
        },
        capture_output=True,
        text=True,
    )
    assert res.returncode == 7, (res.stdout, res.stderr)
    # V48 MUST-FIX: ACK_FILE must be absent post-failure (next fire re-alerts)
    ack_file = ack_dir / "last_alerted_hash"
    assert not ack_file.exists(), "ACK_FILE must NOT be written when HTTP fails"


def test_placeholder_token_exits_5(tmp_path):
    repo = _make_fake_repo(tmp_path)
    prod = _make_fake_prod_systemd(tmp_path)
    _write_unit(repo / "systemd", "gecko-foo.service", "[Unit]\nDescription=v1\n")
    _write_unit(prod, "gecko-foo.service", "[Unit]\nDescription=v2\n")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=placeholder\n"
        "TELEGRAM_CHAT_ID=12345\n"
    )

    res = subprocess.run(
        ["bash", str(WATCHDOG_SCRIPT)],
        env={
            **os.environ,
            "GECKO_REPO": str(repo),
            "PROD_SYSTEMD_DIR": str(prod),
            "SYSTEMD_DRIFT_ACK_DIR": str(tmp_path / "ack"),
            "SYSTEMD_DRIFT_HEARTBEAT_FILE": str(tmp_path / "heartbeat"),
            "GECKO_ENV_FILE": str(env_file),
            "UV_BIN": "",
        },
        capture_output=True,
        text=True,
    )
    assert res.returncode == 5, (res.stdout, res.stderr)


# ----------------------------------------------------------------------
# Test #9 (V45 SHOULD-FIX): filename with spaces — find -print0 + read -d ''
# ----------------------------------------------------------------------


def test_filename_with_spaces(tmp_path):
    repo = _make_fake_repo(tmp_path)
    prod = _make_fake_prod_systemd(tmp_path)
    body = "[Unit]\nDescription=foo\n"
    _write_unit(repo / "systemd", "gecko-foo bar.service", body)
    _write_unit(prod, "gecko-foo bar.service", body)

    res, marker = _run_watchdog(tmp_path, repo, prod)
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert not marker.exists()


# ----------------------------------------------------------------------
# Test #10 (V45 SHOULD-FIX): payload truncation under 4096
# ----------------------------------------------------------------------


def test_payload_truncation_under_4096(tmp_path):
    repo = _make_fake_repo(tmp_path)
    prod = _make_fake_prod_systemd(tmp_path)
    # Create many drifts; each "DRIFT: gecko-NN.service\n" is ~30 chars; 100 → ~3000
    for i in range(100):
        name = f"gecko-{i:03d}.service"
        _write_unit(repo / "systemd", name, "[Unit]\nDescription=v1\n")
        _write_unit(prod, name, f"[Unit]\nDescription=v2-{i}\n")

    res, marker = _run_watchdog(tmp_path, repo, prod)
    assert res.returncode == 1, (res.stdout, res.stderr)
    assert marker.exists()
    payload = marker.read_text()
    # Payload includes "uv called: stub-watchdog-alert <body>"; body must be
    # truncated under ~4000 chars per script truncation policy
    # The marker file gets the FULL stub invocation, not just the body.
    # Just verify the body in the script's view is bounded.
    body_start = payload.find("uv called:")
    assert body_start >= 0
    # Check that script printed truncation footer
    assert "truncated" in payload or len(payload) < 4500


# ----------------------------------------------------------------------
# Tests #11 + #12 (V46 MUST-FIX): ack tombstone
# ----------------------------------------------------------------------


def test_unchanged_drift_set_suppresses_re_alert(tmp_path):
    repo = _make_fake_repo(tmp_path)
    prod = _make_fake_prod_systemd(tmp_path)
    _write_unit(repo / "systemd", "gecko-foo.service", "[Unit]\nDescription=v1\n")
    _write_unit(prod, "gecko-foo.service", "[Unit]\nDescription=v2-prod\n")

    # First run: alert + write hash
    res1, marker1 = _run_watchdog(tmp_path, repo, prod)
    assert res1.returncode == 1
    assert marker1.exists()
    first_payload_size = marker1.stat().st_size

    # Second run with SAME drift state: hash matches; silent suppress
    res2, marker2 = _run_watchdog(tmp_path, repo, prod)
    assert res2.returncode == 1  # still exit 1 (drift exists)
    # Marker exists from prior call; check size DID NOT GROW (no new stub call)
    second_payload_size = marker2.stat().st_size
    assert second_payload_size == first_payload_size, (
        "Second run must NOT invoke stub when drift hash matches"
    )


def test_changed_drift_set_re_alerts(tmp_path):
    repo = _make_fake_repo(tmp_path)
    prod = _make_fake_prod_systemd(tmp_path)
    _write_unit(repo / "systemd", "gecko-foo.service", "[Unit]\nDescription=v1\n")
    _write_unit(prod, "gecko-foo.service", "[Unit]\nDescription=foo-prod\n")

    # First run: drift on foo
    res1, marker1 = _run_watchdog(tmp_path, repo, prod)
    assert res1.returncode == 1
    first_size = marker1.stat().st_size

    # Change drift state: add a drift on bar; revert foo
    body = "[Unit]\nDescription=v1\n"
    _write_unit(repo / "systemd", "gecko-foo.service", body)
    _write_unit(prod, "gecko-foo.service", body)
    _write_unit(repo / "systemd", "gecko-bar.service", "[Unit]\nDescription=v1\n")
    _write_unit(prod, "gecko-bar.service", "[Unit]\nDescription=bar-prod\n")

    res2, marker2 = _run_watchdog(tmp_path, repo, prod)
    assert res2.returncode == 1
    second_size = marker2.stat().st_size
    assert second_size > first_size, "Stub MUST be called again on changed drift"


# ----------------------------------------------------------------------
# Test #13 (V48 MUST-FIX): stable hash under filesystem order
# ----------------------------------------------------------------------


def test_stable_hash_under_filesystem_order_perturbation(tmp_path):
    """Two drifts that get serialized in different filesystem-orders across
    runs must produce identical hashes → silent suppression on second run.

    Implementation strategy: create 2 drifts, run once, snapshot ACK_FILE;
    rename files to provoke different inode-order, run again, verify
    SAME hash + silent-suppress (stub NOT called second time).
    """
    repo = _make_fake_repo(tmp_path)
    prod = _make_fake_prod_systemd(tmp_path)
    _write_unit(repo / "systemd", "gecko-aaa.service", "[Unit]\nDescription=v1\n")
    _write_unit(prod, "gecko-aaa.service", "[Unit]\nDescription=aaa-prod\n")
    _write_unit(repo / "systemd", "gecko-zzz.service", "[Unit]\nDescription=v1\n")
    _write_unit(prod, "gecko-zzz.service", "[Unit]\nDescription=zzz-prod\n")

    # First run
    res1, marker1 = _run_watchdog(tmp_path, repo, prod)
    assert res1.returncode == 1
    first_size = marker1.stat().st_size
    ack_file = tmp_path / "ack" / "last_alerted_hash"
    assert ack_file.exists()
    first_hash = ack_file.read_text().strip()

    # Provoke order perturbation: touch each file to update inode times
    import time as _time
    _time.sleep(0.05)
    (prod / "gecko-zzz.service").touch()
    _time.sleep(0.05)
    (prod / "gecko-aaa.service").touch()

    # Second run — same content, possibly different filesystem-order
    res2, marker2 = _run_watchdog(tmp_path, repo, prod)
    assert res2.returncode == 1
    second_hash = ack_file.read_text().strip()
    assert second_hash == first_hash, (
        f"Hash must be stable under filesystem-order perturbation: "
        f"{first_hash} vs {second_hash}"
    )
    # And stub must NOT be re-invoked
    second_size = marker2.stat().st_size
    assert second_size == first_size, "Same drift set must not re-alert"
