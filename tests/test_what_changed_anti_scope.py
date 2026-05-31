"""Anti-scope contract for the What Changed dashboard panel.

Read-only, frontend-only. Asserts the panel + storage consume ONLY the
allowed read-only GET endpoints, introduce no backend route / response_model /
DB write, and are wired into App.jsx + the layout copy-scan paths list.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "dashboard" / "frontend"

PANEL = FRONTEND / "components" / "WhatChangedPanel.jsx"
STORAGE = FRONTEND / "whatChangedStorage.js"
FACTS = FRONTEND / "whatChangedFacts.js"
APP = FRONTEND / "App.jsx"
LAYOUT_TEST = REPO_ROOT / "tests" / "test_dashboard_frontend_layout.py"

ALLOWED_PATHS = (
    "/api/trading/history",
    "/api/trading/positions",
    "/api/system/health",
)
# /api/trading/history/count is a sub-path of /api/trading/history and is allowed.


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_new_files_exist():
    for path in (PANEL, STORAGE, FACTS):
        assert path.exists(), f"missing required file {path}"


def test_panel_fetches_only_allowed_trading_endpoints():
    text = _read(PANEL)
    urls = re.findall(r"""fetch\(\s*[`'"]([^`'"]+)[`'"]""", text)
    assert urls, "WhatChangedPanel.jsx must issue fetch() calls"
    for url in urls:
        # strip any query string for the prefix check
        base = url.split("?", 1)[0]
        assert base.startswith(
            ALLOWED_PATHS
        ), f"fetch URL {url!r} is outside the allowed trading endpoints"


def test_storage_issues_no_fetch():
    # Storage helper must be pure: no network calls at all.
    text = _read(STORAGE)
    assert "fetch(" not in text, "whatChangedStorage.js must not issue network calls"


def test_no_mutating_http_verbs():
    for path in (PANEL, STORAGE, FACTS):
        text = _read(path)
        assert (
            "method:" not in text
        ), f"{path.name} must not set an HTTP method (GET only)"
        for verb in ("POST", "PUT", "PATCH", "DELETE"):
            assert verb not in text, f"mutating verb {verb} found in {path.name}"


def test_no_backend_route_or_response_model_in_new_js():
    for path in (PANEL, STORAGE, FACTS):
        text = _read(path)
        assert "@app.get" not in text
        assert "@app.post" not in text
        assert "response_model" not in text


def test_no_db_write_in_new_js():
    for path in (PANEL, STORAGE, FACTS):
        text = _read(path).lower()
        for token in ("insert", "update ", "execute(", "commit("):
            assert token not in text, f"db-write token {token!r} found in {path.name}"


def test_panel_wired_in_app():
    text = _read(APP)
    assert "import WhatChangedPanel" in text, "WhatChangedPanel not imported in App.jsx"
    assert (
        "activeTab === 'what_changed'" in text
    ), "what_changed tab not wired in App.jsx"
    assert "<WhatChangedPanel" in text, "WhatChangedPanel not rendered in App.jsx"


def test_new_files_in_layout_copy_scan_paths():
    text = _read(LAYOUT_TEST)
    assert "whatChangedStorage.js" in text
    assert "whatChangedFacts.js" in text
    assert "WhatChangedPanel.jsx" in text


def test_diff_closed_trades_net_realized_excludes_null_pnl():
    """Structural I1: a closed trade with null pnl_usd must be EXCLUDED from the
    net-realized sum AND counted toward the '(excludes K unavailable)'
    disclosure -- never silently summed as 0.

    Static source-assert (no JS runner in repo), consistent with the other
    contract asserts in this file:
      - storage's diffClosedTrades sums only finite pnl (Number.isFinite guard)
      - it derives a net-unavailable count for the excluded null-pnl rows
      - facts' closedHeadline wires the '(excludes ... unavailable)' disclosure
    """
    storage = _read(STORAGE)
    assert "diffClosedTrades" in storage
    # Only finite realized pnl is summed -- null-pnl rows are not added as 0.
    assert (
        "Number.isFinite" in storage
    ), "diffClosedTrades must guard the net-realized sum with Number.isFinite"
    assert (
        "netRealizedSince" in storage
    ), "diffClosedTrades must expose a net-realized sum"
    assert (
        "netUnavailableCount" in storage
    ), "diffClosedTrades must count null-pnl rows as net-unavailable (not summed as 0)"

    facts = _read(FACTS)
    assert (
        "netUnavailableCount" in facts
    ), "closedHeadline must accept the net-unavailable count"
    assert (
        "excludes" in facts
    ), "closedHeadline must wire the '(excludes K unavailable)' disclosure"


def test_count_fetch_failure_footnote_degrades_gracefully():
    """Structural I3: if /api/trading/history/count fails or yields no usable
    total, the 'Showing N of M' footnote must DEGRADE gracefully -- it must not
    render a 'of undefined' / 'of null' string.

    Static source-assert: the count fetch resets historyTotal to null on
    failure, and the footnote is gated on historyTotal != null so it is simply
    omitted when the total is unavailable.
    """
    text = _read(PANEL)
    assert "fetchHistoryCount" in text
    # Failure path sets the total to null (no bogus value leaks to the footnote).
    assert (
        "setHistoryTotal(null)" in text
    ), "count-fetch failure must reset historyTotal to null"
    # Footnote is gated on a usable total, so it is omitted (not rendered broken).
    assert (
        "historyTotal != null" in text
    ), "history-truncation footnote must be gated on a usable (non-null) total"


def test_count_fetch_reads_total_field_from_endpoint_contract():
    """The /api/trading/history/count endpoint returns {"total": N}.

    Reading data.count silently disables the truncation footnote forever.
    """
    text = _read(PANEL)
    assert "Number(data.total)" in text
    assert "Number(data.count)" not in text
