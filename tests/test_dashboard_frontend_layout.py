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
