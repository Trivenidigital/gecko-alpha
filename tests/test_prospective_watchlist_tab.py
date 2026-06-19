"""Task 8 — Prospective Watchlist dashboard tab.

Source-level contract-firewall tests (read the JSX/App source; no runtime, no
OPENSSL). They guard: tab wiring, observe-only/UNVALIDATED framing, the
emerging-vs-high distinction, the mcap_unknown surface, and a copy firewall that
keeps the tab free of buy/sell/advice vocabulary. The committed-dist assertion
verifies the built bundle actually carries the new tab (Vite rewrites src →
dist, so a stale dist would silently ship the old UI).
"""

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "dashboard" / "frontend"


def test_prospective_watchlist_tab_is_wired_to_dashboard():
    app = (FRONTEND / "App.jsx").read_text(encoding="utf-8")
    tab = (FRONTEND / "components" / "ProspectiveWatchlistTab.jsx").read_text(
        encoding="utf-8"
    )

    # wired into App
    assert "import ProspectiveWatchlistTab" in app
    assert "activeTab === 'prospective'" in app
    assert "<ProspectiveWatchlistTab />" in app
    assert "Prospective Watchlist" in app  # tab button label

    # component content / contract
    assert "/api/conviction/prospective" in tab
    assert "Prospective Watchlist" in tab
    # honest forward framing surfaced IN the UI (sibling of RETROSPECTIVE conviction)
    assert "PROSPECTIVE" in tab
    assert "UNVALIDATED" in tab
    assert "not trade advice" in tab.lower()
    assert "observe-only" in tab.lower() or "observe only" in tab.lower()
    # N-gate: never a bare precision number — explicit INSUFFICIENT_DATA framing
    assert "INSUFFICIENT_DATA" in tab
    # emerging (fresh <24h) is surfaced but NOT counted toward high conviction
    assert "fresh_count" in tab
    assert "early_count" in tab  # sustained >=24h surfaces (the high-tier driver)
    assert "emerging" in tab.lower()
    # mcap rules: sub-$30M main table + a separate de-emphasized unknown/stale list
    assert "mcap_unknown" in tab
    assert "market_cap" in tab
    assert "contributing_surfaces" in tab
    # canonical CG-coin_id identity link (no symbol merge)
    assert 'chain="coingecko"' in tab
    assert "TokenLink" in tab
    # NEW-since-last-visit tracking, distinct localStorage key from the
    # retrospective Conviction tab (must NOT collide with convictionSeen)
    assert "prospectiveWatchlistSeen" in tab
    assert "convictionSeen" not in tab
    assert "NEW" in tab
    # min-tier toggle (High / Watch+) like the sibling tab
    assert "min_tier=" in tab


def test_prospective_watchlist_copy_stays_factual():
    """Observe-only copy firewall: the tab must not carry buy/sell/advice
    vocabulary. Strip TokenLink's target="_blank" before scanning (it's a DOM
    attribute, not advice copy)."""
    text = (FRONTEND / "components" / "ProspectiveWatchlistTab.jsx").read_text(
        encoding="utf-8"
    )
    # Strip DOM/React noise that would false-positive on the firewall:
    # TokenLink's target="_blank" and event-handler `.target` (e.target.value).
    # A genuine advice "target" (e.g. "price target") is not dot-prefixed, so
    # \btarget\b still catches it.
    text = re.sub(r'target="_blank"', "", text)
    text = re.sub(r"\.target\b", "", text)
    text = text.lower()
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
        r"\bnow[_\s-]*tradeable\b",
        r"\btradeable[_\s-]*now\b",
        r"\bnow[_\s-]*tradable\b",
    ):
        assert not re.search(forbidden, text), f"firewall: {forbidden!r} present"


def test_committed_dist_references_prospective_watchlist_bundle():
    """Vite rewrites src → dist; a stale dist silently ships the old UI. Assert
    the committed bundle referenced by index.html carries the new tab."""
    dist = FRONTEND / "dist"
    index_html = (dist / "index.html").read_text(encoding="utf-8")
    matches = re.findall(r'src="/assets/([^"]+\.js)"', index_html)
    assert matches, "dist/index.html must reference a hashed index-*.js bundle"
    bundle_text = ""
    for asset in matches:
        path = dist / "assets" / asset
        assert path.is_file(), f"referenced bundle missing on disk: {asset}"
        bundle_text += path.read_text(encoding="utf-8", errors="ignore")
    assert "Prospective Watchlist" in bundle_text
    assert "/api/conviction/prospective" in bundle_text
    assert "UNVALIDATED" in bundle_text
