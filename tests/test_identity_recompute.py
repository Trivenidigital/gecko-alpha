"""Versioned recomputation of legacy chain provenance (ruling C).

`legacy_prefix` means "provenance needs resolution", NOT "evidence is bad".
Treating the two as the same drops 826 of 1188 production rows and takes
tier_high from 341 to 187 — mostly a metadata artefact, since the impact study
found 95.8% of resolved cases were already decided by canonical identity.

The nine falsifiers the ruling names each have a test below, and the hardest
one is the coverage asymmetry: a missing canonical match is only evidence of
absence where history actually reaches back far enough to have found one.
"""

import pytest

from scout.conviction import cross_surface_conviction
from scout.db import Database
from scout.identity import CANONICAL_SEMANTICS, LEGACY_SEMANTICS
from scout.identity_recompute import (
    STATUS_AMBIGUOUS,
    STATUS_CANONICAL_SUB_GATE,
    STATUS_INDETERMINATE,
    STATUS_NO_LEGACY_CREDIT,
    STATUS_PREFIX_ONLY,
    STATUS_VERIFIED_CANONICAL,
    recompute_legacy_provenance,
)

GATE = 1440.0
ANCHOR = "2026-08-20T00:00:00+00:00"
EARLY = "2026-08-15T00:00:00+00:00"  # 7200 min before ANCHOR — clears the gate
LATE = "2026-08-19T23:00:00+00:00"  # 60 min before ANCHOR — below the gate
FLOOR = "2026-08-09T00:40:48+00:00"


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


async def _substrate(db, rows):
    await db._conn.executemany(
        "INSERT OR REPLACE INTO signal_first_seen (token_id, first_seen_at, updated_at) "
        "VALUES (?, ?, ?)",
        [(t, f, f) for t, f in rows],
    )
    await db._conn.commit()


async def _legacy(db, coin_id, symbol, *, anchor=ANCHOR, detected=1, lead=5000.0):
    """Write an archived legacy row. The archive is what the recompute reads."""
    await db._conn.execute(
        """INSERT INTO gainers_comparisons_legacy_prefix_v1
           (id, coin_id, symbol, name, appeared_on_gainers_at,
            detected_by_chains, chains_lead_minutes, is_gap,
            chains_identity_semantics, created_at)
           VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM gainers_comparisons_legacy_prefix_v1),
                   ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
        (coin_id, symbol, coin_id, anchor, detected, lead, LEGACY_SEMANTICS, anchor),
    )
    await db._conn.commit()


async def _status(db, coin_id):
    cur = await db._conn.execute(
        "SELECT evidence_status FROM chain_identity_recompute_v1 WHERE coin_id=?",
        (coin_id,),
    )
    row = await cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# The nine falsifiers
# ---------------------------------------------------------------------------


async def test_a_legacy_exact_canonical_row_RECOVERS_credit(db):
    """The whole point: ~96% of legacy rows were canonical and must not be lost."""
    await _substrate(db, [("pepe", EARLY)])
    await _legacy(db, "pepe", "PEPE")
    await recompute_legacy_provenance(db._conn, gate_minutes=GATE)

    assert await _status(db, "pepe") == STATUS_VERIFIED_CANONICAL

    row = {
        "detected_by_chains": 1,
        "chains_lead_minutes": 5000.0,
        "chains_identity_semantics": LEGACY_SEMANTICS,
        "chains_recompute_status": STATUS_VERIFIED_CANONICAL,
    }
    assert "chains" in cross_surface_conviction(row, _settings()).contributing


async def test_recovery_does_NOT_rewrite_the_archived_evidence(db):
    """The archive is the evidence. Recomputing must sit beside it, not on it."""
    await _substrate(db, [("pepe", EARLY)])
    await _legacy(db, "pepe", "PEPE", lead=9999.0)
    await recompute_legacy_provenance(db._conn, gate_minutes=GATE)

    cur = await db._conn.execute(
        "SELECT chains_lead_minutes, chains_identity_semantics "
        "FROM gainers_comparisons_legacy_prefix_v1 WHERE coin_id='pepe'"
    )
    lead, semantics = await cur.fetchone()
    assert lead == 9999.0, "the archived lead was rewritten"
    assert semantics == LEGACY_SEMANTICS, "the archive was restamped canonical"


async def test_a_known_prefix_only_case_does_NOT_recover(db):
    """`real-world-apparel <- re`, straight from the production impact study."""
    await _substrate(db, [("re", EARLY)])
    await _legacy(db, "real-world-apparel", "JACKET")
    await recompute_legacy_provenance(db._conn, gate_minutes=GATE)

    assert await _status(db, "real-world-apparel") == STATUS_PREFIX_ONLY

    row = {
        "detected_by_chains": 1,
        "chains_lead_minutes": 5000.0,
        "chains_identity_semantics": LEGACY_SEMANTICS,
        "chains_recompute_status": STATUS_PREFIX_ONLY,
    }
    assert "chains" not in cross_surface_conviction(row, _settings()).contributing


async def test_identity_tier_beats_age_in_the_recompute_too(db):
    """An exact match must win over an OLDER prefix candidate."""
    await _substrate(
        db, [("terra-luna", "2020-01-01T00:00:00+00:00"), ("terra-luna-2", EARLY)]
    )
    await _legacy(db, "terra-luna-2", "LUNA")
    await recompute_legacy_provenance(db._conn, gate_minutes=GATE)

    cur = await db._conn.execute(
        "SELECT identity_tier, canonical_lead FROM chain_identity_recompute_v1 "
        "WHERE coin_id='terra-luna-2'"
    )
    tier, lead = await cur.fetchone()
    assert tier == "canonical_id"
    # 7200 min from EARLY, not the ~2.4M minutes the 2020 prefix row would give.
    assert lead == pytest.approx(7200.0, abs=1.0)


async def test_insufficient_coverage_is_INDETERMINATE_not_a_false_negative(db):
    """THE asymmetry that makes this safe.

    An anchor older than the substrate floor cannot be resolved either way:
    the whole pre-anchor window is invisible. Calling that "prefix-only" would
    manufacture false negatives — the same error as the false positives this
    workstream exists to remove, in the other direction.
    """
    # The fixture must make the two outcomes DISTINGUISHABLE. A prefix candidate
    # has to exist, otherwise the classifier reaches "indeterminate" by a second
    # route and removing the coverage guard changes nothing -- which is exactly
    # what my first version of this test did: the mutant survived.
    #
    # "ancient" is a PREFIX of "ancient-coin" and is NOT the symbol, so it is a
    # prefix-only candidate. (My first attempt used "anc", which EQUALS the
    # symbol and is therefore an alias-tier match -- the fixture was testing a
    # different tier than the one it named.)
    #
    # WITHOUT the coverage guard this row is confidently mislabelled
    # verified_prefix_only: a false negative asserted on evidence that does not
    # exist for this era.
    await _substrate(db, [("ancient", EARLY)])
    await _legacy(db, "ancient-coin", "XYZ", anchor="2026-05-01T00:00:00+00:00")
    await recompute_legacy_provenance(db._conn, gate_minutes=GATE)

    assert (
        await _status(db, "ancient-coin") == STATUS_INDETERMINATE
    ), "an anchor predating all coverage was given a verdict"


def test_coverage_decides_between_INDETERMINATE_and_a_real_negative():
    """Tested at the classifier, and the reason is worth recording.

    Through the DB path the coverage guard is currently REDUNDANT: an anchor
    older than the substrate floor also puts the `+5 minutes` cutoff below every
    substrate row, so there are no candidates at all and the classifier reaches
    `indeterminate` by a second route. Removing the guard changed nothing, and
    two attempts at a DB-level fixture failed to discriminate before I stopped
    guessing and traced it.

    That redundancy is a property of TODAY's single history source, not of the
    rule. The moment coverage and candidate-availability diverge — snapshot-
    extended history being the obvious case — the guard is the only thing
    standing between "we cannot see that far" and a confident false negative.

    So it is pinned where it actually discriminates: identical inputs, coverage
    the only difference.
    """
    from scout.identity import Resolution, TIER_UNRESOLVED
    from scout.identity_recompute import classify

    prefix_hit = Resolution(
        tier=TIER_UNRESOLVED,
        prefix_token_id="ancient",
        prefix_first_seen_at=EARLY,
    )
    common = dict(
        resolution=prefix_hit,
        legacy_detected=True,
        legacy_lead=5000.0,
        canonical_lead=None,
        gate_minutes=GATE,
    )

    assert (
        classify(**common, anchor_covered=False) == STATUS_INDETERMINATE
    ), "an uncovered anchor was given a verdict on evidence that cannot exist"
    assert (
        classify(**common, anchor_covered=True) == STATUS_PREFIX_ONLY
    ), "a covered anchor with a prefix candidate is a REAL negative"


async def test_a_canonical_lead_at_or_above_the_gate_survives_left_censoring(db):
    """The monotonic shortcut: missing older history only makes a lead LARGER.

    So an OBSERVED lead >= the gate is safe even though earlier canonical
    history may be gone — the true lead is at least what we can see.
    """
    await _substrate(db, [("pepe", EARLY)])  # 7200 min >= 1440
    await _legacy(db, "pepe", "PEPE")
    await recompute_legacy_provenance(db._conn, gate_minutes=GATE)
    assert await _status(db, "pepe") == STATUS_VERIFIED_CANONICAL


async def test_a_canonical_lead_BELOW_the_gate_is_indeterminate_not_negative(db):
    """The converse does NOT hold.

    An observed lead under the gate cannot be called a failure, because the
    canonical history that would have cleared it may simply have been pruned.
    """
    await _substrate(db, [("shib", LATE)])  # 60 min < 1440
    await _legacy(db, "shib", "SHIB")
    await recompute_legacy_provenance(db._conn, gate_minutes=GATE)
    assert await _status(db, "shib") == STATUS_CANONICAL_SUB_GATE

    row = {
        "detected_by_chains": 1,
        "chains_lead_minutes": 5000.0,
        "chains_identity_semantics": LEGACY_SEMANTICS,
        "chains_recompute_status": STATUS_CANONICAL_SUB_GATE,
    }
    assert "chains" not in cross_surface_conviction(row, _settings()).contributing


def test_ambiguous_identity_earns_nothing():
    """Tested at the classifier, because the DB path cannot reach this state.

    Tier 3 is bare symbol equality, so every alias candidate normalises to the
    symbol itself and the distinctness set can never exceed one -- the same
    unreachability `test_alias_ambiguity_is_unreachable_by_construction` pins in
    tests/test_canonical_identity.py. A fixture pretending otherwise would be
    asserting against a state production cannot produce.

    The branch is kept because it becomes live the moment tier 3 is re-sourced
    from a real provenance-backed alias table, where two genuinely different
    assets can claim one symbol.
    """
    from scout.identity import TIER_UNRESOLVED, Resolution
    from scout.identity_recompute import classify

    ambiguous = Resolution(tier=TIER_UNRESOLVED, alias_candidates=["bless", "blessed"])
    assert (
        classify(
            resolution=ambiguous,
            legacy_detected=True,
            legacy_lead=5000.0,
            canonical_lead=None,
            anchor_covered=True,
            gate_minutes=GATE,
        )
        == STATUS_AMBIGUOUS
    )


async def test_a_row_with_no_legacy_credit_has_nothing_to_recover(db):
    await _substrate(db, [("pepe", EARLY)])
    await _legacy(db, "pepe", "PEPE", detected=0, lead=None)
    await recompute_legacy_provenance(db._conn, gate_minutes=GATE)
    assert await _status(db, "pepe") == STATUS_NO_LEGACY_CREDIT


async def test_rerun_is_idempotent(db):
    await _substrate(db, [("pepe", EARLY)])
    await _legacy(db, "pepe", "PEPE")

    first = await recompute_legacy_provenance(db._conn, gate_minutes=GATE)
    cur = await db._conn.execute(
        "SELECT source_table, source_row_id, evidence_status, canonical_lead "
        "FROM chain_identity_recompute_v1 ORDER BY source_table, source_row_id"
    )
    snap1 = await cur.fetchall()

    second = await recompute_legacy_provenance(db._conn, gate_minutes=GATE)
    cur = await db._conn.execute(
        "SELECT source_table, source_row_id, evidence_status, canonical_lead "
        "FROM chain_identity_recompute_v1 ORDER BY source_table, source_row_id"
    )
    snap2 = await cur.fetchall()

    assert first == second
    assert [tuple(r) for r in snap1] == [tuple(r) for r in snap2]
    cur = await db._conn.execute("SELECT COUNT(*) FROM chain_identity_recompute_v1")
    assert (await cur.fetchone())[0] == 1, "rerun double-counted"


async def test_result_does_not_depend_on_processing_order(db):
    """Order-independence: the same inputs in any insertion order agree."""
    await _substrate(db, [("aaa", EARLY), ("zzz", EARLY), ("mmm", LATE)])
    for coin in ("zzz", "aaa", "mmm"):
        await _legacy(db, coin, coin.upper())
    await recompute_legacy_provenance(db._conn, gate_minutes=GATE)

    cur = await db._conn.execute(
        "SELECT coin_id, evidence_status FROM chain_identity_recompute_v1 ORDER BY coin_id"
    )
    got = {c: s for c, s in await cur.fetchall()}
    assert got["aaa"] == STATUS_VERIFIED_CANONICAL
    assert got["zzz"] == STATUS_VERIFIED_CANONICAL
    assert got["mmm"] == STATUS_CANONICAL_SUB_GATE


async def test_removing_the_overlay_for_ONE_row_changes_its_score(db):
    """Proves the integration is load-bearing, not decorative."""
    row = {
        "detected_by_chains": 1,
        "chains_lead_minutes": 5000.0,
        "chains_identity_semantics": LEGACY_SEMANTICS,
        "chains_recompute_status": STATUS_VERIFIED_CANONICAL,
    }
    with_overlay = cross_surface_conviction(row, _settings())

    row_without = dict(row, chains_recompute_status=None)
    without = cross_surface_conviction(row_without, _settings())

    assert with_overlay.early_count == 1
    assert (
        without.early_count == 0
    ), "dropping the overlay changed nothing — the integration is not wired"


def _settings():
    from types import SimpleNamespace

    return SimpleNamespace(
        CONVICTION_EARLY_LEAD_MINUTES=1440,
        CONVICTION_HIGH_TIER_MIN_SURFACES=4,
        CONVICTION_WATCH_TIER_MIN_SURFACES=2,
    )


def test_rejecting_all_legacy_reproduces_the_CLIFF_and_must_fail():
    """The forbidden implementation, stated as a test.

    Treating every non-canonical row as untrusted is what produced 826 -> 0 and
    tier_high 341 -> 187. A verified-canonical legacy row MUST earn credit; if
    this assertion ever fails, the cliff has been reintroduced.
    """
    row = {
        "detected_by_chains": 1,
        "chains_lead_minutes": 8736.0,
        "chains_identity_semantics": LEGACY_SEMANTICS,
        "chains_recompute_status": STATUS_VERIFIED_CANONICAL,
    }
    assert cross_surface_conviction(row, _settings()).early_count == 1


async def test_extra_history_recovers_a_row_the_substrate_alone_cannot(db):
    """The measured difference between 18.8% and 65.1% recovery.

    The live substrate's floor is 2026-08-09 while legacy anchors reach back to
    April, so 913 of 1188 production rows are indeterminate on substrate history
    alone. The preserved /root snapshots reach to 2026-06-19 and resolve most of
    June-August.
    """
    old_anchor = "2026-07-01T00:00:00+00:00"
    await _substrate(db, [("pepe", EARLY)])  # 2026-08-15, AFTER the anchor
    await _legacy(db, "pepe", "PEPE", anchor=old_anchor)

    await recompute_legacy_provenance(db._conn, gate_minutes=GATE)
    assert (
        await _status(db, "pepe") == STATUS_INDETERMINATE
    ), "substrate-only should not be able to resolve a July anchor"

    await recompute_legacy_provenance(
        db._conn,
        gate_minutes=GATE,
        extra_history={"pepe": "2026-06-20T00:00:00+00:00"},
    )
    assert await _status(db, "pepe") == STATUS_VERIFIED_CANONICAL


async def test_extra_history_only_moves_a_first_seen_EARLIER(db):
    """The merge must never discard a better (earlier) substrate value.

    Moving a first-seen later would shrink a lead and could un-qualify a row
    that already passed — the one direction that can withdraw legitimate credit.
    """
    await _substrate(db, [("pepe", EARLY)])
    await _legacy(db, "pepe", "PEPE")

    await recompute_legacy_provenance(
        db._conn,
        gate_minutes=GATE,
        extra_history={"pepe": "2026-08-19T23:59:00+00:00"},  # LATER than EARLY
    )
    cur = await db._conn.execute(
        "SELECT canonical_lead FROM chain_identity_recompute_v1 WHERE coin_id='pepe'"
    )
    assert (await cur.fetchone())[0] == pytest.approx(
        7200.0, abs=1.0
    ), "a later extra-history value overwrote the earlier substrate value"


async def test_extra_history_extends_the_COVERAGE_floor_too(db):
    """Extending history must also extend what counts as decidable.

    Leaving the floor at the substrate's would keep calling rows
    `indeterminate` that the extra history can now actually resolve — the
    verdict would silently ignore the evidence just supplied. Verified here on
    the branch where coverage is the deciding factor: no canonical match
    exists, so the answer turns entirely on whether we can see far enough back
    to call the absence meaningful.
    """
    anchor = "2026-07-01T00:00:00+00:00"
    # "ancient" is a prefix of the coin_id and is NOT the symbol.
    await _substrate(db, [("ancient", EARLY)])  # 2026-08-15: after the anchor
    await _legacy(db, "ancient-coin", "XYZ", anchor=anchor)

    await recompute_legacy_provenance(db._conn, gate_minutes=GATE)
    assert await _status(db, "ancient-coin") == STATUS_INDETERMINATE

    # Now history reaches 2026-06-20, before the anchor: the prefix candidate is
    # visible AND the absence of a canonical match becomes meaningful.
    await recompute_legacy_provenance(
        db._conn,
        gate_minutes=GATE,
        extra_history={"ancient": "2026-06-20T00:00:00+00:00"},
    )
    assert (
        await _status(db, "ancient-coin") == STATUS_PREFIX_ONLY
    ), "coverage floor was not extended, so resolvable evidence was ignored"
