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
    assert "return row.token_id" in tab
    assert "previous_group" in tab
    assert "function rowStatus" in tab
    assert "10 * 60 * 1000" in tab
