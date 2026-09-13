"""RH watchdog ops review round 2: reason-aware cooldown (R1) and the wrapper
argument contract (R3).

The send path is Linux-only (``fcntl``); a stub ``fcntl`` and a recording
sender keep these tests portable and network-free.
"""

import importlib.util
import os
import sys
import types
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "dex_discovery_watchdog.py"
WRAPPER = ROOT / "scripts" / "rh-pons-watchdog.sh"


def _load_watchdog():
    spec = importlib.util.spec_from_file_location("rh_watchdog_ops_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def watchdog(monkeypatch, tmp_path):
    module = _load_watchdog()
    stub = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=lambda *a: None)
    monkeypatch.setitem(sys.modules, "fcntl", stub)
    sent = []

    async def record(text):
        sent.append(text)

    monkeypatch.setattr(module, "_send_via_alerter", record)
    db = tmp_path / "watchdog.db"
    db.write_bytes(b"")
    return module, sent, db, tmp_path / "state"


def _check(source, reason):
    return {
        "source": source,
        "check": "poll_liveness",
        "status": "breach",
        "reason": reason,
        "last_successful_poll_at": "2026-09-13T00:00:00+00:00",
        "last_new_discovery_at": None,
        "discovery_age_hours": None,
        "discovery_timestamp_valid": None,
        "poll_age_hours": 1.0,
        "poll_age_seconds_signed": 3600.0,
        "staleness_hours": 0.1667,
        "clock_skew_seconds": 300.0,
        "head_lag_blocks": 5000,
        "max_head_lag_blocks": 2000,
        "checkpoint_updated_at": None,
        "consecutive_failed_passes": 12,
        "max_consecutive_failed_passes": 10,
        "last_attempt_at": None,
    }


def _run(module, monkeypatch, db, state_dir, source, reason):
    async def read_state(*args, **kwargs):
        return _check(source, reason)

    monkeypatch.setattr(module, "_read_state", read_state)
    argv = [
        "--source",
        source,
        "--db",
        str(db),
        "--enabled",
        "true",
        "--discovery-enabled",
        "true",
        "--state-dir",
        str(state_dir),
    ]
    return module.main(argv)


def test_rh_reason_change_pages_despite_active_cooldown(watchdog, monkeypatch):
    module, sent, db, state = watchdog
    assert _run(module, monkeypatch, db, state, "rh_pons", "head_lag_exceeded") == 5
    assert len(sent) == 1
    # A different, worse failure inside the 24 h cooldown must still reach a
    # human: a transient lag page cannot silence a real outage.
    assert _run(module, monkeypatch, db, state, "rh_pons", "stale") == 5
    assert len(sent) == 2 and "stalled" in sent[1]
    # The same reason again stays under the existing cooldown.
    assert _run(module, monkeypatch, db, state, "rh_pons", "stale") == 5
    assert len(sent) == 2


def test_rh_legacy_cooldown_without_reason_pages_once(watchdog, monkeypatch):
    module, sent, db, state = watchdog
    state.mkdir(parents=True)
    module._write_cooldown_state(str(state), module.datetime.now(module.timezone.utc))
    (state / "last_alert_poll_liveness_reason").unlink(missing_ok=True)
    assert _run(module, monkeypatch, db, state, "rh_pons", "stale") == 5
    assert len(sent) == 1
    assert _run(module, monkeypatch, db, state, "rh_pons", "stale") == 5
    assert len(sent) == 1


def test_dex_cooldown_behaviour_is_unchanged(watchdog, monkeypatch):
    module, sent, db, state = watchdog
    assert _run(module, monkeypatch, db, state, "dex_discovery", "stale") == 5
    assert (
        _run(module, monkeypatch, db, state, "dex_discovery", "heartbeat_absent") == 5
    )
    assert len(sent) == 1  # DEX keeps one cooldown for every reason
    assert not (state / "last_alert_poll_liveness_reason").exists()


def _bash():
    candidate = (
        Path("C:/Program Files/Git/bin/bash.exe")
        if os.name == "nt"
        else Path("/bin/bash")
    )
    if not candidate.exists():
        pytest.skip("Bash is required for the deployment wrapper")
    return candidate


@pytest.mark.parametrize(
    "env_lines,minutes,streak",
    [
        ((), "10", "10"),
        (
            (
                "RH_PONS_POLL_STALENESS_ALERT_MINUTES=7",
                "RH_PONS_MAX_CONSECUTIVE_FAILED_PASSES=4",
            ),
            "7",
            "4",
        ),
    ],
)
def test_wrapper_passes_minute_staleness_and_streak_limit(
    tmp_path, env_lines, minutes, streak
):
    bash = _bash()
    stub = tmp_path / "echo-python.sh"
    stub.write_bytes(b"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n")
    stub.chmod(0o755)
    env_file = tmp_path / "synthetic.env"
    env_file.write_text("".join(f"{line}\n" for line in env_lines))
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GECKO_ENV_FILE": env_file.as_posix(),
        "GECKO_PYTHON": stub.as_posix(),
        "RH_PONS_WATCHDOG_ENABLED": "true",
    }
    result = subprocess.run(
        [str(bash), WRAPPER.as_posix()],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    args = result.stdout.split()
    assert args[args.index("--staleness-minutes") + 1] == minutes
    assert args[args.index("--max-consecutive-failed-passes") + 1] == streak
    assert "--staleness-hours" not in args
    assert args[args.index("--source") + 1] == "rh_pons"
