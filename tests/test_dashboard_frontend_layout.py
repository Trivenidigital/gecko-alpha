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
    assert "usage_started_at" in storage
    assert "buildUsageExport" in storage
    assert "localStorage" in storage
    assert "Usage evidence" in panel
    assert "JSON.stringify(usageExport, null, 2)" in panel
    assert "tokenId={row.token_id}" in panel
    assert "symbol={title.symbol}" in panel
    assert "import { researchLinks } from '../todayFocusLinks.js'" in panel
    assert "import { buildFocusDetailRows, primaryBlockFacts } from '../todayFocusFacts.js'" in panel
    assert "links.chartLabel" in panel
    assert "links.cgLabel" in panel
    assert "expandedRows" in panel
    assert "todays-focus-details-toggle" in panel
    assert "todays-focus-detail-grid" in panel
    assert "todays-focus-detail-label" in panel
    assert "aria-label={`Open ${title.symbol} ${links.chartLabel}`}" in panel
    assert "aria-label={`Open ${title.symbol} ${links.cgLabel}`}" in panel
    assert "block={row.block_cause}" in panel
    # PR #307 follow-up — a11y polish: Details button aria-controls the
    # expanded detail panel by id; panel carries role="region".
    assert "aria-controls={detailPanelId(row.row_key)}" in panel
    assert "id={detailPanelId(row.row_key)}" in panel
    assert "todays-focus-detail-panel-" in panel
    assert 'role="region"' in panel
    # PR #307 follow-up — dismiss clears stale expandedRows entry.
    assert "patch.dismissed" in panel
    assert "setExpandedRows" in panel
    # PR-A — detection age cell + new-since-last-view counter/marker.
    assert "import { formatDetectionAge } from '../todayFocusAge.js'" in panel
    assert "countNewRowKeys" in panel
    assert "isRowKeyNewSinceLastView" in panel
    assert "markRowsSeen" in panel
    assert "markCurrentRowsSeen" in panel
    assert "todays-focus-detected" in panel
    assert "todays-focus-new-marker" in panel
    assert "new since last view" in panel
    assert "formatDetectionAge(row.opened_age_hours)" in panel
    assert panel.index("todays-focus-list") < panel.index("todays-focus-usage")
    assert "save_for_review" in panel
    assert "dismiss" in panel
    assert "note" in panel
    assert "note:" not in storage
    assert "note," not in storage
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
    assert ".todays-focus-rank" in css
    assert ".todays-focus-links" in css
    assert ".todays-focus-block-cause" in css
    assert ".todays-focus-detail-grid" in css
    assert ".todays-focus-detail-label" in css
    assert ".todays-focus-detail-value" in css
    assert ".todays-focus-name" in css
    assert ".todays-focus-usage" in css
    # PR-A — detection age + new-since marker CSS classes present.
    assert ".todays-focus-detected" in css
    assert ".todays-focus-new-marker" in css
    assert "min-width: 0" in css
    assert "width: 100%" in css
    assert "min-height: calc(100vh - 170px)" in css
    assert "grid-template-columns: 28px minmax(0, 1fr) minmax(220px, 0.32fr)" in css
    assert re.search(r"\.todays-focus-row\s*\{[^}]*padding:\s*(?:8|10)px", css, re.S)
    mobile = re.search(r"@media \(max-width: 480px\).*", css, re.S)
    assert mobile
    assert ".todays-focus-detail-grid" in mobile.group(0)
    assert "grid-template-columns: 1fr" in mobile.group(0)


def test_todays_focus_frontend_copy_stays_factual():
    paths = [
        ROOT / "dashboard" / "frontend" / "components" / "TodayFocusPanel.jsx",
        ROOT / "dashboard" / "frontend" / "todayFocusStorage.js",
        ROOT / "dashboard" / "frontend" / "todayFocusFacts.js",
        # PR-A — extend factual-copy scan to cover the relative-age formatter.
        ROOT / "dashboard" / "frontend" / "todayFocusAge.js",
    ]
    text = "\n".join(p.read_text(encoding="utf-8") for p in paths if p.exists()).lower()
    text = re.sub(r'target="_blank"', "", text)
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
        r"\bact[_\s-]*now\b",
        r"\baction[_\s-]*required\b",
        r"\bacting\b",
        r"\bnow[_\s-]*tradeable\b",
        r"\btradeable[_\s-]*now\b",
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
    assert "todays-focus-detail-grid" in bundle_text
    assert "Price snapshot missing" in bundle_text
    # PR-A — relative-age formatter strings + new-since marker class shipped.
    assert "m ago" in bundle_text
    assert "h ago" in bundle_text
    assert "d ago" in bundle_text
    assert "todays-focus-new-marker" in bundle_text
    assert "todays-focus-detected" in bundle_text
    assert "new since last view" in bundle_text
