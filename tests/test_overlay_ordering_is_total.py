"""Every overlay subquery must select its row under a TOTAL order.

Two independent scalar subqueries feed `chains_recompute_status` and
`chains_canonical_lead`. If rows sharing all ordering terms remain unordered,
`LIMIT 1` may take the status from one overlay row and the lead from another —
the claim/value decoupling the pair exists to prevent, reintroduced in SQL.

This pins STRUCTURE, deliberately. The property is determinism, and a
behavioural test cannot reliably force SQLite to expose a free choice: a mutant
removing the total-order term passed every behavioural test in the tie-break
suite. The same reasoning as `test_unconditional_commits.py`.

The three ordering terms, in order, and why each is where it is:
  1. `CASE evidence_status WHEN 'verified_canonical' THEN 0 ELSE 1` — ASC, so
     the credit-bearing status must sort LOWEST. Written the other way it
     preferred the non-credit-bearing sibling.
  2. `canonical_lead DESC` — the probe asks "does ANY verified row clear the
     gate"; the reader tests the lead of the ONE row it picked. Without this
     the reader could take a sub-gate row the probe had counted as recovered.
  3. `source_row_id` — breaks any remaining tie, making the order total.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TRACKERS = [
    ROOT / "scout" / "gainers" / "tracker.py",
    ROOT / "scout" / "losers" / "tracker.py",
    ROOT / "scout" / "trending" / "tracker.py",
]

#: An ORDER BY belonging to an overlay subquery, up to its LIMIT.
_ORDER_BY = re.compile(r"ORDER BY CASE cir\.evidence_status(?P<body>.*?)LIMIT 1", re.S)


def _order_bys(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    # Strip comment lines so a term named only in prose cannot satisfy the
    # assertions below -- the failure mode where a guard matches the text that
    # explains it rather than the code it governs.
    stripped = "\n".join(l for l in text.splitlines() if not l.strip().startswith("--"))
    return [m.group("body") for m in _ORDER_BY.finditer(stripped)]


@pytest.mark.parametrize("path", TRACKERS, ids=lambda p: p.parent.name)
def test_both_subqueries_order_totally(path):
    bodies = _order_bys(path)
    assert (
        len(bodies) == 2
    ), f"{path.name}: expected 2 overlay subqueries, found {len(bodies)}"
    for i, body in enumerate(bodies):
        assert "cir.source_row_id" in body, (
            f"{path.name} subquery {i}: no total-order term, so two rows "
            "sharing status and lead are free to be picked differently by the "
            "two subqueries"
        )
        assert "cir.canonical_lead DESC" in body, (
            f"{path.name} subquery {i}: canonical_lead is not an ordering "
            "term, so the reader can pick a sub-gate row the probe counted"
        )


@pytest.mark.parametrize("path", TRACKERS, ids=lambda p: p.parent.name)
def test_the_ordering_terms_are_in_the_right_sequence(path):
    """Order matters: lead before id, and the CASE first.

    `source_row_id` ahead of `canonical_lead` would make the id decide which
    verified row wins, which is the B2 defect wearing a different arrangement.
    """
    for i, body in enumerate(_order_bys(path)):
        lead = body.index("cir.canonical_lead DESC")
        rid = body.index("cir.source_row_id")
        assert lead < rid, (
            f"{path.name} subquery {i}: source_row_id is ordered before "
            "canonical_lead, so the row id decides which verified row wins"
        )


def test_the_scanner_is_not_vacuous():
    """A regex that matches nothing passes every assertion above it."""
    total = sum(len(_order_bys(p)) for p in TRACKERS)
    assert total == 6, f"scanner found {total} overlay subqueries, expected 6"
