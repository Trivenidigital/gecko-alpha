"""The trade board's entry basis must be one immutable observation.

The defect
----------
The tracker cohort computed ``pct_from_entry`` from
``gainers_comparisons.detected_price`` and reported ``opened_at`` from
``gainers_comparisons.appeared_on_gainers_at``. Those describe DIFFERENT events
by construction:

* ``appeared_on_gainers_at`` is ``MIN(snapshot_at)`` over a **rolling 24-hour
  window** (``scout/gainers/tracker.py``: ``WHERE datetime(snapshot_at) >=
  datetime('now','-24 hours')``), recomputed on every run — so it slides forward
  daily and is not a first sighting.
* ``detected_price`` is ``price_cache.current_price`` sampled on whichever later
  run first found a cached price, then preserved forever.

Comparing a price from one moment against a timestamp from another made the
lateness guards unreachable: ``already_ran`` / ``late`` / ``closed`` could not
fire, and rows that had already run showed as fresh.

The basis
---------
``gainers_snapshots`` is append-only — INSERT plus a retention DELETE, with no
UPDATE/REPLACE anywhere in the repo and no UNIQUE constraint for an UPSERT to
target. Its ``price_at_snapshot`` and ``snapshot_at`` live in the SAME row, so
the earliest surviving row is a defensible entry: one price, one timestamp, one
observation.

Retention prunes those snapshots after 7 days while ``gainers_comparisons`` rows
persist indefinitely, so an older coin outlives the only immutable record of its
first observed price. Those rows are UNKNOWN — never "fresh", and never
reconstructed from the current price or the 24h percentage.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from dashboard.db import get_trade_inbox

NOW = datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


async def _make_db(tmp_path, coins):
    """Build the tables `get_trade_inbox`'s tracker path reads.

    `coins` is a list of dicts:
        coin_id, current_price, snapshots=[(when, price)], detected_price,
        appeared_at
    """
    path = str(tmp_path / "inbox.db")
    conn = await aiosqlite.connect(path)
    await conn.execute("""CREATE TABLE paper_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token_id TEXT, symbol TEXT,
        name TEXT, chain TEXT, signal_type TEXT, signal_data TEXT,
        entry_price REAL, amount_usd REAL, quantity REAL, tp_pct REAL,
        sl_pct REAL, tp_price REAL, sl_price REAL, status TEXT,
        exit_price REAL, exit_reason TEXT, pnl_usd REAL, pnl_pct REAL,
        checkpoint_1h_pct REAL, checkpoint_6h_pct REAL,
        checkpoint_24h_pct REAL, checkpoint_48h_pct REAL,
        peak_price REAL, peak_pct REAL, opened_at TEXT, closed_at TEXT,
        leg_1_filled_at TEXT, leg_2_filled_at TEXT, remaining_qty REAL,
        realized_pnl_usd REAL, floor_armed INTEGER, would_be_live INTEGER,
        actionable INTEGER, actionability_reason TEXT,
        actionability_version TEXT)""")
    await conn.execute("""CREATE TABLE price_cache (
        coin_id TEXT PRIMARY KEY, current_price REAL, market_cap REAL,
        price_change_24h REAL, updated_at TEXT)""")
    await conn.execute("""CREATE TABLE gainers_comparisons (
        id INTEGER PRIMARY KEY AUTOINCREMENT, coin_id TEXT, symbol TEXT,
        name TEXT, price_change_24h REAL, appeared_on_gainers_at TEXT,
        detected_price REAL, peak_price REAL, peak_gain_pct REAL,
        is_gap INTEGER, created_at TEXT,
        entry_basis_price REAL, entry_basis_at TEXT)""")
    await conn.execute("""CREATE TABLE gainers_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, coin_id TEXT NOT NULL,
        symbol TEXT NOT NULL, name TEXT NOT NULL, price_change_24h REAL,
        market_cap REAL, volume_24h REAL, snapshot_at TEXT NOT NULL,
        created_at TEXT, price_at_snapshot REAL)""")

    for c in coins:
        cid = c["coin_id"]
        await conn.execute(
            "INSERT INTO price_cache (coin_id, current_price, market_cap, "
            "price_change_24h, updated_at) VALUES (?,?,?,?,?)",
            (cid, c["current_price"], 5_000_000, 30.0, _iso(NOW)),
        )
        # Anchor exactly as `compare_gainers_with_signals` does: the earliest
        # snapshot carrying a usable price, with price and timestamp taken from
        # that one row. Written once here, as the writer writes it once.
        usable = sorted((w, pr) for w, pr in c.get("snapshots", []) if pr and pr > 0)
        anchor_at, anchor_price = usable[0] if usable else (None, None)
        await conn.execute(
            "INSERT INTO gainers_comparisons (coin_id, symbol, name, "
            "price_change_24h, appeared_on_gainers_at, detected_price, is_gap, "
            "created_at, entry_basis_price, entry_basis_at) "
            "VALUES (?,?,?,?,?,?,1,?,?,?)",
            (
                cid,
                cid.upper()[:6],
                cid.title(),
                30.0,
                c.get("appeared_at", _iso(NOW - timedelta(hours=2))),
                c.get("detected_price"),
                _iso(NOW),
                anchor_price,
                _iso(anchor_at) if anchor_at else None,
            ),
        )
        for when, price in c.get("snapshots", []):
            await conn.execute(
                "INSERT INTO gainers_snapshots (coin_id, symbol, name, "
                "price_change_24h, market_cap, volume_24h, snapshot_at, "
                "created_at, price_at_snapshot) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    cid,
                    cid.upper()[:6],
                    cid.title(),
                    30.0,
                    5_000_000,
                    100_000,
                    _iso(when),
                    _iso(when),
                    price,
                ),
            )
    await conn.commit()
    await conn.close()
    return path


async def _row_for(path, coin_id):
    inbox = await get_trade_inbox(path)
    for rows in inbox["groups"].values():
        for r in rows:
            if r.get("token_id") == coin_id:
                return r
    return None


# Entry basis 100.0 six hours ago; current price varies to hit each threshold.
def _coin(coin_id, current_price, *, basis=100.0, hours_ago=6, snapshots=True):
    first = NOW - timedelta(hours=hours_ago)
    return {
        "coin_id": coin_id,
        "current_price": current_price,
        "detected_price": current_price,  # the defect: basis == current price
        "appeared_at": _iso(NOW - timedelta(minutes=30)),
        "snapshots": (
            [(first, basis), (first + timedelta(hours=1), basis * 1.5)]
            if snapshots
            else []
        ),
    }


class TestThresholdsAreReachable:
    """Each classification must be produced by a real fixture.

    Under the defect every one of these collapsed to "open"/fresh, because the
    basis was resampled to the current price and the ratio was ~0%.
    """

    @pytest.mark.parametrize(
        "current_price,expected_window,expected_quality",
        [
            (104.0, "open", "fresh_entry"),  # +4%
            (112.0, "closing", "acceptable_pullback"),  # +12%
            # +20% lands in the 15<pct<=25 band, where the pre-existing
            # entry_quality ladder falls through to `already_ran` while
            # window_state still says `closing`. Recorded, not changed —
            # out of scope for this fix.
            (120.0, "closing", "already_ran"),
            (400.0, "late", "already_ran"),  # +300%
            (50.0, "closed", "already_faded"),  # -50%
        ],
    )
    async def test_each_threshold_fires(
        self, tmp_path, current_price, expected_window, expected_quality
    ):
        path = await _make_db(tmp_path, [_coin("t1", current_price)])
        row = await _row_for(path, "t1")
        assert row is not None, "tracker row missing from inbox"
        assert row["window_state"] == expected_window
        assert row["entry_quality"] == expected_quality

    async def test_the_old_basis_would_have_said_fresh_for_all_of_them(self, tmp_path):
        """Pins the defect, so the fix cannot become unmotivated.

        `detected_price` equals the current price in every fixture above, so the
        old computation yields ~0% — "open"/fresh — regardless of how far the
        coin actually moved from its real first-observed price.
        """
        for current_price in (104.0, 120.0, 400.0, 50.0):
            coin = _coin("t1", current_price)
            old_pct = (
                (coin["current_price"] - coin["detected_price"])
                / coin["detected_price"]
                * 100
            )
            assert round(old_pct, 2) == 0.0


class TestBasisAndTimestampAreOneEvent:
    async def test_opened_at_equals_the_basis_observation(self, tmp_path):
        first = NOW - timedelta(hours=9)
        path = await _make_db(
            tmp_path,
            [
                {
                    "coin_id": "pair",
                    "current_price": 150.0,
                    "detected_price": 150.0,
                    "appeared_at": _iso(NOW - timedelta(minutes=5)),
                    "snapshots": [(first, 100.0), (first + timedelta(hours=2), 130.0)],
                }
            ],
        )
        row = await _row_for(path, "pair")
        assert row["entry_basis_source"] == "gainers_snapshot"
        assert row["entry_price"] == 100.0
        # the timestamp reported IS the timestamp of that price
        assert row["entry_basis_at"] == _iso(first)
        assert row["opened_at"] == _iso(first)
        # and NOT the rolling-window artifact, which is kept only for context
        assert row["first_listed_at"] != row["opened_at"]

    async def test_the_percentage_derives_from_the_reported_basis(self, tmp_path):
        """Displayed pct and the classification must come from one number."""
        path = await _make_db(tmp_path, [_coin("derive", 125.0)])
        row = await _row_for(path, "derive")
        expected = round(
            (row["current_price"] - row["entry_price"]) / row["entry_price"] * 100, 2
        )
        assert row["pct_from_entry"] == expected == 25.0
        assert row["window_state"] == "closing"

    async def test_the_earliest_snapshot_wins_not_the_latest(self, tmp_path):
        first = NOW - timedelta(hours=10)
        path = await _make_db(
            tmp_path,
            [
                {
                    "coin_id": "earliest",
                    "current_price": 200.0,
                    "detected_price": 200.0,
                    "snapshots": [
                        (first + timedelta(hours=3), 180.0),
                        (first, 100.0),  # inserted later, but earlier in time
                        (first + timedelta(hours=1), 150.0),
                    ],
                }
            ],
        )
        row = await _row_for(path, "earliest")
        assert row["entry_price"] == 100.0
        assert row["pct_from_entry"] == 100.0


class TestMissingBasisIsUnknownNotFresh:
    """*** THE LOAD-BEARING SAFETY TEST. ***

    893 of 1115 production rows have no surviving snapshot history. Under the
    defect, 109 of those rendered as `open` — tradeable — on a basis that could
    not be justified.
    """

    async def test_no_snapshots_yields_unknown_not_open(self, tmp_path):
        path = await _make_db(tmp_path, [_coin("nobasis", 104.0, snapshots=False)])
        row = await _row_for(path, "nobasis")
        assert row is not None
        assert row["entry_basis_source"] == "unavailable"
        assert row["entry_price"] is None
        assert row["pct_from_entry"] is None
        assert row["window_state"] == "unknown"
        assert row["entry_quality"] == "data_insufficient"
        assert "entry_basis_unavailable" in row["risk_reasons"]
        # Blocked, and blocked for the RIGHT reason — a row with a current
        # price but no defensible entry is DATA_INSUFFICIENT, not NO_PRICE.
        assert row["block_reason_primary"] == "DATA_INSUFFICIENT"
        assert row["group"] == "blocked"

    async def test_the_current_price_is_never_used_as_the_basis(self, tmp_path):
        """No fabrication: an absent basis must not be reconstructed from the
        current price or the 24h percentage."""
        path = await _make_db(tmp_path, [_coin("nofab", 987.65, snapshots=False)])
        row = await _row_for(path, "nofab")
        assert row["entry_price"] is None
        assert row["pct_from_entry"] is None

    @pytest.mark.parametrize("bad_price", [0.0, -5.0, None])
    async def test_malformed_basis_is_refused(self, tmp_path, bad_price):
        first = NOW - timedelta(hours=4)
        path = await _make_db(
            tmp_path,
            [
                {
                    "coin_id": "bad",
                    "current_price": 120.0,
                    "detected_price": 120.0,
                    "snapshots": [(first, bad_price)],
                }
            ],
        )
        row = await _row_for(path, "bad")
        assert row["entry_basis_source"] == "unavailable"
        assert row["pct_from_entry"] is None
        assert row["window_state"] == "unknown"

    async def test_a_later_valid_snapshot_is_used_when_the_first_is_malformed(
        self, tmp_path
    ):
        """A zero-priced row must not poison the coin: the earliest row with a
        usable price is the basis."""
        first = NOW - timedelta(hours=8)
        path = await _make_db(
            tmp_path,
            [
                {
                    "coin_id": "recover",
                    "current_price": 150.0,
                    "detected_price": 150.0,
                    "snapshots": [(first, 0.0), (first + timedelta(hours=1), 100.0)],
                }
            ],
        )
        row = await _row_for(path, "recover")
        assert row["entry_price"] == 100.0
        assert row["entry_basis_at"] == _iso(first + timedelta(hours=1))
        assert row["opened_at"] == row["entry_basis_at"]


class TestBasisIsStableAcrossRuns:
    async def test_repeated_reads_do_not_move_the_basis(self, tmp_path):
        path = await _make_db(tmp_path, [_coin("stable", 130.0)])
        first = await _row_for(path, "stable")
        second = await _row_for(path, "stable")
        assert first["entry_price"] == second["entry_price"]
        assert first["entry_basis_at"] == second["entry_basis_at"]
        assert first["pct_from_entry"] == second["pct_from_entry"]

    async def test_new_snapshots_do_not_move_the_basis(self, tmp_path):
        """Ongoing snapshots append; the earliest surviving one still wins."""
        path = await _make_db(tmp_path, [_coin("append", 130.0)])
        before = await _row_for(path, "append")

        conn = await aiosqlite.connect(path)
        await conn.execute(
            "INSERT INTO gainers_snapshots (coin_id, symbol, name, "
            "price_change_24h, market_cap, volume_24h, snapshot_at, created_at,"
            " price_at_snapshot) VALUES ('append','APPEND','Append',30.0,1,1,?,?,?)",
            (_iso(NOW), _iso(NOW), 130.0),
        )
        await conn.commit()
        await conn.close()

        after = await _row_for(path, "append")
        assert after["entry_price"] == before["entry_price"] == 100.0


class TestRetentionCannotMoveAnEstablishedBasis:
    """*** THE ANCHORING INVARIANT. ***

    Per-row immutability is NOT enough. Retention deletes snapshots after 7
    days, so "the earliest surviving snapshot" is a moving target: once S1 is
    pruned while S2 and S3 remain, a read-time `MIN(snapshot_at)` silently
    rebases the entry onto S2 and every percentage on the board shifts.

    Two outcomes are acceptable and one is not:
      OK   — the basis stays S1 (anchored to the original observation);
      OK   — the basis becomes unavailable, row reads unknown/DATA_INSUFFICIENT;
      NOT  — the basis quietly becomes S2.
    """

    async def _three_snapshot_db(self, tmp_path):
        s1 = NOW - timedelta(hours=12)
        s2 = NOW - timedelta(hours=8)
        s3 = NOW - timedelta(hours=4)
        path = await _make_db(
            tmp_path,
            [
                {
                    "coin_id": "anchor",
                    "current_price": 200.0,
                    "detected_price": 200.0,
                    "appeared_at": _iso(s1),
                    "snapshots": [(s1, 100.0), (s2, 150.0), (s3, 180.0)],
                }
            ],
        )
        return path, s1, s2, s3

    async def _delete_snapshot(self, path, coin_id, when):
        conn = await aiosqlite.connect(path)
        await conn.execute(
            "DELETE FROM gainers_snapshots WHERE coin_id=? AND snapshot_at=?",
            (coin_id, _iso(when)),
        )
        await conn.commit()
        await conn.close()

    async def test_retention_deleting_s1_does_not_rebase_onto_s2(self, tmp_path):
        path, s1, s2, s3 = await self._three_snapshot_db(tmp_path)

        established = await _row_for(path, "anchor")
        assert established["entry_price"] == 100.0, "S1 must be the basis"
        assert established["entry_basis_at"] == _iso(s1)

        # recompute — still S1
        again = await _row_for(path, "anchor")
        assert again["entry_price"] == 100.0
        assert again["entry_basis_at"] == _iso(s1)

        # retention prunes S1; S2 and S3 survive
        await self._delete_snapshot(path, "anchor", s1)
        after = await _row_for(path, "anchor")

        rebased_to_s2 = after["entry_price"] == 150.0 or after[
            "entry_basis_at"
        ] == _iso(s2)
        assert not rebased_to_s2, (
            "PROHIBITED: retention silently rebased the entry from S1 onto S2 — "
            f"basis is now {after['entry_price']} @ {after['entry_basis_at']}. "
            "Every percentage on the board shifted with no event and no notice."
        )
        anchored = after["entry_price"] == 100.0 and after["entry_basis_at"] == _iso(s1)
        unavailable = (
            after["entry_basis_source"] == "unavailable"
            and after["entry_price"] is None
            and after["pct_from_entry"] is None
            and after["window_state"] == "unknown"
            and after["block_reason_primary"] == "DATA_INSUFFICIENT"
        )
        assert anchored or unavailable, (
            "basis must either stay anchored to S1 or become unavailable; got "
            f"{after['entry_basis_source']} {after['entry_price']} "
            f"@ {after['entry_basis_at']}"
        )

    async def test_continuing_snapshots_do_not_move_the_basis(self, tmp_path):
        path, s1, _s2, _s3 = await self._three_snapshot_db(tmp_path)
        before = await _row_for(path, "anchor")
        conn = await aiosqlite.connect(path)
        await conn.execute(
            "INSERT INTO gainers_snapshots (coin_id, symbol, name, "
            "price_change_24h, market_cap, volume_24h, snapshot_at, created_at,"
            " price_at_snapshot) VALUES ('anchor','ANCHOR','Anchor',30,1,1,?,?,?)",
            (_iso(NOW), _iso(NOW), 250.0),
        )
        await conn.commit()
        await conn.close()
        after = await _row_for(path, "anchor")
        assert after["entry_price"] == before["entry_price"] == 100.0
        assert after["entry_basis_at"] == _iso(s1)

    async def test_the_basis_survives_a_process_restart(self, tmp_path):
        """Nothing may live in process memory: every read opens a new
        connection, and the board is served by a restartable process."""
        path, s1, _s2, _s3 = await self._three_snapshot_db(tmp_path)
        first = await _row_for(path, "anchor")
        # a fresh read is exactly what a restarted process does
        second = await _row_for(path, "anchor")
        assert first["entry_price"] == second["entry_price"] == 100.0
        assert second["entry_basis_at"] == _iso(s1)


class TestTheBasisPairIsAtomic:
    """A price and a timestamp, or nothing.

    Half a pair is not a weaker entry — it is no entry. A price with no time
    cannot be aged, and a time with no price cannot be measured against. The
    earlier revision leaked `appeared_on_gainers_at` into `opened_at` whenever
    the basis was unavailable, so an unknown row still displayed an "opened"
    time bound to no entry price.
    """

    async def _row_with_pair(self, tmp_path, price, at):
        path = str(tmp_path / "pair.db")
        conn = await aiosqlite.connect(path)
        await conn.execute("""CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, token_id TEXT, symbol TEXT,
            name TEXT, chain TEXT, signal_type TEXT, signal_data TEXT,
            entry_price REAL, amount_usd REAL, quantity REAL, tp_pct REAL,
            sl_pct REAL, tp_price REAL, sl_price REAL, status TEXT,
            exit_price REAL, exit_reason TEXT, pnl_usd REAL, pnl_pct REAL,
            checkpoint_1h_pct REAL, checkpoint_6h_pct REAL,
            checkpoint_24h_pct REAL, checkpoint_48h_pct REAL,
            peak_price REAL, peak_pct REAL, opened_at TEXT, closed_at TEXT,
            leg_1_filled_at TEXT, leg_2_filled_at TEXT, remaining_qty REAL,
            realized_pnl_usd REAL, floor_armed INTEGER, would_be_live INTEGER,
            actionable INTEGER, actionability_reason TEXT,
            actionability_version TEXT)""")
        await conn.execute("""CREATE TABLE price_cache (
            coin_id TEXT PRIMARY KEY, current_price REAL, market_cap REAL,
            price_change_24h REAL, updated_at TEXT)""")
        await conn.execute("""CREATE TABLE gainers_comparisons (
            id INTEGER PRIMARY KEY AUTOINCREMENT, coin_id TEXT, symbol TEXT,
            name TEXT, price_change_24h REAL, appeared_on_gainers_at TEXT,
            detected_price REAL, peak_price REAL, peak_gain_pct REAL,
            is_gap INTEGER, created_at TEXT,
            entry_basis_price REAL, entry_basis_at TEXT)""")
        await conn.execute("""CREATE TABLE gainers_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, coin_id TEXT NOT NULL,
            symbol TEXT NOT NULL, name TEXT NOT NULL, price_change_24h REAL,
            market_cap REAL, volume_24h REAL, snapshot_at TEXT NOT NULL,
            created_at TEXT, price_at_snapshot REAL)""")
        rolling = _iso(NOW - timedelta(minutes=20))
        await conn.execute(
            "INSERT INTO price_cache (coin_id, current_price, market_cap, "
            "price_change_24h, updated_at) VALUES ('pair',130.0,5e6,30.0,?)",
            (_iso(NOW),),
        )
        await conn.execute(
            "INSERT INTO gainers_comparisons (coin_id, symbol, name, "
            "price_change_24h, appeared_on_gainers_at, detected_price, is_gap, "
            "created_at, entry_basis_price, entry_basis_at) "
            "VALUES ('pair','PAIR','Pair',30.0,?,130.0,1,?,?,?)",
            (rolling, _iso(NOW), price, at),
        )
        # A later snapshot exists and must NOT be used to repair the pair.
        await conn.execute(
            "INSERT INTO gainers_snapshots (coin_id, symbol, name, "
            "price_change_24h, market_cap, volume_24h, snapshot_at, created_at,"
            " price_at_snapshot) VALUES ('pair','PAIR','Pair',30,1,1,?,?,?)",
            (_iso(NOW - timedelta(hours=2)), _iso(NOW), 111.0),
        )
        await conn.commit()
        await conn.close()
        return await _row_for(path, "pair"), rolling

    def _assert_unavailable(self, row):
        assert row["entry_basis_source"] == "unavailable"
        assert row["entry_price"] is None
        assert row["pct_from_entry"] is None
        assert row["opened_at"] is None
        assert row["opened_age_hours"] is None
        assert row["window_state"] == "unknown"
        assert row["block_reason_primary"] == "DATA_INSUFFICIENT"
        assert "entry_basis_unavailable" in row["risk_reasons"]

    async def test_price_present_timestamp_null(self, tmp_path):
        row, _ = await self._row_with_pair(tmp_path, 100.0, None)
        self._assert_unavailable(row)

    async def test_price_present_timestamp_malformed(self, tmp_path):
        row, _ = await self._row_with_pair(tmp_path, 100.0, "not-a-timestamp")
        self._assert_unavailable(row)

    async def test_timestamp_present_price_null(self, tmp_path):
        row, _ = await self._row_with_pair(
            tmp_path, None, _iso(NOW - timedelta(hours=6))
        )
        self._assert_unavailable(row)

    @pytest.mark.parametrize("bad_price", [0.0, -1.0])
    async def test_timestamp_present_price_not_positive(self, tmp_path, bad_price):
        row, _ = await self._row_with_pair(
            tmp_path, bad_price, _iso(NOW - timedelta(hours=6))
        )
        self._assert_unavailable(row)

    async def test_unavailable_keeps_first_listed_at_but_not_opened_at(self, tmp_path):
        """*** THE LEAK THIS FIXES. ***

        The rolling observation timestamp stays visible and labelled, and must
        NOT reappear as `opened_at`.
        """
        row, rolling = await self._row_with_pair(tmp_path, None, None)
        self._assert_unavailable(row)
        assert (
            row["first_listed_at"] == rolling
        ), "the rolling timestamp must still be available for context"
        assert row["opened_at"] is None, (
            "the rolling timestamp leaked back into opened_at: an unknown row "
            "would display an 'opened' time bound to no entry price"
        )

    async def test_a_complete_pair_still_works(self, tmp_path):
        """The invariant must not swallow valid rows."""
        at = _iso(NOW - timedelta(hours=6))
        row, rolling = await self._row_with_pair(tmp_path, 100.0, at)
        assert row["entry_basis_source"] == "gainers_snapshot"
        assert row["entry_price"] == 100.0
        assert row["opened_at"] == at
        assert row["pct_from_entry"] == 30.0
        assert row["first_listed_at"] == rolling


class TestTheWriterAnchorsAndNeverOverwrites:
    """The anchoring mechanism itself, driven through the REAL writer.

    `compare_gainers_with_signals` rewrites `gainers_comparisons` with
    DELETE + INSERT on every run. The anchor survives only because the writer
    reads the existing row and carries it forward verbatim. Hand-rolled SQL
    cannot test that; this drives the production function.
    """

    async def _db(self, tmp_path):
        from scout.db import Database

        d = Database(tmp_path / "writer.db")
        await d.initialize()
        return d

    async def _snap(self, d, coin_id, when, price, chg=30.0):
        await d._conn.execute(
            """INSERT INTO gainers_snapshots (coin_id, symbol, name,
                   price_change_24h, market_cap, volume_24h, snapshot_at,
                   created_at, price_at_snapshot)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                coin_id,
                coin_id.upper()[:6],
                coin_id.title(),
                chg,
                5_000_000,
                100_000,
                _iso(when),
                _iso(when),
                price,
            ),
        )
        await d._conn.commit()

    async def _basis(self, d, coin_id):
        cur = await d._conn.execute(
            "SELECT entry_basis_price, entry_basis_at FROM gainers_comparisons "
            "WHERE coin_id = ?",
            (coin_id,),
        )
        return await cur.fetchone()

    async def test_the_writer_anchors_then_never_moves_it(self, tmp_path):
        from scout.gainers.tracker import compare_gainers_with_signals

        d = await self._db(tmp_path)
        try:
            s1 = NOW - timedelta(hours=20)
            s2 = NOW - timedelta(hours=10)
            await self._snap(d, "anchored", s1, 100.0)
            await self._snap(d, "anchored", s2, 150.0)

            await compare_gainers_with_signals(d)
            first = await self._basis(d, "anchored")
            assert first[0] == 100.0 and first[1] == _iso(s1)

            # rerun: DELETE + INSERT, must carry the anchor forward verbatim
            await compare_gainers_with_signals(d)
            assert tuple(await self._basis(d, "anchored")) == tuple(first)

            # retention prunes S1 while S2 survives — the prohibited rebase
            await d._conn.execute(
                "DELETE FROM gainers_snapshots WHERE coin_id='anchored' "
                "AND snapshot_at = ?",
                (_iso(s1),),
            )
            await d._conn.commit()
            await compare_gainers_with_signals(d)

            after = await self._basis(d, "anchored")
            assert after[0] != 150.0, (
                "PROHIBITED: the writer rebased the anchor onto S2 after "
                "retention pruned S1"
            )
            assert after[0] == 100.0 and after[1] == _iso(
                s1
            ), "the anchor must stay pinned to the original observation"
        finally:
            await d.close()

    async def test_no_snapshots_means_no_anchor_ever_invented(self, tmp_path):
        """A coin whose history was already pruned gets NULL, not a stand-in."""
        from scout.gainers.tracker import compare_gainers_with_signals

        d = await self._db(tmp_path)
        try:
            # a snapshot with no usable price: present, but unusable
            await self._snap(d, "noanchor", NOW - timedelta(hours=3), 0.0)
            await d._conn.execute(
                "INSERT OR REPLACE INTO price_cache (coin_id, current_price, "
                "updated_at) VALUES ('noanchor', 999.0, ?)",
                (_iso(NOW),),
            )
            await d._conn.commit()
            await compare_gainers_with_signals(d)
            basis = await self._basis(d, "noanchor")
            assert (
                basis[0] is None and basis[1] is None
            ), "a price_cache value must never become the anchor"
        finally:
            await d.close()

    @pytest.mark.parametrize(
        "price,at",
        [
            (100.0, None),  # price without a time
            (None, "2026-08-01T00:00:00+00:00"),  # time without a price
        ],
    )
    async def test_an_incomplete_pair_is_carried_forward_not_repaired(
        self, tmp_path, price, at
    ):
        """*** FAIL CLOSED. ***

        A half-written pair must survive reruns unchanged. "Repairing" it from
        a later surviving snapshot would silently rebase the row onto a
        different observation — a wrong anchor, presented as a real one.
        Unavailable is recoverable; wrong is not.
        """
        from scout.gainers.tracker import compare_gainers_with_signals

        d = await self._db(tmp_path)
        try:
            await self._snap(d, "partial", NOW - timedelta(hours=5), 777.0)
            await d._conn.execute(
                "INSERT INTO gainers_comparisons (coin_id, symbol, name, "
                "price_change_24h, appeared_on_gainers_at, is_gap, "
                "entry_basis_price, entry_basis_at) "
                "VALUES ('partial','PARTIA','Partial',30.0,?,1,?,?)",
                (_iso(NOW - timedelta(hours=5)), price, at),
            )
            await d._conn.commit()

            await compare_gainers_with_signals(d)
            after = await self._basis(d, "partial")
            assert after[0] == price and after[1] == at, (
                "the writer repaired an incomplete pair by re-anchoring onto a "
                f"later snapshot: {tuple(after)}"
            )
            assert after[0] != 777.0, "rebased onto the surviving snapshot"
        finally:
            await d.close()

    async def test_a_late_arriving_snapshot_cannot_claim_an_established_anchor(
        self, tmp_path
    ):
        """Backdated inserts must not retroactively move an anchor."""
        from scout.gainers.tracker import compare_gainers_with_signals

        d = await self._db(tmp_path)
        try:
            s2 = NOW - timedelta(hours=10)
            await self._snap(d, "late", s2, 150.0)
            await compare_gainers_with_signals(d)
            assert (await self._basis(d, "late"))[0] == 150.0

            # a row that predates the anchor shows up afterwards
            await self._snap(d, "late", NOW - timedelta(hours=20), 100.0)
            await compare_gainers_with_signals(d)
            after = await self._basis(d, "late")
            assert after[0] == 150.0 and after[1] == _iso(
                s2
            ), "an established anchor must not move, even backwards"
        finally:
            await d.close()


class TestTheImmutabilityAssumptionHolds:
    def test_nothing_in_the_repo_mutates_gainers_snapshots(self):
        """The basis is only trustworthy because these rows are never updated.

        If a writer ever starts mutating them, this fix silently becomes as
        unreliable as the one it replaced.
        """
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        offenders = []
        for sub in ("scout", "dashboard", "scripts"):
            for p in (root / sub).rglob("*.py"):
                text = p.read_text("utf-8", errors="ignore")
                if re.search(
                    r"(UPDATE\s+gainers_snapshots"
                    r"|REPLACE\s+INTO\s+gainers_snapshots"
                    r"|INSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO\s+gainers_snapshots)",
                    text,
                    re.IGNORECASE,
                ):
                    offenders.append(str(p.relative_to(root)))
        assert offenders == [], (
            f"gainers_snapshots is mutated by {offenders}; the entry basis is "
            "only immutable while these rows are insert-only"
        )
