"""Static-grep guard for operator guardrail #3 + design's
"DexScreener `/dex/search?q=` is NEVER called in this design" claim.

Turns the prohibition into a runtime CI contract per
``feedback_anti_scope_as_runtime_contract.md``: when a plan says "no
fuzzy resolution", encode that boundary as a contract checker / lint /
CI gate where possible.

This test scans the liquidity-enrichment cron path for any reference
to DexScreener's search endpoint patterns:
  - ``/dex/search``     (the path-style call)
  - ``search?q=``       (the query-string-style call)
  - ``dex/search``      (defensive substring match)

Any match anywhere in the cron path FAILS the test. Add new banned
substrings here when the design forbids more fuzzy-resolution surfaces.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The cron writer + its imports — paths that MUST NEVER call DexScreener
# search. Extend this list if Phase 2 ships any code path that
# functionally resolves liquidity.
SCANNED_PATHS = [
    REPO_ROOT / "scripts" / "backfill_dexscreener_liquidity.py",
]

# Banned substrings — if any of these appear in the source, the
# resolution path is at risk of going symbol-fuzzy. Substring match is
# intentional: catches "/dex/search?q=" AND "dex/search" AND any
# rearrangement.
BANNED_SUBSTRINGS = [
    "/dex/search",
    "dex/search?q=",
    "dex/search",
    "dex_search",
    "/search?q=",
    "search/pairs",
]


def test_cron_path_does_not_reference_dexscreener_search():
    """Static scan: the cron writer must NOT contain any DexScreener
    search-endpoint substring. Symbol-fuzzy resolution is structurally
    banned per operator guardrail #3 + the design's anti-scope section.
    """
    violations: list[tuple[Path, str]] = []
    for path in SCANNED_PATHS:
        assert path.exists(), (
            f"Scanned path missing: {path}. Update SCANNED_PATHS when "
            "the cron file is moved/renamed."
        )
        source = path.read_text(encoding="utf-8")
        for banned in BANNED_SUBSTRINGS:
            if banned in source:
                violations.append((path, banned))
    assert not violations, (
        "DexScreener-search substring detected in cron path — operator "
        "guardrail #3 (no symbol-only resolution) violated.\n"
        + "\n".join(f"  {p}: {b!r}" for p, b in violations)
    )


def test_scanned_paths_all_exist():
    """If the cron file is renamed, the scan would silently pass with
    no violations. Verify the scan target still exists."""
    for path in SCANNED_PATHS:
        assert path.exists(), f"Scanned path missing: {path}"


def test_banned_substrings_actually_match_themselves():
    """Smoke test the substring match logic itself — if BANNED_SUBSTRINGS
    were empty or the match were broken, the suite would silently pass.
    Hard-code a tiny known-bad string and verify it would be caught."""
    bad_source = "url = 'https://api.dexscreener.com/dex/search?q=BILL'"
    matched = [b for b in BANNED_SUBSTRINGS if b in bad_source]
    assert matched, (
        "BANNED_SUBSTRINGS configuration is broken — none of the "
        "patterns match a known DexScreener search URL."
    )
