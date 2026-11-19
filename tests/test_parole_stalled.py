"""F2 — `parole_stalled`: the livelock that never paged.

THE DEFECT. A combo can be suppressed with its parole window OPEN, its full
slot budget INTACT, and ZERO trades ever admitted. `_classify_retest` called
that `waiting`, which is the wrong word: there is nothing to wait for. The
retest cannot advance without an admission, no admission is arriving, and the
terminal-incomplete pager only ever looked at combos whose budget was
EXHAUSTED — so no page fired, ever. Live for slow_burn (~6 weeks) and
narrative_prediction (~4 weeks) at the time of writing.

Every test here asserts existence AND count, and the fixture asserts it is
actually in the livelock state before exercising the classifier.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading import combo_refresh

_counter = itertools.count()


class _StubSender:
    def __init__(self):
        self.calls = 0
        self.messages: list[str] = []

    async def __call__(self, *args):
        self.calls += 1
        texts = [a for a in args if isinstance(a, str)]
        assert len(texts) == 1, f"cannot identify the message body in {args!r}"
        self.messages.append(texts[0])


async def _seed_livelocked(db, combo_key="slow_burn", *, days_open=6.0, remaining=5):
    """Suppressed, window OPEN, budget FULL, cohort EMPTY — the live shape."""
    now = datetime.now(timezone.utc)
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, avg_pnl_pct, "
        " win_rate_pct, suppressed, suppressed_at, parole_at, "
        " parole_trades_remaining, refresh_failures, last_refreshed) "
        "VALUES (?, '30d', 25, 4, 21, -100.0, -2.0, 16.0, 1, ?, ?, ?, 0, ?)",
        (
            combo_key,
            (now - timedelta(days=days_open + 14)).isoformat(),
            (now - timedelta(days=days_open)).isoformat(),
            remaining,
            (now - timedelta(hours=1)).isoformat(),
        ),
    )
    await db._conn.commit()


async def _suppression_fields(db, combo_key):
    cur = await db._conn.execute(
        "SELECT suppressed, suppressed_at, parole_at, parole_trades_remaining "
        "FROM combo_performance WHERE combo_key = ? AND window = '30d'",
        (combo_key,),
    )
    return tuple(await cur.fetchone())


async def _events(db, **where):
    clause = " WHERE event_type != 'ledger_installed'"
    params: tuple = ()
    if where:
        clause += " AND " + " AND ".join(f"{k} = ?" for k in where)
        params = tuple(where.values())
    cur = await db._conn.execute(
        "SELECT event_type, combo_key, transition, delivery_result, detail "
        f"FROM alert_events{clause} ORDER BY id",
        params,
    )
    cols = "event_type combo_key transition delivery_result detail".split()
    return [dict(zip(cols, r)) for r in await cur.fetchall()]


# --- the classifier --------------------------------------------------------


def test_livelock_classifies_as_parole_stalled():
    acct = dict(
        valid_closed=0,
        invalid_closed=0,
        open_now=0,
        cohort_total=0,
        spent=0,
        contaminated=False,
        window_open=True,
    )
    assert combo_refresh._classify_retest(acct, 5, 5) == "parole_stalled"


def test_a_closed_window_is_still_waiting_not_stalled():
    """Fails CLOSED. A window that has not opened yet cannot be blocked — the
    combo is serving its lock period, which is the system working."""
    acct = dict(
        valid_closed=0,
        invalid_closed=0,
        open_now=0,
        cohort_total=0,
        spent=0,
        contaminated=False,
        window_open=False,
    )
    assert combo_refresh._classify_retest(acct, 5, 5) == "waiting"


def test_one_admission_is_enough_to_be_waiting_again():
    """`parole_stalled` means ZERO admissions. One trade proves the path is
    open, so the combo really is just waiting for it to resolve."""
    acct = dict(
        valid_closed=0,
        invalid_closed=0,
        open_now=1,
        cohort_total=1,
        spent=1,
        contaminated=False,
        window_open=True,
    )
    assert combo_refresh._classify_retest(acct, 4, 5) == "waiting"


def test_a_drained_budget_with_an_empty_cohort_is_still_stalled():
    """PARTITIONED ON THE COHORT, NOT THE BUDGET.

    A combo can burn its entire slot budget and still admit NOTHING — slots are
    reserved at the gate and were leaked without a refund (the pre-#522 bug did
    exactly this). An earlier revision of this classifier required an intact
    budget, so these rows fell through to `terminal_incomplete` and were paged
    as an evidence-quality problem: "valid resolved 0/5, so this retest can
    never complete". That is confidently wrong about a retest that never
    started, and it points the operator at the cohort instead of at the
    dispatch path that is actually blocking.

    PROD, 2026-08-13: this exact shape (spent=5, cohort_total=0) is what
    chain_completed and cg_trending_rank+first_signal were mis-paged as."""
    acct = dict(
        valid_closed=0,
        invalid_closed=0,
        open_now=0,
        cohort_total=0,
        spent=5,
        contaminated=False,
        window_open=True,
    )
    assert combo_refresh._classify_retest(acct, 0, 5) == "parole_stalled"


def test_terminal_incomplete_is_retained_when_the_cohort_is_non_empty():
    """The other side of the same partition, and the regression that matters:
    reclassifying on the cohort must NOT swallow the class it replaced.

    PROD, 2026-08-13: losers_contrarian had cohort_total=3 with the budget
    drained — trades WERE admitted, they just did not produce enough usable
    outcomes. Its terminal-incomplete page was accurate and must stay."""
    acct = dict(
        valid_closed=3,
        invalid_closed=0,
        open_now=0,
        cohort_total=3,
        spent=5,
        contaminated=False,
        window_open=True,
    )
    assert combo_refresh._classify_retest(acct, 0, 5) == "terminal_incomplete"


def test_the_two_prod_shapes_are_no_longer_indistinguishable():
    """Before the fix both 2026-08-13 shapes returned `terminal_incomplete`, so
    the classifier could not tell "nothing was ever admitted" from "three
    trades produced too little". Pin that they now differ."""
    drained_empty = dict(
        valid_closed=0,
        invalid_closed=0,
        open_now=0,
        cohort_total=0,
        spent=5,
        contaminated=False,
        window_open=True,
    )
    drained_with_cohort = dict(
        valid_closed=3,
        invalid_closed=0,
        open_now=0,
        cohort_total=3,
        spent=5,
        contaminated=False,
        window_open=True,
    )
    assert combo_refresh._classify_retest(
        drained_empty, 0, 5
    ) != combo_refresh._classify_retest(drained_with_cohort, 0, 5)


def test_classifier_ordering_prefers_evidence_over_the_stall_diagnosis():
    """MUTATION GUARD on the ordering. A cohort with evidence must be diagnosed
    on that evidence — `contaminated` and `accounting_inconsistent` are checked
    BEFORE the stall, so moving the stall check earlier would mask them.

    Both fixtures below have cohort_total > 0, so neither can be `stalled`
    anyway; the point is that they keep their specific diagnosis rather than
    degrading to the generic one."""
    contaminated = dict(
        valid_closed=1,
        invalid_closed=0,
        open_now=0,
        cohort_total=2,
        spent=0,
        contaminated=True,
        window_open=True,
    )
    assert combo_refresh._classify_retest(contaminated, 5, 5) == "contaminated"

    inconsistent = dict(
        valid_closed=6,
        invalid_closed=0,
        open_now=0,
        cohort_total=6,
        spent=0,
        contaminated=True,
        window_open=True,
    )
    assert (
        combo_refresh._classify_retest(inconsistent, 5, 5) == "accounting_inconsistent"
    )


async def test_accounting_reports_window_open(tmp_path, settings_factory):
    """`window_open` is a pure read — it must not touch the row it describes."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        now = datetime.now(timezone.utc)
        await _seed_livelocked(db, "slow_burn", days_open=6.0)
        before = await _suppression_fields(db, "slow_burn")

        past = (now - timedelta(days=6)).isoformat()
        acct = await combo_refresh._retest_accounting(db, "slow_burn", past, 5, 5)
        assert acct["window_open"] is True
        assert acct["cohort_total"] == 0

        future = (now + timedelta(days=6)).isoformat()
        acct = await combo_refresh._retest_accounting(db, "slow_burn", future, 5, 5)
        assert acct["window_open"] is False

        assert await _suppression_fields(db, "slow_burn") == before
    finally:
        await db.close()


# --- the page --------------------------------------------------------------


async def test_livelocked_combo_pages_exactly_once_per_generation(
    tmp_path, settings_factory, monkeypatch
):
    """THE POINT OF THE WHOLE CHANGE. Before this, the pager only looked at
    combos whose budget was exhausted, so a livelocked combo was invisible
    forever. It must page — and page ONCE, not nightly."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        sender = _StubSender()
        monkeypatch.setattr(combo_refresh, "_send_retest_incomplete_alert", sender)
        await _seed_livelocked(db, "slow_burn", days_open=6.0, remaining=5)

        for _ in range(3):
            await combo_refresh._process_retest_terminal_incomplete(db, s)

        assert sender.calls == 1, f"paged {sender.calls}x for one generation"
        body = sender.messages[0]
        assert "STALLED" in body
        assert "slow_burn" in body
        assert "zero admissions" in body
        assert "slot budget 5/5 remaining" in body
        # Must not ASSERT fullness — under a drained stall this renders
        # 0/5, and "full slot budget (0/5)" would be a false statement.
        assert "full slot budget" not in body
        assert "SIGNAL_DISPATCH_QUARANTINE" in body
        assert "signal_params.enabled" in body
        assert "*" not in body, "plain text only — underscores must not be mangled"

        dispatched = await _events(db, event_type="alert_dispatched")
        delivered = await _events(db, event_type="alert_delivered")
        assert len(dispatched) == 1
        assert len(delivered) == 1
        assert dispatched[0]["transition"] == "parole_stalled"
        assert delivered[0]["transition"] == "parole_stalled"
        stamped = await _events(db, event_type="marker_stamped")
        assert len(stamped) == 1
    finally:
        await db.close()


async def test_the_page_does_not_mutate_any_suppression_field(
    tmp_path, settings_factory, monkeypatch
):
    """ZERO decision-flow change. This PR adds a diagnosis and a page; it must
    not clear, extend, re-arm or otherwise touch the suppression state — that
    would be an unapproved auto-revival."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        monkeypatch.setattr(
            combo_refresh, "_send_retest_incomplete_alert", _StubSender()
        )
        await _seed_livelocked(db, "slow_burn", days_open=6.0, remaining=5)
        before = await _suppression_fields(db, "slow_burn")

        await combo_refresh._process_retest_terminal_incomplete(db, s)
        assert await _suppression_fields(db, "slow_burn") == before
    finally:
        await db.close()


async def test_a_fresh_parole_window_does_not_page_on_the_first_night(
    tmp_path, settings_factory, monkeypatch
):
    """THE 1-DAY GRACE. A window that opened hours ago has legitimately not
    been used yet. Without the grace this would fire a false page on every
    fresh generation, which is how a real signal gets trained away."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        sender = _StubSender()
        monkeypatch.setattr(combo_refresh, "_send_retest_incomplete_alert", sender)
        await _seed_livelocked(db, "slow_burn", days_open=0.2, remaining=5)

        await combo_refresh._process_retest_terminal_incomplete(db, s)
        assert sender.calls == 0, "paged on a window that opened hours ago"
        assert await _events(db, event_type="alert_dispatched") == []
    finally:
        await db.close()


async def test_a_new_generation_re_arms_the_stalled_page(
    tmp_path, settings_factory, monkeypatch
):
    """One page per GENERATION, not one ever. A fresh parole window is a new
    fact about a still-blocked combo and deserves its own page."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        sender = _StubSender()
        monkeypatch.setattr(combo_refresh, "_send_retest_incomplete_alert", sender)
        await _seed_livelocked(db, "slow_burn", days_open=6.0, remaining=5)
        await combo_refresh._process_retest_terminal_incomplete(db, s)
        assert sender.calls == 1

        # combo_refresh re-arms the marker when the generation moves.
        now = datetime.now(timezone.utc)
        await db._conn.execute(
            "UPDATE combo_performance SET parole_at = ?, "
            "retest_incomplete_alerted_at = NULL "
            "WHERE combo_key = 'slow_burn' AND window = '30d'",
            ((now - timedelta(days=3)).isoformat(),),
        )
        await db._conn.commit()

        await combo_refresh._process_retest_terminal_incomplete(db, s)
        assert sender.calls == 2, "a new generation did not get its own page"
    finally:
        await db.close()


async def test_terminal_incomplete_still_pages_with_its_own_body(
    tmp_path, settings_factory, monkeypatch
):
    """The widened query must not cannibalise the class it already served."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        sender = _StubSender()
        monkeypatch.setattr(combo_refresh, "_send_retest_incomplete_alert", sender)
        now = datetime.now(timezone.utc)
        await _seed_livelocked(db, "combo_x", days_open=6.0, remaining=0)
        # Two valid closes admitted under the parole, none still open.
        for _ in range(2):
            await db._conn.execute(
                "INSERT INTO paper_trades "
                "(token_id, symbol, name, chain, signal_type, signal_data, "
                " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, "
                " sl_price, status, pnl_usd, pnl_pct, opened_at, closed_at, "
                " signal_combo) "
                "VALUES (?, 'S', 'N', 'coingecko', 'volume_spike', '{}', 1.0, "
                " 100.0, 100.0, 20.0, 10.0, 1.2, 0.9, 'closed_sl', -10.0, -5.0, "
                " ?, ?, 'combo_x')",
                (
                    f"tok-{next(_counter)}",
                    (now - timedelta(days=2)).isoformat(),
                    (now - timedelta(days=1)).isoformat(),
                ),
            )
        await db._conn.commit()

        await combo_refresh._process_retest_terminal_incomplete(db, s)
        assert sender.calls == 1
        assert "STUCK" in sender.messages[0]
        assert "STALLED" not in sender.messages[0]
        dispatched = await _events(db, event_type="alert_dispatched")
        assert len(dispatched) == 1
        assert dispatched[0]["transition"] == "terminal_incomplete_held"
    finally:
        await db.close()


async def test_stalled_delivery_failure_leaves_the_marker_unset(
    tmp_path, settings_factory, monkeypatch
):
    """Same #525 semantics as the sibling class: no marker without a confirmed
    send, so the next refresh re-attempts."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()

        async def _boom(message, settings_):
            raise RuntimeError("telegram send failed status=400")

        monkeypatch.setattr(combo_refresh, "_send_retest_incomplete_alert", _boom)
        await _seed_livelocked(db, "slow_burn", days_open=6.0, remaining=5)
        await combo_refresh._process_retest_terminal_incomplete(db, s)

        failed = await _events(db, event_type="alert_failed")
        assert len(failed) == 1
        assert failed[0]["transition"] == "parole_stalled"
        assert failed[0]["delivery_result"] == "error:RuntimeError"
        assert await _events(db, event_type="marker_stamped") == []
        assert await _events(db, event_type="alert_delivered") == []

        cur = await db._conn.execute(
            "SELECT retest_incomplete_alerted_at FROM combo_performance "
            "WHERE combo_key='slow_burn' AND window='30d'"
        )
        (marker,) = await cur.fetchone()
        assert marker is None
    finally:
        await db.close()


# --- folded-in #535 re-verify finding (b) ---------------------------------


async def test_preserve_branch_records_when_the_generation_moves_concurrently(
    tmp_path, settings_factory, monkeypatch
):
    """POSITIVE CASE for the delta gate's escape hatch. A preserve branch
    normally writes nothing, but `_refresh_combo_locked` re-reads `parole_at`
    from the row before writing — so a concurrent generation move IS a real
    state change and must be recorded, not swallowed as "steady state"."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        now = datetime.now(timezone.utc)
        await _seed_livelocked(db, "combo_a", days_open=3.0, remaining=3)
        # One admitted trade so the branch classifies as `waiting`.
        await db._conn.execute(
            "INSERT INTO paper_trades "
            "(token_id, symbol, name, chain, signal_type, signal_data, "
            " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, "
            " sl_price, status, pnl_usd, pnl_pct, opened_at, closed_at, "
            " signal_combo) "
            "VALUES (?, 'S', 'N', 'coingecko', 'volume_spike', '{}', 1.0, "
            " 100.0, 100.0, 20.0, 10.0, 1.2, 0.9, 'open', NULL, NULL, ?, NULL, "
            " 'combo_a')",
            (f"tok-{next(_counter)}", (now - timedelta(days=2)).isoformat()),
        )
        await db._conn.commit()

        # Settle the first-entry classification row so the next refresh is a
        # genuine steady state.
        assert await combo_refresh.refresh_combo(db, "combo_a", s)
        baseline = len(await _events(db, event_type="suppression_transition"))
        assert baseline == 1

        # Now move the generation underneath the preserve branch: the re-read
        # returns a parole_at that differs from the one loaded at entry.
        real_execute = db._conn.execute
        moved = {"done": False}

        async def _move_generation_on_reread(sql, *a, **k):
            if not moved["done"] and "SELECT parole_at FROM combo_performance" in str(
                sql
            ):
                moved["done"] = True
                await real_execute(
                    "UPDATE combo_performance SET parole_at = ? "
                    "WHERE combo_key='combo_a' AND window='30d'",
                    ((now + timedelta(days=9)).isoformat(),),
                )
            return await real_execute(sql, *a, **k)

        monkeypatch.setattr(db._conn, "execute", _move_generation_on_reread)
        assert await combo_refresh.refresh_combo(db, "combo_a", s)
        monkeypatch.undo()

        assert moved["done"], "the fixture never hit the parole_at re-read"
        rows = await _events(db, event_type="suppression_transition")
        assert len(rows) == baseline + 1, (
            "a concurrent generation move on a preserve branch was swallowed "
            "as steady state"
        )
        assert rows[-1]["detail"] == "generation moved on a preserve branch"
    finally:
        await db.close()


async def test_a_drained_empty_cohort_pages_as_stalled_not_as_stuck(
    tmp_path, settings_factory, monkeypatch
):
    """End-to-end on the mis-paged prod shape: the combo that burned 5 slots
    and admitted nothing must now get the STALLED body (look downstream of the
    gate), not the STUCK body (look at the evidence)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        sender = _StubSender()
        monkeypatch.setattr(combo_refresh, "_send_retest_incomplete_alert", sender)
        # spent = target - remaining = 5; cohort empty because nothing was
        # ever admitted, exactly the pre-#522 no-refund shape.
        await _seed_livelocked(db, "chain_completed", days_open=6.0, remaining=0)

        await combo_refresh._process_retest_terminal_incomplete(db, s)

        assert sender.calls == 1
        body = sender.messages[0]
        assert "STALLED" in body
        assert "STUCK" not in body
        assert "can never complete" not in body, (
            "the evidence-quality claim was made about a retest that never " "started"
        )
        assert "slot budget 0/5 remaining" in body
        assert "zero admissions" in body
        dispatched = await _events(db, event_type="alert_dispatched")
        assert len(dispatched) == 1
        assert dispatched[0]["transition"] == "parole_stalled"
    finally:
        await db.close()


async def test_the_pager_sql_no_longer_gates_the_stalled_arm_on_budget(
    tmp_path, settings_factory, monkeypatch
):
    """The SQL only narrows; the CLASSIFIER decides. A drained combo has to
    reach the classifier at all, which the old `parole_trades_remaining > 0`
    conjunct prevented."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        sender = _StubSender()
        monkeypatch.setattr(combo_refresh, "_send_retest_incomplete_alert", sender)
        # remaining=0 AND an open window: admitted by the exhausted arm, and it
        # must classify as stalled rather than be filtered out or mis-paged.
        await _seed_livelocked(
            db, "cg_trending_rank+first_signal", days_open=9.0, remaining=0
        )
        await combo_refresh._process_retest_terminal_incomplete(db, s)
        assert sender.calls == 1
        assert "STALLED" in sender.messages[0]
    finally:
        await db.close()


def test_both_axes_share_one_parole_window_predicate():
    """DRY, and not for tidiness. The admission gate decides whether to SPEND a
    slot against this predicate; the classifier decides whether the combo is
    STALLED against it. Two copies drifting apart would let the system admit
    trades against a window its own classifier considers shut — the two-axis
    desync class this PR exists to surface."""
    from scout import timeutil
    from scout.trading import suppression

    assert combo_refresh.parole_window_open is timeutil.parole_window_open
    assert suppression.parole_window_open is timeutil.parole_window_open


async def test_null_budget_row_reaches_the_classifier_and_renders_honestly(
    tmp_path, settings_factory, monkeypatch
):
    """PINS THE ONLY BEHAVIOURAL DELTA of dropping the budget conjunct from the
    stalled SQL arm.

    Worked out exactly: a drained row (`remaining = 0`) was already admitted by
    the exhausted arm, and an intact row satisfied the old conjunct, so for
    every non-NULL budget the drop is a pure simplification — which is why no
    other test moved when it was reverted. The single row the union newly
    admits is `parole_trades_remaining IS NULL`: `COALESCE(remaining, 1) <= 0`
    excludes it and `COALESCE(remaining, 0) > 0` excluded it too.

    That is the N-2 corner the operator ticketed as out of scope, so this test
    documents rather than endorses the new behaviour — it is here so the
    consequence is visible and pinned instead of arriving unnoticed. The
    diagnosis itself stands on its own feet: an open window with an empty
    cohort is a stall whatever the budget says.

    It also pins the rendering. `remaining` interpolates straight into the page
    body, so a NULL would have read "slot budget None/5 remaining"."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        sender = _StubSender()
        monkeypatch.setattr(combo_refresh, "_send_retest_incomplete_alert", sender)
        now = datetime.now(timezone.utc)
        await db._conn.execute(
            "INSERT INTO combo_performance "
            "(combo_key, window, trades, wins, losses, total_pnl_usd, "
            " avg_pnl_pct, win_rate_pct, suppressed, suppressed_at, parole_at, "
            " parole_trades_remaining, refresh_failures, last_refreshed) "
            "VALUES ('nullbudget', '30d', 25, 4, 21, -100.0, -2.0, 16.0, 1, ?, "
            " ?, NULL, 0, ?)",
            (
                (now - timedelta(days=20)).isoformat(),
                (now - timedelta(days=6)).isoformat(),
                (now - timedelta(hours=1)).isoformat(),
            ),
        )
        await db._conn.commit()

        await combo_refresh._process_retest_terminal_incomplete(db, s)

        assert sender.calls == 1
        body = sender.messages[0]
        assert "STALLED" in body
        assert "slot budget unknown/5 remaining" in body
        assert "None" not in body, "a NULL budget leaked into the page as 'None'"
    finally:
        await db.close()


async def test_reclassification_does_not_re_page_already_alerted_combos(
    tmp_path, settings_factory, monkeypatch
):
    """NO RE-PAGE STORM ON DEPLOY.

    chain_completed and cg_trending_rank+first_signal were mis-paged as
    terminal_incomplete on 2026-08-13 and therefore already carry
    `retest_incomplete_alerted_at`. Reclassifying them to `parole_stalled`
    changes the DIAGNOSIS, not the marker: the candidate query requires
    `retest_incomplete_alerted_at IS NULL`, and the marker re-arms only when
    the combo leaves suppression or its parole generation moves.

    So on deploy they stay silent until something actually changes. Proven
    here rather than argued: the marker is pre-set, the pass is run, and the
    sender must not fire — then the marker is cleared (a generation change)
    and it must fire with the corrected body."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        sender = _StubSender()
        monkeypatch.setattr(combo_refresh, "_send_retest_incomplete_alert", sender)
        now = datetime.now(timezone.utc)
        await _seed_livelocked(db, "chain_completed", days_open=9.0, remaining=0)
        # The 08-13 page already went out.
        await db._conn.execute(
            "UPDATE combo_performance SET retest_incomplete_alerted_at = ? "
            "WHERE combo_key = 'chain_completed' AND window = '30d'",
            ((now - timedelta(days=2)).isoformat(),),
        )
        await db._conn.commit()

        for _ in range(3):
            await combo_refresh._process_retest_terminal_incomplete(db, s)
        assert sender.calls == 0, "reclassification re-paged an already-alerted combo"

        # A generation change re-arms it, and the page is now the corrected one.
        await db._conn.execute(
            "UPDATE combo_performance SET retest_incomplete_alerted_at = NULL "
            "WHERE combo_key = 'chain_completed' AND window = '30d'"
        )
        await db._conn.commit()
        await combo_refresh._process_retest_terminal_incomplete(db, s)
        assert sender.calls == 1
        assert "STALLED" in sender.messages[0]
    finally:
        await db.close()


# --- the EMIT path -------------------------------------------------------
#
# Everything above tests `_classify_retest` in isolation, which is why the
# defect below survived: the classifier returned "parole_stalled" correctly
# in prod, and the per-combo log line still said "waiting". The if/elif chain
# in `_refresh_combo_locked` had branches for complete / terminal_incomplete /
# contaminated / accounting_inconsistent and an `else` — and no arm for the
# state this module exists to introduce, so it fell through to the generic
# "be patient" log. Same shape as the classifier being pinned while the thing
# that CONSUMES it is not.
#
# Measured on prod 2026-09-03: five combos classified `parole_stalled` when
# the real function was run against the real DB, while every nightly run had
# logged `parole_retest_waiting` for them and the string "parole_stalled"
# appeared ZERO times in the entire retained journal.


def _silence_senders(monkeypatch):
    """Silence every combo_refresh sender. Local to this module -- the
    identically-named helper in test_alert_events_insertion_sites.py is not
    importable from here."""
    for name in (
        "_send_permanent_suppression_alert",
        "_send_suppression_reversal_alert",
        "_send_retest_incomplete_alert",
    ):
        monkeypatch.setattr(combo_refresh, name, _StubSender())


async def _stalled_states(db, settings, combo_key):
    """Run one refresh and return the retest events it logged."""
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        await combo_refresh.refresh_combo(db, combo_key, settings)
    return [e for e in logs if str(e.get("event", "")).startswith("parole_retest")]


async def test_stalled_combo_is_not_logged_as_waiting(
    tmp_path, settings_factory, monkeypatch
):
    """The whole point of the state: a stalled retest must not be reported
    with the word that means 'be patient'.

    Asserted as an ABSENCE plus a matching presence, not absence alone -- an
    absence-only assertion would also pass if the refresh silently did
    nothing at all.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _silence_senders(monkeypatch)
        await _seed_livelocked(db, "slow_burn")

        events = await _stalled_states(db, s, "slow_burn")
        names = [e["event"] for e in events]

        assert "parole_retest_waiting" not in names, (
            "a stalled combo was reported as 'waiting' -- the exact "
            f"mislabel this state exists to remove: {names}"
        )
        assert "parole_retest_stalled" in names, (
            f"no stall-specific event was emitted: {names}"
        )
    finally:
        await db.close()


async def test_stalled_log_carries_the_diagnostic_fields(
    tmp_path, settings_factory, monkeypatch
):
    """The page tells the operator to check signal_params.enabled; the LOG has
    to carry enough to confirm the diagnosis without re-deriving it. Asserts
    real values, not mere presence."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _silence_senders(monkeypatch)
        await _seed_livelocked(db, "volume_spike", days_open=6.0, remaining=5)

        events = await _stalled_states(db, s, "volume_spike")
        stalled = [e for e in events if e["event"] == "parole_retest_stalled"]
        assert len(stalled) == 1, f"expected exactly one stall log: {events}"
        e = stalled[0]
        assert e["combo_key"] == "volume_spike"
        assert e["cohort_total"] == 0
        assert e["remaining"] == 5
        assert e["required"] == 5
        assert e["slots_spent"] == 0
        # The remedy pointer is the one field that tells the operator what to
        # DO. Mutation showed `slots_spent`, `required` and the entire
        # `detail=` string could all be stripped with the suite still green.
        assert "signal_params.enabled" in e["detail"]
        assert "SIGNAL_DISPATCH_QUARANTINE" in e["detail"]
        # WARNING, not info: an empty cohort under a window open for more than
        # the grace period is a stuck system.
        assert e["log_level"] == "warning"
    finally:
        await db.close()


async def test_budget_exhausted_stall_reports_spent_not_cohort(
    tmp_path, settings_factory, monkeypatch
):
    """A stall with the budget BURNED — slots reserved at the gate then
    leaked, never admitting anything.

    Every other fixture here uses remaining=5, which equals
    FEEDBACK_PAROLE_RETEST_TRADES, so `spent` is 0 and `remaining` == `required`
    in all of them. That makes three distinct fields indistinguishable and lets
    two real mutants live: `cohort_total=acct["spent"]` and
    `remaining=retest`. Under the first, a combo with an EMPTY cohort would be
    logged `cohort_total=5` -- the exact misstatement this state exists to
    remove, in the exact prod scenario (chain_completed, 2026-08-13, spent=5
    cohort_total=0) the classifier was re-partitioned onto the cohort to catch.

    This is also the only emit-path coverage of the budget-exhausted stall.
    """
    from structlog.testing import capture_logs

    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _silence_senders(monkeypatch)
        await _seed_livelocked(db, "chain_completed", days_open=9.0, remaining=0)

        with capture_logs() as logs:
            await combo_refresh.refresh_combo(db, "chain_completed", s)
        stalled = [e for e in logs if e.get("event") == "parole_retest_stalled"]
        assert len(stalled) == 1, f"expected a stall log: {logs}"
        e = stalled[0]
        # The discriminating trio: an empty cohort, a fully spent budget, and
        # nothing remaining. All three differ here.
        assert e["cohort_total"] == 0
        assert e["slots_spent"] == 5
        assert e["remaining"] == 0
        assert e["required"] == 5
    finally:
        await db.close()


async def test_fresh_window_is_not_yet_warned_about(
    tmp_path, settings_factory, monkeypatch
):
    """The 1-day grace, mirrored from the pager.

    `_classify_retest`'s window_open is a bare `parole_at <= now`, but
    `_process_retest_terminal_incomplete` applies a 1-day grace and says why:
    a window that opened minutes ago has legitimately not been used yet.
    Without mirroring it, the LOG asserts "the retest cannot start" for a
    generation the PAGE correctly judges too fresh -- and since parole_at is
    suppressed_at+14d at an arbitrary time of day against a nightly refresh,
    roughly half of all fresh generations would draw a spurious WARNING.

    Still logged, at INFO: the classification is real, it is just not yet
    evidence of a stuck system.
    """
    from structlog.testing import capture_logs

    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _silence_senders(monkeypatch)
        # Window opened 2 hours ago -- inside the grace.
        await _seed_livelocked(db, "slow_burn", days_open=2.0 / 24.0)

        with capture_logs() as logs:
            await combo_refresh.refresh_combo(db, "slow_burn", s)
        stalled = [e for e in logs if e.get("event") == "parole_retest_stalled"]
        assert len(stalled) == 1, f"a fresh stall must still be traced: {logs}"
        assert stalled[0]["log_level"] == "info", (
            "a window open for 2 hours must not be WARNED about -- that is the "
            f"false-alarm class the pager's grace exists to avoid: {stalled[0]}"
        )
        assert stalled[0]["days_open"] < 1.0
    finally:
        await db.close()


async def test_aged_stall_carries_its_elapsed_time(
    tmp_path, settings_factory, monkeypatch
):
    """Elapsed time is what makes five stalled combos rankable from one grep.
    Without it a one-night stall and a six-week one read identically."""
    from structlog.testing import capture_logs

    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _silence_senders(monkeypatch)
        await _seed_livelocked(db, "narrative_prediction", days_open=42.0)

        with capture_logs() as logs:
            await combo_refresh.refresh_combo(db, "narrative_prediction", s)
        e = [x for x in logs if x.get("event") == "parole_retest_stalled"][0]
        assert e["log_level"] == "warning"
        assert 41.5 <= e["days_open"] <= 42.5, e
        assert e["parole_at"] is not None
    finally:
        await db.close()


async def test_waiting_is_still_used_when_the_cohort_is_genuinely_progressing(
    tmp_path, settings_factory, monkeypatch
):
    """The fix must not turn every non-terminal state into 'stalled'.

    A combo with an OPEN trade has a non-empty cohort, so it is genuinely
    waiting -- and must still say so. Without this, replacing the `else` arm
    wholesale would pass the two tests above.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _silence_senders(monkeypatch)
        await _seed_livelocked(db, "chain_completed", remaining=3)
        # One admitted, still-open trade inside the parole window.
        cur = await db._conn.execute(
            "SELECT parole_at FROM combo_performance WHERE combo_key='chain_completed'"
        )
        (parole_at,) = await cur.fetchone()
        await db._conn.execute(
            "INSERT INTO paper_trades "
            "(token_id, symbol, name, chain, signal_type, signal_data, "
            " entry_price, amount_usd, quantity, tp_price, sl_price, status, "
            " opened_at, created_at, signal_combo) "
            "VALUES ('t1','T','T','solana','chain_completed','{}',1.0,100.0,100.0,"
            "1.2,0.9,'open',?,?,'chain_completed')",
            (parole_at, parole_at),
        )
        await db._conn.commit()

        events = await _stalled_states(db, s, "chain_completed")
        names = [e["event"] for e in events]
        assert "parole_retest_waiting" in names, (
            f"a progressing cohort must still read as waiting: {names}"
        )
        assert "parole_retest_stalled" not in names, names
    finally:
        await db.close()


async def test_an_unclassified_state_announces_itself(
    tmp_path, settings_factory, monkeypatch
):
    """The SHAPE guard, not the instance.

    This PR exists because a new classifier state fell silently into the
    `else` and was reported as "waiting" -- a confident lie. Adding the
    missing arm fixes that instance; it does not stop the NEXT state added to
    `_classify_retest` from inheriting the same mislabel. This pins the guard
    that turns that recurrence from a silent lie into a loud one.

    Driven by forcing the classifier to return a state the emit chain has no
    arm for, because by construction every state it can currently return now
    HAS an arm -- so there is no natural input that reaches the guard.
    """
    from structlog.testing import capture_logs

    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _silence_senders(monkeypatch)
        await _seed_livelocked(db, "slow_burn")
        monkeypatch.setattr(
            combo_refresh, "_classify_retest", lambda *a, **k: "some_future_state"
        )

        with capture_logs() as logs:
            await combo_refresh.refresh_combo(db, "slow_burn", s)

        unknown = [e for e in logs if e.get("event") == "parole_retest_unclassified"]
        assert len(unknown) == 1, f"an unhandled state must announce itself: {logs}"
        assert unknown[0]["retest_state"] == "some_future_state"
        assert unknown[0]["log_level"] == "error"
        # Still reported as waiting afterwards -- the guard adds a signal, it
        # does not change the fallback behaviour.
        assert any(e.get("event") == "parole_retest_waiting" for e in logs)
    finally:
        await db.close()


async def test_fresh_burned_budget_stall_warns_without_grace(
    tmp_path, settings_factory, monkeypatch
):
    """The pager's grace is a DISJUNCTION; the log must mirror it.

    `_process_retest_terminal_incomplete`'s WHERE admits on
        (COALESCE(parole_trades_remaining, 1) <= 0 OR parole_at <= cutoff)
    so the 1-day cutoff binds only the SECOND disjunct: a burned-budget stall
    pages with NO grace. An earlier revision graced everything, so a fresh
    burned-budget stall paged the operator at ERROR while logging INFO --
    a log contradicting a page, which is this module's own defect class.

    Reachable by the prod case that motivated the state: chain_completed on
    2026-08-13 was spent=5 with cohort_total=0, and five slots can be
    reserved-then-leaked within hours of a window opening.
    """
    from structlog.testing import capture_logs

    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _silence_senders(monkeypatch)
        # Window open FIVE HOURS -- far inside the calendar grace -- but the
        # budget is gone. The pager fires on this; so must the log.
        await _seed_livelocked(db, "chain_completed", days_open=5.0 / 24.0, remaining=0)

        with capture_logs() as logs:
            await combo_refresh.refresh_combo(db, "chain_completed", s)
        e = [x for x in logs if x.get("event") == "parole_retest_stalled"][0]
        assert e["days_open"] < 1.0, "fixture must be inside the calendar grace"
        assert e["remaining"] == 0
        assert e["log_level"] == "warning", (
            "a burned-budget stall pages with no grace, so the log must not "
            f"call it uninteresting: {e}"
        )
    finally:
        await db.close()


async def test_grace_boundary_is_one_day(tmp_path, settings_factory, monkeypatch):
    """Pins the threshold to ~1 day, not merely to some value in a wide band.

    Without both sides, any threshold in (0.083, 6.0] passed -- the existing
    fixtures only bracket it at 2 hours and 6 days.
    """
    from structlog.testing import capture_logs

    async def _level_at(days_open, combo):
        db = Database(tmp_path / f"{combo}.db")
        await db.initialize()
        try:
            s = settings_factory()
            _silence_senders(monkeypatch)
            # remaining=5 so the budget disjunct cannot mask the calendar one.
            await _seed_livelocked(db, combo, days_open=days_open, remaining=5)
            with capture_logs() as logs:
                await combo_refresh.refresh_combo(db, combo, s)
            return [
                x for x in logs if x.get("event") == "parole_retest_stalled"
            ][0]["log_level"]
        finally:
            await db.close()

    assert await _level_at(0.98, "slow_burn") == "info"
    assert await _level_at(1.02, "volume_spike") == "warning"


async def test_null_budget_stall_is_not_treated_as_burned(
    tmp_path, settings_factory, monkeypatch
):
    """The NULL edge of the budget disjunct.

    The pager's predicate is `COALESCE(parole_trades_remaining, 1) <= 0`, so
    a NULL budget coalesces to 1 and is NOT burned -- the pager declines. The
    Python mirror must agree, which is why it reads
    `remaining is not None and remaining <= 0` rather than the obvious
    simplification `(remaining or 0) <= 0`.

    Both simplifications a future reader would reach for --
    `(remaining or 0) <= 0` and `remaining is None or remaining <= 0` --
    invert that edge and survived the suite before this test: they WARN on a
    NULL budget while the pager stays silent, which is the log-contradicts-
    page shape this PR exists to remove, one value further over.

    NULL is a real anticipated state here, not a theoretical one: the pager
    renders it specially ("unknown" rather than the number),
    `_retest_accounting` special-cases it for `spent`, and `_classify_retest`
    guards on it.
    """
    from structlog.testing import capture_logs

    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _silence_senders(monkeypatch)
        # Inside the calendar grace, so only the budget disjunct can decide.
        await _seed_livelocked(db, "slow_burn", days_open=2.0 / 24.0)
        await db._conn.execute(
            "UPDATE combo_performance SET parole_trades_remaining = NULL "
            "WHERE combo_key = 'slow_burn' AND window = '30d'"
        )
        await db._conn.commit()

        with capture_logs() as logs:
            await combo_refresh.refresh_combo(db, "slow_burn", s)
        stalled = [x for x in logs if x.get("event") == "parole_retest_stalled"]
        assert len(stalled) == 1, f"a NULL-budget stall must still classify: {logs}"
        e = stalled[0]
        assert e["remaining"] is None, "fixture must actually exercise the NULL edge"
        assert e["days_open"] < 1.0, "fixture must be inside the calendar grace"
        assert e["log_level"] == "info", (
            "NULL coalesces to 1 in the pager's predicate, so it is NOT burned "
            f"and the pager declines -- the log must not WARN: {e}"
        )
    finally:
        await db.close()
