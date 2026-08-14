"""Stage B — the real caller-feature provider's as-of semantics.

The four operator-pinned contract fixtures from design v1.4 §B live here:

  (i)   the current signal is excluded from caller-history features;
  (ii)  the current signal IS visible to contemporaneous duplicate /
        corroboration context;
  (iii) a 10-minute-old call does not depress 30m coverage;
  (iv)  that same call enters the 30m coverage denominator once its 45-minute
        window has closed.

Plus the determinism invariant the whole program rests on: recomputing a
decision's features at any later wall-clock time, on a database that has since
learned more, returns a BYTE-IDENTICAL document. If that fails, catch-up after
a disabled window silently decides old calls on new information and the cohort
becomes a mix of two rules wearing one gate_version.

All timestamps are anchored to a fixed past instant rather than `now()`, so
these pin the semantics on every future run rather than only on the day they
were written.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scout.db import Database
from scout.social.telegram.caller_features import (
    COVERAGE_HORIZON,
    FEATURE_SCHEMA,
    FORWARD_WINDOWS,
    PRICE_AT_CALL_TOLERANCE_SEC,
    TgCallerFeatureProvider,
    canonical_identity,
    snapshot_identity,
)
from scout.social.telegram.snapshot import canonical_json_dumps

# Fixed anchor: 2026-08-12 12:00 UTC. Never `now()`.
T0 = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)

CHANNEL = "@gem"
OTHER_CHANNEL = "@alpha"


# ---------------------------------------------------------------------------
# Fixture plumbing
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "caller_features.db")
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def provider(db):
    return TgCallerFeatureProvider(db._db_path)


async def _message(
    db: Database,
    *,
    pk: int,
    posted_at: datetime,
    parsed_at: datetime | None = None,
    channel: str = CHANNEL,
) -> int:
    await db._conn.execute(
        "INSERT INTO tg_social_messages "
        "(id, channel_handle, msg_id, posted_at, parsed_at) VALUES (?, ?, ?, ?, ?)",
        (pk, channel, pk, posted_at.isoformat(), (parsed_at or posted_at).isoformat()),
    )
    await db._conn.commit()
    return pk


async def _signal(
    db: Database,
    *,
    signal_id: int,
    message_pk: int,
    token_id: str,
    created_at: datetime,
    contract: str | None = None,
    chain: str | None = None,
    channel: str = CHANNEL,
) -> int:
    await db._conn.execute(
        """INSERT INTO tg_social_signals
           (id, message_pk, token_id, symbol, contract_address, chain,
            mcap_at_sighting, resolution_state, source_channel_handle,
            created_at)
           VALUES (?, ?, ?, 'TOK', ?, ?, 250000.0, 'RESOLVED', ?, ?)""",
        (
            signal_id,
            message_pk,
            token_id,
            contract,
            chain,
            channel,
            created_at.isoformat(),
        ),
    )
    await db._conn.commit()
    return signal_id


async def _call(
    db: Database,
    *,
    n: int,
    posted_at: datetime,
    token_id: str,
    contract: str | None = None,
    chain: str | None = None,
    channel: str = CHANNEL,
    parsed_at: datetime | None = None,
    created_at: datetime | None = None,
) -> int:
    """One message plus its signal — the shape the listener actually writes."""
    await _message(db, pk=n, posted_at=posted_at, parsed_at=parsed_at, channel=channel)
    return await _signal(
        db,
        signal_id=n,
        message_pk=n,
        token_id=token_id,
        contract=contract,
        chain=chain,
        channel=channel,
        created_at=created_at or parsed_at or posted_at,
    )


async def _snapshot(
    db: Database,
    *,
    identity_key: str,
    price: float,
    snapshot_at: datetime,
    identity_kind: str = "contract",
    chain: str | None = "solana",
    created_at: datetime | None = None,
) -> None:
    """One price observation.

    `created_at` is stamped EXPLICITLY and defaults to `snapshot_at`. Leaving
    it to the column default would stamp real wall-clock time, which post-dates
    every fixed-anchor `as_of` in this file — so the double bound on price
    facts would exclude every fixture row and the whole suite would measure
    nothing. Tests that need the commit-clock straddle pass a later value on
    purpose.
    """
    await db._conn.execute(
        "INSERT INTO source_call_price_snapshots "
        "(identity_key, identity_kind, chain, price, snapshot_at, source, "
        " created_at) VALUES (?, ?, ?, ?, ?, 'gt', ?)",
        (
            identity_key,
            identity_kind,
            chain,
            price,
            snapshot_at.isoformat(),
            (created_at or snapshot_at).isoformat(),
        ),
    )
    await db._conn.commit()


def _contract_key(chain: str, contract: str) -> str:
    return f"{chain}|{contract.lower()}"


async def _priced_call(
    db: Database,
    *,
    n: int,
    posted_at: datetime,
    token_id: str,
    contract: str,
    chain: str = "solana",
    channel: str = CHANNEL,
    anchor_price: float = 100.0,
    later_price: float = 110.0,
) -> int:
    """A call with a contract identity AND the two observations needed to price
    its 30m window: an anchor at the call, and one inside [+30m, +45m]."""
    signal_id = await _call(
        db,
        n=n,
        posted_at=posted_at,
        token_id=token_id,
        contract=contract,
        chain=chain,
        channel=channel,
    )
    key = _contract_key(chain, contract)
    await _snapshot(db, identity_key=key, price=anchor_price, snapshot_at=posted_at)
    await _snapshot(
        db,
        identity_key=key,
        price=later_price,
        snapshot_at=posted_at + timedelta(minutes=35),
    )
    return signal_id


# ---------------------------------------------------------------------------
# (i) current signal excluded from caller-history
# ---------------------------------------------------------------------------


async def test_current_signal_is_excluded_from_caller_history(db, provider):
    """Pinned fixture (i). Shadow evaluation runs AFTER persistence, so a naive
    `created_at <= as_of` query would include the signal being scored and let a
    call improve its own caller's reputation."""
    await _call(db, n=1, posted_at=T0 - timedelta(hours=5), token_id="alpha-coin")
    await _call(db, n=2, posted_at=T0 - timedelta(hours=4), token_id="alpha-coin")
    current = await _call(db, n=3, posted_at=T0, token_id="beta-coin")

    features = provider.features(CHANNEL, T0, current)

    assert features["history_raw_calls"] == 2
    # `beta-coin` is the current call's own cluster; it must not appear.
    assert features["history_distinct_clusters"] == 1


async def test_sibling_signals_from_the_current_message_are_excluded(db, provider):
    """`current_signal_id` AND its message row. One message can extract several
    tokens; none of them may build the caller's track record for a decision
    being made about that same message."""
    await _call(db, n=1, posted_at=T0 - timedelta(hours=5), token_id="alpha-coin")
    await _message(db, pk=2, posted_at=T0)
    await _signal(db, signal_id=2, message_pk=2, token_id="beta-coin", created_at=T0)
    await _signal(db, signal_id=3, message_pk=2, token_id="gamma-coin", created_at=T0)

    features = provider.features(CHANNEL, T0, 2)

    assert features["history_raw_calls"] == 1
    assert features["history_distinct_clusters"] == 1


# ---------------------------------------------------------------------------
# (ii) current signal visible to contemporaneous context
# ---------------------------------------------------------------------------


async def test_current_signal_is_visible_to_duplicate_context(db, provider):
    """Pinned fixture (ii), duplicate half. `event_*` describes THIS call, so
    the call itself must be counted — a second call of a token this channel
    already pushed today is rank 2, and blocking on that is the point."""
    await _call(
        db,
        n=1,
        posted_at=T0 - timedelta(hours=2),
        token_id="dex:solana:abc",
        contract="ABC",
        chain="solana",
    )
    current = await _call(
        db,
        n=2,
        posted_at=T0,
        token_id="dex:solana:abc",
        contract="abc",
        chain="solana",
    )

    features = provider.features(CHANNEL, T0, current)

    assert features["event_duplicate_rank_in_cluster"] == 2
    assert features["event_cluster_size"] == 2


async def test_current_signal_is_visible_to_cross_channel_corroboration(db, provider):
    """Pinned fixture (ii), corroboration half. The channel-independent
    identity is what makes this countable at all: the ledger's own cluster key
    embeds the channel, so the same token called by two channels gets two keys
    there by construction."""
    await _call(
        db,
        n=1,
        posted_at=T0 - timedelta(hours=1),
        token_id="dex:solana:abc",
        contract="ABC",
        chain="solana",
        channel=OTHER_CHANNEL,
    )
    current = await _call(
        db,
        n=2,
        posted_at=T0,
        token_id="dex:solana:abc",
        contract="abc",
        chain="solana",
    )

    features = provider.features(CHANNEL, T0, current)

    # Both channels, including the one being scored.
    assert features["event_corroborating_channels"] == 2


async def test_corroboration_excludes_calls_not_yet_knowable(db, provider):
    """A corroborating call posted before the decision but INGESTED after it
    was not knowable then. Counting it would make a decision reproduce
    differently once the backlog drained."""
    await _call(
        db,
        n=1,
        posted_at=T0 - timedelta(minutes=30),
        parsed_at=T0 + timedelta(hours=2),
        created_at=T0 + timedelta(hours=2),
        token_id="dex:solana:abc",
        contract="ABC",
        chain="solana",
        channel=OTHER_CHANNEL,
    )
    current = await _call(
        db, n=2, posted_at=T0, token_id="dex:solana:abc", contract="abc", chain="solana"
    )

    features = provider.features(CHANNEL, T0, current)

    assert features["event_corroborating_channels"] == 1


async def test_corroboration_is_bucketed_by_utc_day(db, provider):
    """A different UTC day is a different bucket — corroboration means 'these
    channels called this token at the same time', not 'ever'."""
    await _call(
        db,
        n=1,
        posted_at=T0 - timedelta(days=1),
        token_id="dex:solana:abc",
        contract="ABC",
        chain="solana",
        channel=OTHER_CHANNEL,
    )
    current = await _call(
        db, n=2, posted_at=T0, token_id="dex:solana:abc", contract="abc", chain="solana"
    )

    features = provider.features(CHANNEL, T0, current)

    assert features["event_corroborating_channels"] == 1


# ---------------------------------------------------------------------------
# (iii) + (iv) coverage-denominator maturity
# ---------------------------------------------------------------------------


async def _coverage_fixture(db) -> int:
    """One matured, covered cluster plus one 10-minute-old priceable call.

    The fresh call has a priceable identity and NO observations — exactly the
    shape that would depress coverage if the denominator ignored maturity.
    """
    await _priced_call(
        db,
        n=1,
        posted_at=T0 - timedelta(hours=3),
        token_id="dex:solana:old",
        contract="OLD",
    )
    await _call(
        db,
        n=2,
        posted_at=T0 - timedelta(minutes=10),
        token_id="dex:solana:fresh",
        contract="FRESH",
        chain="solana",
    )
    return await _call(db, n=3, posted_at=T0, token_id="beta-coin")


async def test_ten_minute_old_call_does_not_depress_30m_coverage(db, provider):
    """Pinned fixture (iii). Without the maturity rule a channel receiving a
    burst of fresh calls would read as deteriorating coverage when the snapshot
    writer has had no opportunity to measure them."""
    current = await _coverage_fixture(db)

    features = provider.features(CHANNEL, T0, current)

    assert features["history_mature_clusters"] == 1
    assert features["history_coverage_rate"] == 1.0
    assert features["history_priceable_coverage_rate"] == 1.0
    assert features["history_eligible_distinct_clusters"] == 1


async def test_same_call_enters_the_denominator_once_its_window_closes(db, provider):
    """Pinned fixture (iv). The fresh call is not excluded forever — it enters
    the denominator the moment its 45-minute window closes, and (having no
    observations) it correctly halves coverage from that point on."""
    current = await _coverage_fixture(db)
    # 10 minutes old at T0; its 45-minute window closes 35 minutes later.
    as_of = T0 + timedelta(minutes=35)

    features = provider.features(CHANNEL, as_of, current)

    assert features["history_mature_clusters"] == 2
    assert features["history_mature_priceable_clusters"] == 2
    assert features["history_eligible_distinct_clusters"] == 1
    assert features["history_coverage_rate"] == 0.5
    assert features["history_priceable_coverage_rate"] == 0.5


async def test_denominator_boundary_is_inclusive_at_exactly_45_minutes(db, provider):
    """The window CLOSES at +45m; at exactly that instant the call is in."""
    current = await _coverage_fixture(db)
    at_boundary = (T0 - timedelta(minutes=10)) + FORWARD_WINDOWS[COVERAGE_HORIZON][1]

    just_before = provider.features(
        CHANNEL, at_boundary - timedelta(seconds=1), current
    )
    exactly = provider.features(CHANNEL, at_boundary, current)

    assert just_before["history_mature_clusters"] == 1
    assert exactly["history_mature_clusters"] == 2


# ---------------------------------------------------------------------------
# Determinism — THE invariant
# ---------------------------------------------------------------------------


async def test_recomputation_after_later_evidence_is_byte_identical(db, provider):
    """Compute at T, teach the database more, recompute at T.

    Every kind of later evidence is added: a later-posted message, a
    later-created signal, and later price observations. All four timestamp
    families (posted_at / parsed_at / created_at / snapshot_at) move AFTER T.
    """
    await _priced_call(
        db,
        n=1,
        posted_at=T0 - timedelta(hours=3),
        token_id="dex:solana:old",
        contract="OLD",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")

    before = canonical_json_dumps(provider.features(CHANNEL, T0, current))

    await _priced_call(
        db,
        n=10,
        posted_at=T0 + timedelta(hours=1),
        token_id="dex:solana:new",
        contract="NEW",
    )
    await _snapshot(
        db,
        identity_key=_contract_key("solana", "OLD"),
        price=999.0,
        snapshot_at=T0 + timedelta(hours=2),
    )

    after = canonical_json_dumps(provider.features(CHANNEL, T0, current))

    assert after == before


async def test_late_ingested_call_is_not_knowable_at_decision_time(db, provider):
    """The double bound's whole point: `posted_at <= as_of` alone is not
    enough. A call posted before T but parsed after it did not exist to us at
    T, and admitting it would make replay differ from the live decision."""
    await _priced_call(
        db,
        n=1,
        posted_at=T0 - timedelta(hours=3),
        token_id="dex:solana:old",
        contract="OLD",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")
    baseline = canonical_json_dumps(provider.features(CHANNEL, T0, current))

    # Posted 2h before the decision, ingested 5h after it.
    await _call(
        db,
        n=3,
        posted_at=T0 - timedelta(hours=2),
        parsed_at=T0 + timedelta(hours=5),
        created_at=T0 + timedelta(hours=5),
        token_id="dex:solana:late",
        contract="LATE",
        chain="solana",
    )

    assert canonical_json_dumps(provider.features(CHANNEL, T0, current)) == baseline


# Each of the next three isolates ONE clause of the knowability bound by moving
# exactly one timestamp past `as_of` and leaving the other two knowable. The
# earlier late-ingest test could not do that: its helper moved `parsed_at` and
# `created_at` together, so the `created_at` clause alone still excluded the row
# and the `parsed_at` clause was never exercised. Two of these three states are
# deliberately SYNTHETIC — real writers keep `posted_at <= parsed_at <=
# created_at` — because the point is to pin each clause independently, not to
# reproduce a shape production can emit.


async def test_parsed_at_alone_past_as_of_excludes_the_row(db, provider):
    """Ingested after the decision, posted and persisted before it."""
    await _call(
        db,
        n=1,
        posted_at=T0 - timedelta(hours=2),
        parsed_at=T0 + timedelta(hours=1),
        created_at=T0 - timedelta(hours=1),
        token_id="alpha-coin",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")

    assert provider.features(CHANNEL, T0, current)["history_raw_calls"] == 0


async def test_posted_at_alone_past_as_of_excludes_the_row(db, provider):
    await _call(
        db,
        n=1,
        posted_at=T0 + timedelta(hours=1),
        parsed_at=T0 - timedelta(hours=1),
        created_at=T0 - timedelta(hours=1),
        token_id="alpha-coin",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")

    assert provider.features(CHANNEL, T0, current)["history_raw_calls"] == 0


async def test_created_at_alone_past_as_of_excludes_the_row(db, provider):
    """The realistic one: the message was parsed before the decision but the
    signal row was not written until after it."""
    await _call(
        db,
        n=1,
        posted_at=T0 - timedelta(hours=2),
        parsed_at=T0 - timedelta(hours=2),
        created_at=T0 + timedelta(hours=1),
        token_id="alpha-coin",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")

    assert provider.features(CHANNEL, T0, current)["history_raw_calls"] == 0


async def test_open_window_is_excluded_even_when_priced_inside_it(db, provider):
    """The maturity gate's only discriminating case.

    An observation lands INSIDE the 6h window and is itself knowable, but the
    window has not closed at `as_of`. Admitting it would mean the same decision
    returns a different 6h number once the window finishes — which is precisely
    the leak the rule exists to stop. The earlier immature-horizon test could
    not see this: it had no observation in the window at all, so the horizon
    came back None whether or not the gate was there.
    """
    posted = T0 - timedelta(hours=6, minutes=30)
    await _call(
        db,
        n=1,
        posted_at=posted,
        token_id="dex:solana:old",
        contract="OLD",
        chain="solana",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")
    key = _contract_key("solana", "OLD")
    await _snapshot(db, identity_key=key, price=100.0, snapshot_at=posted)
    # Inside [posted+6h, posted+7h] and recorded 15 minutes before the decision;
    # the window itself does not close until 30 minutes AFTER it.
    await _snapshot(
        db,
        identity_key=key,
        price=180.0,
        snapshot_at=posted + timedelta(hours=6, minutes=15),
    )

    features = provider.features(CHANNEL, T0, current)

    assert features["history_avg_forward_6h_pct"] is None
    assert features["history_forward_6h_sample_count"] == 0


async def test_observations_recorded_after_as_of_leave_the_cluster_unobserved(
    db, provider
):
    """The snapshot knowability bound's discriminating case.

    Per-horizon maturity already hides late observations from every RETURN, so
    the bound only shows up in the substrate-gap counter: a cluster whose sole
    observation post-dates the decision was unobserved AT that decision, and
    must be counted as such.
    """
    posted = T0 - timedelta(hours=3)
    await _call(
        db,
        n=1,
        posted_at=posted,
        token_id="dex:solana:old",
        contract="OLD",
        chain="solana",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")
    await _snapshot(
        db,
        identity_key=_contract_key("solana", "OLD"),
        price=100.0,
        snapshot_at=T0 + timedelta(hours=1),
    )

    features = provider.features(CHANNEL, T0, current)

    assert features["history_clusters_without_observations"] == 1


async def test_price_row_committed_after_as_of_does_not_change_the_replay(db, provider):
    """B1 — price-fact knowability is on the COMMIT clock, not the stamp clock.

    `snapshot_writer` stamps `snapshot_at` ONCE at cycle start and commits the
    whole cycle ONCE at the end, so a row can carry a timestamp before `as_of`
    and only become VISIBLE after it. Forward-only monotonicity does not imply
    committed-by-T. A live evaluation and its replay then disagree — which
    falsifies the determinism invariant this whole module rests on.

    The row below is stamped 35 minutes after the call (inside its 30m window,
    before `as_of`) but is inserted only AFTER the first `features()` call, so
    its `created_at` post-dates the decision. It must not move a single field.
    """
    posted = T0 - timedelta(hours=3)
    await _call(
        db,
        n=1,
        posted_at=posted,
        token_id="dex:solana:old",
        contract="OLD",
        chain="solana",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")
    key = _contract_key("solana", "OLD")
    await _snapshot(db, identity_key=key, price=100.0, snapshot_at=posted)

    before = canonical_json_dumps(provider.features(CHANNEL, T0, current))

    # Stamped before as_of, committed after it — the exact straddle.
    await db._conn.execute(
        "INSERT INTO source_call_price_snapshots "
        "(identity_key, identity_kind, chain, price, snapshot_at, source, "
        " created_at) VALUES (?, 'contract', 'solana', 130.0, ?, 'gt', ?)",
        (
            key,
            (posted + timedelta(minutes=35)).isoformat(),
            (T0 + timedelta(hours=2)).isoformat(),
        ),
    )
    await db._conn.commit()

    after = canonical_json_dumps(provider.features(CHANNEL, T0, current))

    assert after == before, (
        "a price row that became visible after the decision changed its "
        "features — replay no longer reproduces the live evaluation"
    )


async def test_gate_inputs_specifically_survive_a_late_committed_price_row(
    db, provider
):
    """The same straddle, asserted on the two GATE inputs by name.

    Byte-identity above already covers this, but these two fields are what the
    evaluator actually branches on, so a regression in exactly them is worth
    naming rather than leaving to a whole-document comparison.
    """
    posted = T0 - timedelta(hours=3)
    await _call(
        db,
        n=1,
        posted_at=posted,
        token_id="dex:solana:old",
        contract="OLD",
        chain="solana",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")
    key = _contract_key("solana", "OLD")
    await _snapshot(db, identity_key=key, price=100.0, snapshot_at=posted)
    await db._conn.execute(
        "INSERT INTO source_call_price_snapshots "
        "(identity_key, identity_kind, chain, price, snapshot_at, source, "
        " created_at) VALUES (?, 'contract', 'solana', 130.0, ?, 'gt', ?)",
        (
            key,
            (posted + timedelta(minutes=35)).isoformat(),
            (T0 + timedelta(hours=2)).isoformat(),
        ),
    )
    await db._conn.commit()

    features = provider.features(CHANNEL, T0, current)

    assert features["history_eligible_distinct_clusters"] == 0
    assert features["history_priceable_coverage_rate"] == 0.0


async def test_features_read_is_a_single_point_in_time(db, provider, monkeypatch):
    """B2 — every statement in one `features()` call sees ONE database state.

    Default sqlite3 isolation opens no transaction for SELECTs, so each
    statement takes its own WAL snapshot. A commit landing between the
    channel-history read and the observations read would be half-seen, and the
    resulting `features_json` would describe a state that never existed at any
    instant — unreproducible by construction.

    The commit is straddled into the middle of the call by hooking the
    observations load, which runs BETWEEN the channel-history read and the
    corroboration read. The row committed there is a second channel calling the
    CURRENT signal's identity in the same UTC day — so the still-pending
    corroboration SELECT would see it if that statement took a fresh snapshot,
    and `event_corroborating_channels` would read 2 instead of 1.

    Choosing a row a LATER statement actually queries is the whole test: an
    earlier version committed a row no subsequent statement looked at, so it
    passed identically with and without the transaction and proved nothing.
    """
    posted = T0 - timedelta(hours=3)
    await _priced_call(
        db, n=1, posted_at=posted, token_id="dex:solana:old", contract="OLD"
    )
    current = await _call(
        db, n=2, posted_at=T0, token_id="dex:solana:cur", contract="CUR", chain="solana"
    )

    undisturbed = canonical_json_dumps(provider.features(CHANNEL, T0, current))
    assert (
        provider.features(CHANNEL, T0, current)["event_corroborating_channels"] == 1
    ), "baseline should see only the scored channel"

    import sqlite3

    from scout.social.telegram.caller_features import TgCallerFeatureProvider

    original = TgCallerFeatureProvider._load_observations
    fired: list[bool] = []
    mid = T0 - timedelta(hours=1)

    def _load_then_commit_a_new_row(self, conn, calls, as_of):
        result = original(self, conn, calls, as_of)
        if not fired:
            fired.append(True)
            # Another channel calling THIS call's contract, same UTC day, fully
            # knowable at as_of — i.e. a row the corroboration SELECT below
            # WILL match if it takes its own snapshot.
            writer = sqlite3.connect(db._db_path, timeout=30.0)
            try:
                writer.execute(
                    "INSERT INTO tg_social_messages (id, channel_handle, msg_id,"
                    " posted_at, parsed_at) VALUES (99, ?, 99, ?, ?)",
                    (OTHER_CHANNEL, mid.isoformat(), mid.isoformat()),
                )
                writer.execute(
                    "INSERT INTO tg_social_signals (id, message_pk, token_id,"
                    " symbol, contract_address, chain, mcap_at_sighting,"
                    " resolution_state, source_channel_handle, created_at)"
                    " VALUES (99, 99, 'dex:solana:cur', 'CUR', 'CUR', 'solana',"
                    " 250000.0, 'RESOLVED', ?, ?)",
                    (OTHER_CHANNEL, mid.isoformat()),
                )
                writer.commit()
            finally:
                writer.close()
        return result

    monkeypatch.setattr(
        TgCallerFeatureProvider, "_load_observations", _load_then_commit_a_new_row
    )

    straddled = canonical_json_dumps(provider.features(CHANNEL, T0, current))

    assert fired, "the mid-call commit never happened — this test proved nothing"
    assert straddled == undisturbed, (
        "a commit landing mid-call was half-seen: features describe a database "
        "state that never existed at any single instant"
    )


async def test_snapshot_at_alone_past_as_of_excludes_the_observation(db, provider):
    """Isolates the `snapshot_at` clause of the price double bound.

    DELIBERATELY SYNTHETIC, like the single-clause knowability fixtures above:
    the real writer inserts rows after stamping them, so `created_at >=
    snapshot_at` always holds and `snapshot_at > as_of` implies `created_at >
    as_of`. The clause is therefore redundant against a well-behaved writer —
    but it is the design's stated bound, it is what the ledger's forward-only
    contract is expressed in, and a future writer that stamped a claimed
    historical time would make it the only thing standing. Pinned on its own
    rather than left resting on its neighbour.
    """
    posted = T0 - timedelta(hours=3)
    await _call(
        db,
        n=1,
        posted_at=posted,
        token_id="dex:solana:old",
        contract="OLD",
        chain="solana",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")
    await _snapshot(
        db,
        identity_key=_contract_key("solana", "OLD"),
        price=100.0,
        snapshot_at=T0 + timedelta(hours=1),
        created_at=T0 - timedelta(hours=1),
    )

    features = provider.features(CHANNEL, T0, current)

    assert features["history_clusters_without_observations"] == 1


async def test_snapshot_recorded_after_the_decision_is_excluded(db, provider):
    """A price observation is knowable only if it was RECORDED by `as_of`.
    Sufficient because the writer is forward-only — pinned separately."""
    await _call(
        db,
        n=1,
        posted_at=T0 - timedelta(hours=3),
        token_id="dex:solana:old",
        contract="OLD",
        chain="solana",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")
    key = _contract_key("solana", "OLD")
    await _snapshot(
        db, identity_key=key, price=100.0, snapshot_at=T0 - timedelta(hours=3)
    )
    # Inside the call's 30m window, but recorded after the decision.
    await _snapshot(
        db, identity_key=key, price=150.0, snapshot_at=T0 + timedelta(minutes=5)
    )

    features = provider.features(CHANNEL, T0, current)

    assert features["history_eligible_distinct_clusters"] == 0
    assert features["history_avg_forward_30m_pct"] is None


# ---------------------------------------------------------------------------
# Forward-return reconstruction
# ---------------------------------------------------------------------------


async def test_forward_return_is_computed_from_the_snapshot_series(db, provider):
    await _priced_call(
        db,
        n=1,
        posted_at=T0 - timedelta(hours=3),
        token_id="dex:solana:old",
        contract="OLD",
        anchor_price=100.0,
        later_price=110.0,
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")

    features = provider.features(CHANNEL, T0, current)

    assert features["history_avg_forward_30m_pct"] == pytest.approx(10.0)
    assert features["history_forward_30m_sample_count"] == 1


async def test_immature_horizon_yields_no_sample_rather_than_zero(db, provider):
    """A window still open at `as_of` is excluded from the numerator AND the
    denominator — neither a win, a loss, nor a sample."""
    await _priced_call(
        db,
        n=1,
        posted_at=T0 - timedelta(hours=3),
        token_id="dex:solana:old",
        contract="OLD",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")

    features = provider.features(CHANNEL, T0, current)

    # 30m matured (3h old); 6h and 24h have not.
    assert features["history_forward_30m_sample_count"] == 1
    assert features["history_forward_6h_sample_count"] == 0
    assert features["history_avg_forward_6h_pct"] is None
    assert features["history_forward_24h_sample_count"] == 0
    assert features["history_avg_forward_24h_pct"] is None


async def test_zero_anchor_price_yields_no_return_rather_than_raising(db, provider):
    """`price` is REAL NOT NULL, so a bad feed writes 0.0 rather than NULL.
    Dividing by it would raise inside a decision path whose failure mode is an
    immutable row that never gets written at all."""
    posted = T0 - timedelta(hours=3)
    await _call(
        db,
        n=1,
        posted_at=posted,
        token_id="dex:solana:old",
        contract="OLD",
        chain="solana",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")
    key = _contract_key("solana", "OLD")
    await _snapshot(db, identity_key=key, price=0.0, snapshot_at=posted)
    await _snapshot(
        db, identity_key=key, price=110.0, snapshot_at=posted + timedelta(minutes=35)
    )

    features = provider.features(CHANNEL, T0, current)

    assert features["history_avg_forward_30m_pct"] is None
    assert features["history_eligible_distinct_clusters"] == 0


async def test_stale_anchor_beyond_max_age_does_not_price_the_window(db, provider):
    """The 30m window tolerates a 15-minute-old anchor. An anchor older than
    that prices a different moment than the call."""
    posted = T0 - timedelta(hours=3)
    await _call(
        db,
        n=1,
        posted_at=posted,
        token_id="dex:solana:old",
        contract="OLD",
        chain="solana",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")
    key = _contract_key("solana", "OLD")
    await _snapshot(
        db, identity_key=key, price=100.0, snapshot_at=posted - timedelta(minutes=20)
    )
    await _snapshot(
        db, identity_key=key, price=110.0, snapshot_at=posted + timedelta(minutes=35)
    )

    features = provider.features(CHANNEL, T0, current)

    assert features["history_avg_forward_30m_pct"] is None


async def test_just_after_anchor_is_accepted_within_tolerance(db, provider):
    """Forward-only series often have no observation at/before a call; the
    earliest one inside the tolerance is the anchor."""
    posted = T0 - timedelta(hours=3)
    await _call(
        db,
        n=1,
        posted_at=posted,
        token_id="dex:solana:old",
        contract="OLD",
        chain="solana",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")
    key = _contract_key("solana", "OLD")
    await _snapshot(
        db,
        identity_key=key,
        price=100.0,
        snapshot_at=posted + timedelta(seconds=PRICE_AT_CALL_TOLERANCE_SEC - 10),
    )
    await _snapshot(
        db, identity_key=key, price=120.0, snapshot_at=posted + timedelta(minutes=35)
    )

    features = provider.features(CHANNEL, T0, current)

    assert features["history_avg_forward_30m_pct"] == pytest.approx(20.0)


async def test_excursion_matures_at_24h_and_reports_both_extremes(db, provider):
    posted = T0 - timedelta(hours=25)
    await _call(
        db,
        n=1,
        posted_at=posted,
        token_id="dex:solana:old",
        contract="OLD",
        chain="solana",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")
    key = _contract_key("solana", "OLD")
    await _snapshot(db, identity_key=key, price=100.0, snapshot_at=posted)
    await _snapshot(
        db, identity_key=key, price=150.0, snapshot_at=posted + timedelta(hours=2)
    )
    await _snapshot(
        db, identity_key=key, price=70.0, snapshot_at=posted + timedelta(hours=5)
    )

    features = provider.features(CHANNEL, T0, current)

    assert features["history_avg_max_favorable_pct_24h"] == pytest.approx(50.0)
    assert features["history_avg_max_adverse_pct_24h"] == pytest.approx(-30.0)
    assert features["history_excursion_sample_count"] == 1


async def test_excursion_is_absent_before_its_window_closes(db, provider):
    posted = T0 - timedelta(hours=5)
    await _call(
        db,
        n=1,
        posted_at=posted,
        token_id="dex:solana:old",
        contract="OLD",
        chain="solana",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")
    key = _contract_key("solana", "OLD")
    await _snapshot(db, identity_key=key, price=100.0, snapshot_at=posted)
    await _snapshot(
        db, identity_key=key, price=150.0, snapshot_at=posted + timedelta(hours=2)
    )

    features = provider.features(CHANNEL, T0, current)

    assert features["history_avg_max_favorable_pct_24h"] is None
    assert features["history_excursion_sample_count"] == 0


# ---------------------------------------------------------------------------
# Cluster / duplicate axes
# ---------------------------------------------------------------------------


async def test_repeat_calls_are_one_cluster_not_many_samples(db, provider):
    """The min-N denominator is eligible distinct CLUSTERS: a caller who posts
    the same token ten times has one sample, not ten."""
    for n in range(1, 4):
        await _call(
            db,
            n=n,
            posted_at=T0 - timedelta(hours=6) + timedelta(minutes=n),
            token_id="dex:solana:abc",
            contract="ABC",
            chain="solana",
        )
    current = await _call(db, n=9, posted_at=T0, token_id="beta-coin")

    features = provider.features(CHANNEL, T0, current)

    assert features["history_raw_calls"] == 3
    assert features["history_distinct_clusters"] == 1
    assert features["history_duplicate_rate"] == pytest.approx(2 / 3)


async def test_same_token_on_a_later_day_is_a_new_cluster(db, provider):
    await _call(
        db,
        n=1,
        posted_at=T0 - timedelta(days=2),
        token_id="dex:solana:abc",
        contract="ABC",
        chain="solana",
    )
    await _call(
        db,
        n=2,
        posted_at=T0 - timedelta(days=1),
        token_id="dex:solana:abc",
        contract="ABC",
        chain="solana",
    )
    current = await _call(db, n=3, posted_at=T0, token_id="beta-coin")

    features = provider.features(CHANNEL, T0, current)

    assert features["history_distinct_clusters"] == 2


async def test_unpriceable_cluster_proportion_is_reported(db, provider):
    """A cashtag-only call has no priceable identity. Keeping it visible is the
    #482 lesson: unpriceable traffic must never be hidden inside a rate."""
    await _call(
        db,
        n=1,
        posted_at=T0 - timedelta(hours=6),
        token_id="dex:solana:abc",
        contract="ABC",
        chain="solana",
    )
    await _message(db, pk=2, posted_at=T0 - timedelta(hours=5))
    await db._conn.execute(
        """INSERT INTO tg_social_signals
           (id, message_pk, token_id, symbol, contract_address, chain,
            mcap_at_sighting, resolution_state, source_channel_handle, created_at)
           VALUES (2, 2, 'PEPE', 'PEPE', NULL, NULL, 1.0, 'RESOLVED', ?, ?)""",
        (CHANNEL, (T0 - timedelta(hours=5)).isoformat()),
    )
    await db._conn.commit()
    current = await _call(db, n=3, posted_at=T0, token_id="beta-coin")

    features = provider.features(CHANNEL, T0, current)

    assert features["history_distinct_clusters"] == 2
    assert features["history_priceable_clusters"] == 1
    assert features["history_unpriceable_clusters"] == 1
    assert features["history_unpriceable_cluster_rate"] == pytest.approx(0.5)


async def test_unpriceable_clusters_are_not_counted_as_substrate_gaps(db, provider):
    """N4 — `clusters_without_observations` counts only PRICEABLE identities.

    This counter is the substrate-gap legibility story: it is what will show
    the CG `coin_id` substrate landing. A cashtag-only call has no priceable
    identity at all, so it can never have observations and must NOT inflate
    this number — otherwise "how big is the substrate gap" silently becomes
    "how many cashtags does this channel post", and the counter stops
    measuring the thing it exists to measure.
    """
    # One CG-slug cluster: priceable identity, no observations -> a real gap.
    await _call(db, n=1, posted_at=T0 - timedelta(hours=6), token_id="some-real-coin")
    # One cashtag-only cluster: NOT priceable -> not a substrate gap.
    await _message(db, pk=2, posted_at=T0 - timedelta(hours=5))
    await db._conn.execute(
        """INSERT INTO tg_social_signals
           (id, message_pk, token_id, symbol, contract_address, chain,
            mcap_at_sighting, resolution_state, source_channel_handle, created_at)
           VALUES (2, 2, 'PEPE', 'PEPE', NULL, NULL, 1.0, 'RESOLVED', ?, ?)""",
        (CHANNEL, (T0 - timedelta(hours=5)).isoformat()),
    )
    await db._conn.commit()
    current = await _call(db, n=3, posted_at=T0, token_id="beta-coin")

    features = provider.features(CHANNEL, T0, current)

    assert features["history_distinct_clusters"] == 2
    assert features["history_unpriceable_clusters"] == 1
    assert (
        features["history_clusters_without_observations"] == 1
    ), "the unpriceable cashtag cluster was counted as a substrate gap"


async def test_substrate_gap_is_counted_separately_from_thin_coverage(db, provider):
    """SEAM: a CG-slug identity is structurally priceable but the snapshot
    writer emits only `identity_kind='contract'` rows today, so it has NO
    observations. That is a substrate gap, not a caller with sparse data, and
    the two must be countable apart."""
    await _call(db, n=1, posted_at=T0 - timedelta(hours=6), token_id="some-real-coin")
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")

    features = provider.features(CHANNEL, T0, current)

    assert features["history_coin_id_identity_clusters"] == 1
    assert features["history_clusters_without_observations"] == 1
    assert features["history_priceable_clusters"] == 1


# ---------------------------------------------------------------------------
# Trailing-30d recency split
# ---------------------------------------------------------------------------


async def test_trailing_30d_split_excludes_older_history(db, provider):
    await _priced_call(
        db,
        n=1,
        posted_at=T0 - timedelta(days=45),
        token_id="dex:solana:stale",
        contract="STALE",
        anchor_price=100.0,
        later_price=200.0,
    )
    await _priced_call(
        db,
        n=2,
        posted_at=T0 - timedelta(days=2),
        token_id="dex:solana:recent",
        contract="RECENT",
        anchor_price=100.0,
        later_price=110.0,
    )
    current = await _call(db, n=3, posted_at=T0, token_id="beta-coin")

    features = provider.features(CHANNEL, T0, current)

    assert features["history_eligible_distinct_clusters"] == 2
    assert features["history_avg_forward_30m_pct"] == pytest.approx(55.0)
    assert features["history_trailing_30d_eligible_distinct_clusters"] == 1
    assert features["history_trailing_30d_avg_forward_30m_pct"] == pytest.approx(10.0)
    assert features["history_trailing_30d_priceable_coverage_rate"] == 1.0


# ---------------------------------------------------------------------------
# Contract surface
# ---------------------------------------------------------------------------


async def test_returned_keys_exactly_match_the_declared_schema(db, provider):
    """Internal-consistency contract: the schema is fingerprinted and the
    features are the evidence record, so a field present in one and absent from
    the other is a silent divergence between what a gate_version claims to
    bind and what it actually saw."""
    current = await _call(db, n=1, posted_at=T0, token_id="beta-coin")

    features = provider.features(CHANNEL, T0, current)

    assert set(features) == {name for name, _ in FEATURE_SCHEMA}
    assert len(features) == len(FEATURE_SCHEMA)


async def test_every_key_the_evaluator_reads_is_produced(db, provider):
    """The three keys `evaluate_tg_actionability_shadow` looks up by name. A
    missing one resolves to the conservative value and produces a block that
    looks like evidence about the caller."""
    current = await _call(db, n=1, posted_at=T0, token_id="beta-coin")

    features = provider.features(CHANNEL, T0, current)

    for key in (
        "history_eligible_distinct_clusters",
        "history_priceable_coverage_rate",
        "event_duplicate_rank_in_cluster",
    ):
        assert key in features


async def test_features_are_json_serializable_without_nan(db, provider):
    """`canonical_json_dumps` refuses NaN/Infinity. A feature that produced one
    would raise at the `features_json` boundary, after the decision was made
    and before it could be persisted."""
    await _priced_call(
        db,
        n=1,
        posted_at=T0 - timedelta(hours=3),
        token_id="dex:solana:old",
        contract="OLD",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")

    canonical_json_dumps(provider.features(CHANNEL, T0, current))


async def test_empty_channel_history_is_zeroed_not_absent(db, provider):
    """A brand-new caller yields a full feature set of zeros, which the
    evaluator turns into `shadow_block_caller_insufficient_n`."""
    current = await _call(db, n=1, posted_at=T0, token_id="beta-coin")

    features = provider.features(CHANNEL, T0, current)

    assert features["history_raw_calls"] == 0
    assert features["history_eligible_distinct_clusters"] == 0
    assert features["history_priceable_coverage_rate"] == 0.0
    assert features["history_coverage_rate"] == 0.0
    assert features["history_duplicate_rate"] == 0.0
    assert features["event_duplicate_rank_in_cluster"] == 1


async def test_unknown_current_signal_degrades_rather_than_raises(db, provider):
    """An absent signal id must not raise: the alternative to a degraded event
    context is no decision row at all, and the live hook would swallow the
    exception."""
    await _call(db, n=1, posted_at=T0 - timedelta(hours=6), token_id="alpha-coin")

    features = provider.features(CHANNEL, T0, 987654)

    assert features["event_duplicate_rank_in_cluster"] == 1
    assert features["event_cluster_size"] == 0
    assert features["event_identity_kind"] == "unknown"


async def test_other_channels_history_does_not_leak_in(db, provider):
    await _call(
        db,
        n=1,
        posted_at=T0 - timedelta(hours=6),
        token_id="alpha-coin",
        channel=OTHER_CHANNEL,
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")

    features = provider.features(CHANNEL, T0, current)

    assert features["history_raw_calls"] == 0


async def test_provider_writes_nothing(db, provider):
    """Stage B is read-only. `mode=ro` makes that structural, but the property
    worth asserting is the observable one: no row count moves."""
    await _priced_call(
        db,
        n=1,
        posted_at=T0 - timedelta(hours=3),
        token_id="dex:solana:old",
        contract="OLD",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")

    tables = (
        "tg_social_messages",
        "tg_social_signals",
        "source_call_price_snapshots",
        "tg_act_shadow",
        "tg_act_shadow_generations",
        "tg_social_health",
    )

    async def _counts() -> dict[str, int]:
        out = {}
        for table in tables:
            cur = await db._conn.execute(f"SELECT COUNT(*) FROM {table}")
            out[table] = (await cur.fetchone())[0]
        return out

    before = await _counts()
    provider.features(CHANNEL, T0, current)
    assert await _counts() == before


def test_provider_connection_refuses_writes(provider):
    """The structural half: SQLite itself rejects a write on this handle."""
    import sqlite3

    conn = provider._connect()
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE should_not_exist (x)")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Identity + window conventions (drift alarms)
# ---------------------------------------------------------------------------


def test_forward_windows_match_the_ledger_convention():
    """The local copy exists so the gate fingerprint binds it. This asserts the
    copy has not FORKED: if the ledger's windows move, Stage B's evidence
    claims stop meaning what the ledger's do, and that must be a deliberate,
    fingerprint-splitting edit rather than a silent divergence."""
    from scout.source_quality import ledger

    assert {
        key.replace("forward_", "").replace("_pct", ""): value
        for key, value in ledger.WINDOWS.items()
    } == FORWARD_WINDOWS
    assert PRICE_AT_CALL_TOLERANCE_SEC == ledger.PRICE_AT_CALL_TOLERANCE_SEC


@pytest.mark.parametrize(
    "chain, contract, token_id",
    [
        ("solana", "So111", "dex:solana:So111"),
        ("ethereum", "0xABC", "dex:ethereum:0xABC"),
        # Mixed-case chain: the writer does NOT lowercase the chain segment,
        # only the contract. Every all-lowercase case above passes whether or
        # not this module lowercases it too, so this row is the one that
        # actually pins the format.
        ("Solana", "So111", "dex:solana:So111"),
        (None, "0xABC", "some-coin"),
        ("solana", None, "bitcoin"),
        (None, None, "bitcoin"),
    ],
)
def test_snapshot_identity_matches_the_writers_key_format(chain, contract, token_id):
    """The snapshot writer stores keys produced by `ledger._priceable_identity`.
    A divergence in the KEY would look up nothing and read as a caller with no
    track record — a wrong decision that looks exactly like a right one.

    The KIND legitimately differs: the ledger's `token_id` is a routing value
    meaning "price from the gainers/losers board", while
    `source_call_price_snapshots` names the same thing `coin_id`. Pinning the
    mapping is what keeps that difference deliberate.
    """
    from scout.source_quality.ledger import _priceable_identity

    row = {"chain": chain, "contract_address": contract, "token_id": token_id}
    ledger_identity = _priceable_identity(row)
    mine = snapshot_identity(chain=chain, contract_address=contract, token_id=token_id)
    if ledger_identity is None:
        assert mine is None
        return
    ledger_kind, ledger_key = ledger_identity
    assert mine is not None
    assert mine[1] == ledger_key
    assert mine[0] == {"token_id": "coin_id", "contract": "contract"}[ledger_kind]


async def test_cg_identity_observations_are_read_under_the_coin_id_kind(db, provider):
    """CROSS-PR CONTRACT with the CG `coin_id` substrate PR.

    The substrate writes CG-identity observations as `identity_kind='coin_id'`
    — the storage kind, and the only CG kind the table's CHECK permits. The
    ledger's `'token_id'` is an IN-MEMORY routing value that never appears in
    this table. A read filtering `'token_id'` would return zero rows with no
    error, which looks exactly like "the substrate hasn't written yet".

    This is end-to-end on purpose: it seeds `coin_id` rows and requires a real
    forward return to come out. A unit assertion on the kind string would still
    pass if the SQL filtered something else.

    The literal `'coin_id'` is spelled out here because the substrate PR is not
    merged and its constants cannot be imported; dedup onto one shared constant
    when it lands.
    """
    posted = T0 - timedelta(hours=3)
    await _call(db, n=1, posted_at=posted, token_id="some-real-coin")
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")
    # Substrate-shaped rows: keyed by the bare CG coin id, kind 'coin_id'.
    await _snapshot(
        db,
        identity_key="some-real-coin",
        identity_kind="coin_id",
        price=100.0,
        snapshot_at=posted,
        chain=None,
    )
    await _snapshot(
        db,
        identity_key="some-real-coin",
        identity_kind="coin_id",
        price=125.0,
        snapshot_at=posted + timedelta(minutes=35),
        chain=None,
    )

    features = provider.features(CHANNEL, T0, current)

    assert features["history_avg_forward_30m_pct"] == pytest.approx(25.0)
    assert features["history_eligible_distinct_clusters"] == 1
    assert features["history_priceable_coverage_rate"] == 1.0
    # The substrate gap counter must now read empty for this cluster.
    assert features["history_clusters_without_observations"] == 0


def test_cg_identity_kind_is_the_storage_kind_not_the_ledgers_routing_kind():
    """The contract in one assertion, so a rename cannot pass review quietly."""
    assert snapshot_identity(
        chain=None, contract_address=None, token_id="some-real-coin"
    ) == ("coin_id", "some-real-coin")
    kind, _ = snapshot_identity(
        chain=None, contract_address=None, token_id="some-real-coin"
    )
    assert kind != "token_id", (
        "'token_id' is the ledger's in-memory routing kind and never appears in "
        "source_call_price_snapshots storage — filtering on it silently returns "
        "zero rows"
    )


async def test_snapshot_identity_kinds_are_accepted_by_the_table(db):
    """The kind strings must satisfy the table's real CHECK constraint. A kind
    outside it would make every lookup for that identity class return nothing,
    silently, forever."""
    for kind in ("coin_id", "contract"):
        await db._conn.execute(
            "INSERT INTO source_call_price_snapshots "
            "(identity_key, identity_kind, chain, price, snapshot_at, source) "
            "VALUES ('k', ?, 'solana', 1.0, ?, 'gt')",
            (kind, T0.isoformat()),
        )
    await db._conn.commit()

    produced = {
        snapshot_identity(
            chain="solana", contract_address="abc", token_id="dex:solana:abc"
        )[0],
        snapshot_identity(chain=None, contract_address=None, token_id="bitcoin")[0],
    }
    assert produced == {"contract", "coin_id"}


# Representative token_id shapes: the `dex:` pseudo-ids the TG resolver and the
# GT new-pools lane mint, genuine CG slugs, EVM addresses, a real Solana base58
# mint, and the edge shapes each clause of the heuristic exists to reject.
_CG_ID_SHAPES = [
    # dex: pseudo-ids — must NOT be coin ids, or every CA-bearing TG call would
    # route to a structurally dead coin_id lookup.
    "dex:solana:So11111111111111111111111111111111111111112",
    "dex:ethereum:0xabc",
    "dex:robinhood:0xff",
    "dex:",
    # genuine CG slugs
    "bitcoin",
    "some-real-coin",
    "wrapped_bitcoin",
    "pepe",
    "a",
    "spy-bstocks-tokenized-stock",
    "1inch",
    # EVM contract addresses
    "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "0xabc",
    "0x",
    # Solana base58 mint — caught by the mixed-case clause, not by length
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    # edge shapes
    None,
    "",
    "   ",
    "UPPER",
    "MiXeD-case",
    "has space",
    "has.dot",
    "has:colon",
    "x" * 60,
    "x" * 61,
]


@pytest.mark.parametrize("token_id", _CG_ID_SHAPES)
def test_cg_coin_id_replica_matches_the_shared_predicate(token_id):
    """DRIFT ALARM for the bound replica of `token_ids.is_cg_coin_id`.

    The replica exists because this classification is decision semantics and
    the gate fingerprint binds only `caller_features.py`'s source: reached
    through the import, an upstream refinement would silently reclassify
    identities under an UNCHANGED gate_version.

    If this test fails, the shared heuristic was refined and the replica did
    not follow. That is intended behaviour, not a defect in this test — someone
    must consciously re-ratify the replica and accept that doing so splits the
    gate_version and starts a new evidence generation.
    """
    from scout.social.telegram.caller_features import _is_cg_coin_id
    from scout.token_ids import is_cg_coin_id

    assert _is_cg_coin_id(token_id) == is_cg_coin_id(token_id), (
        f"replica drifted from scout.token_ids.is_cg_coin_id on {token_id!r} — "
        "re-ratify the replica deliberately; it splits gate_version"
    )


def test_cg_coin_id_replica_covers_every_clause():
    """The shape set above must actually exercise all four rejection clauses,
    or the drift test could pass while a clause is missing from the replica."""
    from scout.social.telegram.caller_features import _is_cg_coin_id

    assert _is_cg_coin_id("0xabc") is False, "0x clause"
    assert _is_cg_coin_id("x" * 61) is False, "length clause"
    assert _is_cg_coin_id("MiXeD-case") is False, "mixed-case clause"
    assert _is_cg_coin_id("has:colon") is False, "charset clause"
    assert _is_cg_coin_id("bitcoin") is True, "genuine slug still accepted"


def test_module_source_hash_is_line_ending_independent(tmp_path):
    """I4 — a CRLF working copy and an LF checkout are the SAME commit.

    Hashing raw bytes gave them different digests, so a locally recomputed
    gate_version could never match production's and replay verification was
    impossible. Free to fix while zero generations exist; after activation it
    would retire every accumulated cohort.
    """
    from scout.social.telegram.caller_features import hash_module_source

    body = "def f():\n    return 1\n"
    lf = tmp_path / "lf.py"
    crlf = tmp_path / "crlf.py"
    lf.write_bytes(body.encode())
    crlf.write_bytes(body.replace("\n", "\r\n").encode())
    assert lf.read_bytes() != crlf.read_bytes(), "fixture built no real difference"

    assert hash_module_source(lf) == hash_module_source(crlf)


def test_every_fingerprint_bound_module_normalizes_identically():
    """All three bound modules must agree on what "the same source" means; a
    single un-normalized site would reintroduce the split for that module."""
    from scout.social.telegram import shadow, snapshot
    from scout.social.telegram import caller_features as cf

    for module in (cf, shadow, snapshot):
        path = Path(module.__file__)
        expected = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        assert module.module_source_hash() == expected, module.__name__


def test_the_two_hash_helpers_agree(tmp_path):
    """`caller_features` keeps its own copy to stay import-free; this stops the
    two copies drifting apart."""
    from scout.social.telegram.caller_features import hash_module_source as a
    from scout.social.telegram.snapshot import hash_module_source as b

    sample = tmp_path / "s.py"
    sample.write_bytes(b"x = 1\r\ny = 2\n")
    assert a(sample) == b(sample)


async def test_cluster_representative_is_the_earliest_call_not_the_last(db, provider):
    """I3(a) — the rank-1 call represents its cluster.

    A multi-call cluster where the calls have DIFFERENT priced outcomes: the
    reported forward return must be the FIRST call's. A last-wins
    representative would silently score a caller on their re-emphasis rather
    than on the call that actually carried information.
    """
    first = T0 - timedelta(hours=6)
    second = first + timedelta(hours=1)
    key = _contract_key("solana", "ABC")
    for n, posted in ((1, first), (2, second)):
        await _call(
            db,
            n=n,
            posted_at=posted,
            token_id="dex:solana:abc",
            contract="ABC",
            chain="solana",
        )
    # First call: 100 -> 110 (+10%). Second call: 200 -> 300 (+50%).
    await _snapshot(db, identity_key=key, price=100.0, snapshot_at=first)
    await _snapshot(
        db, identity_key=key, price=110.0, snapshot_at=first + timedelta(minutes=35)
    )
    await _snapshot(db, identity_key=key, price=200.0, snapshot_at=second)
    await _snapshot(
        db, identity_key=key, price=300.0, snapshot_at=second + timedelta(minutes=35)
    )
    current = await _call(db, n=9, posted_at=T0, token_id="beta-coin")

    features = provider.features(CHANNEL, T0, current)

    assert features["history_distinct_clusters"] == 1
    assert features["history_forward_30m_sample_count"] == 1
    assert features["history_avg_forward_30m_pct"] == pytest.approx(10.0), (
        "cluster representative is not the earliest call — a caller is being "
        "scored on their re-emphasis instead of their original call"
    )


async def test_the_two_coverage_denominators_can_differ(db, provider):
    """I3(b) — the #482 dual-denominator point, asserted where it bites.

    One mature PRICEABLE cluster with a forward return, plus one mature
    UNPRICEABLE cluster (cashtag only). `coverage_rate` counts the unpriceable
    call — that is the honest all-call diagnostic — while
    `priceable_coverage_rate` does not, because a call we could never price
    contributes no bias about our forward data. Nothing previously forced these
    two apart, so a single shared denominator would have passed every test.
    """
    await _priced_call(
        db,
        n=1,
        posted_at=T0 - timedelta(hours=3),
        token_id="dex:solana:old",
        contract="OLD",
    )
    await _message(db, pk=2, posted_at=T0 - timedelta(hours=2))
    await db._conn.execute(
        """INSERT INTO tg_social_signals
           (id, message_pk, token_id, symbol, contract_address, chain,
            mcap_at_sighting, resolution_state, source_channel_handle, created_at)
           VALUES (2, 2, 'PEPE', 'PEPE', NULL, NULL, 1.0, 'RESOLVED', ?, ?)""",
        (CHANNEL, (T0 - timedelta(hours=2)).isoformat()),
    )
    await db._conn.commit()
    current = await _call(db, n=3, posted_at=T0, token_id="beta-coin")

    features = provider.features(CHANNEL, T0, current)

    assert features["history_mature_clusters"] == 2
    assert features["history_mature_priceable_clusters"] == 1
    assert features["history_eligible_distinct_clusters"] == 1
    assert features["history_coverage_rate"] == pytest.approx(0.5)
    assert features["history_priceable_coverage_rate"] == 1.0
    assert (
        features["history_coverage_rate"] != features["history_priceable_coverage_rate"]
    )


async def test_corroboration_counts_channels_not_calls(db, provider):
    """I3(c) — one channel calling the same identity twice in a day is ONE
    corroborating channel. Counting calls would let a single spammer
    manufacture the appearance of independent confirmation."""
    for n, offset in ((1, timedelta(hours=3)), (2, timedelta(hours=2))):
        await _call(
            db,
            n=n,
            posted_at=T0 - offset,
            token_id="dex:solana:abc",
            contract="ABC",
            chain="solana",
            channel=OTHER_CHANNEL,
        )
    current = await _call(
        db, n=3, posted_at=T0, token_id="dex:solana:abc", contract="abc", chain="solana"
    )

    features = provider.features(CHANNEL, T0, current)

    assert (
        features["event_corroborating_channels"] == 2
    ), "expected the scored channel plus ONE other, not one per call"


async def test_excursion_requires_a_fresh_anchor(db, provider):
    """R-a — the excursion's anchor-age gate, previously uncovered.

    Max favorable/adverse is a percentage against the entry anchor, so an
    anchor an hour-plus stale prices a different moment than the call and the
    excursion would describe a move the caller never had. This gate is also
    this module's one deliberate divergence from the ledger's axis (the ledger
    checks it only inside its own extrema branch), which is exactly why it
    needs its own coverage.
    """
    posted = T0 - timedelta(hours=25)
    await _call(
        db,
        n=1,
        posted_at=posted,
        token_id="dex:solana:old",
        contract="OLD",
        chain="solana",
    )
    current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")
    key = _contract_key("solana", "OLD")
    # Anchor is 70 minutes stale — beyond the 60-minute excursion tolerance.
    await _snapshot(
        db, identity_key=key, price=100.0, snapshot_at=posted - timedelta(minutes=70)
    )
    await _snapshot(
        db, identity_key=key, price=150.0, snapshot_at=posted + timedelta(hours=2)
    )

    features = provider.features(CHANNEL, T0, current)

    assert features["history_avg_max_favorable_pct_24h"] is None
    assert features["history_excursion_sample_count"] == 0


def test_feature_schema_types_match_the_values_produced(db, provider):
    """R-d — the schema's type column is fingerprint-bound, so it must be TRUE.

    It was decorative: nothing asserted a declared type matched the value
    produced, so a future field could bind a false declaration into
    gate_version and no test would notice.
    """
    import asyncio

    async def _build() -> dict:
        await _priced_call(
            db,
            n=1,
            posted_at=T0 - timedelta(hours=30),
            token_id="dex:solana:old",
            contract="OLD",
        )
        current = await _call(db, n=2, posted_at=T0, token_id="beta-coin")
        return provider.features(CHANNEL, T0, current)

    features = asyncio.get_event_loop().run_until_complete(_build())

    checkers = {
        "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "float": lambda v: isinstance(v, float),
        "float|null": lambda v: v is None or isinstance(v, float),
        "str": lambda v: isinstance(v, str),
    }
    for name, declared in FEATURE_SCHEMA:
        assert declared in checkers, f"undeclared type name {declared!r} for {name}"
        assert checkers[declared](features[name]), (
            f"{name} declares {declared!r} but produced {features[name]!r} "
            f"({type(features[name]).__name__})"
        )


async def _sentinel_call(
    db: Database, *, n: int, posted_at: datetime, channel: str = CHANNEL
) -> int:
    """A resolution FAILURE row, exactly as `listener.py` writes it: the
    `(unresolved)` placeholder in both token_id and symbol, no contract."""
    await _message(db, pk=n, posted_at=posted_at, channel=channel)
    await db._conn.execute(
        """INSERT INTO tg_social_signals
           (id, message_pk, token_id, symbol, contract_address, chain,
            mcap_at_sighting, resolution_state, source_channel_handle, created_at)
           VALUES (?, ?, '(unresolved)', '(unresolved)', NULL, NULL, NULL,
                   'UNRESOLVED_TRANSIENT', ?, ?)""",
        (n, n, channel, posted_at.isoformat()),
    )
    await db._conn.commit()
    return n


async def test_adding_100_sentinels_moves_only_the_diagnostic(db, provider):
    """OPERATOR DISCRIMINATOR (Ruling 1).

    Adding 100 unresolved sentinel calls to a channel MUST change the
    unresolved/coverage diagnostic and MUST NOT change duplicate_rate, the
    distinct canonical-cluster count, or corroboration.

    Without the exclusion every sentinel shares the single `(unresolved)`
    canonical identity, so they collapse into ONE cluster per UTC day: the
    cluster count moves by 1 and duplicate_rate rockets toward 1.0 while the
    caller has not repeated a single token. In prod that is 866 rows.
    """
    # Real behaviour: two genuine calls of the SAME token today (a true
    # duplicate) plus one other token, so duplicate_rate is non-trivial.
    base = T0 - timedelta(hours=6)
    for n, offset in ((1, timedelta(0)), (2, timedelta(minutes=5))):
        await _call(
            db,
            n=n,
            posted_at=base + offset,
            token_id="dex:solana:abc",
            contract="ABC",
            chain="solana",
        )
    await _call(db, n=3, posted_at=base + timedelta(minutes=10), token_id="other-coin")
    current = await _call(
        db, n=4, posted_at=T0, token_id="dex:solana:cur", contract="CUR", chain="solana"
    )
    await _call(
        db,
        n=5,
        posted_at=T0 - timedelta(hours=1),
        token_id="dex:solana:cur",
        contract="CUR",
        chain="solana",
        channel=OTHER_CHANNEL,
    )

    before = provider.features(CHANNEL, T0, current)

    for i in range(100):
        await _sentinel_call(db, n=1000 + i, posted_at=base + timedelta(minutes=20 + i))

    after = provider.features(CHANNEL, T0, current)

    # MUST NOT move — caller behaviour.
    assert after["history_duplicate_rate"] == before["history_duplicate_rate"]
    assert after["history_distinct_clusters"] == before["history_distinct_clusters"]
    assert (
        after["history_eligible_distinct_clusters"]
        == before["history_eligible_distinct_clusters"]
    )
    assert (
        after["event_corroborating_channels"] == before["event_corroborating_channels"]
    )
    assert after["event_duplicate_rank_in_cluster"] == (
        before["event_duplicate_rank_in_cluster"]
    )
    # MUST move — resolution-quality diagnostic and the total funnel.
    assert after["history_unresolved_identity_calls"] == 100
    assert before["history_unresolved_identity_calls"] == 0
    assert after["history_unresolved_identity_rate"] > (
        before["history_unresolved_identity_rate"]
    )
    assert after["history_raw_calls"] == before["history_raw_calls"] + 100
    assert after["history_identified_calls"] == before["history_identified_calls"]


async def test_duplicate_rate_measures_tokens_not_resolver_failures(db, provider):
    """The concrete arithmetic the ruling protects.

    Three identified calls, two of them the same token: 2 clusters / 3 calls
    -> duplicate_rate 1/3. Adding 9 sentinels must leave that exactly 1/3. On
    the raw-funnel denominator it would read 1 - 2/12 = 0.833.
    """
    base = T0 - timedelta(hours=6)
    for n, offset in ((1, timedelta(0)), (2, timedelta(minutes=5))):
        await _call(
            db,
            n=n,
            posted_at=base + offset,
            token_id="dex:solana:abc",
            contract="ABC",
            chain="solana",
        )
    await _call(db, n=3, posted_at=base + timedelta(minutes=10), token_id="other-coin")
    for i in range(9):
        await _sentinel_call(db, n=200 + i, posted_at=base + timedelta(minutes=30 + i))
    current = await _call(db, n=4, posted_at=T0, token_id="beta-coin")

    features = provider.features(CHANNEL, T0, current)

    assert features["history_identified_calls"] == 3
    assert features["history_distinct_clusters"] == 2
    assert features["history_duplicate_rate"] == pytest.approx(1 / 3)
    assert features["history_unresolved_identity_rate"] == pytest.approx(9 / 12)


async def test_sentinels_do_not_corroborate(db, provider):
    """Two channels failing to resolve on the same day agree about NOTHING —
    there is no identity for them to agree about."""
    await _sentinel_call(
        db, n=1, posted_at=T0 - timedelta(hours=1), channel=OTHER_CHANNEL
    )
    current = await _sentinel_call(db, n=2, posted_at=T0)

    features = provider.features(CHANNEL, T0, current)

    assert features["event_corroborating_channels"] == 0


async def test_sentinels_cannot_match_the_corroboration_query(db, provider):
    """Pins the structural property that keeps sentinels out of corroboration.

    `_corroborating_channels` carries no sentinel filter because one would be
    unreachable: the contract branch cannot match rows with a NULL
    contract_address, and the token_id branch matches a genuine identity that
    the `(unresolved)` placeholder never equals. That is a property of the
    QUERY SHAPES, so it is what gets pinned — a guard in the loop would be dead
    code implying a protection it does not provide.

    A channel that only ever failed to resolve therefore corroborates nothing,
    even while another channel is making a real call the same day.
    """
    # Sentinels from a second channel, same day as the current genuine call.
    for i in range(5):
        await _sentinel_call(
            db,
            n=300 + i,
            posted_at=T0 - timedelta(hours=2) + timedelta(minutes=i),
            channel=OTHER_CHANNEL,
        )
    current = await _call(
        db, n=1, posted_at=T0, token_id="dex:solana:cur", contract="CUR", chain="solana"
    )

    features = provider.features(CHANNEL, T0, current)

    assert features["event_corroborating_channels"] == 1, (
        "only the scored channel should count — sentinels from another channel "
        "are not corroboration of anything"
    )

    # And the same for a no-contract genuine identity (the token_id branch).
    current2 = await _call(db, n=2, posted_at=T0, token_id="genuine-slug")
    features2 = provider.features(CHANNEL, T0, current2)
    assert features2["event_corroborating_channels"] == 1


async def test_sentinel_current_signal_reports_no_event_context(db, provider):
    """Documented answer for a sentinel CURRENT signal (Ruling 1 item 4).

    Unreachable in production — the shadow evaluates only RESOLVED rows and no
    sentinel row is RESOLVED — but the provider answers whatever id it is
    given. It reports rank 1 / cluster 0 / corroboration 0, the same shape as
    an absent signal, with a truthful identity kind of its own so the state is
    distinguishable in `features_json` rather than silently looking like a
    clean first call.
    """
    await _sentinel_call(db, n=1, posted_at=T0 - timedelta(hours=2))
    current = await _sentinel_call(db, n=2, posted_at=T0)

    features = provider.features(CHANNEL, T0, current)

    assert features["event_duplicate_rank_in_cluster"] == 1
    assert features["event_cluster_size"] == 0
    assert features["event_corroborating_channels"] == 0
    assert features["event_identity_kind"] == "unresolved_identity"


async def test_sentinels_never_reach_cluster_representative_or_min_n(db, provider):
    """A channel with ONLY sentinels has no track record at all — not a
    one-cluster record that could creep toward the min-N gate."""
    for i in range(20):
        await _sentinel_call(
            db, n=100 + i, posted_at=T0 - timedelta(hours=5) + timedelta(minutes=i)
        )
    current = await _call(db, n=1, posted_at=T0, token_id="beta-coin")

    features = provider.features(CHANNEL, T0, current)

    assert features["history_raw_calls"] == 20
    assert features["history_identified_calls"] == 0
    assert features["history_distinct_clusters"] == 0
    assert features["history_eligible_distinct_clusters"] == 0
    assert features["history_duplicate_rate"] == 0.0
    assert features["history_unresolved_identity_rate"] == 1.0


def test_sentinel_literal_still_matches_the_listener():
    """DRIFT ALARM. The placeholder is declared in this module (fingerprint
    binding) but WRITTEN by `listener.py`. If the listener's literal changes
    and this one does not, the exclusion silently stops matching and every
    unresolved row floods back into caller behaviour under an unchanged
    gate_version — the failure mode this ruling exists to remove.
    """
    from scout.social.telegram.caller_features import UNRESOLVED_IDENTITY_SENTINEL

    listener_src = (
        Path(__file__).resolve().parents[1]
        / "scout"
        / "social"
        / "telegram"
        / "listener.py"
    ).read_text(encoding="utf-8")

    assert f'token_id="{UNRESOLVED_IDENTITY_SENTINEL}"' in listener_src, (
        "listener.py no longer writes this placeholder into token_id — the "
        "sentinel exclusion is now matching nothing"
    )
    assert f'symbol="{UNRESOLVED_IDENTITY_SENTINEL}"' in listener_src


def test_canonical_identity_is_channel_independent_and_normalized():
    a = canonical_identity(
        chain="Solana", contract_address="ABC123", token_id="dex:solana:ABC123"
    )
    b = canonical_identity(
        chain="solana", contract_address="abc123", token_id="some-other-slug"
    )
    assert a == b == "solana:abc123"
    # No contract: falls back to the token_id, mirroring the ledger's
    # cluster-identity convention.
    assert (
        canonical_identity(chain=None, contract_address=None, token_id="bitcoin")
        == "bitcoin"
    )


def test_dex_pseudo_ids_are_not_treated_as_coin_ids():
    """`is_cg_coin_id` is the shared identity convention this module depends on.
    If a `dex:` pseudo-id were classified as a CG coin id, every CA-bearing TG
    call would route to a structurally dead `coin_id` lookup."""
    assert snapshot_identity(
        chain="solana", contract_address="So111", token_id="dex:solana:So111"
    ) == ("contract", "solana|so111")
    assert snapshot_identity(chain=None, contract_address=None, token_id="bitcoin") == (
        "coin_id",
        "bitcoin",
    )
