"""Regression tests for the review blockers found AFTER #555 shipped.

Three independent reviewers converged on the same defects in a PR that had
already merged and deployed. Each test below is named for the defect it pins,
and each was confirmed to fail against the shipped code before the fix.

The unifying lesson: #555's structural guard was a literal-substring scan, so
it reported "no consumer still derives first-seen from signal_events" while six
derivations sat in the scanned trees. A guard that cannot see the phrasings
actually used in the codebase is worse than no guard, because it converts an
unchecked assumption into a passing test.
"""

from datetime import datetime, timedelta, timezone

import pytest

from scout.chains.events import emit_event
from scout.config import Settings
from scout.db import Database
from scout.trending.tracker import _check_detector


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


def _s(**over):
    base = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="t", ANTHROPIC_API_KEY="t")
    base.update(over)
    return Settings(**base)


async def _raw_event(db, token_id, created_at):
    await db._conn.execute(
        """INSERT INTO signal_events
           (token_id, pipeline, event_type, event_data, source_module, created_at)
           VALUES (?, 'memecoin', 'candidate_scored', '{}', 'scorer', ?)""",
        (token_id, created_at),
    )
    await db._conn.commit()


# ---------------------------------------------------------------------------
# D1 — the trending short-symbol branch was never migrated
# ---------------------------------------------------------------------------


async def test_short_symbol_detection_survives_signal_events_pruning(db):
    """THE shipped defect. Short symbols are BTC, ETH, SOL, XRP — not marginal.

    #555 migrated the `len(symbol) >= 4` branch and left the `else` twenty
    lines below reading signal_events. The result was one function deriving
    first-seen from two different historical boundaries depending on symbol
    LENGTH — harder to detect than the uniform coupling the substrate removes,
    because neither site looks wrong on its own.
    """
    old = "2026-06-01T00:00:00+00:00"
    await _raw_event(db, "pod", old)
    await db.record_signal_first_seen("pod", old)
    await db._conn.commit()

    trending_at = "2026-08-01T00:00:00+00:00"
    before = await _check_detector(
        db,
        "signal_first_seen",
        "token_id",
        "pod",
        "POD",
        trending_at,
        symbol_col="token_id",
    )
    assert before[0] is True

    # Retention prunes the event; the substrate is not pruned.
    await db._conn.execute(
        "DELETE FROM signal_events WHERE created_at < ?", ("2026-07-20T00:00:00+00:00",)
    )
    await db._conn.commit()

    after = await _check_detector(
        db,
        "signal_first_seen",
        "token_id",
        "pod",
        "POD",
        trending_at,
        symbol_col="token_id",
    )
    assert after[0] is True, "short-symbol detection was lost to retention"
    assert after[1] == before[1], "short-symbol lead time moved with retention"

    # The un-migrated form no longer even resolves: with the dead
    # "signal_events" default removed, a caller trying to re-couple falls
    # through to "detected_at", which signal_events does not have, and fails
    # LOUDLY instead of silently returning a retention-truncated answer.
    import sqlite3

    with pytest.raises(sqlite3.OperationalError, match="detected_at"):
        await _check_detector(
            db,
            "signal_events",
            "token_id",
            "pod",
            "POD",
            trending_at,
            symbol_col="token_id",
        )

    # And with its real column supplied, it genuinely loses the detection —
    # proving this test is not vacuously passing on a path that never mattered.
    stale = await _check_detector(
        db,
        "signal_events",
        "token_id",
        "pod",
        "POD",
        trending_at,
        symbol_col="token_id",
        timestamp_col="created_at",
    )
    assert stale[0] is False


def test_the_detector_has_no_signal_events_default_left():
    """Removing the mapping is what makes a re-coupling fail loudly."""
    import inspect

    from scout.trending import tracker

    src = inspect.getsource(tracker._check_detector)
    assert '"signal_' + 'events": "created_at"' not in src


# ---------------------------------------------------------------------------
# F2 — token_id='' committed an event with no substrate row
# ---------------------------------------------------------------------------


async def test_empty_token_id_is_refused_rather_than_silently_skipped(db):
    """`signal_events.token_id` is TEXT NOT NULL, but '' satisfies that.

    The shipped writer returned silently on a falsy token_id, so the event
    committed and the fold did not — the exact divergence the substrate exists
    to prevent, invisible because the write "succeeded".
    """
    with pytest.raises(ValueError, match="non-empty token_id"):
        await db.record_signal_first_seen("", "2026-08-01T00:00:00+00:00")
    with pytest.raises(ValueError):
        await db.record_signal_first_seen("tok", "")


async def test_an_empty_token_id_row_would_poison_every_consumer(db):
    """Why '' is not merely untidy — it matches EVERY coin and MIN makes it win.

    Consumers match with `LOWER(?) LIKE LOWER(token_id || '%')`, which for ''
    degenerates to LIKE '%'. Nothing prunes this table, so such a row would be
    permanent where retention used to age it out.
    """
    await db._conn.execute(
        "INSERT INTO signal_first_seen (token_id, first_seen_at, updated_at) "
        "VALUES ('', '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')"
    )
    await db.record_signal_first_seen("pepe", "2026-08-05T00:00:00+00:00")
    await db._conn.commit()

    cur = await db._conn.execute(
        """SELECT MIN(first_seen_at) FROM signal_first_seen
           WHERE (token_id = ? OR LOWER(?) LIKE LOWER(token_id || '%'))""",
        ("pepe", "pepe"),
    )
    poisoned = (await cur.fetchone())[0]
    assert (
        poisoned == "2020-01-01T00:00:00+00:00"
    ), "fixture failed to reproduce the poisoning it is meant to demonstrate"

    # Reconciliation sweeps it.
    result = await db.reconcile_signal_first_seen()
    assert result["poisoned_removed"] == 1
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM signal_first_seen WHERE token_id = ''"
    )
    assert (await cur.fetchone())[0] == 0


# ---------------------------------------------------------------------------
# F1/F3 — reconciliation repairs what the write path cannot guarantee
# ---------------------------------------------------------------------------


async def test_reconciliation_restores_a_lost_fold(db):
    """F1: a foreign rollback on the shared connection can discard the fold.

    `scout/chains/tracker.py` opens its own BEGIN on `db._conn` and rolls back
    on failure — which happens several times a day in production. A concurrent
    emit whose fold is pending loses it, then commits its own event insert.
    No savepoint or write ordering fixes that, so the substrate is kept correct
    by repair.
    """
    await _raw_event(db, "orphan", "2026-08-01T00:00:00+00:00")
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM signal_first_seen WHERE token_id='orphan'"
    )
    assert (await cur.fetchone())[0] == 0, "fixture did not create an orphan"

    result = await db.reconcile_signal_first_seen()
    assert result["missing"] == 1
    assert result["repaired"] == 1
    cur = await db._conn.execute(
        "SELECT first_seen_at FROM signal_first_seen WHERE token_id='orphan'"
    )
    assert (await cur.fetchone())[0] == "2026-08-01T00:00:00+00:00"


async def test_reconciliation_lowers_a_late_value_but_never_raises_one(db):
    """The property that makes repair safe to run unconditionally."""
    await _raw_event(db, "t", "2026-08-01T00:00:00+00:00")
    # A late substrate value, as a lost-then-refolded event would produce.
    await db.record_signal_first_seen("t", "2026-08-10T00:00:00+00:00")
    await db._conn.commit()

    result = await db.reconcile_signal_first_seen()
    assert result["late"] == 1
    cur = await db._conn.execute(
        "SELECT first_seen_at FROM signal_first_seen WHERE token_id='t'"
    )
    assert (await cur.fetchone())[0] == "2026-08-01T00:00:00+00:00"

    # An EARLIER substrate value than any surviving event must survive repair —
    # that is the whole decoupling, and a repair that raised it would undo it.
    await db.record_signal_first_seen("t", "2026-07-01T00:00:00+00:00")
    await db._conn.commit()
    await db.reconcile_signal_first_seen()
    cur = await db._conn.execute(
        "SELECT first_seen_at FROM signal_first_seen WHERE token_id='t'"
    )
    assert (await cur.fetchone())[
        0
    ] == "2026-07-01T00:00:00+00:00", (
        "reconciliation raised a minimum that predates surviving history"
    )


async def test_reconciliation_repairs_a_truncated_table(db):
    """F3: the one-shot migration early-returns forever once its marker exists.

    So a truncated or partially-restored substrate would never rebuild, and
    every token's first_seen would silently become its first post-truncation
    event.
    """
    await emit_event(db, "0xabc", "memecoin", "candidate_scored", {}, "scorer")
    await db._conn.execute("DELETE FROM signal_first_seen")
    await db._conn.commit()

    await db.reconcile_signal_first_seen()
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM signal_first_seen WHERE token_id='0xabc'"
    )
    assert (await cur.fetchone())[0] == 1


async def test_reconciliation_is_idempotent(db):
    await _raw_event(db, "t", "2026-08-01T00:00:00+00:00")
    first = await db.reconcile_signal_first_seen()
    second = await db.reconcile_signal_first_seen()
    assert first["repaired"] == 1
    assert second["repaired"] == 0, "a clean run must report no drift"


# ---------------------------------------------------------------------------
# S2-B — the last un-migrated consumer's window must stay covered
# ---------------------------------------------------------------------------


def test_prospective_lookback_floor():
    """`prospective.py` stays exempt only while retention covers its lookback.

    Before this, the two knobs merely COINCIDED at 14.
    """
    assert _s().CHAIN_EVENT_RETENTION_DAYS >= _s().CONVICTION_PROSPECTIVE_LOOKBACK_DAYS
    with pytest.raises(ValueError, match="CONVICTION_PROSPECTIVE_LOOKBACK_DAYS"):
        _s(CHAIN_EVENT_RETENTION_DAYS=7)
    ok = _s(CHAIN_EVENT_RETENTION_DAYS=7, CONVICTION_PROSPECTIVE_LOOKBACK_DAYS=7)
    assert ok.CHAIN_EVENT_RETENTION_DAYS == 7


# ---------------------------------------------------------------------------
# §12a — the derived table needs a freshness surface
# ---------------------------------------------------------------------------


def test_signal_first_seen_has_a_freshness_registry_entry():
    """A substrate that stops updating still answers every query plausibly.

    That is precisely the failure no consumer can see, so it has to be
    monitored at the table.
    """
    import inspect

    from dashboard import db as dash_db

    src = inspect.getsource(dash_db)
    assert '("signal_first_seen", "updated_at")' in src
