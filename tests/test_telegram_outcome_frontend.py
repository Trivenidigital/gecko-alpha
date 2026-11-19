"""Execute the panel's real request lifecycle without a browser dependency."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_request_lifecycle():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    script = Path(__file__).with_name("telegram_outcome_request_checks.mjs")
    result = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_panel_is_in_dispatch_section_without_changing_parent_fetch():
    frontend = Path(__file__).resolve().parents[1] / "dashboard" / "frontend"
    source = (frontend / "components" / "TGAlertsTab.jsx").read_text()
    assert "<TelegramOutcomePanel />" in source
    assert "fetch('/api/tg_alerts/recent?limit=80')" in source
    assert source.index("<TelegramOutcomePanel />") > source.index(
        "{subTab === 'dispatch'"
    )
