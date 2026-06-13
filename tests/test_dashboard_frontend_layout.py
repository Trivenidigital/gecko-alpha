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


def test_trade_detail_drawer_surfaces_entry_snapshot_fields():
    jsx = (
        ROOT / "dashboard" / "frontend" / "components" / "TradeDetailDrawer.jsx"
    ).read_text(encoding="utf-8")

    assert 'title="Entry snapshot"' in jsx
    assert "entry_snapshot_version" in jsx
    assert "entry_snapshot_complete" in jsx
    assert "mcap_usd_at_entry" in jsx
    assert "liquidity_usd_at_entry" in jsx
    assert "first_seen_at_at_entry" in jsx
    assert "detected_by_combo_at_entry" in jsx
    assert "source_confluence_count_at_entry" in jsx
    assert "actionability_reason_at_entry" in jsx
    assert "tp_pct_at_entry" in jsx
    assert "pre-cutover (no snapshot)" in jsx


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


def test_trade_inbox_decision_board_is_primary_scan_surface():
    tab = (
        ROOT / "dashboard" / "frontend" / "components" / "TradeInboxTab.jsx"
    ).read_text(encoding="utf-8")
    css = (ROOT / "dashboard" / "frontend" / "style.css").read_text(encoding="utf-8")

    assert "import { buildTradeDecisionBoard } from './tradeDecisionBoard.js'" in tab
    assert "const decisionBoard = useMemo" in tab
    assert "buildTradeDecisionBoard(payload)" in tab
    assert "Trade Decision Board" in tab
    assert "No clean review-now rows" in tab
    assert "Review first" in tab
    assert "Best watch" in tab
    assert "Too late" in tab
    assert "Blocked diagnostics" in tab
    assert "renderDecisionRow" in tab
    assert "decisionBoard.primary" in tab
    assert "decisionBoard.watchlist" in tab
    assert "decisionBoard.late" in tab
    assert "decisionBoard.blocked_summary" in tab
    assert tab.index("Trade Decision Board") < tab.index("GROUPS.map")
    assert (
        "<table"
        not in tab[tab.index("Trade Decision Board") : tab.index("GROUPS.map")].lower()
    )

    for selector in (
        ".trade-decision-board",
        ".trade-decision-headline",
        ".trade-decision-grid",
        ".trade-decision-lane",
        ".trade-decision-row",
        ".trade-decision-row.primary",
        ".trade-decision-meta",
        ".trade-decision-risk",
        ".trade-decision-empty",
    ):
        assert selector in css

    mobile = re.search(r"@media \(max-width: 480px\).*", css, re.S)
    assert mobile
    assert ".trade-decision-grid" in mobile.group(0)
    assert "grid-template-columns: 1fr" in mobile.group(0)


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
    assert (
        "import { buildFocusDetailRows, primaryBlockFacts } from '../todayFocusFacts.js'"
        in panel
    )
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
    # PR-C — sparkline component import + per-row conditional render +
    # factual "Sparkline unavailable" fallback string.
    assert "import Sparkline from './Sparkline'" in panel
    assert "row.price_path_points" in panel
    assert "<Sparkline points={row.price_path_points} />" in panel
    assert "todays-focus-sparkline-unavailable" in panel
    assert "Sparkline unavailable" in panel
    assert 'aria-label="Sparkline unavailable"' in panel
    # PR-D — BTC + SOL benchmark strip imported and rendered inline
    # within todays-focus-heading meta chips. Aria-label strict-pinned.
    assert "import BtcSolBenchmarkStrip from './BtcSolBenchmarkStrip'" in panel
    assert "<BtcSolBenchmarkStrip benchmarks={meta.market_benchmarks} />" in panel
    # The strip lives inside .todays-focus-heading (siblings of
    # todays-focus-meta chips), NOT as a banner above/below the rows.
    heading_start = panel.find('<div className="todays-focus-heading">')
    heading_end = panel.find("</div>", heading_start)
    assert heading_start != -1 and heading_end != -1
    assert "BtcSolBenchmarkStrip" in panel[heading_start:heading_end]
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
    # PR-C — sparkline + fallback CSS classes present; mobile collapses them
    # alongside the existing meta-chip font-size rule (no horizontal overflow).
    assert ".todays-focus-sparkline" in css
    assert ".todays-focus-sparkline-unavailable" in css
    # PR-D — benchmark chips uniformly styled (no sign-based color branch).
    assert ".todays-focus-benchmark" in css
    assert ".todays-focus-benchmark-group" in css
    # Reviewer A B2 fold: ensure .todays-focus-benchmark rule body has
    # exactly one `color:` declaration AND no `[data-sign]`, `:has(`,
    # `:nth-child`, or `+ .` sibling selectors that could branch color.
    benchmark_rule_match = re.search(
        r"\.todays-focus-benchmark\s*\{([^}]*)\}", css, re.S
    )
    assert benchmark_rule_match
    benchmark_rule_body = benchmark_rule_match.group(1)
    color_decls = re.findall(r"\bcolor\s*:", benchmark_rule_body)
    assert len(color_decls) == 1, (
        f".todays-focus-benchmark must have exactly one color declaration; "
        f"got {len(color_decls)}"
    )
    # No conditional / sibling / has selector should branch the benchmark color.
    for forbidden in (
        ".todays-focus-benchmark[data-sign",
        ".todays-focus-benchmark:nth-child",
        ".todays-focus-benchmark:has(",
        ".todays-focus-benchmark + .",
    ):
        assert forbidden not in css, (
            f"Forbidden conditional selector {forbidden!r} would branch "
            "benchmark color by sign; ban via plan anti-scope §5."
        )
    assert "min-width: 0" in css
    assert "width: 100%" in css
    assert "min-height: calc(100vh - 170px)" in css
    assert "grid-template-columns: 28px minmax(0, 1fr) minmax(220px, 0.32fr)" in css
    assert re.search(r"\.todays-focus-row\s*\{[^}]*padding:\s*(?:8|10)px", css, re.S)
    mobile = re.search(r"@media \(max-width: 480px\).*", css, re.S)
    assert mobile
    assert ".todays-focus-detail-grid" in mobile.group(0)
    assert "grid-template-columns: 1fr" in mobile.group(0)


def test_todays_focus_benchmark_strip_has_no_regime_or_advice_vocabulary():
    """PR-D Reviewer A N8 fold: static-scan BtcSolBenchmarkStrip.jsx for
    regime/advice vocabulary. The strip emits only numeric deltas; any
    sentiment/regime word in the source file is a smuggle attempt."""
    strip = (
        ROOT / "dashboard" / "frontend" / "components" / "BtcSolBenchmarkStrip.jsx"
    ).read_text(encoding="utf-8")
    banned_vocab = (
        "risk-on",
        "risk-off",
        "range-bound",
        "choppy",
        "size up",
        "sit out",
        "take profit",
        "trending",
        "consolidating",
        "blow-off",
        "capitulation",
        "fading",
        "pumping",
    )
    lowered = strip.lower()
    for token in banned_vocab:
        assert (
            token.lower() not in lowered
        ), f"BtcSolBenchmarkStrip.jsx contains regime/advice token {token!r}"
    # SVG anti-scope inherited from Sparkline pattern (defense in depth
    # even though this component is text-only today).
    for tag in ("<text", "<tspan", "<title", "<desc", "<circle", "<rect"):
        assert (
            tag not in strip
        ), f"BtcSolBenchmarkStrip.jsx contains banned SVG tag {tag!r}"
    # aria-label strict-pinned.
    assert 'aria-label="BTC and SOL 4-hour deltas"' in strip


def test_todays_focus_sparkline_component_has_no_banned_svg_substrings():
    """PR-C strict structural guard: Sparkline.jsx source MUST contain only
    `<polyline>` geometry. Banned tags (text/title/circle/rect/etc.) would
    enable smuggling interpretation via SVG content; firewall is structural
    by source-substring exclusion."""
    sparkline = (
        ROOT / "dashboard" / "frontend" / "components" / "Sparkline.jsx"
    ).read_text(encoding="utf-8")
    banned_tags = (
        "<text",
        "<tspan",
        "<title",
        "<desc",
        "<foreignObject",
        "<circle",
        "<rect",
        "<ellipse",
        "<marker",
        "<path",
    )
    for tag in banned_tags:
        assert tag not in sparkline, (
            f"Sparkline.jsx contains banned SVG tag substring {tag!r} — "
            "anti-scope §4 (polyline-only)"
        )
    # aria-label is strict-pinned to the literal "Sparkline" — no other
    # extension permitted. Test asserts the exact attribute string.
    assert 'aria-label="Sparkline"' in sparkline
    # Polyline is the only geometry permitted.
    assert "<polyline" in sparkline


def test_todays_focus_frontend_copy_stays_factual():
    paths = [
        ROOT / "dashboard" / "frontend" / "components" / "TodayFocusPanel.jsx",
        ROOT / "dashboard" / "frontend" / "todayFocusStorage.js",
        ROOT / "dashboard" / "frontend" / "todayFocusFacts.js",
        # PR-A — extend factual-copy scan to cover the relative-age formatter.
        ROOT / "dashboard" / "frontend" / "todayFocusAge.js",
        # PR-C — extend factual-copy scan to cover the Sparkline component.
        ROOT / "dashboard" / "frontend" / "components" / "Sparkline.jsx",
        # PR-D — extend factual-copy scan to cover the BTC + SOL benchmark strip.
        ROOT / "dashboard" / "frontend" / "components" / "BtcSolBenchmarkStrip.jsx",
        # BL-NEW-DASHBOARD-WHAT-CHANGED — extend factual-copy scan to cover the
        # new What Changed panel + its storage + facts chokepoint.
        ROOT / "dashboard" / "frontend" / "whatChangedStorage.js",
        ROOT / "dashboard" / "frontend" / "whatChangedFacts.js",
        ROOT / "dashboard" / "frontend" / "components" / "WhatChangedPanel.jsx",
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

    assert "What Changed" in bundle_text
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
    # PR-C — sparkline className + fallback literal shipped to dist.
    assert "todays-focus-sparkline" in bundle_text
    assert "Sparkline unavailable" in bundle_text
    # PR-D — benchmark chip className + literal labels shipped to dist.
    assert "todays-focus-benchmark" in bundle_text
    assert "BTC 4h" in bundle_text
    assert "SOL 4h" in bundle_text


def test_what_changed_files_exist():
    assert (ROOT / "dashboard" / "frontend" / "whatChangedStorage.js").exists()
    assert (ROOT / "dashboard" / "frontend" / "whatChangedFacts.js").exists()
    assert (
        ROOT / "dashboard" / "frontend" / "components" / "WhatChangedPanel.jsx"
    ).exists()


def test_what_changed_storage_exports_required_pure_helpers():
    text = (ROOT / "dashboard" / "frontend" / "whatChangedStorage.js").read_text(
        encoding="utf-8"
    )
    assert "gecko.whatChanged.v0" in text
    # must NOT collide with the Today's-Focus key
    assert "gecko.todaysFocus.v0" not in text
    for symbol in (
        "export const STORAGE_KEY",
        "export function blankState",
        "export function loadState",
        "export function saveState",
        "export function markCurrentRowsSeen",
        "export function diffClosedTrades",
        "export function diffPnlSwings",
        "export function diffHealthStatusChanges",
    ):
        assert symbol in text, f"whatChangedStorage.js missing {symbol!r}"
    # closed-id set must be length-capped to avoid unbounded growth (Codex NIT)
    assert "MAX_CLOSED_IDS" in text
    # never-crash contract: tolerant getters return sentinels, loader try/catch
    assert "return null" in text
    assert "try {" in text


def test_what_changed_panel_first_visit_guard_present():
    text = (
        ROOT / "dashboard" / "frontend" / "components" / "WhatChangedPanel.jsx"
    ).read_text(encoding="utf-8")
    # baseline-write-before-render guard (no one-frame flash on first visit)
    assert "baselineReady" in text


def test_what_changed_tab_wired_in_app():
    text = (ROOT / "dashboard" / "frontend" / "App.jsx").read_text(encoding="utf-8")
    assert "import WhatChangedPanel" in text
    assert "What Changed" in text
    assert "activeTab === 'what_changed'" in text
    assert "<WhatChangedPanel" in text


def test_committed_dist_references_what_changed_bundle():
    dist = ROOT / "dashboard" / "frontend" / "dist"
    index_html = (dist / "index.html").read_text(encoding="utf-8")
    matches = re.findall(r'src="/assets/([^"]+\.js)"', index_html)
    assert matches, "dist/index.html must reference a hashed index-*.js bundle"
    bundle_text = ""
    for asset in matches:
        path = dist / "assets" / asset
        assert path.is_file(), f"referenced bundle missing on disk: {asset}"
        bundle_text += path.read_text(encoding="utf-8", errors="ignore")
    assert "What Changed" in bundle_text, "What Changed tab string missing from bundle"


def test_tg_alerts_tab_exposes_factual_operator_action_buttons():
    text = (
        ROOT / "dashboard" / "frontend" / "components" / "TGAlertsTab.jsx"
    ).read_text(encoding="utf-8")
    assert "/api/tg_alerts/recent?limit=80" in text
    assert "/operator-action" in text
    for label in ("Acted", "Useful", "Ignored", "Bad"):
        assert label in text
    banned = ("buy now", "trade now", "act now", "should buy", "should sell")
    lowered = text.lower()
    for phrase in banned:
        assert phrase not in lowered


def test_conviction_tab_is_wired_to_dashboard():
    app = (ROOT / "dashboard" / "frontend" / "App.jsx").read_text(encoding="utf-8")
    tab = (
        ROOT / "dashboard" / "frontend" / "components" / "ConvictionTab.jsx"
    ).read_text(encoding="utf-8")
    # wired into App
    assert "ConvictionTab" in app
    assert "activeTab === 'conviction'" in app
    assert "<ConvictionTab />" in app
    # component content / contract
    assert "/api/conviction/shortlist" in tab
    assert "Conviction Shortlist" in tab
    assert "RETROSPECTIVE" in tab  # honest framing surfaced in the UI
    assert "Top conviction" in tab and "Newest" in tab  # the two sort views
    assert (
        "sort=recency" in tab or "sort: 'recency'" in tab or "setSort('recency')" in tab
    )
    assert "contributing_surfaces" in tab
    assert "convictionSeen" in tab  # new-since-last-visit tracking
    assert "NEW" in tab
    # column filtering + sorting
    assert "import { useSort, SortHeader } from './useSort.jsx'" in tab
    assert "<SortHeader" in tab
    assert "Filter symbol" in tab  # symbol search box
    assert "min surfaces" in tab  # min early_count filter
    assert "min peak%" in tab  # min peak filter
    assert "any surface" in tab and "surfaceFilter" in tab  # surface dropdown
    assert "showing {sorted.length} of {rows.length}" in tab  # filtered count
    assert "No rows match the active filters" in tab  # filtered-empty state distinct
    assert "_tier_rank" in tab  # tier sorts by rank not text
