"""Subprocess tests for investigation/vps_query_pack.sh secret-safety
(PR #467 review gate).

The env report must print threshold values by EXACT-NAME allowlist only —
a prefix match like ^NARRATIVE would also print NARRATIVE_API_KEY, and the
recommended invocation redirects output into /tmp. A second denylist layer
refuses any name containing KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL even if
someone later broadens the allowlist (exercised via EXTRA_THRESHOLD_NAMES).
"""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "investigation" / "vps_query_pack.sh"

FIXTURE_ENV = """\
MIN_SCORE=65
CONVICTION_THRESHOLD=75
QUANT_WEIGHT=0.6
NARRATIVE_WEIGHT=0.4
NARRATIVE_API_KEY=supersecret-narrative-key
NARRATIVE_TOKEN=supersecret-narrative-token
QUANT_PROVIDER_SECRET=supersecret-quant-cred
TELEGRAM_BOT_TOKEN=supersecret-telegram
DETECTION_ALERT_LANE_ENABLED=true
"""


def _run(tmp_path, extra_env=None):
    env_file = tmp_path / "fixture.env"
    env_file.write_text(FIXTURE_ENV)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GECKO_ENV": str(env_file),
        "GECKO_DB": str(tmp_path / "absent.db"),
    }
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--env-report-only"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc


def test_secrets_never_appear_in_output(tmp_path):
    proc = _run(tmp_path)
    assert proc.returncode == 0
    assert "supersecret" not in proc.stdout
    assert "supersecret" not in proc.stderr
    for name in (
        "NARRATIVE_API_KEY",
        "NARRATIVE_TOKEN",
        "QUANT_PROVIDER_SECRET",
        "TELEGRAM_BOT_TOKEN",
    ):
        assert f"{name}=" not in proc.stdout


def test_allowlisted_thresholds_are_printed(tmp_path):
    proc = _run(tmp_path)
    assert "MIN_SCORE=65" in proc.stdout
    assert "CONVICTION_THRESHOLD=75" in proc.stdout
    assert "QUANT_WEIGHT=0.6" in proc.stdout
    assert "NARRATIVE_WEIGHT=0.4" in proc.stdout
    # Flags reported as set/unset, value shown only for the known flag names
    assert "DETECTION_ALERT_LANE_ENABLED set" in proc.stdout
    assert "MOVED_ALREADY_POSTMORTEM_ENABLED unset" in proc.stdout


def test_secret_like_name_refused_even_if_allowlisted(tmp_path):
    """Broadening the allowlist with a secret-like name must not leak it:
    the denylist layer refuses names containing KEY/TOKEN/SECRET/..."""
    proc = _run(
        tmp_path,
        extra_env={"EXTRA_THRESHOLD_NAMES": "NARRATIVE_API_KEY QUANT_PROVIDER_SECRET"},
    )
    assert proc.returncode == 0
    assert "supersecret" not in proc.stdout
    assert "NARRATIVE_API_KEY: REFUSED" in proc.stdout
    assert "QUANT_PROVIDER_SECRET: REFUSED" in proc.stdout


def test_shell_syntax_is_valid():
    for script in ("vps_query_pack.sh", "revival_evidence_queries.sh"):
        proc = subprocess.run(
            ["bash", "-n", str(REPO_ROOT / "investigation" / script)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
