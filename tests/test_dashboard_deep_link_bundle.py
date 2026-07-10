"""ALR-09 hash-route smoke: the committed dashboard bundle must ship the
deep-link route handler.

The frontend has no router — App.jsx parses ``window.location.hash`` for the
``#/trade/{paper_trade_id}`` prefix and selects/scrolls the matching row. This
guard asserts the built (minified) bundle actually contains that route literal,
so a stale dist that predates the ALR-09 wiring is caught in CI (pairs with the
commit-dist convention).
"""

from __future__ import annotations

from pathlib import Path

_DIST_ASSETS = (
    Path(__file__).resolve().parent.parent
    / "dashboard"
    / "frontend"
    / "dist"
    / "assets"
)


def _bundle_text() -> str:
    js_files = sorted(_DIST_ASSETS.glob("index-*.js"))
    assert js_files, f"no built bundle under {_DIST_ASSETS}"
    return js_files[-1].read_text(encoding="utf-8")


def test_bundle_ships_trade_deep_link_route():
    """The `#/trade/` prefix is a string literal in App.jsx; minification
    preserves string literals, so it must appear verbatim in the bundle."""
    assert "#/trade/" in _bundle_text()


def test_bundle_ships_row_anchor_ids():
    """Rows are addressable via id=`trade-${id}` so the deep-link can scroll
    to the exact row; the template prefix survives minification."""
    assert "trade-" in _bundle_text()
