"""Canonical asset identity for chain-detection truth (ruling C).

**Detection truth requires stable asset identity. Prefix similarity is not
identity.**

Every case below is drawn from the read-only impact study run against
production on 2026-08-23, which found prefix similarity deciding truth for 18
of 424 resolved cohort members (4.2%) — including `real-world-apparel` credited
from a substrate token literally called `re`.

The six assertions the ruling names each have a test, and each was confirmed to
fail against the old predicate.
"""

import pytest

from scout.identity import (
    CANONICAL_SEMANTICS,
    LEGACY_SEMANTICS,
    TIER_ALIAS_UNIQUE,
    TIER_CANONICAL_ID,
    TIER_CONTRACT,
    TIER_UNRESOLVED,
    classify_candidate,
    is_prefix_only,
    resolve,
    resolve_chain_first_seen,
)

OLD = "2020-01-01T00:00:00+00:00"
MID = "2026-08-01T00:00:00+00:00"
NEW = "2026-08-20T00:00:00+00:00"


# ---------------------------------------------------------------------------
# The six the ruling names
# ---------------------------------------------------------------------------


def test_unrelated_older_prefix_cannot_win():
    """The production case: `re` is a prefix of `real-world-apparel`.

    Under the old MIN-over-any-match predicate this two-character token won
    outright, because it was oldest and nothing checked whether it was the same
    asset.
    """
    r = resolve(
        [("re", OLD), ("real-world-apparel", NEW)], "real-world-apparel", "JACKET"
    )
    assert r.token_id == "real-world-apparel"
    assert r.first_seen_at == NEW
    assert r.tier == TIER_CANONICAL_ID
    # ...and the discarded match is still measurable.
    assert r.prefix_token_id == "re"
    assert r.prefix_would_have_credited_more is True


def test_a_prefix_match_ALONE_earns_no_detection():
    """The case with NO identity candidate at all — and the one my first pass missed.

    Mutation testing caught this: letting prefix decide truth *only when nothing
    else matched* passed every other test in this file, because they all happen
    to have an identity candidate present. That is precisely the production
    shape of `blind-boxes` (credited from `bless-2`) and `floki-ceo` (credited
    from `floki`) — the two cohort members the impact study found would lose
    detection entirely under canonical truth. They SHOULD lose it: nothing ever
    established they were the same asset.
    """
    r = resolve([("bless-2", OLD)], "blind-boxes", "BLES")
    assert r.detected is False, "a prefix-only match earned detection credit"
    assert r.tier == TIER_UNRESOLVED
    assert r.first_seen_at is None
    # still measurable as a diagnostic
    assert r.prefix_token_id == "bless-2"
    assert r.prefix_would_have_credited_more is True

    r2 = resolve([("floki", OLD)], "floki-ceo", "FLOKICEO")
    assert r2.detected is False
    assert r2.tier == TIER_UNRESOLVED


async def test_db_path_prefix_alone_earns_no_detection(db):
    """Same invariant, end-to-end through the real substrate table."""
    await _seed(db, [("bless-2", OLD)])
    r = await resolve_chain_first_seen(db._conn, "blind-boxes", "BLES", NEW)
    assert r.detected is False
    assert r.tier == TIER_UNRESOLVED
    assert r.prefix_token_id == "bless-2"


def test_exact_identity_beats_any_older_fuzzy_candidate():
    """Tier before age. This is what stops age promoting a weaker claim."""
    r = resolve([("terra-luna", OLD), ("terra-luna-2", NEW)], "terra-luna-2", "LUNA")
    assert r.token_id == "terra-luna-2", "LUNC's history was credited to LUNA"
    assert r.first_seen_at == NEW


def test_ambiguous_alias_gives_no_detection_credit():
    """Two DISTINCT tokens claiming one symbol is not an identity assertion.

    `token_id` is the substrate's primary key, so ambiguity here means
    case-variant ids -- `BLESS` and `bless` are different rows that both equal
    the symbol under LOWER(). Rarer than the prefix problem, but the tier is
    only sound if it refuses the ambiguous case.
    """
    # The same token appearing twice is NOT ambiguous.
    assert resolve([("pepe", OLD), ("pepe", MID)], "unrelated", "PEPE").detected

    r = resolve([("BLESS", OLD), ("bless", MID)], "some-coin", "BLESS")
    assert r.detected is False, "an ambiguous symbol earned detection credit"
    assert r.tier == TIER_UNRESOLVED
    assert r.alias_candidates == ["BLESS", "bless"]


def test_a_prefix_sibling_does_not_make_a_unique_alias_ambiguous():
    """`bless-2` is a PREFIX match, not an alias claim, so it cannot blur one.

    Conflating the two would silently withdraw credit from correctly-identified
    assets, which is the opposite failure to the one being fixed.
    """
    r = resolve([("bless", MID), ("bless-2", OLD)], "bless-network", "BLESS")
    assert r.detected is True
    assert r.tier == TIER_ALIAS_UNIQUE
    assert r.token_id == "bless"
    assert r.prefix_token_id == "bless-2"


def test_explicit_alias_maps_correctly():
    """A symbol resolving to exactly one token IS an identity assertion."""
    r = resolve([("bless", MID)], "bless-network", "BLESS")
    assert r.detected is True
    assert r.tier == TIER_ALIAS_UNIQUE
    assert r.token_id == "bless"
    assert r.first_seen_at == MID


def test_short_symbols_cannot_broaden_into_global_false_positives():
    """A 2-3 character ticker must not sweep the substrate.

    `re`, `st`, `op` are prefixes of an enormous share of CoinGecko slugs.
    """
    candidates = [
        ("re", OLD),
        ("real-world-apparel", NEW),
        ("st", OLD),
        ("status", OLD),
    ]
    for coin_id, symbol in (
        ("real-world-apparel", "RE"),
        ("starpower", "ST"),
        ("stat", "ST"),
    ):
        r = resolve(candidates, coin_id, symbol)
        assert r.tier in (TIER_CANONICAL_ID, TIER_ALIAS_UNIQUE, TIER_UNRESOLVED)
        if r.detected:
            assert r.token_id.lower() in (
                coin_id.lower(),
                symbol.lower(),
            ), f"{coin_id}/{symbol} was credited from {r.token_id}"


def test_substrate_age_alone_cannot_move_a_canonical_first_seen_earlier():
    """THE accumulation mechanism, stated as an invariant.

    The substrate never prunes, so the pool of prefix-matchable tokens grows
    without limit while signal_events stays 14-day bounded. Adding ever-older
    unrelated tokens must not move a canonical answer.
    """
    truth = [("vanar-chain-2", NEW)]
    baseline = resolve(truth, "vanar-chain-2", "VANRY")
    assert baseline.first_seen_at == NEW

    aged = truth + [
        ("vanar-chain", MID),
        ("vanar", "2019-01-01T00:00:00+00:00"),
        ("v", "2015-01-01T00:00:00+00:00"),
    ]
    after = resolve(aged, "vanar-chain-2", "VANRY")
    assert (
        after.first_seen_at == NEW
    ), "accumulated older prefix matches moved the canonical first-seen earlier"
    assert after.token_id == "vanar-chain-2"


# ---------------------------------------------------------------------------
# Precedence and classification
# ---------------------------------------------------------------------------


def test_contract_identity_outranks_canonical_id():
    """Tier 1 must be reachable, not silently collapsed into tier 2."""
    addr = "0x" + "a" * 40
    assert classify_candidate(addr, addr, None) == TIER_CONTRACT
    assert classify_candidate("dex:solana:x", "dex:solana:x", None) == TIER_CONTRACT
    assert classify_candidate("pepe", "pepe", None) == TIER_CANONICAL_ID


def test_prefix_relationships_are_not_identity_at_all():
    assert classify_candidate("re", "real-world-apparel", "JACKET") is None
    assert classify_candidate("status", "stat", "STAT") is None
    assert is_prefix_only("re", "real-world-apparel", "JACKET") is True
    assert is_prefix_only("real-world-apparel", "real-world-apparel", "X") is False


def test_case_and_whitespace_do_not_defeat_identity():
    r = resolve([("  PePe  ", MID)], "pepe", "PEPE")
    assert r.detected is True


def test_no_candidates_is_unresolved_not_a_wrong_answer():
    r = resolve([], "pepe", "PEPE")
    assert r.detected is False
    assert r.tier == TIER_UNRESOLVED
    assert r.first_seen_at is None


def test_semantics_markers_are_distinct():
    assert CANONICAL_SEMANTICS != LEGACY_SEMANTICS


# ---------------------------------------------------------------------------
# The DB-backed path
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    from scout.db import Database

    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


async def _seed(db, rows):
    await db._conn.executemany(
        "INSERT OR REPLACE INTO signal_first_seen "
        "(token_id, first_seen_at, updated_at) VALUES (?, ?, ?)",
        [(t, f, f) for t, f in rows],
    )
    await db._conn.commit()


async def test_db_path_discards_the_prefix_winner(db):
    """End-to-end against the real substrate table and the real time bound."""
    await _seed(db, [("re", OLD), ("real-world-apparel", MID)])
    r = await resolve_chain_first_seen(db._conn, "real-world-apparel", "JACKET", NEW)
    assert r.token_id == "real-world-apparel"
    assert r.first_seen_at == MID
    assert r.prefix_token_id == "re", "the diagnostic was lost"


async def test_db_path_respects_the_time_bound(db):
    """A match after the cutoff must not count, exactly as before."""
    await _seed(db, [("pepe", NEW)])
    r = await resolve_chain_first_seen(db._conn, "pepe", "PEPE", MID)
    assert r.detected is False


async def test_db_path_is_identical_for_short_and_long_symbols(db):
    """Symbol length must no longer change the TRUTH path.

    The old code branched on `len(symbol) >= 4`, which left one function
    deriving first-seen from two different historical boundaries depending on
    how many characters a ticker had.
    """
    await _seed(db, [("btc", MID), ("bitcoin", MID)])
    short = await resolve_chain_first_seen(db._conn, "bitcoin", "BTC", NEW)
    long_ = await resolve_chain_first_seen(
        db._conn, "bitcoin", "BTCOIN", NEW, prefix_diagnostic=False
    )
    assert short.token_id == "bitcoin"
    assert short.tier == TIER_CANONICAL_ID
    assert long_.token_id == "bitcoin"
    assert long_.tier == TIER_CANONICAL_ID
