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


def test_case_variants_of_one_token_are_the_SAME_asset_not_ambiguity():
    """This test previously asserted the OPPOSITE, and was wrong.

    Review caught it: the ambiguity check compared RAW token_ids while tier-3
    membership requires `token_id.lower() == symbol.lower()`. So raw values
    could differ only by case or whitespace -- the same asset -- and `PEPE` vs
    `pepe` read as ambiguity, silently withdrawing a detection the old
    predicate made. That is the opposite failure to the one this module fixes.

    I had "fixed" the earlier failure by rewriting the test to match the code
    instead of asking whether the code was right.
    """
    r = resolve([("PEPE", OLD), ("pepe", MID)], "pepe-coin", "PEPE")
    assert r.detected is True, "case variants of one token were treated as ambiguous"
    assert r.tier == TIER_ALIAS_UNIQUE
    assert r.first_seen_at == OLD


def test_alias_ambiguity_is_unreachable_by_construction():
    """Prove the guard is currently dead rather than pretending to exercise it.

    Tier-3 membership is bare symbol equality, so EVERY alias candidate
    normalises to the symbol itself and `distinct` can never exceed one. The
    guard is kept for the day tier 3 is re-sourced from a real alias table --
    where two genuinely different assets can claim one symbol -- but until then
    no input can trigger it, and a test claiming otherwise would be theatre.
    """
    from scout.identity import TIER_ALIAS_UNIQUE as _T, classify_candidate as _c

    for tok in ("PEPE", "pepe", "  pepe  ", "PePe"):
        assert _c(tok, "unrelated-coin", "PEPE") == _T
        assert tok.strip().lower() == "pepe"


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


# ---------------------------------------------------------------------------
# Mutants that survived the whole suite until review found them
# ---------------------------------------------------------------------------


def test_earliest_wins_WITHIN_a_tier():
    """`hits.sort()` -- half the module's stated core rule, previously unpinned.

    Reversing it survived all 113 related tests: nothing anywhere had two
    same-tier candidates at different timestamps and asserted the earlier won.
    Tier-before-age is only half the rule; this is the other half.
    """
    r = resolve([("bitcoin", MID), ("bitcoin", OLD)], "bitcoin", "BTC")
    assert r.first_seen_at == OLD

    r2 = resolve([("btc", NEW), ("btc", MID)], "unrelated", "BTC")
    assert r2.first_seen_at == MID


def test_the_prefix_diagnostic_reports_the_EARLIEST_prefix():
    """`prefix_hits.sort()` -- reversing it understates the headline measurement.

    The diagnostic exists to quantify the fabricated lead the old semantics
    handed out. Reporting the LATEST prefix instead of the earliest understates
    exactly the number this PR is built on.
    """
    r = resolve(
        [("vanar-chain-2", NEW), ("vanar-chain", MID), ("vanar", OLD)],
        "vanar-chain-2",
        "VANRY",
    )
    assert r.first_seen_at == NEW
    assert r.prefix_first_seen_at == OLD, "the diagnostic understated the fabrication"
    assert r.prefix_token_id == "vanar"


async def test_BOUND_RAW_LT_is_not_the_datetime_bound(db):
    """The losers bound was defended in prose and pinned by nothing.

    Swapping it for the `datetime(..., '+5 minutes')` form survived the entire
    suite. The two are NOT interchangeable: raw `<` compares in the same BINARY
    order the stored value was minimised under, and the +5min form admits rows
    the raw form excludes.
    """
    from scout.identity import BOUND_DATETIME_PLUS_5M, BOUND_RAW_LT

    # A row 2 minutes AFTER the cutoff: inside the +5min tolerance, outside raw.
    await _seed(db, [("pepe", "2026-08-01T00:02:00+00:00")])
    cutoff = "2026-08-01T00:00:00+00:00"

    raw = await resolve_chain_first_seen(
        db._conn, "pepe", "PEPE", cutoff, bound=BOUND_RAW_LT
    )
    tol = await resolve_chain_first_seen(
        db._conn, "pepe", "PEPE", cutoff, bound=BOUND_DATETIME_PLUS_5M
    )
    assert raw.detected is False, "raw `<` admitted a row after the cutoff"
    assert tol.detected is True, "the +5min tolerance excluded a row inside it"


async def test_an_unknown_bound_is_refused(db):
    """The ValueError branch was unexercised; a typo'd bound must not fall back."""
    with pytest.raises(ValueError, match="unknown bound"):
        await resolve_chain_first_seen(
            db._conn, "pepe", "PEPE", NEW, bound="not_a_bound"
        )
