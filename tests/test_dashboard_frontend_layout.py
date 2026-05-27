from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_uses_wide_viewport_for_operator_tables():
    css = (ROOT / "dashboard" / "frontend" / "style.css").read_text(encoding="utf-8")

    assert "--dashboard-max-width: 1880px" in css
    assert re.search(
        r"\.dashboard\s*\{[^}]*max-width:\s*min\(var\(--dashboard-max-width\),\s*calc\(100vw - 32px\)\)",
        css,
        re.S,
    )


def test_open_positions_table_gets_compact_layout_class():
    jsx = (ROOT / "dashboard" / "frontend" / "components" / "TradingTab.jsx").read_text(
        encoding="utf-8"
    )

    assert 'className="open-positions-scroll"' in jsx
    assert 'className="candidates-table open-positions-table"' in jsx


def test_trade_inbox_tab_is_wired_to_dashboard():
    app = (ROOT / "dashboard" / "frontend" / "App.jsx").read_text(encoding="utf-8")
    tab = (
        ROOT / "dashboard" / "frontend" / "components" / "TradeInboxTab.jsx"
    ).read_text(encoding="utf-8")

    assert "TradeInboxTab" in app
    assert "trade_inbox" in app
    assert "<TradeInboxTab />" in app
    assert "/api/trade_inbox" in tab
    assert "Review Now" in tab
    assert "Show more" in tab
    assert "Max scan" in tab
    assert "row.block_reason_primary" in tab
    assert "return `${row.source_corpus || 'paper'}:${row.token_id}`" in tab
    assert "`${row.group}:${row.source_corpus || 'paper'}:${row.token_id}`" in tab
    assert "Source: {row.source_corpus || 'paper'}" in tab
    assert "previous_group" in tab
    assert "function rowStatus" in tab
    assert "10 * 60 * 1000" in tab
    assert "function counterRiskText" in tab
    assert "function renderCounterRisk" in tab
    assert "counter_risk_score" in tab
    assert "counter_flags" in tab
    assert "counter_risk_predicted_at" in tab
    assert "Counter-risk context" in tab
    assert "Counter-risk unavailable" in tab
    assert "counterRiskText(row)" in tab
    assert "trade[\\s_-]*now" in tab
    assert "watch[\\s_-]*breakout" in tab
    assert "return ''" in tab
    without_counter_block = re.sub(
        r"function counterRiskText\(row\).*?^}",
        "",
        tab,
        flags=re.S | re.M,
    )
    without_counter_block = re.sub(
        r"function renderCounterRisk\(row\).*?^}",
        "",
        without_counter_block,
        flags=re.S | re.M,
    )
    assert "counter_risk_score" not in without_counter_block


def test_trade_inbox_counter_risk_block_stays_neutral():
    tab = (
        ROOT / "dashboard" / "frontend" / "components" / "TradeInboxTab.jsx"
    ).read_text(encoding="utf-8")
    block = re.search(
        r"function renderCounterRisk\(row\).*?^}",
        tab,
        flags=re.S | re.M,
    )
    assert block, "renderCounterRisk block missing"
    text = block.group(0).lower()
    for forbidden in (
        "high",
        "low",
        "urgent",
        "alert",
        "trade now",
        "trade_now",
        "watch_breakout",
        "research_only",
    ):
        assert forbidden not in text


def test_todays_focus_tab_is_wired_with_local_storage_only_state():
    app = (ROOT / "dashboard" / "frontend" / "App.jsx").read_text(encoding="utf-8")
    panel_path = ROOT / "dashboard" / "frontend" / "components" / "TodayFocusPanel.jsx"
    storage_path = ROOT / "dashboard" / "frontend" / "todayFocusStorage.js"

    assert panel_path.exists()
    assert storage_path.exists()
    panel = panel_path.read_text(encoding="utf-8")
    storage = storage_path.read_text(encoding="utf-8")

    assert "TodayFocusPanel" in app
    assert "todays_focus" in app
    assert "<TodayFocusPanel />" in app
    assert "/api/todays_focus?window_hours=36" in panel
    assert "gecko.todaysFocus.v0" in storage
    assert "schema_version" in storage
    assert "cached_payload" in storage
    assert "actions_by_row_key" in storage
    assert "usage_counters" in storage
    assert "localStorage" in storage
    assert "save_for_review" in panel
    assert "dismiss" in panel
    assert "note" in panel
    assert "I'm in" not in panel
    assert "I’m in" not in panel
    assert not re.search(
        r"fetch\([^)]*,\s*\{[^}]*(?:POST|PUT|PATCH|DELETE)", panel, re.S
    )


def test_todays_focus_mobile_constraints_and_no_table_layout():
    css = (ROOT / "dashboard" / "frontend" / "style.css").read_text(encoding="utf-8")
    panel = (
        ROOT / "dashboard" / "frontend" / "components" / "TodayFocusPanel.jsx"
    ).read_text(encoding="utf-8")

    assert "todays-focus-panel" in panel
    assert "<table" not in panel.lower()
    assert "@media (max-width: 480px)" in css
    assert ".todays-focus-row" in css
    assert "min-width: 375px" in css
    assert re.search(r"\.todays-focus-row\s*\{[^}]*padding:\s*(?:8|10)px", css, re.S)


def test_todays_focus_frontend_copy_stays_factual():
    paths = [
        ROOT / "dashboard" / "frontend" / "components" / "TodayFocusPanel.jsx",
        ROOT / "dashboard" / "frontend" / "todayFocusStorage.js",
    ]
    text = "\n".join(p.read_text(encoding="utf-8") for p in paths if p.exists()).lower()
    for forbidden in (
        r"\btrade\s+now\b",
        r"\bwatch\s+breakout\b",
        r"\bentry\s+is\s+late\b",
        r"\bconsider\b",
        r"\bbuy\b",
        r"\bsell\b",
        r"\bshould\b",
        r"\btarget\b",
        r"\btake\s+profit\b",
        r"\bstrong\s+buy\b",
    ):
        assert not re.search(forbidden, text)


def test_committed_dashboard_dist_references_existing_signal_trust_bundle():
    index_html = (ROOT / "dashboard" / "frontend" / "dist" / "index.html").read_text(
        encoding="utf-8"
    )
    matches = re.findall(r'src="/assets/([^"]+\.js)"', index_html)
    assert matches

    bundle_text = ""
    for asset in matches:
        path = ROOT / "dashboard" / "frontend" / "dist" / "assets" / asset
        assert (
            path.is_file()
        ), f"dist bundle referenced by index.html is missing: {asset}"
        bundle_text += path.read_text(encoding="utf-8", errors="ignore")

    assert "/api/signal_trust/scorecards" in bundle_text
    assert "Closed paper-trade evidence" in bundle_text
