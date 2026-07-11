"""DASH-12 — dist build guardrail.

The committed SPA entrypoint ``dashboard/frontend/dist/index.html`` references
content-hashed asset bundles (``/assets/index-<hash>.js`` / ``.css``). When a
rebuild changes a bundle's hash but index.html is not re-committed alongside (or
an asset is not staged), the deployed page points at a dead hash and 404s on
first load — the 2026-05-12 silent-failure shape.
``scripts/pre-commit-dist-consistency.sh`` guards this at commit time; this test
guards it in CI.

Cheap + Windows-runnable: parse the asset refs out of index.html and assert each
referenced file exists on disk. A full hash-match (rebuild via vite and compare)
is a CI-only, node-gated concern — see ``docs/dist_build_guardrail.md``; this
test deliberately does NOT invoke node, so it runs everywhere the Python suite
does.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "dashboard" / "frontend" / "dist"
INDEX_HTML = DIST_DIR / "index.html"

# src="/assets/…"  or  href="/assets/…"  (strip any ?query / #fragment)
_ASSET_REF_RE = re.compile(r'(?:src|href)\s*=\s*"(/assets/[^"?#]+)"')


def _referenced_assets():
    return _ASSET_REF_RE.findall(INDEX_HTML.read_text(encoding="utf-8"))


def test_dist_index_html_exists():
    assert INDEX_HTML.exists(), f"missing committed dist entrypoint: {INDEX_HTML}"


def test_dist_index_references_at_least_one_asset():
    assert _referenced_assets(), (
        "no /assets/* refs found in dist/index.html — the vite build output "
        "shape changed; update the guardrail regex"
    )


def test_all_referenced_dist_assets_exist_on_disk():
    missing = []
    for ref in _referenced_assets():
        # "/assets/index-6vj13d1h.js" -> dist/assets/index-6vj13d1h.js
        if not (DIST_DIR / ref.lstrip("/")).exists():
            missing.append(ref)
    assert not missing, (
        "dist/index.html references asset hash(es) with no file on disk — a "
        "stale bundle was committed (rebuild + re-commit dist/, or the new "
        "assets were not staged alongside index.html):\n  " + "\n  ".join(missing)
    )
