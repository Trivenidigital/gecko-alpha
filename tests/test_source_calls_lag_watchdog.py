"""Tests for scripts/source-calls-lag-watchdog.sh.

Modeled on tests/test_systemd_drift_watchdog.py: stub-binary + PATH-injection +
marker-file pattern. Skipped on Windows: bash + set -a + source semantics are
Linux-only.

Stubs:
  * python — replaces scripts/check_source_calls_lag.py invocation, returns a
    test-controlled exit code + JSON body on stdout.
  * curl — captures the args the wrapper would have sent to Telegram so the
    test can assert on the dispatched message (parse_mode=, chat_id, text).

Fixture write strategy: write_bytes(content.encode()) — V45 SHOULD-FIX CRLF
guard, mirrors test_systemd_drift_watchdog.py.
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
    reason="bash + set -a + source are Linux-specific",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHDOG_SCRIPT = REPO_ROOT / "scripts" / "source-calls-lag-watchdog.sh"


def _make_python_stub(tmp_path: Path, exit_code: int, stdout_body: str) -> Path:
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "python_stub"
    stub.write_bytes(
        (
            "#!/usr/bin/env bash\n"
            f"cat <<'PYOUT'\n{stdout_body}\nPYOUT\n"
            f"exit {exit_code}\n"
        ).encode()
    )
    stub.chmod(0o755)
    return stub


def _make_curl_stub(tmp_path: Path) -> tuple[Path, Path]:
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "curl"
    marker = tmp_path / "curl_marker"
    quoted = shlex.quote(str(marker))
    stub.write_bytes(
        (
            "#!/usr/bin/env bash\n"
            f"echo \"curl-args: $@\" >> {quoted}\n"
            "exit 0\n"
        ).encode()
    )
    stub.chmod(0o755)
    return stub, marker


def _write_env_file(tmp_path: Path, *, token: str, chat_id: str) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_bytes(
        (f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={chat_id}\n").encode()
    )
    return env_file


def _run(
    python_stub: Path,
    *,
    env_file: Path | None,
    curl_stub: Path | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if curl_stub is not None:
        env["PATH"] = f"{curl_stub.parent}:" + env.get("PATH", "")
    env["GECKO_PYTHON"] = str(python_stub)
    if env_file is not None:
        env["GECKO_ENV_FILE"] = str(env_file)
    args = ["bash", str(WATCHDOG_SCRIPT), "--db", "/dev/null"]
    if extra_args:
        args.extend(extra_args)
    return subprocess.run(args, env=env, capture_output=True, text=True)


def test_clean_exits_zero_and_skips_curl(tmp_path):
    python_stub = _make_python_stub(
        tmp_path,
        exit_code=0,
        stdout_body='{"ok":true,"unledgered_tg":0,"unledgered_x":0}',
    )
    env_file = _write_env_file(tmp_path, token="tok", chat_id="chat")
    curl_stub, marker = _make_curl_stub(tmp_path)

    res = _run(python_stub, env_file=env_file, curl_stub=curl_stub)

    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "OK:" in res.stdout
    assert not marker.exists(), "curl must NOT be invoked on clean path"


def test_lag_dispatches_telegram_with_plain_text(tmp_path):
    body = '{"ok":false,"unledgered_tg":7,"unledgered_x":2}'
    python_stub = _make_python_stub(tmp_path, exit_code=2, stdout_body=body)
    env_file = _write_env_file(tmp_path, token="tok-xyz", chat_id="6337722878")
    curl_stub, marker = _make_curl_stub(tmp_path)

    res = _run(python_stub, env_file=env_file, curl_stub=curl_stub)

    assert res.returncode == 1, (res.stdout, res.stderr)
    assert "ALERT_SENT:" in res.stdout
    assert marker.exists(), "curl must be invoked on lag path"
    payload = marker.read_text()
    assert "api.telegram.org/bot" in payload
    assert "tok-xyz" in payload
    assert "chat_id=6337722878" in payload
    assert "parse_mode=" in payload, "must pass parse_mode= (plain text) per CLAUDE.md §12b"
    assert "source-calls-lag-watchdog" in payload
    assert "status=2" in payload
    assert "unledgered_tg" in payload


def test_missing_env_file_exits_2_without_curl(tmp_path):
    python_stub = _make_python_stub(
        tmp_path, exit_code=2, stdout_body='{"ok":false}'
    )
    curl_stub, marker = _make_curl_stub(tmp_path)
    missing_env = tmp_path / "does-not-exist.env"

    res = _run(python_stub, env_file=missing_env, curl_stub=curl_stub)

    assert res.returncode == 2, (res.stdout, res.stderr)
    assert "ALERT:" in res.stdout
    assert "env file missing" in res.stderr
    assert not marker.exists()


def test_missing_telegram_creds_exits_3_without_curl(tmp_path):
    python_stub = _make_python_stub(
        tmp_path, exit_code=2, stdout_body='{"ok":false}'
    )
    env_file = _write_env_file(tmp_path, token="", chat_id="")
    curl_stub, marker = _make_curl_stub(tmp_path)

    res = _run(python_stub, env_file=env_file, curl_stub=curl_stub)

    assert res.returncode == 3, (res.stdout, res.stderr)
    assert "ALERT:" in res.stdout
    assert "Telegram env missing" in res.stderr
    assert not marker.exists()


def test_unknown_argument_exits_64(tmp_path):
    python_stub = _make_python_stub(tmp_path, exit_code=0, stdout_body="")
    env = os.environ.copy()
    env["GECKO_PYTHON"] = str(python_stub)
    res = subprocess.run(
        ["bash", str(WATCHDOG_SCRIPT), "--bogus-flag"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 64, (res.stdout, res.stderr)
    assert "unknown argument" in res.stderr
