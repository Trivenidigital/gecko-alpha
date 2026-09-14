"""Run production controller and actual React presentation fixtures."""

import subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_owned_controller():
    result = subprocess.run(
        ["node", "--test", "dashboard/frontend/suppressionHealth.test.mjs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_actual_rendered_states(tmp_path):
    if not (ROOT / "dashboard/frontend/node_modules/esbuild").exists():
        pytest.skip(
            "Actual React render needs npm ci; controller test remains Node-only"
        )
    result = subprocess.run(
        [
            "node",
            "tests/render_suppression_health.mjs",
            str(tmp_path / "render.cjs"),
            str(tmp_path / "fixture.html"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
