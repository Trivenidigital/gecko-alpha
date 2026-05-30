"""Anti-scope contract for the What Changed dashboard panel.

Read-only, frontend-only. Asserts the panel + storage consume ONLY the two
allowed trading GET endpoints, introduce no backend route / response_model /
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

ALLOWED_PATHS = ("/api/trading/history", "/api/trading/positions")
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
