"""DASH-04 — two-level dashboard nav map guard.

Source-level contract-firewall tests (read App.jsx; no runtime, no OPENSSL).
They lock the invariant that the ``NAV_GROUPS`` map is the single source of
truth for tab grouping and that it covers *every* legacy ``activeTab`` render
branch exactly once — so no tab can be silently orphaned (unreachable from the
nav) or double-listed (in two groups) by a future edit.
"""

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "dashboard" / "frontend" / "App.jsx"

# The 14 legacy activeTab strings grouped by their DASH-04 lane. This is the
# human-readable expectation; the tests below also derive the mapping straight
# from the source so a drift in either direction fails.
EXPECTED_GROUPS = {
    "act": ["todays_focus", "trade_inbox", "now_tradable", "conviction"],
    "watch": ["prospective", "what_changed", "signals"],
    "performance": ["trading", "signal_trust", "briefing"],
    "system": ["pipeline", "health", "tg", "x"],
}
EXPECTED_TABS = {tab for tabs in EXPECTED_GROUPS.values() for tab in tabs}


def _app_source():
    return APP.read_text(encoding="utf-8")


def _nav_block(app):
    """Slice just the NAV_GROUPS literal so group-level `id:` keys (act/watch/
    …) are separated from the per-tab `id:` keys inside each `tabs: [ … ]`."""
    start = app.index("const NAV_GROUPS")
    end = app.index("const DEFAULT_TAB")
    assert start < end, "NAV_GROUPS must be declared before DEFAULT_TAB"
    return app[start:end]


def _nav_tab_ids(app):
    """Ordered list of every tab id declared inside a group's `tabs: [ … ]`."""
    nav_block = _nav_block(app)
    ids = []
    for tabs_body in re.findall(r"tabs:\s*\[(.*?)\]", nav_block, re.S):
        ids.extend(re.findall(r"id:\s*'([^']+)'", tabs_body))
    return ids


def _render_tab_ids(app):
    """Every tab id that has an `activeTab === '<id>'` render branch."""
    return set(re.findall(r"activeTab === '([^']+)'", app))


def test_nav_map_covers_every_render_branch_exactly_once():
    app = _app_source()
    nav_ids = _nav_tab_ids(app)
    render_ids = _render_tab_ids(app)

    # No tab appears in two groups (each legacy tab id → exactly one group).
    assert len(nav_ids) == len(
        set(nav_ids)
    ), f"duplicate tab in NAV_GROUPS: {sorted(nav_ids)}"
    # Nav covers every rendered tab and introduces no phantom tabs.
    assert set(nav_ids) == render_ids, (
        f"nav/render mismatch: only-in-nav={set(nav_ids) - render_ids}, "
        f"only-in-render={render_ids - set(nav_ids)}"
    )
    # And it matches the intended 14-tab expectation.
    assert set(nav_ids) == EXPECTED_TABS


def test_nav_groups_match_expected_lane_membership():
    app = _app_source()
    nav_block = _nav_block(app)

    # Group order + labels present (top-level lane row).
    for group_label in ("Act", "Watch", "Performance", "System"):
        assert (
            f"label: '{group_label}'" in nav_block
        ), f"missing group label {group_label!r}"

    # Per-group membership: each group's tabs block holds exactly its lane's ids.
    group_blocks = re.findall(
        r"id:\s*'(act|watch|performance|system)',\s*"
        r"label:\s*'[^']+',\s*"
        r"tabs:\s*\[(.*?)\]",
        nav_block,
        re.S,
    )
    found = {gid: re.findall(r"id:\s*'([^']+)'", body) for gid, body in group_blocks}
    assert found == EXPECTED_GROUPS, f"lane membership drift: {found}"


def test_default_tab_is_todays_focus_and_map_is_rendered():
    app = _app_source()
    # Default landing = Act / Today's Focus.
    assert "const DEFAULT_TAB = 'todays_focus'" in app
    # The map is actually consumed to render the nav (not a dead const).
    assert "NAV_GROUPS.map(" in app
    # Deep-link target resolves into a real group (Performance / Trading).
    assert "trading" in _nav_tab_ids(app)
