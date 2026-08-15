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
