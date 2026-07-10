"""Import-purity guards for cron-script entrypoints (INF-03).

Three scripts configured structlog at MODULE scope
(``structlog.configure(...)`` at import time). That is a global, process-wide
mutation: importing the module in-process — as any unit test does — swaps
structlog's ``logger_factory`` for every other test in the session, silently
emptying their captured log output (the CI log-leak class fixed for
``scripts/alert_channel_watchdog.py``). The fix moves the configure into a
``_configure_logging()`` helper called only from the ``__main__`` / cron path,
so a plain import is side-effect-free.

This mirrors ``test_importing_module_does_not_reconfigure_structlog`` in
tests/test_alert_channel_watchdog_script.py — one parametrization per script.
All three scripts import only aiohttp-free modules at top level
(``scout.source_quality.*`` pulls aiosqlite/structlog only), so these tests
collect and run on Windows.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import structlog

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

_TARGET_SCRIPTS = [
    "source_call_price_snapshots_writer.py",
    "source_call_coverage_watchdog.py",
    "source_calls_live_writer.py",
]


@pytest.fixture(autouse=True)
def _preserve_structlog_config():
    """Snapshot structlog's global config before each test and restore it after,
    so a regression here can't leak into the rest of the pytest session."""
    saved = structlog.get_config()
    try:
        yield
    finally:
        structlog.configure(**saved)


@pytest.mark.parametrize("script_name", _TARGET_SCRIPTS)
def test_importing_script_does_not_reconfigure_structlog(script_name):
    """Importing the script must NOT mutate structlog's global config at import
    time. Compares the configured ``logger_factory`` identity across a fresh
    import — ``structlog.configure()`` would swap it."""
    script = SCRIPTS_DIR / script_name
    mod_name = f"_import_purity_{script_name[:-3]}"

    before = structlog.get_config()["logger_factory"]
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, script)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # re-runs module top-level (import side effects)
    after = structlog.get_config()["logger_factory"]

    assert after is before, (
        f"importing scripts/{script_name} reconfigured structlog's "
        "logger_factory at import time — a global side effect that empties "
        "other tests' captured logs (move the configure into __main__)"
    )
