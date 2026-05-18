"""Tests for scripts/cron-drift-watchdog.sh.

Mirrors tests/test_systemd_drift_watchdog.py structure. Skipped on Windows
(bash + flock + awk semantics are Linux-specific).

BL-NEW-CRON-DRIFT-WATCHDOG (cycle 12).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash + flock + awk semantics are Linux-specific",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHDOG_SCRIPT = REPO_ROOT / "scripts" / "cron-drift-watchdog.sh"

SENTINEL_START = "# === BEGIN gecko-alpha managed block (do not edit between sentinels) ==="
SENTINEL_END = "# === END gecko-alpha managed block ==="


# ---------------------------------------------------------------------------
# Stubs / fixtures
# ---------------------------------------------------------------------------


def _make_uv_stub(tmp_path: Path) -> tuple[Path, Path]:
    """uv stub that records all invocations to a marker file.

    Mirrors tests/test_systemd_drift_watchdog.py:_make_uv_stub. The watchdog
    uses UV_BIN as a Telegram-alert mock seam — it calls
    `"$UV_BIN" stub-watchdog-alert "$ALERT_BODY"` instead of curl.
    """
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "uv"
    marker = tmp_path / "alert_marker"
    qm = shlex.quote(str(marker))
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "uv called: $@" >> {qm}\n'
        # Also record the alert body (passed as $2) so tests can assert on it
        f'echo "BODY: $2" >> {qm}\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    return stub, marker


def _make_crontab_stub(tmp_path: Path, content: str) -> Path:
    """Stub crontab that emits `content` on `-l`, errors on anything else.

    Per plan v2 R1 #5 fold: stub MUST loudly error on unexpected
    invocations so a future refactor that adds `crontab install` doesn't
    test-pass while doing nothing in prod.
    """
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "crontab"
    qc = shlex.quote(content)
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "-l" ]]; then\n'
        f"  printf '%s' {qc}\n"
        "  exit 0\n"
        "else\n"
        '  echo "ERROR: stub crontab got unexpected invocation: $*" >&2\n'
        "  exit 99\n"
        "fi\n"
    )
    stub.chmod(0o755)
    return stub


def _make_fragment(tmp_path: Path, body: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    cron_dir = repo / "cron"
    cron_dir.mkdir(exist_ok=True)
    frag = cron_dir / "gecko-alpha.crontab"
    frag.write_text(f"{SENTINEL_START}\n{body}\n{SENTINEL_END}\n")
    return frag


def _run_watchdog(
    tmp_path: Path,
    *,
    env_extras: dict[str, str] | None = None,
    uv_stub: Path | None = None,
    crontab_stub: Path | None = None,
    omit_crontab: bool = False,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CRON_DRIFT_ACK_DIR"] = str(tmp_path / "ack")
    env["GECKO_REPO"] = str(tmp_path / "repo")
    env["GECKO_ENV_FILE"] = "/dev/null"  # unused on stub path
    if uv_stub:
        env["UV_BIN"] = str(uv_stub)
        # PR R1 #15 fold: tests must opt-in to stub path so prod accidental
        # UV_BIN doesn't silently absorb alerts.
        env["GECKO_WATCHDOG_ALLOW_UV_STUB"] = "1"
    if crontab_stub:
        env["CRONTAB_BIN"] = str(crontab_stub)
    elif omit_crontab:
        env["CRONTAB_BIN"] = str(tmp_path / "nonexistent-crontab-binary")
    if env_extras:
        env.update(env_extras)
    return subprocess.run(
        ["bash", str(WATCHDOG_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Basic clean / drift tests
# ---------------------------------------------------------------------------


def test_clean_when_managed_block_matches_fragment(tmp_path):
    body = (
        "30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh\n"
        "45 3 * * 0 /root/gecko-alpha/scripts/wal_archive.sh"
    )
    _make_fragment(tmp_path, body)
    live = f"{SENTINEL_START}\n{body}\n{SENTINEL_END}\n"
    crontab_stub = _make_crontab_stub(tmp_path, live)
    r = _run_watchdog(tmp_path, crontab_stub=crontab_stub)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "OK: 0 drifts" in r.stdout
    assert (tmp_path / "ack" / "heartbeat").exists()


def test_drift_when_managed_block_differs(tmp_path):
    body = (
        "30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh\n"
        "45 3 * * 0 /root/gecko-alpha/scripts/wal_archive.sh"
    )
    _make_fragment(tmp_path, body)
    # Live missing wal_archive line → drift
    live = (
        f"{SENTINEL_START}\n"
        "30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh\n"
        f"{SENTINEL_END}\n"
    )
    crontab_stub = _make_crontab_stub(tmp_path, live)
    uv_stub, marker = _make_uv_stub(tmp_path)
    r = _run_watchdog(tmp_path, crontab_stub=crontab_stub, uv_stub=uv_stub)
    assert r.returncode == 1, f"expected DRIFT exit=1; stdout={r.stdout!r} stderr={r.stderr!r}"
    assert marker.exists(), "uv-stub should have been called"
    body_text = marker.read_text()
    assert "drift" in body_text.lower()


def test_drift_when_managed_block_missing(tmp_path):
    body = "30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh"
    _make_fragment(tmp_path, body)
    # Live crontab has only an unrelated entry; no sentinels
    live = "0 */6 * * * /opt/polymarket-ml-signal/scripts/extract_data.sh\n"
    crontab_stub = _make_crontab_stub(tmp_path, live)
    uv_stub, marker = _make_uv_stub(tmp_path)
    r = _run_watchdog(tmp_path, crontab_stub=crontab_stub, uv_stub=uv_stub)
    assert r.returncode == 1
    body_text = marker.read_text()
    assert "managed block missing" in body_text


def test_silent_suppress_on_unchanged_drift_hash(tmp_path):
    body = "30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh"
    _make_fragment(tmp_path, body)
    live = f"{SENTINEL_START}\n# wrong content\n{SENTINEL_END}\n"
    crontab_stub = _make_crontab_stub(tmp_path, live)
    uv_stub, marker = _make_uv_stub(tmp_path)
    r1 = _run_watchdog(tmp_path, crontab_stub=crontab_stub, uv_stub=uv_stub)
    assert r1.returncode == 1
    n_lines_after_first = len(marker.read_text().splitlines())

    r2 = _run_watchdog(tmp_path, crontab_stub=crontab_stub, uv_stub=uv_stub)
    assert r2.returncode == 1
    assert "SUPPRESS" in r2.stdout
    n_lines_after_second = len(marker.read_text().splitlines())
    assert n_lines_after_first == n_lines_after_second, \
        "uv-stub must NOT be re-invoked on identical drift hash"


# ---------------------------------------------------------------------------
# R1 fold tests
# ---------------------------------------------------------------------------


def test_clean_when_fragment_has_internal_blank_line(tmp_path):
    """R1 #2/#3 fold: tempfile-based diff must not flag false drift on
    fragments with internal blank lines (command-substitution-newline
    asymmetry was the original bug)."""
    body = (
        "30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh\n"
        "\n"  # internal blank line
        "45 3 * * 0 /root/gecko-alpha/scripts/wal_archive.sh"
    )
    _make_fragment(tmp_path, body)
    live = f"{SENTINEL_START}\n{body}\n{SENTINEL_END}\n"
    crontab_stub = _make_crontab_stub(tmp_path, live)
    r = _run_watchdog(tmp_path, crontab_stub=crontab_stub)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "OK: 0 drifts" in r.stdout


def test_drift_on_malformed_sentinel_only_begin(tmp_path):
    """R1 #1 fold: explicit DRIFT line for sentinel-count != 1."""
    body = "30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh"
    _make_fragment(tmp_path, body)
    # BEGIN present but no END
    live = f"{SENTINEL_START}\n{body}\n"
    crontab_stub = _make_crontab_stub(tmp_path, live)
    uv_stub, marker = _make_uv_stub(tmp_path)
    r = _run_watchdog(tmp_path, crontab_stub=crontab_stub, uv_stub=uv_stub)
    assert r.returncode == 1
    body_text = marker.read_text()
    assert "malformed sentinel structure" in body_text
    assert "begin=1 end=0" in body_text


def test_drift_on_sentinel_text_typo(tmp_path):
    """R1 #8 fold: detect renamed/typo'd sentinel via loose grep."""
    body = "30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh"
    _make_fragment(tmp_path, body)
    # Operator typo: extra space before "managed"
    live = (
        "# === BEGIN gecko-alpha  managed block (do not edit between sentinels) ===\n"
        f"{body}\n"
        f"{SENTINEL_END}\n"
    )
    crontab_stub = _make_crontab_stub(tmp_path, live)
    uv_stub, marker = _make_uv_stub(tmp_path)
    r = _run_watchdog(tmp_path, crontab_stub=crontab_stub, uv_stub=uv_stub)
    assert r.returncode == 1
    body_text = marker.read_text()
    assert "sentinel text does not match canonical form" in body_text


def test_crontab_binary_missing_exits_6(tmp_path):
    """R1 #4 fold: loud failure when crontab binary not found."""
    body = "30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh"
    _make_fragment(tmp_path, body)
    r = _run_watchdog(tmp_path, omit_crontab=True)
    assert r.returncode == 6, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "crontab binary not found" in r.stderr


def test_payload_does_not_set_parse_mode(tmp_path):
    """R2 #2 fold: regression guard against future copy from Markdown-using
    code path (cf. CLAUDE.md §12b trending_catch incident).

    Asserts the Python payload-assembly inline doesn't include parse_mode.
    Reads the script source directly because the curl branch isn't
    exercised under the UV_BIN stub.
    """
    script_src = WATCHDOG_SCRIPT.read_text()
    # The Python payload assembly is inline in the script; confirm it
    # does not include "parse_mode" anywhere in its body or surrounding
    # context.
    assert "parse_mode" not in script_src, \
        "watchdog must NOT set parse_mode (CLAUDE.md §12b Markdown mangling risk)"
    # Defense-in-depth: payload literal is `{"chat_id": ..., "text": ...}`
    assert '"chat_id"' in script_src
    assert '"text"' in script_src


def test_fragment_file_missing_exits_8(tmp_path):
    """R1 #12 fold: distinct exit code for FRAGMENT missing (vs ENV)."""
    # Don't call _make_fragment; just stub crontab
    live = ""
    crontab_stub = _make_crontab_stub(tmp_path, live)
    r = _run_watchdog(tmp_path, crontab_stub=crontab_stub)
    assert r.returncode == 8, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "repo fragment" in r.stderr
    assert "not found" in r.stderr


def test_diff_body_included_in_alert(tmp_path):
    """Smoke test that the unified-diff body reaches the alert payload so
    operator sees what changed, not just that something changed."""
    body = (
        "30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh\n"
        "45 3 * * 0 /root/gecko-alpha/scripts/wal_archive.sh"
    )
    _make_fragment(tmp_path, body)
    live = (
        f"{SENTINEL_START}\n"
        "30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh\n"
        f"{SENTINEL_END}\n"
    )
    crontab_stub = _make_crontab_stub(tmp_path, live)
    uv_stub, marker = _make_uv_stub(tmp_path)
    _run_watchdog(tmp_path, crontab_stub=crontab_stub, uv_stub=uv_stub)
    body_text = marker.read_text()
    # The wal_archive line should appear in the diff body (as a `-` line)
    assert "wal_archive.sh" in body_text


def test_alert_body_uses_cron_watchdog_prefix_not_systemd(tmp_path):
    """R1 #11 / R2 #15 fold: ALERT_BODY prefix must say 'cron-drift-watchdog',
    NOT 'systemd-drift-watchdog' (verbatim-copy bug guard)."""
    script_src = WATCHDOG_SCRIPT.read_text()
    # The ALERT_BODY string lives in the script source
    assert "⚠️ cron-drift-watchdog:" in script_src
    # And does NOT carry the systemd prefix
    assert "⚠️ systemd-drift-watchdog:" not in script_src
    # Same for the trunc-footer journalctl reference
    assert "see journalctl for cron-drift-watchdog" in script_src
    assert "see journalctl -u systemd-drift-watchdog" not in script_src


def test_response_file_uses_mktemp_not_pid(tmp_path):
    """R2 #4 fold: response file path must use mktemp (symlink-attack-safe),
    not the predictable /tmp/.gecko-*-resp.$$ PID-based pattern."""
    script_src = WATCHDOG_SCRIPT.read_text()
    # Per fold table: should be `mktemp -t gecko-cron-drift-resp.XXXXXX`
    assert "mktemp -t gecko-cron-drift-resp" in script_src
    # And does NOT carry the predictable PID pattern
    assert "/tmp/.gecko-drift-resp.$$" not in script_src


def test_curl_uses_max_time(tmp_path):
    """R2 #12 fold: curl must use --max-time to bound the held-lock window."""
    script_src = WATCHDOG_SCRIPT.read_text()
    assert "--max-time 30" in script_src, \
        "curl invocation must bound execution to prevent stale flock-held alerts"


# ---------------------------------------------------------------------------
# PR-stage 3-reviewer fold tests
# ---------------------------------------------------------------------------


def test_uv_bin_set_without_opt_in_refuses_prod_silent_suppression(tmp_path):
    """PR R1 #15 fold: UV_BIN accidentally set in prod must NOT silently
    absorb the alert; require explicit GECKO_WATCHDOG_ALLOW_UV_STUB=1
    opt-in.

    Without this guard, an operator who sources a shell profile that
    exports UV_BIN into cron environment would silently lose all alert
    delivery while writing ACK files (false-clean signal).
    """
    body = "30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh"
    _make_fragment(tmp_path, body)
    live = f"{SENTINEL_START}\n# wrong content\n{SENTINEL_END}\n"
    crontab_stub = _make_crontab_stub(tmp_path, live)
    # uv_stub passed but GECKO_WATCHDOG_ALLOW_UV_STUB not set (env_extras
    # would normally include it via _run_watchdog; we manually construct
    # the env to demonstrate the guard fires).
    env = os.environ.copy()
    env["CRON_DRIFT_ACK_DIR"] = str(tmp_path / "ack")
    env["GECKO_REPO"] = str(tmp_path / "repo")
    env["GECKO_ENV_FILE"] = "/dev/null"
    env["CRONTAB_BIN"] = str(crontab_stub)
    env["UV_BIN"] = str(tmp_path / "stubs" / "fake-uv")  # path doesn't need to exist
    # Deliberately omit GECKO_WATCHDOG_ALLOW_UV_STUB
    r = subprocess.run(
        ["bash", str(WATCHDOG_SCRIPT)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 6, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "UV_BIN set" in r.stderr
    assert "refusing stub path" in r.stderr


def test_alert_body_includes_actionable_next_step(tmp_path):
    """PR R3 #1 fold: alert body must tell operator what to do."""
    body = "30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh"
    _make_fragment(tmp_path, body)
    live = f"{SENTINEL_START}\n# wrong content\n{SENTINEL_END}\n"
    crontab_stub = _make_crontab_stub(tmp_path, live)
    uv_stub, marker = _make_uv_stub(tmp_path)
    _run_watchdog(tmp_path, crontab_stub=crontab_stub, uv_stub=uv_stub)
    body_text = marker.read_text()
    assert "ACTION:" in body_text
    assert "cron/deploy.sh" in body_text


def test_sentinel_typo_diagnostic_includes_expected_form(tmp_path):
    """PR R3 #2 fold: typo diagnostic must show expected form, not just
    the wrong line found."""
    body = "30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh"
    _make_fragment(tmp_path, body)
    live = (
        "# === BEGIN gecko-alpha  managed block (do not edit between sentinels) ===\n"
        f"{body}\n"
        f"{SENTINEL_END}\n"
    )
    crontab_stub = _make_crontab_stub(tmp_path, live)
    uv_stub, marker = _make_uv_stub(tmp_path)
    _run_watchdog(tmp_path, crontab_stub=crontab_stub, uv_stub=uv_stub)
    body_text = marker.read_text()
    assert "expected:" in body_text
    assert "got:" in body_text


def test_malformed_sentinel_diagnostic_includes_inspect_command(tmp_path):
    """PR R3 #3 fold: malformed-sentinel diagnostic must include the
    inspect command operator can paste."""
    body = "30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh"
    _make_fragment(tmp_path, body)
    live = f"{SENTINEL_START}\n{body}\n"  # missing END
    crontab_stub = _make_crontab_stub(tmp_path, live)
    uv_stub, marker = _make_uv_stub(tmp_path)
    _run_watchdog(tmp_path, crontab_stub=crontab_stub, uv_stub=uv_stub)
    body_text = marker.read_text()
    assert "inspect with: crontab -l" in body_text


def test_diff_body_lines_preserve_order_in_alert(tmp_path):
    """PR R1 #7 fold: DIFF_BODY +/- lines must NOT be sorted into the
    DRIFT_MARKERS pool (would scramble the unified diff into nonsense)."""
    # Create a multi-line drift so the diff has clear `+`/`-`/`@@` lines
    body = (
        "30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh\n"
        "45 3 * * 0 /root/gecko-alpha/scripts/wal_archive.sh\n"
        "0 4 * * 0 /root/gecko-alpha/scripts/cron-drift-watchdog.sh"
    )
    _make_fragment(tmp_path, body)
    # Live has the lines in DIFFERENT order — diff would naturally
    # produce `-` and `+` pairs
    live = (
        f"{SENTINEL_START}\n"
        "0 4 * * 0 /root/gecko-alpha/scripts/cron-drift-watchdog.sh\n"
        "45 3 * * 0 /root/gecko-alpha/scripts/wal_archive.sh\n"
        "30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh\n"
        f"{SENTINEL_END}\n"
    )
    crontab_stub = _make_crontab_stub(tmp_path, live)
    uv_stub, marker = _make_uv_stub(tmp_path)
    _run_watchdog(tmp_path, crontab_stub=crontab_stub, uv_stub=uv_stub)
    body_text = marker.read_text()
    # Headers should appear in the alert
    assert "--- repo:cron/gecko-alpha.crontab" in body_text
    assert "+++ live:crontab -l" in body_text
    # A `@@` hunk header should be present (proves diff structure preserved)
    assert "@@" in body_text
