"""Tests for scripts/source-calls-lag-watchdog.sh.

Modeled on tests/test_systemd_drift_watchdog.py: stub-binary + PATH-injection +
marker-file pattern. Skipped on Windows: bash + set -a + source semantics are
Linux-only.

Stubs:
  * python — replaces scripts/check_source_calls_lag.py invocation, returns a
    test-controlled exit code + JSON body on stdout.
  * curl — captures the args the wrapper would have sent to Telegram so the
    test can assert on the dispatched message (parse_mode=, chat_id, text).
    Echoes a configurable HTTP code (default 200) to stdout for the
    wrapper's -w "%{http_code}" capture.

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


def _make_curl_stub(tmp_path: Path, http_code: str = "200", exit_code: int = 0) -> tuple[Path, Path]:
    """Mock curl: append args to marker, echo HTTP code to stdout."""
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "curl"
    marker = tmp_path / "curl_marker"
    quoted = shlex.quote(str(marker))
    stub.write_bytes(
        (
            "#!/usr/bin/env bash\n"
            f"echo \"curl-args: $@\" >> {quoted}\n"
            f"echo {http_code}\n"
            f"exit {exit_code}\n"
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
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GECKO_ENV_FILE"] = "/tmp/gecko-alpha-test-missing.env"
    if curl_stub is not None:
        env["PATH"] = f"{curl_stub.parent}:" + env.get("PATH", "")
    env["GECKO_PYTHON"] = str(python_stub)
    if env_file is not None:
        env["GECKO_ENV_FILE"] = str(env_file)
    if extra_env:
        env.update(extra_env)
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
    env["GECKO_ENV_FILE"] = str(tmp_path / "missing.env")
    env["GECKO_PYTHON"] = str(python_stub)
    res = subprocess.run(
        ["bash", str(WATCHDOG_SCRIPT), "--bogus-flag"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 64, (res.stdout, res.stderr)
    assert "unknown argument" in res.stderr


# ----- Writer-side branch tests (new in BL-NEW-SOURCE-CALL-CRON-TICK-WATCHDOG) -----


def test_writer_stale_alert_text_says_last_succeeded(tmp_path):
    """When the Python check returns status=writer_stale, alert text must
    use the honest 'last SUCCEEDED' wording, not 'last fired' — mtime only
    advances on successful runs, so the operator-facing phrasing must
    differentiate (Reviewer-A C2)."""
    body = (
        '{"ok":false,"status":"writer_stale","detail":{"age_minutes":47.2,'
        '"threshold_minutes":20,"path":"/var/lib/heartbeat",'
        '"last_writer_success_at":"2026-05-21T00:00:00+00:00"}}'
    )
    python_stub = _make_python_stub(tmp_path, exit_code=5, stdout_body=body)
    env_file = _write_env_file(tmp_path, token="tok", chat_id="chat")
    curl_stub, marker = _make_curl_stub(tmp_path)

    res = _run(python_stub, env_file=env_file, curl_stub=curl_stub)

    assert res.returncode == 1, (res.stdout, res.stderr)
    assert marker.exists()
    payload = marker.read_text()
    assert "writer cron stale" in payload
    assert "last SUCCEEDED" in payload, (
        "Reviewer-A C2: must say 'last SUCCEEDED' not 'last fired' — "
        "mtime advances only on success, so the wording must be honest "
        "about the semantic distinction"
    )


def test_writer_heartbeat_missing_alert_text_explains_cause(tmp_path):
    body = (
        '{"ok":false,"status":"writer_heartbeat_missing","detail":{'
        '"path":"/var/lib/heartbeat","ledger_has_rows":true}}'
    )
    python_stub = _make_python_stub(tmp_path, exit_code=5, stdout_body=body)
    env_file = _write_env_file(tmp_path, token="tok", chat_id="chat")
    curl_stub, marker = _make_curl_stub(tmp_path)

    res = _run(python_stub, env_file=env_file, curl_stub=curl_stub)

    assert res.returncode == 1, (res.stdout, res.stderr)
    payload = marker.read_text()
    assert "writer heartbeat missing" in payload
    assert "ledger has rows" in payload, (
        "alert body must explain why this is distinct from writer_stale"
    )


def test_writer_never_fired_alert_text_explains_escalation_threshold(tmp_path):
    body = (
        '{"ok":false,"status":"writer_never_fired","detail":{'
        '"path":"/var/lib/heartbeat","age_hours":7.0,"escalation_hours":6}}'
    )
    python_stub = _make_python_stub(tmp_path, exit_code=5, stdout_body=body)
    env_file = _write_env_file(tmp_path, token="tok", chat_id="chat")
    curl_stub, marker = _make_curl_stub(tmp_path)

    res = _run(python_stub, env_file=env_file, curl_stub=curl_stub)

    assert res.returncode == 1, (res.stdout, res.stderr)
    payload = marker.read_text()
    assert "writer never fired" in payload
    assert ">6h" in payload, "alert must mention the escalation threshold (6h, dropped from 24h per PR review fold)"


def test_writer_pending_returns_zero_no_alert(tmp_path):
    """First-run guard: heartbeat absent + ledger empty + age < 24h →
    Python returns exit 0 with status=writer_heartbeat_pending → wrapper
    treats as healthy, no Telegram dispatch."""
    body = (
        '{"ok":true,"status":"writer_heartbeat_pending","detail":{'
        '"path":"/var/lib/heartbeat","ledger_has_rows":false,"alert_suppressed":true}}'
    )
    python_stub = _make_python_stub(tmp_path, exit_code=0, stdout_body=body)
    env_file = _write_env_file(tmp_path, token="tok", chat_id="chat")
    curl_stub, marker = _make_curl_stub(tmp_path)

    res = _run(python_stub, env_file=env_file, curl_stub=curl_stub)

    assert res.returncode == 0, (res.stdout, res.stderr)
    assert not marker.exists()


def test_telegram_delivery_failure_exits_7_emits_failed_log(tmp_path):
    """Reviewer-B §12b triplet: HTTP non-200 → exit 7 + structured
    alert_failed log line (NOT alert_delivered)."""
    body = (
        '{"ok":false,"status":"writer_stale","detail":{"age_minutes":50}}'
    )
    python_stub = _make_python_stub(tmp_path, exit_code=5, stdout_body=body)
    env_file = _write_env_file(tmp_path, token="tok", chat_id="chat")
    curl_stub, marker = _make_curl_stub(tmp_path, http_code="500", exit_code=0)

    res = _run(python_stub, env_file=env_file, curl_stub=curl_stub)

    assert res.returncode == 7, (res.stdout, res.stderr)
    assert "ALERT_FAILED_DELIVERY:" in res.stderr
    assert "source_calls_lag_alert_failed" in res.stderr
    assert "source_calls_lag_alert_delivered" not in res.stderr
    assert "source_calls_lag_alert_dispatched" in res.stderr


def test_section_12b_log_triplet_on_success(tmp_path):
    body = (
        '{"ok":false,"status":"writer_stale","detail":{"age_minutes":50}}'
    )
    python_stub = _make_python_stub(tmp_path, exit_code=5, stdout_body=body)
    env_file = _write_env_file(tmp_path, token="tok", chat_id="chat")
    curl_stub, marker = _make_curl_stub(tmp_path, http_code="200")

    res = _run(python_stub, env_file=env_file, curl_stub=curl_stub)

    assert res.returncode == 1
    # All three triplet lines must appear on stderr (journal capture).
    assert "source_calls_lag_alert_dispatched" in res.stderr
    assert "source_calls_lag_alert_delivered" in res.stderr
    assert "source_calls_lag_alert_failed" not in res.stderr


def test_writer_args_passed_through_to_python(tmp_path):
    """Wrapper must pass --writer-heartbeat-file and --writer-threshold-minutes
    through to the Python check."""
    python_stub_dir = tmp_path / "stubs"
    python_stub_dir.mkdir(exist_ok=True)
    args_capture = tmp_path / "py_args_capture"
    quoted = shlex.quote(str(args_capture))
    python_stub = python_stub_dir / "python_stub"
    python_stub.write_bytes(
        (
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" > {quoted}\n"
            'echo \'{"ok":true,"unledgered_tg":0,"unledgered_x":0}\'\n'
            "exit 0\n"
        ).encode()
    )
    python_stub.chmod(0o755)

    env = os.environ.copy()
    env["GECKO_ENV_FILE"] = str(tmp_path / "missing.env")
    env["GECKO_PYTHON"] = str(python_stub)
    env["WRITER_HEARTBEAT_FILE"] = "/tmp/heartbeat"
    env["WRITER_THRESHOLD_MINUTES"] = "30"

    res = subprocess.run(
        ["bash", str(WATCHDOG_SCRIPT), "--db", "/dev/null"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, (res.stdout, res.stderr)
    captured = args_capture.read_text()
    assert "--writer-heartbeat-file /tmp/heartbeat" in captured
    assert "--writer-threshold-minutes 30" in captured


def test_writer_branch_disabled_by_default(tmp_path):
    """No WRITER_HEARTBEAT_FILE env → writer-branch args NOT passed to
    Python (back-compat)."""
    python_stub_dir = tmp_path / "stubs"
    python_stub_dir.mkdir(exist_ok=True)
    args_capture = tmp_path / "py_args_capture"
    quoted = shlex.quote(str(args_capture))
    python_stub = python_stub_dir / "python_stub"
    python_stub.write_bytes(
        (
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" > {quoted}\n"
            'echo \'{"ok":true,"unledgered_tg":0,"unledgered_x":0}\'\n'
            "exit 0\n"
        ).encode()
    )
    python_stub.chmod(0o755)

    env = os.environ.copy()
    env["GECKO_ENV_FILE"] = str(tmp_path / "missing.env")
    env["GECKO_PYTHON"] = str(python_stub)
    # Explicitly clear in case parent shell has it set
    env.pop("WRITER_HEARTBEAT_FILE", None)

    res = subprocess.run(
        ["bash", str(WATCHDOG_SCRIPT), "--db", "/dev/null"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    captured = args_capture.read_text()
    assert "--writer-heartbeat-file" not in captured
    assert "--writer-threshold-minutes" not in captured


def _extract_alert_text(payload: str) -> str:
    """Isolate the Telegram alert body (the --data-urlencode text= value) from
    the flat curl-args marker line, bounded by the trailing ` -d parse_mode=`
    arg so the curl `-w "%{http_code}"` brace can't be mistaken for alert
    content."""
    start = payload.index("text=") + len("text=")
    end = payload.index(" -d parse_mode=", start)
    return payload[start:end]


def test_alert_body_does_not_leak_raw_json_in_writer_stale_text(tmp_path):
    """The operator-facing alert must be human prose only — NO raw JSON blob
    appended (supersedes the earlier 'JSON appended for diagnosis' behavior;
    the full JSON still lands in the journal via the wrapper's stdout). Guards
    against re-introducing the `detail=${result}` dump."""
    body = (
        '{"ok":false,"status":"writer_stale","detail":{"age_minutes":47.2,'
        '"last_writer_success_at":"2026-05-21T00:00:00+00:00"}}'
    )
    python_stub = _make_python_stub(tmp_path, exit_code=5, stdout_body=body)
    env_file = _write_env_file(tmp_path, token="tok", chat_id="chat")
    curl_stub, marker = _make_curl_stub(tmp_path)

    res = _run(python_stub, env_file=env_file, curl_stub=curl_stub)

    assert res.returncode == 1
    alert_text = _extract_alert_text(marker.read_text())
    assert "source-calls-lag-watchdog" in alert_text
    assert "{" not in alert_text and "}" not in alert_text, (
        f"Alert body must not contain raw JSON braces. Got: {alert_text!r}"
    )
    assert '"ok":' not in alert_text and "detail=" not in alert_text


def test_writer_stale_alert_shows_readable_age_and_last_success(tmp_path):
    """Instead of dumping raw JSON, the writer_stale alert surfaces the two
    actionable diagnostic fields (age in minutes + last success timestamp) as
    readable prose."""
    body = (
        '{"ok":false,"status":"writer_stale","detail":{"age_minutes":135.0,'
        '"threshold_minutes":20,"path":"/var/lib/heartbeat",'
        '"last_writer_success_at":"2026-06-22T15:45:03+00:00"}}'
    )
    python_stub = _make_python_stub(tmp_path, exit_code=5, stdout_body=body)
    env_file = _write_env_file(tmp_path, token="tok", chat_id="chat")
    curl_stub, marker = _make_curl_stub(tmp_path)

    res = _run(python_stub, env_file=env_file, curl_stub=curl_stub)

    assert res.returncode == 1
    alert_text = _extract_alert_text(marker.read_text())
    # Specific prose phrasing the extraction produces — these strings do NOT
    # appear in the raw JSON (`"age_minutes":135.0`), so this fails if the
    # fields are merely dumped rather than formatted.
    assert "(135.0 min ago)" in alert_text, "must surface age_minutes as prose"
    assert "Last success: 2026-06-22T15:45:03+00:00" in alert_text, (
        "must surface last_writer_success_at as prose"
    )
