"""PR-C — wrapper-level cron-gate enforcement (subprocess tests).

The watchdog enable gate must come from the CRON ENVIRONMENT only: the
wrapper captures DEX_DISCOVERY_WATCHDOG_ENABLED before sourcing .env and
unsets it afterwards, so a stray .env entry can neither arm nor disarm the
watchdog. Proven end-to-end by running scripts/dex-discovery-watchdog.sh as
a real subprocess against a synthetic .env for all three env combinations.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "scripts" / "dex-discovery-watchdog.sh"


def _run_wrapper(tmp_path, *, cron_value=None, env_file_lines=()):
    """Run the wrapper with a controlled cron env + synthetic .env file."""
    env_file = tmp_path / "synthetic.env"
    env_file.write_text("".join(f"{line}\n" for line in env_file_lines))
    env = {
        # Minimal clean environment: PATH for bash/coreutils, explicit knobs.
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "GECKO_ENV_FILE": str(env_file),
        "GECKO_PYTHON": sys.executable,
        "DEX_DISCOVERY_WATCHDOG_STATE_DIR": str(tmp_path / "state"),
    }
    if cron_value is not None:
        env["DEX_DISCOVERY_WATCHDOG_ENABLED"] = cron_value
    proc = subprocess.run(
        ["bash", str(WRAPPER), "--db", str(tmp_path / "absent.db")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    payload = None
    for line in proc.stdout.splitlines():
        if line.startswith("{"):
            payload = json.loads(line)
            break
    return proc.returncode, payload


def test_cron_gate_absent_env_true_stays_disabled(tmp_path):
    """.env alone can NEVER arm the watchdog: cron env unset + .env=true →
    disabled no-op."""
    rc, payload = _run_wrapper(
        tmp_path,
        cron_value=None,
        env_file_lines=["DEX_DISCOVERY_WATCHDOG_ENABLED=true"],
    )
    assert rc == 0
    assert payload["status"] == "disabled_noop"


def test_cron_gate_true_env_false_is_enabled(tmp_path):
    """.env can NEVER disarm the watchdog either: cron=true + .env=false →
    armed (proven by reaching the discovery-disabled gate, which sits
    strictly after the watchdog-enabled gate)."""
    rc, payload = _run_wrapper(
        tmp_path,
        cron_value="true",
        env_file_lines=[
            "DEX_DISCOVERY_WATCHDOG_ENABLED=false",
            "DEX_DISCOVERY_ENABLED=false",
        ],
    )
    assert rc == 0
    assert payload["status"] == "not_armed_discovery_disabled"


def test_cron_gate_false_env_true_stays_disabled(tmp_path):
    rc, payload = _run_wrapper(
        tmp_path,
        cron_value="false",
        env_file_lines=[
            "DEX_DISCOVERY_WATCHDOG_ENABLED=true",
            "DEX_DISCOVERY_ENABLED=true",
        ],
    )
    assert rc == 0
    assert payload["status"] == "disabled_noop"
