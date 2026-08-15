"""Nightly combo refresh (spec §5.3)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import aiosqlite
import structlog

from scout.db import Database
from scout.timeutil import parole_window_open, sql_utc_cutoff
from scout.trading.alert_events import (
    encode_state,
    generation_state,
    payload_digest,
    record_alert_event,
)
from scout.trading.paper import VALID_TERMINAL_OUTCOME_SQL

log = structlog.get_logger()


async def refresh_combo(db: Database, combo_key: str, settings) -> bool:
    """Recompute 7d + 30d rows for `combo_key`. Apply suppression rule to 30d.
    Returns True on success, False otherwise.
    """
    # Acquire the shared asyncio.Lock so the multi-statement read→write
    # sequence here cannot interleave with should_open's BEGIN...COMMIT block
    # across asyncio suspend points within the same event loop.
    if db._txn_lock is None:
        raise RuntimeError(
            "Database._txn_lock is None — Database.initialize() was not awaited "
            "before refresh_combo(). A fresh ephemeral Lock here would silently "
            "break mutual exclusion across concurrent callers."
        )
    async with db._txn_lock:
        return await _refresh_combo_locked(db, combo_key, settings)


async def _send_retest_incomplete_alert(message: str, settings) -> None:
    """Deferred-import Telegram send. Monkeypatched in tests.

    ``parse_mode=None``: the body carries combo keys containing underscores
    (``losers_contrarian``, ``cg_trending_rank+first_signal``), which Telegram
    MarkdownV1 would silently consume as italics markers — HTTP 200 with a
    mangled body (CLAUDE.md §12b).

    ``raise_on_failure=True`` is LOAD-BEARING, not decoration. The alerter
    defaults to ``False`` and merely LOGS on a non-200 or a network error, so
    without it the caller would emit ``..._delivered`` and set the dedup marker
    for a page Telegram actually rejected — and the marker would then suppress
    every future attempt. "Set the marker only after a confirmed send" is only
    a true statement if failure propagates.
    """
    import aiohttp  # deferred — module-level import aborts on Windows

    from scout import alerter  # deferred — pulls aiohttp at import time

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15)
    ) as session:
        await alerter.send_telegram_message(
            message,
            session,
            settings,
            parse_mode=None,
            source="combo_refresh_retest_terminal_incomplete",
            raise_on_failure=True,
        )


async def _process_retest_terminal_incomplete(db: Database, settings) -> list[str]:
    """Page ONCE per generation for parole retests that can never complete.

    Runs AFTER the locked per-combo refreshes, mirroring
    ``_process_permanent_suppression``. Delivery must not happen inside
    ``_refresh_combo_locked``: that holds ``db._txn_lock`` and sits in the
    middle of the refresh's multi-statement read->write sequence, so a Telegram
    await would pin the trading lock for the client timeout and the marker's
    commit would split the refresh transaction — making the 7d write durable
    before the 30d write landed.

    Marker writes are GENERATION-BOUND on ``(suppressed_at, parole_at)``. A
    slow send followed by a re-arm must not stamp the NEW generation as already
    alerted; if the generation moved while we were sending, the update matches
    zero rows and the next run pages the new generation. Same principle as the
    D1 parole reservation.

    Returns the combos newly alerted this run.
    """
    conn = db._conn
    target = settings.FEEDBACK_PAROLE_RETEST_TRADES

    # Two structurally-stuck classes share this pass, because they share the
    # thing that makes them dangerous: the combo is suppressed, cannot advance
    # on its own, and NOTHING pages the operator about it.
    #
    #   terminal_incomplete — budget spent, nothing open, not enough evidence.
    #   parole_stalled      — budget INTACT and zero admissions ever. The
    #                         window opened and no trade has arrived, so the
    #                         retest cannot start, let alone finish.
    #
    # The 1-day grace on the stalled arm is load-bearing: a parole window that
    # opened minutes ago has legitimately not been used yet, and paging on it
    # would fire a false alarm on every fresh generation. A window open for a
    # full day with the full budget untouched is not "waiting", it is blocked.
    stalled_grace_cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    cur = await conn.execute(
        "SELECT combo_key, suppressed_at, parole_at, parole_trades_remaining "
        "FROM combo_performance "
        "WHERE window = '30d' AND suppressed = 1 "
        "  AND retest_incomplete_alerted_at IS NULL "
        "  AND parole_at IS NOT NULL "
        "  AND (COALESCE(parole_trades_remaining, 1) <= 0 OR parole_at <= ?)",
        (stalled_grace_cutoff,),
    )
    candidates = await cur.fetchall()

    alerted: list[str] = []
    for combo_key, suppressed_at, parole_at, remaining in candidates:
        acct = await _retest_accounting(db, combo_key, parole_at, remaining, target)
        state = _classify_retest(acct, remaining, target)
        # The classifier decides; the SQL only narrows. A row admitted by the
        # widened arm that classifies as anything else (a trade landed between
        # the query and here) falls out here rather than being paged as stalled
        # on the strength of the WHERE clause alone.
        if state not in ("terminal_incomplete", "parole_stalled"):
            continue

        if state == "parole_stalled":
            ledger_transition = "parole_stalled"
            days_open = _days_since(parole_at)
            elapsed = f"{days_open:.1f} days" if days_open is not None else "unknown"
            # A NULL budget renders as the word, not as "None". The stall
            # diagnosis does not depend on the budget — an empty cohort under
            # an open window is the whole finding — so an unknown budget is
            # reported as unknown rather than suppressing an accurate page.
            budget = "unknown" if remaining is None else remaining
            message = (
                f"gecko-alpha: parole retest STALLED for {combo_key}\n"
                f"parole window opened {parole_at} ({elapsed} elapsed), "
                f"slot budget {budget}/{target} remaining, zero admissions "
                f"since the window opened.\n"
                "The window is open and nothing is being admitted, so this "
                "retest cannot start — something DOWNSTREAM of the suppression "
                "gate is blocking every open. Check signal_params.enabled and "
                "SIGNAL_DISPATCH_QUARANTINE for this signal.\n"
                "Suppression is HELD and will not auto-clear."
            )
        else:
            ledger_transition = "terminal_incomplete_held"
            message = (
                f"gecko-alpha: parole retest STUCK for {combo_key}\n"
                f"valid resolved {acct['valid_closed']}/{target} - "
                f"invalid closes {acct['invalid_closed']}, "
                f"still open {acct['open_now']}, slots spent {acct['spent']}.\n"
                "No slots left and nothing still open, so this retest can never "
                "complete. Suppression is HELD and will not auto-clear. "
                "Re-arming a retest requires operator authorization."
            )
        digest = payload_digest(message)
        detected_at = datetime.now(timezone.utc).isoformat()
        log.info("retest_incomplete_alert_dispatched", combo_key=combo_key)
        await record_alert_event(
            db,
            event_type="alert_dispatched",
            combo_key=combo_key,
            alert_source="combo_refresh_retest_terminal_incomplete",
            transition=ledger_transition,
            detected_at=detected_at,
            retry=0,
            payload_hash=digest,
        )
        try:
            await _send_retest_incomplete_alert(message, settings)
        except Exception as exc:
            log.warning(
                "retest_incomplete_alert_failed",
                combo_key=combo_key,
                err=str(exc),
                detail="marker NOT set - will re-attempt on the next refresh",
            )
            await record_alert_event(
                db,
                event_type="alert_failed",
                combo_key=combo_key,
                alert_source="combo_refresh_retest_terminal_incomplete",
                transition=ledger_transition,
                detected_at=detected_at,
                delivery_result=f"error:{type(exc).__name__}",
                retry=0,
                payload_hash=digest,
                detail="marker NOT set - will re-attempt on the next refresh",
            )
            continue
        log.info("retest_incomplete_alert_delivered", combo_key=combo_key)
        await record_alert_event(
            db,
            event_type="alert_delivered",
            combo_key=combo_key,
            alert_source="combo_refresh_retest_terminal_incomplete",
            transition=ledger_transition,
            detected_at=detected_at,
            delivery_result="ok",
            retry=0,
            payload_hash=digest,
        )

        # Delivery is done; the DB mutation is not. Reacquire `_txn_lock` for
        # the short marker write: every commit on the shared connection must
        # hold it, or concurrent writers interleave executes and leave
        # half-open transactions. Only the NETWORK call belongs outside.
        try:
            async with db._txn_lock:
                cur = await conn.execute(
                    "UPDATE combo_performance "
                    "SET retest_incomplete_alerted_at = ? "
                    "WHERE combo_key = ? AND window = '30d' "
                    "  AND suppressed_at IS ? AND parole_at IS ?",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        combo_key,
                        suppressed_at,
                        parole_at,
                    ),
                )
                # THIS statement's affected-row count — never
                # `Connection.total_changes`, which is connection-wide and
                # cumulative. Because delivery ran unlocked, an unrelated
                # coroutine can write on the shared connection around this
                # point; a total_changes delta would then read as success even
                # when this generation-bound UPDATE matched zero rows, exactly
                # defeating the stale-generation check.
                changed = cur.rowcount
                if changed:
                    await record_alert_event(
                        db,
                        event_type="marker_stamped",
                        combo_key=combo_key,
                        transition="retest_incomplete_alerted_at",
                        detected_at=detected_at,
                        payload_hash=digest,
                        state_json=encode_state(
                            suppressed_at=suppressed_at,
                            parole_at=parole_at,
                            parole_trades_remaining=remaining,
                        ),
                        managed_txn=True,
                    )
                await conn.commit()
            if changed == 0:
                # The generation moved while we were sending. The marker is
                # deliberately NOT stamped onto the replacement generation —
                # that would silence the page the new generation is entitled
                # to. This run's page described a state that no longer exists.
                log.info(
                    "retest_incomplete_marker_stale_generation",
                    combo_key=combo_key,
                    detail="parole generation changed during delivery; marker "
                    "not written, new generation will page on its own merits",
                )
                await record_alert_event(
                    db,
                    event_type="marker_anomaly",
                    combo_key=combo_key,
                    transition="retest_incomplete_alerted_at",
                    detected_at=detected_at,
                    delivery_result="stale_generation",
                    payload_hash=digest,
                    state_json=encode_state(
                        suppressed_at=suppressed_at,
                        parole_at=parole_at,
                        parole_trades_remaining=remaining,
                    ),
                    detail="parole generation changed during delivery; marker "
                    "not written, new generation will page on its own merits",
                )
                continue
            alerted.append(combo_key)
        except aiosqlite.Error as exc:
            log.exception(
                "retest_incomplete_marker_update_failed",
                combo_key=combo_key,
                err=str(exc),
            )
    return alerted


# The preserve-branch classifications, i.e. every `_classify_retest` outcome
# that does NOT move the suppression state. `terminal_incomplete` is spelled
# `_held` in the ledger: the classifier names the STATE, the ledger names what
# the refresh DID about it.
_RETEST_CLASSIFICATIONS = (
    "waiting",
    "contaminated",
    "accounting_inconsistent",
    "terminal_incomplete_held",
    "parole_stalled",
)


async def _prior_classification(db: Database, combo_key: str) -> str | None:
    """The last preserve-branch diagnosis recorded for `combo_key`, or None.

    Read back out of `alert_events` rather than tracked in a new column: the
    ledger already IS the durable record of what the previous refresh
    concluded, and a second copy of that state would be one more thing that can
    disagree with it.

    Scoped to `_RETEST_CLASSIFICATIONS` so the state-moving transitions
    (`newly_suppressed`, `clear`, `page_rearm`, ...) cannot be mistaken for a
    diagnosis — otherwise a re-arm sitting on top of `contaminated` would read
    as "the classification changed" on the next refresh and page-adjacent noise
    would resume.

    Ordered by `id`, not `created_at`: two rows written inside the same refresh
    share a timestamp to the microsecond, and insertion order is the only total
    order that is actually reliable here.
    """
    if db._conn is None:
        return None
    placeholders = ",".join("?" * len(_RETEST_CLASSIFICATIONS))
    try:
        cur = await db._conn.execute(
            "SELECT transition FROM alert_events "
            "WHERE combo_key = ? AND event_type = 'suppression_transition' "
            f"  AND transition IN ({placeholders}) "
            "ORDER BY id DESC LIMIT 1",
            (combo_key, *_RETEST_CLASSIFICATIONS),
        )
        row = await cur.fetchone()
    except aiosqlite.Error as exc:
        # Never break the refresh over a ledger read. Unknown prior means the
        # classification is treated as changed, which over-records by at most
        # one row — the safe direction for an audit trail.
        log.warning(
            "prior_classification_read_failed",
            combo_key=combo_key,
            err=str(exc),
        )
        return None
    return row[0] if row else None


def _days_since(iso_ts: str | None) -> float | None:
    """Whole-and-fractional days since `iso_ts`, or None if unparsable.

    Returns None rather than 0.0 on a bad timestamp so a page can say
    "unknown" instead of asserting the window opened this instant.
    """
    if iso_ts is None:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def _classify_retest(acct: dict, remaining, target: int) -> str:
    """Which retest state is this parole generation in?

    ``complete`` requires agreement on TWO axes — enough valid resolved
    outcomes AND an exhausted slot budget. Calendar membership, slot
    consumption and usable evidence are independent facts.

    ``terminal_incomplete`` is the state that cannot resolve itself: no slots
    left, nothing still open, and fewer valid outcomes than required (because
    some closes were operator-manual / fabricated-provenance, or a slot was
    deliberately leaked by an ambiguous D1 outcome). Distinguishing it from
    ``waiting`` is the difference between "be patient" and "this is stuck".
    """
    slots_exhausted = remaining is not None and remaining <= 0
    # Order matters. "Enough evidence but slots remain" is checked FIRST
    # because it strictly implies contamination (cohort >= target > spent), so
    # testing `contaminated` first would make this branch unreachable and
    # report the less specific diagnosis.
    if acct["valid_closed"] >= target and not slots_exhausted:
        return "accounting_inconsistent"
    if acct["contaminated"]:
        return "contaminated"
    if acct["valid_closed"] >= target:
        return "complete"
    # LIVELOCK. The parole window is open and NOTHING has ever been admitted —
    # not one trade. `waiting` is the wrong answer because there is nothing to
    # wait for: the retest cannot advance without an admission, and no
    # admission is arriving. Something downstream of the suppression gate is
    # refusing every open, so the combo sits suppressed and no page fires.
    #
    # PARTITIONED ON THE COHORT, NOT THE BUDGET. An earlier revision required
    # `not slots_exhausted`, which split the wrong way: a combo can burn its
    # whole budget and still admit nothing (slots reserved at the gate, then
    # leaked — the pre-#522 no-refund bug did exactly this). Those rows fell
    # through to `terminal_incomplete` and were paged as an evidence-quality
    # problem — "valid resolved 0/5, so this retest can never complete" — which
    # is confidently wrong about a retest that never started.
    #
    # Prod, 2026-08-13: chain_completed and cg_trending_rank+first_signal both
    # had spent=5 with cohort_total=0 and got that page; only
    # losers_contrarian (cohort_total=3) was genuinely terminal_incomplete. An
    # empty cohort is the discriminating fact, and it is independent of how the
    # budget was spent.
    #
    # Checked BEFORE `terminal_incomplete` for that reason, and after
    # `contaminated` / `complete` so a cohort that does have trades is always
    # diagnosed on its evidence first. The two are mutually exclusive anyway:
    # `open_now <= cohort_total`, so `cohort_total == 0` implies
    # `open_now == 0`.
    if acct.get("window_open") and acct["cohort_total"] == 0:
        return "parole_stalled"
    if slots_exhausted and acct["open_now"] == 0:
        return "terminal_incomplete"
    return "waiting"


async def _retest_accounting(
    db: Database, combo_key: str, parole_at: str | None, remaining, target: int
) -> dict:
    """Account for the current parole generation along THREE separate axes.

    Calendar membership, slot consumption and usable resolved evidence are
    distinct facts; treating any one as proof of the others is what let three
    closes clear a suppression earned on 103 trades.

    SCOPE CAVEAT — ``opened_at >= parole_at`` is a TEMPORAL proxy for "admitted
    under this parole", not an admission-membership stamp. It is sound only for
    suppression-managed lanes, i.e. those whose opens pass through
    ``parole_reservation``. It is NOT universally true: ``tg_social`` dispatches
    via ``scout/social/telegram/dispatcher.py`` and never calls the suppression
    gate (see engine.py's SIG-03 note), so such a lane could contribute a
    post-``parole_at`` close without ever consuming a slot. The caller
    falsifies that cheaply by comparing the cohort against slots actually
    spent — see ``contaminated`` below. A durable per-trade generation stamp is
    the real fix and is deliberately NOT attempted here.

    Returns a dict with:
      valid_closed   — closed outcomes passing the canonical validity predicate
      invalid_closed — closed but EXCLUDED (closed_manual, fabricated $0, ...)
      open_now       — admitted, still unresolved
      cohort_total   — every current-generation trade, any state
      spent          — slots consumed = target - remaining
      contaminated   — cohort_total > spent, i.e. a trade committed without
                       consuming a reservation → the cohort is not trustworthy
    """
    empty = dict(
        valid_closed=0,
        invalid_closed=0,
        open_now=0,
        cohort_total=0,
        spent=0,
        contaminated=False,
        window_open=False,
    )
    if parole_at is None:
        return empty
    cur = await db._conn.execute(
        f"""SELECT
              SUM(CASE WHEN {VALID_TERMINAL_OUTCOME_SQL} THEN 1 ELSE 0 END),
              SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END),
              COUNT(*)
            FROM paper_trades
            WHERE signal_combo = ? AND opened_at >= ?""",
        (combo_key, parole_at),
    )
    row = await cur.fetchone()
    if row is None:
        return empty
    valid_closed = row[0] or 0
    open_now = row[1] or 0
    cohort_total = row[2] or 0
    invalid_closed = cohort_total - valid_closed - open_now
    spent = target - remaining if remaining is not None else 0
    return dict(
        valid_closed=valid_closed,
        invalid_closed=invalid_closed,
        open_now=open_now,
        cohort_total=cohort_total,
        spent=spent,
        contaminated=cohort_total > spent,
        window_open=parole_window_open(parole_at),
    )


async def _refresh_combo_locked(db: Database, combo_key: str, settings) -> bool:
    """Inner implementation — called with db._txn_lock already held."""
    try:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        stats = {}
        for window, days in (("7d", 7), ("30d", 30)):
            cur = await db._conn.execute(
                f"""SELECT
                     COUNT(*) AS trades,
                     SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                     SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses,
                     COALESCE(SUM(pnl_usd), 0) AS total_pnl_usd,
                     COALESCE(AVG(pnl_pct), 0) AS avg_pnl_pct
                   FROM paper_trades
                   WHERE signal_combo = ?
                     -- Canonical validity predicate, NOT an exit-status
                     -- whitelist (see paper.VALID_TERMINAL_OUTCOME_SQL).
                     -- Restores the locked spec's "only closed trades count
                     -- (status != 'open')" population, while excluding
                     -- sentinel rows and the fabricated-$0 class.
                     AND {VALID_TERMINAL_OUTCOME_SQL}
                     AND closed_at >= ?""",
                (
                    combo_key,
                    (now - timedelta(days=days)).isoformat(),
                ),
            )
            row = await cur.fetchone()
            trades = row["trades"] or 0
            wins = row["wins"] or 0
            losses = row["losses"] or 0
            total_pnl = float(row["total_pnl_usd"] or 0)
            avg_pct = float(row["avg_pnl_pct"] or 0)
            wr = (100.0 * wins / trades) if trades else 0.0
            stats[window] = dict(
                trades=trades,
                wins=wins,
                losses=losses,
                total_pnl=total_pnl,
                avg_pct=avg_pct,
                wr=wr,
            )

        # 7d row: plain UPSERT.
        w7 = stats["7d"]
        await db._conn.execute(
            "INSERT INTO combo_performance "
            "(combo_key, window, trades, wins, losses, total_pnl_usd, "
            " avg_pnl_pct, win_rate_pct, suppressed, last_refreshed) "
            "VALUES (?, '7d', ?, ?, ?, ?, ?, ?, 0, ?) "
            "ON CONFLICT(combo_key, window) DO UPDATE SET "
            " trades=excluded.trades, wins=excluded.wins, losses=excluded.losses, "
            " total_pnl_usd=excluded.total_pnl_usd, avg_pnl_pct=excluded.avg_pnl_pct, "
            " win_rate_pct=excluded.win_rate_pct, last_refreshed=excluded.last_refreshed, "
            " refresh_failures=0",
            (
                combo_key,
                w7["trades"],
                w7["wins"],
                w7["losses"],
                w7["total_pnl"],
                w7["avg_pct"],
                w7["wr"],
                now_iso,
            ),
        )

        # 30d row: apply suppression rule.
        w30 = stats["30d"]
        cur = await db._conn.execute(
            "SELECT suppressed, parole_trades_remaining, suppressed_at, parole_at, "
            "       retest_incomplete_alerted_at "
            "FROM combo_performance WHERE combo_key = ? AND window = '30d'",
            (combo_key,),
        )
        existing = await cur.fetchone()

        min_trades = settings.FEEDBACK_SUPPRESSION_MIN_TRADES
        wr_thresh = settings.FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT
        parole_days = settings.FEEDBACK_PAROLE_DAYS
        retest = settings.FEEDBACK_PAROLE_RETEST_TRADES

        new_suppressed = 0
        new_suppressed_at = None
        new_parole_at = None
        new_parole_remaining = None
        # F3: the label of whichever branch below fired. The label alone does
        # NOT cause a row — emission is gated on an actual state delta below.
        # Vocabulary is `_classify_reversal`'s, verbatim, so the two axes of
        # this ledger name the same event the same way.
        transition: str | None = None
        transition_detail: str | None = None
        # The preserve-branch diagnosis, when one applies. Kept SEPARATE from
        # `transition` because the two are gated on different things: a
        # transition is worth recording when the STATE moved, a classification
        # when the DIAGNOSIS moved. Conflating them is what made the preserve
        # branches mint a row per refresh.
        classification: str | None = None

        if existing is None:
            # First write — maybe suppress immediately if bad enough.
            if w30["trades"] >= min_trades and w30["wr"] < wr_thresh:
                new_suppressed = 1
                new_suppressed_at = now_iso
                new_parole_at = (now + timedelta(days=parole_days)).isoformat()
                new_parole_remaining = retest
                transition = "newly_suppressed"
                transition_detail = "first_write"
        else:
            was_suppressed = bool(existing["suppressed"])
            remaining = existing["parole_trades_remaining"]
            if not was_suppressed:
                if w30["trades"] >= min_trades and w30["wr"] < wr_thresh:
                    new_suppressed = 1
                    new_suppressed_at = now_iso
                    new_parole_at = (now + timedelta(days=parole_days)).isoformat()
                    new_parole_remaining = retest
                    transition = "newly_suppressed"
            else:
                # PAROLE-COMPLETION GATE. Spec D5 is "14 days locked, then a
                # 5-trade re-test" — so clear/re-suppress may only be decided
                # once that re-test has actually RESOLVED: `retest` valid,
                # closed outcomes admitted under the CURRENT parole generation.
                #
                # `parole_trades_remaining == 0` does NOT prove that, and
                # gating on it was the defect. A slot is spent at the admission
                # GATE, so the counter can reach zero while trades are still
                # open; and the D1 reservation deliberately LEAKS a slot on
                # ambiguous outcomes and process death. Exhaustion is therefore
                # compatible with fewer than `retest` usable closes — which is
                # how three closes came to clear a suppression earned on 103
                # trades.
                #
                # Counting resolved outcomes instead also SUBSUMES the old
                # frozen-lock guard: a locked combo with no retest data has
                # 0 < retest resolved outcomes and routes to preserve, so it
                # still cannot be handed an unapproved auto-revival.
                #
                # Deliberately NOT "require 20 closes to clear": that would
                # silently convert D5's 5-trade parole into a 20-trade
                # experiment and make legitimate recovery unreachable for
                # low-volume lanes. Once the gate is satisfied, the decision
                # keeps the original 30d-WR semantics unchanged.
                parole_at_existing = existing["parole_at"]
                acct = await _retest_accounting(
                    db, combo_key, parole_at_existing, remaining, retest
                )
                retest_state = _classify_retest(acct, remaining, retest)

                if retest_state == "complete" and w30["wr"] >= wr_thresh:
                    # Re-test completed and recovered — clear suppression.
                    new_suppressed = 0
                    new_suppressed_at = None
                    new_parole_at = None
                    new_parole_remaining = None
                    transition = "clear"
                elif retest_state == "complete":
                    # Re-test completed and failed — re-suppress, fresh parole.
                    new_suppressed = 1
                    new_suppressed_at = now_iso
                    new_parole_at = (now + timedelta(days=parole_days)).isoformat()
                    new_parole_remaining = retest
                    transition = "parole_exhausted_resuppressed"
                else:
                    # Every non-complete state PRESERVES. They differ only in
                    # what the operator needs to know, and whether the state
                    # can still resolve on its own.
                    #
                    # These branches PRESERVE the generation verbatim, so the
                    # delta gate below emits nothing for them on an ordinary
                    # nightly hold — which is the point. A suppressed combo
                    # sitting in `waiting` would otherwise mint one identical
                    # row every refresh, forever, and steady-state rows are
                    # exactly the "ordinary log lines" this ledger must not
                    # become. Their durable evidence lives in the
                    # `marker_stamped` / `alert_*` rows and the
                    # `refresh_completed` aggregates instead.
                    #
                    # The label is still computed, because a preserve branch
                    # CAN legitimately move the generation (the `parole_at`
                    # re-read below picks up a concurrent change). If that
                    # happens it is a real state change and the delta gate
                    # records it under its classification.
                    classification = (
                        "terminal_incomplete_held"
                        if retest_state == "terminal_incomplete"
                        else retest_state
                    )
                    if retest_state == "terminal_incomplete":
                        # Slots exhausted, nothing still open, not enough valid
                        # outcomes: this generation can NEVER complete. Deliberately
                        # NOT auto-re-armed — refilling slots would silently turn
                        # D5's 5-trade retest into "trade until 5 usable
                        # observations". Pages once; needs operator action.
                        log.error(
                            "parole_retest_terminal_incomplete",
                            combo_key=combo_key,
                            valid_closed=acct["valid_closed"],
                            invalid_closed=acct["invalid_closed"],
                            open_now=acct["open_now"],
                            slots_spent=acct["spent"],
                            required=retest,
                            detail="parole generation can no longer complete; "
                            "suppression held, operator action required",
                        )
                        # Delivery deliberately NOT done here. This function
                        # runs with `db._txn_lock` held and inside the refresh's
                        # multi-statement read→write sequence, so an aiohttp
                        # await would hold the trading lock for the client
                        # timeout, and the marker's own commit would split this
                        # refresh transaction (making the 7d write durable
                        # before the 30d write lands). Paging happens in
                        # `_process_retest_terminal_incomplete` after the locked
                        # per-combo refreshes, mirroring
                        # `_process_permanent_suppression`.
                    elif retest_state == "contaminated":
                        log.error(
                            "parole_retest_cohort_contaminated",
                            combo_key=combo_key,
                            cohort_total=acct["cohort_total"],
                            slots_spent=acct["spent"],
                            detail="committed trades exceed reservations spent "
                            "— a bypass path (a dispatcher that never calls "
                            "the suppression gate) contributed to this cohort; "
                            "holding, no automatic decision",
                        )
                    elif retest_state == "accounting_inconsistent":
                        log.error(
                            "parole_retest_accounting_inconsistent",
                            combo_key=combo_key,
                            valid_closed=acct["valid_closed"],
                            required=retest,
                            remaining=remaining,
                            detail="enough resolved outcomes but slots still "
                            "remain — inconsistent with a clean parole; holding",
                        )
                    else:
                        log.info(
                            "parole_retest_waiting",
                            combo_key=combo_key,
                            valid_closed=acct["valid_closed"],
                            open_now=acct["open_now"],
                            remaining=remaining,
                            required=retest,
                        )
                    new_suppressed = 1
                    new_suppressed_at = existing["suppressed_at"]
                    cur2 = await db._conn.execute(
                        "SELECT parole_at FROM combo_performance "
                        "WHERE combo_key = ? AND window = '30d'",
                        (combo_key,),
                    )
                    new_parole_at = (await cur2.fetchone())[0]
                    new_parole_remaining = remaining

        # Re-arm the terminal-incomplete page whenever the combo LEAVES that
        # state — cleared suppression, or a fresh parole generation. Preserve
        # branches keep it, so a stuck generation pages exactly once.
        rearm_window_open = existing is not None and (
            new_suppressed == 0 or new_parole_at != existing["parole_at"]
        )
        if rearm_window_open:
            await db._conn.execute(
                "UPDATE combo_performance "
                "SET retest_incomplete_alerted_at = NULL "
                "WHERE combo_key = ? AND window = '30d'",
                (combo_key,),
            )
        # F3: only a re-arm that actually CLEARED a stamped marker is an event.
        # The UPDATE above is unconditional inside its window and matches every
        # unsuppressed combo on every refresh, so keying the ledger row on the
        # UPDATE firing would write one row per healthy combo per run — noise
        # that would bury the transitions this ledger exists to preserve.
        page_rearmed = (
            rearm_window_open and existing["retest_incomplete_alerted_at"] is not None
        )

        await db._conn.execute(
            "INSERT INTO combo_performance "
            "(combo_key, window, trades, wins, losses, total_pnl_usd, "
            " avg_pnl_pct, win_rate_pct, suppressed, suppressed_at, parole_at, "
            " parole_trades_remaining, refresh_failures, last_refreshed) "
            "VALUES (?, '30d', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?) "
            "ON CONFLICT(combo_key, window) DO UPDATE SET "
            " trades=excluded.trades, wins=excluded.wins, losses=excluded.losses, "
            " total_pnl_usd=excluded.total_pnl_usd, avg_pnl_pct=excluded.avg_pnl_pct, "
            " win_rate_pct=excluded.win_rate_pct, "
            " suppressed=excluded.suppressed, suppressed_at=excluded.suppressed_at, "
            " parole_at=excluded.parole_at, "
            " parole_trades_remaining=excluded.parole_trades_remaining, "
            " refresh_failures=0, last_refreshed=excluded.last_refreshed",
            (
                combo_key,
                w30["trades"],
                w30["wins"],
                w30["losses"],
                w30["total_pnl"],
                w30["avg_pct"],
                w30["wr"],
                new_suppressed,
                new_suppressed_at,
                new_parole_at,
                new_parole_remaining,
                now_iso,
            ),
        )

        # F3: record the transition IN this transaction, immediately before the
        # commit that makes it real. These four branches (initial latch,
        # re-latch, clear, parole re-arm) previously emitted NO event at all —
        # they existed only as mutated `combo_performance` columns, so once the
        # row moved on there was nothing left to reconstruct the decision from.
        # `managed_txn=True` binds each row to the state change it describes:
        # both land or neither does.
        ledger_state = encode_state(
            **generation_state(existing, prefix="before"),
            after_suppressed=new_suppressed,
            after_suppressed_at=new_suppressed_at,
            after_parole_at=new_parole_at,
            after_parole_trades_remaining=new_parole_remaining,
            trades_30d=w30["trades"],
            win_rate_pct_30d=w30["wr"],
        )
        # THE DELTA GATE. A row is written only when the generation quadruple
        # actually moved. Labelling a branch is not the same as changing state:
        # the preserve branches (waiting / contaminated /
        # accounting_inconsistent / terminal_incomplete_held) re-write the same
        # values every night, and gating on the label alone minted one identical
        # row per suppressed combo per refresh, forever — confirmed empirically
        # at 3 identical rows over 3 refreshes. That is a log line wearing a
        # ledger's clothes, and it buries the four transitions that matter.
        #
        # `existing is None` counts as a delta: a first write has no prior state
        # to be equal to.
        state_moved = existing is None or (
            (
                existing["suppressed"],
                existing["suppressed_at"],
                existing["parole_at"],
                existing["parole_trades_remaining"],
            )
            != (new_suppressed, new_suppressed_at, new_parole_at, new_parole_remaining)
        )
        if transition is not None and state_moved:
            await record_alert_event(
                db,
                event_type="suppression_transition",
                combo_key=combo_key,
                transition=transition,
                detected_at=now_iso,
                state_json=ledger_state,
                detail=transition_detail,
                managed_txn=True,
            )

        # CLASSIFICATION CHANGES. A preserve branch moves no state, so the delta
        # gate above never fires for it — and yet `contaminated` and
        # `accounting_inconsistent` are diagnoses the operator needs a durable
        # record of, and neither one pages. Recording them every refresh was the
        # original defect; recording them on CHANGE keeps steady state silent
        # while making entry into an anomaly (and exit from it) reconstructable.
        #
        # The prior diagnosis is read back out of the ledger itself, so this
        # needs no schema change and no new column: the newest
        # classification-class row for this combo IS the previous answer.
        #
        # Why the whole class and not only the two anomalies: if exits were not
        # recorded, `contaminated -> waiting -> contaminated` would look like
        # one unbroken `contaminated` and the re-entry would be swallowed.
        # "Changed" is only computable if both directions are written.
        if classification is not None:
            prior = await _prior_classification(db, combo_key)
            if classification != prior or state_moved:
                await record_alert_event(
                    db,
                    event_type="suppression_transition",
                    combo_key=combo_key,
                    transition=classification,
                    detected_at=now_iso,
                    state_json=ledger_state,
                    detail=(
                        f"classification {prior or 'none'} -> {classification}"
                        if classification != prior
                        else "generation moved on a preserve branch"
                    ),
                    managed_txn=True,
                )
        if page_rearmed:
            # Separate row, not a field on the one above: the re-arm can fire
            # alongside `clear` or `relatch`, and collapsing them would make the
            # "did this generation get its page back?" question unanswerable.
            await record_alert_event(
                db,
                event_type="suppression_transition",
                combo_key=combo_key,
                transition="page_rearm",
                detected_at=now_iso,
                state_json=ledger_state,
                managed_txn=True,
            )
        await db._conn.commit()
        return True
    except (aiosqlite.Error, ValueError) as e:
        log.error(
            "combo_refresh_error",
            combo_key=combo_key,
            err=str(e),
            err_id="COMBO_REFRESH",
        )
        try:
            # Scope failure increment to the 30d window only: the 30d row drives
            # suppression decisions and the chronic-failure alert. Incrementing
            # both windows caused the alert to fire at half the expected day count
            # (each single refresh failure would increment two rows, so the
            # chronic threshold appeared reached after threshold/2 days).
            await db._conn.execute(
                "UPDATE combo_performance SET refresh_failures = refresh_failures + 1 "
                "WHERE combo_key = ? AND window = '30d'",
                (combo_key,),
            )
            await db._conn.commit()
        except Exception as counter_err:
            # The chronic-failure surfacing in weekly_digest depends on this
            # counter incrementing — if the counter write itself fails, log
            # loudly so the operator notices the counter is blind, not silent.
            log.exception(
                "combo_refresh_failure_counter_update_failed",
                combo_key=combo_key,
                err=str(counter_err),
                err_id="COMBO_REFRESH_COUNTER",
            )
        return False


async def refresh_all(db: Database, settings) -> dict:
    """Rebuild `combo_performance` for every combo whose rolling-window
    economics can have moved since the last refresh.

    THE ENUMERATION INVARIANT: every persisted combo whose 7d/30d economic
    state can change because outcomes entered or aged out of the rolling window
    must be refreshed.

    Membership used to be bound to ``opened_at`` while ``refresh_combo``
    computes economics from ``VALID_TERMINAL_OUTCOME_SQL AND closed_at >=
    cutoff`` — two different columns. A combo whose opens aged out while its
    closes stayed inside the window was therefore never re-selected and its row
    froze indefinitely. On 2026-08-13 ``volume_spike`` had ``MAX(opened_at)``
    69 minutes before the cutoff, 21 valid closes INSIDE the window, and was
    unsuppressed — so it was excluded entirely and its row stayed byte-stale;
    seven unsuppressed 30d rows carried stale economics, some since May. The
    three arms below close that gap:

    1. recent opens (``opened_at >= cutoff``) — KEPT deliberately. It is the
       only arm that sees a combo which has opened but not yet closed anything,
       so it protects cold start and first-materialization.
    2. recent VALID closes (``closed_at >= cutoff``) — binds membership to the
       same column and the same canonical validity predicate the economics use.
       Bound to the predicate, not merely to closedness: otherwise the
       fabricated-$0 and operator-action rows that the economics deliberately
       exclude would manufacture membership for a combo that has no row.
    3. every existing 30d key — the only arm that can refresh a row DOWN as its
       outcomes age out of the window. It also strictly SUBSUMES the former
       ``suppressed = 1`` arm (fix/frozen-suppression-lock), which is why that
       arm is gone rather than merely redundant: a suppressed combo blocks its
       own trades, so under a trade-only refresh set it would fall out, never be
       refreshed again, and latch at ``parole_exhausted`` indefinitely with no
       operator notification (indefinitely, not irreversibly — a passing retest
       still clears it; nothing was starting one). ``refresh_combo`` still preserves its suppressed
       state verbatim for zero-trade combos (no auto-revival — constraint a).

    The window comes from ``FEEDBACK_REFRESH_WINDOW_DAYS``.

    Returns ``{"refreshed": N, "failed": M, "chronic_failures": [keys],
    "permanent_suppression": [keys], "suppression_reversals": [dicts]}`` where
    ``permanent_suppression`` lists the combos newly alerted this run as
    entering the suppressed-and-idle state (the key name predates the rename
    and is kept for wire compatibility), and ``suppression_reversals`` lists the combos
    §12b-alerted this run for a reversal of their 30d suppression state. Since
    fix/reversal-alert-durable-retry that list is "DELIVERED this run", not
    "transitioned this run": it can include pages first detected on an EARLIER
    refresh whose delivery had failed (see :func:`_process_suppression_reversals`).
    """
    window_days = settings.FEEDBACK_REFRESH_WINDOW_DAYS
    # INF-04: opened_at is stored as Python .isoformat() ('T'-separated,
    # +00:00). Bind an ISO-8601 cutoff in the SAME format and compare
    # like-for-like so the window is a true N-day rolling bound, not the
    # 'T'>' ' day-boundary artifact of comparing against datetime('now', ...).
    # Still Settings-driven (no literal window in the SQL) and index-usable
    # (opened_at is not function-wrapped).
    window_cutoff = sql_utc_cutoff(days=window_days)
    cur = await db._conn.execute(
        # Arm 1 — recent opens. Kept: the only arm that sees a combo which has
        # opened but not yet closed anything (cold start / materialization).
        "SELECT DISTINCT signal_combo AS combo FROM paper_trades "
        "WHERE signal_combo IS NOT NULL "
        "  AND opened_at >= ? "
        "UNION "
        # Arm 2 — recent VALID closes. Same column AND same predicate the
        # economics use, so membership can no longer disagree with them.
        "SELECT DISTINCT signal_combo AS combo FROM paper_trades "
        "WHERE signal_combo IS NOT NULL "
        f"  AND {VALID_TERMINAL_OUTCOME_SQL} "
        "  AND closed_at >= ? "
        "UNION "
        # Arm 3 — every existing 30d key. The only arm that can refresh a row
        # DOWN as its outcomes age out. Strictly subsumes the former
        # `AND suppressed = 1` arm, which is why that predicate is gone.
        "SELECT combo_key AS combo FROM combo_performance WHERE window = '30d'",
        (window_cutoff, window_cutoff),
    )
    rows = await cur.fetchall()
    combos = [r[0] for r in rows if r[0]]

    # §12b: snapshot the 30d suppression state BEFORE the refresh loop so a
    # transition into (or re-latch of) suppression can be detected afterward
    # and the operator alerted. Reads only — no mutation.
    pre_state = await _snapshot_suppression_state(db._conn)

    refreshed = 0
    failed = 0
    for combo in combos:
        ok = await refresh_combo(db, combo, settings)
        if ok:
            refreshed += 1
        else:
            failed += 1

    post_state = await _snapshot_suppression_state(db._conn)

    cur = await db._conn.execute(
        "SELECT combo_key FROM combo_performance "
        "WHERE window = '30d' AND refresh_failures >= ?",
        (settings.FEEDBACK_CHRONIC_FAILURE_THRESHOLD,),
    )
    chronic = [r[0] for r in await cur.fetchall()]
    for key in chronic:
        log.warning(
            "combo_refresh_chronic_failure",
            combo_key=key,
        )

    reversals = await _process_suppression_reversals(
        db, settings, pre_state, post_state
    )

    permanent = await _process_permanent_suppression(db, settings, window_cutoff)

    # Also outside the per-combo lock, for the same reason: Telegram delivery
    # must not run inside the refresh transaction.
    retest_stuck = await _process_retest_terminal_incomplete(db, settings)

    log.info(
        "combo_refresh_summary",
        refreshed=refreshed,
        failed=failed,
        chronic=len(chronic),
        permanent_suppression=len(permanent),
        suppression_reversals=len(reversals),
        retest_terminal_incomplete=len(retest_stuck),
    )
    # F3 §12a heartbeat: exactly one row per `refresh_all` run, unconditionally.
    # Every other event in this ledger is conditional, so without this row a
    # table full of fresh conditional events would read "healthy" during exactly
    # the stretch in which the refresh pass had stopped running. The watchdog
    # keys its freshness check on this event type for that reason. Its own short
    # transaction (`managed_txn=False`): the per-combo transactions are already
    # committed and this summarises them rather than participating in one.
    await record_alert_event(
        db,
        event_type="refresh_completed",
        state_json=encode_state(
            refreshed=refreshed,
            failed=failed,
            chronic=len(chronic),
            permanent_suppression=len(permanent),
            suppression_reversals=len(reversals),
            retest_terminal_incomplete=len(retest_stuck),
            combos_enumerated=len(combos),
        ),
    )
    return {
        "refreshed": refreshed,
        "failed": failed,
        "chronic_failures": chronic,
        "permanent_suppression": permanent,
        "suppression_reversals": reversals,
    }


async def _snapshot_suppression_state(conn) -> dict[str, dict]:
    """Return ``{combo_key: {suppressed, parole_at, win_rate_pct, trades}}`` for
    every 30d ``combo_performance`` row.

    Used by :func:`refresh_all` to diff the suppression state across the refresh
    loop so a §12b reversal alert can fire on the exact transition.
    """
    cur = await conn.execute(
        "SELECT combo_key, suppressed, parole_at, win_rate_pct, trades "
        "FROM combo_performance WHERE window = '30d'"
    )
    rows = await cur.fetchall()
    return {
        r["combo_key"]: {
            "suppressed": r["suppressed"],
            "parole_at": r["parole_at"],
            "win_rate_pct": r["win_rate_pct"],
            "trades": r["trades"],
        }
        for r in rows
    }


def _classify_reversal(pre: dict | None, post: dict) -> str | None:
    """Classify a combo's 30d suppression transition as an operator-favorable-
    state reversal, or ``None`` if it is not one.

    * ``newly_suppressed`` — was unsuppressed (or absent) and is now
      ``suppressed = 1``. The dispatcher now blocks the combo's opens; the
      active state the operator relied on has been reversed.
    * ``parole_exhausted_resuppressed`` — stayed suppressed but a FRESH parole
      window was opened (``parole_at`` advanced). ``combo_refresh`` does that on
      exactly one branch: a paroled combo whose retest allowance was exhausted
      and whose real-trade win-rate failed, so it re-latches. The preserve
      branch keeps ``parole_at`` verbatim; the clear branch drops
      ``suppressed`` to 0 — neither matches.

    Naturally deduped: once ``suppressed = 1`` persists, the next refresh sees
    ``pre.suppressed = 1`` with an unchanged ``parole_at`` → no transition.
    """
    if not post["suppressed"]:
        return None
    pre_suppressed = bool(pre["suppressed"]) if pre else False
    if not pre_suppressed:
        return "newly_suppressed"
    if (
        post["parole_at"] is not None
        and pre is not None
        and post["parole_at"] != pre["parole_at"]
    ):
        return "parole_exhausted_resuppressed"
    return None


def _build_reversal_message(combo: str, transition: str, post: dict, settings) -> str:
    """The §12b reversal page body. Content is unchanged from #424 — extracted
    only so the page can be rendered once, at DETECTION time, and persisted.

    Rendering once matters: a retry days later must re-send the page describing
    the transition that actually happened, not one re-derived from state that
    has since moved on.
    """
    wr_thresh = settings.FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT
    parole_days = settings.FEEDBACK_PAROLE_DAYS
    wr = post["win_rate_pct"] or 0.0
    trades = post["trades"] or 0
    revival = (
        f"Revival: db.revive_signal_with_baseline('{combo}', reason=...) for "
        f"a base combo, or clear combo_performance.suppressed for the base "
        f"combo. See docs/runbook_gainers_early_revival.md"
    )
    if transition == "newly_suppressed":
        return (
            f"combo {combo} auto-suppressed (win-rate {wr:.1f}% < "
            f"{wr_thresh:.0f}% over n={trades} trades, 30d) — the dispatcher "
            f"now blocks its opens. {revival}"
        )
    # parole_exhausted_resuppressed
    return (
        f"combo {combo} failed its parole retest (win-rate {wr:.1f}% < "
        f"{wr_thresh:.0f}% over n={trades} trades, 30d) — re-suppressed "
        f"with a fresh {parole_days}d parole window. {revival}"
    )


async def _record_pending_reversals(
    db: Database, settings, pre_state: dict, post_state: dict
) -> str:
    """Persist every reversal detected this refresh BEFORE any delivery.

    Ordering is the whole point: the alert fact has to be durable before the
    network call, or a crash (or a rejected page) between detection and delivery
    loses it — and a reversal is diffed across a single refresh, so it is never
    re-detected.

    LATEST-WINS on a combo that already has an undelivered page. The page
    describes the CURRENT suppression state, so an older undelivered one is
    strictly less useful than the transition that superseded it (a
    newly_suppressed followed by a parole_exhausted_resuppressed would otherwise
    page twice, the first about a state that no longer holds). The superseded
    fact is not lost — it is emitted as ``suppression_reversal_alert_superseded``
    carrying the old transition, its detection time, its rendered body and its
    raw bytes.

    Returns the ISO timestamp stamped on everything recorded by THIS pass. The
    delivery pass compares against it to tell a page it just recorded from one
    carried over from an earlier refresh, which decides whether the body is
    stamped with its detection time.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    # Combos whose UPDATE landed. Only meaningful until the commit succeeds —
    # its failure path uses this to name what the rollback throws away.
    recorded: list[str] = []
    recorded_payloads: dict[str, str] = {}
    # Filled only by the commit-failure path. Written AFTER the lock is
    # released, so the rows are self-committed and survive the rollback that
    # destroyed the pages they describe.
    lost_pages: list[tuple[str, str]] = []
    # Every commit on the shared connection holds `_txn_lock`, or concurrent
    # writers interleave executes and leave half-open transactions. Only the
    # NETWORK call belongs outside it — and no network call happens here.
    async with db._txn_lock:
        for combo in sorted(post_state):
            transition = _classify_reversal(pre_state.get(combo), post_state[combo])
            if transition is None:
                continue
            # Per-combo containment, mirroring the retest-marker sibling. A DB
            # error here used to propagate all the way out of `refresh_all`,
            # skipping BOTH sibling §12b passes — a durability mechanism that
            # silences two older alert paths is a net loss. One bad combo now
            # costs only its own page, retried on the next refresh.
            try:
                payload = json.dumps(
                    {
                        "transition": transition,
                        "detected_at": now_iso,
                        "message": _build_reversal_message(
                            combo, transition, post_state[combo], settings
                        ),
                    },
                    sort_keys=True,
                )
                cur = await db._conn.execute(
                    "SELECT reversal_alert_pending_json FROM combo_performance "
                    "WHERE combo_key = ? AND window = '30d'",
                    (combo,),
                )
                row = await cur.fetchone()
                existing = row[0] if row else None
                superseded_state: str | None = None
                # `is not None`, not truthiness: an empty-string marker is
                # present-but-corrupt, not absent, and silently skipping the
                # supersede log for it would drop the only record that a page
                # was overwritten.
                if existing is not None:
                    try:
                        old = json.loads(existing)
                    except (ValueError, TypeError):
                        old = {}
                    if not isinstance(old, dict):
                        old = {}
                    # The superseded page is an operator notification that will
                    # now NEVER be delivered, and it was journald-only — i.e.
                    # gone within minutes of the 03:00 backup window. Carry its
                    # full content, not a reference to it: the body is the
                    # thing the ledger exists to preserve, and it cannot be
                    # re-derived (the transition it describes is diffed across a
                    # single refresh and never recurs). `superseded_raw` keeps
                    # the exact bytes so an undecodable payload still leaves
                    # evidence of what was overwritten.
                    superseded_state = encode_state(
                        superseded_transition=old.get("transition"),
                        superseded_detected_at=old.get("detected_at"),
                        superseded_message=old.get("message"),
                        superseded_payload_hash=payload_digest(existing),
                        superseded_raw=existing,
                        new_transition=transition,
                    )
                    log.warning(
                        "suppression_reversal_alert_superseded",
                        combo_key=combo,
                        superseded_transition=old.get("transition"),
                        superseded_detected_at=old.get("detected_at"),
                        superseded_message=old.get("message"),
                        # The raw bytes, so an undecodable payload still leaves
                        # evidence of what was overwritten.
                        superseded_raw=existing,
                        new_transition=transition,
                        detail="an undelivered page was replaced by a newer "
                        "transition; the superseded body is preserved here",
                    )
                cur = await db._conn.execute(
                    "UPDATE combo_performance SET reversal_alert_pending_json = ? "
                    "WHERE combo_key = ? AND window = '30d'",
                    (payload, combo),
                )
                # F3: recorded either way, in this same transaction. The
                # rowcount-0 case is the one that most needs durable evidence —
                # it is a page that will never be delivered and never
                # re-detected, and until now it existed only as a log line.
                await record_alert_event(
                    db,
                    event_type="reversal_pending_recorded",
                    combo_key=combo,
                    transition=transition,
                    detected_at=now_iso,
                    delivery_result="ok" if cur.rowcount else "no_30d_row",
                    payload_hash=payload_digest(payload),
                    state_json=superseded_state,
                    detail=(
                        "superseded an undelivered page"
                        if existing is not None
                        else None
                    ),
                    managed_txn=True,
                )
                if cur.rowcount == 0:
                    # No 30d row to hold the marker, so this page cannot be made
                    # durable. Loud, because it is otherwise a silent lost alert.
                    log.error(
                        "suppression_reversal_pending_write_missed",
                        combo_key=combo,
                        transition=transition,
                        detail="no 30d row — page cannot be persisted for retry",
                    )
                else:
                    # Tracked so a failed COMMIT can name what it discards. These
                    # combos succeeded individually and were therefore never
                    # reported by the per-combo handler below. The PAYLOAD is
                    # kept alongside the key because the commit-failure path
                    # must preserve the page bodies, not just their names.
                    recorded.append(combo)
                    recorded_payloads[combo] = payload
            except aiosqlite.Error as exc:
                log.exception(
                    "suppression_reversal_pending_write_failed",
                    combo_key=combo,
                    transition=transition,
                    err=str(exc),
                    detail="page not recorded; refresh continues and the next "
                    "run re-detects nothing — this page is lost",
                )
                continue
        try:
            await db._conn.commit()
        except aiosqlite.Error as exc:
            # Never hand the next writer a foreign open transaction. The rollback
            # discards EVERY page this pass recorded — not just failures — and
            # none of them were reported by the per-combo handler above, which
            # only fires for combos whose own UPDATE raised. So they are NAMED
            # here; otherwise a commit failure silently destroys §12b pages that
            # will never be re-detected.
            log.exception(
                "suppression_reversal_pending_commit_failed",
                err=str(exc),
                lost_combos=recorded,
                lost_count=len(recorded),
                detail="rolling back; the connection must not be left half-open. "
                "Every page listed in lost_combos is discarded and lost.",
            )
            try:
                await db._conn.rollback()
            except aiosqlite.Error:
                log.exception("suppression_reversal_pending_rollback_failed")
            # Hand the bodies to the post-lock writer. They CANNOT be recorded
            # here: every `reversal_pending_recorded` row this pass wrote was
            # `managed_txn=True`, so the rollback just destroyed them too, and
            # another in-transaction write would share the same fate.
            lost_pages = [(c, recorded_payloads[c]) for c in recorded]

    # OUTSIDE the lock, self-committed, one row per lost page. This is the case
    # the ledger most needs to survive: a commit failure silently destroys §12b
    # pages that are diffed across a single refresh and therefore NEVER
    # re-detected — no retry, no re-derivation, no second chance. Until now the
    # only trace was a journald line with a ~minutes half-life on prod.
    for combo, payload in lost_pages:
        try:
            body = json.loads(payload)
        except (ValueError, TypeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        await record_alert_event(
            db,
            event_type="marker_anomaly",
            combo_key=combo,
            transition="pending_commit_lost",
            detected_at=body.get("detected_at"),
            delivery_result="pending_commit_lost",
            payload_hash=payload_digest(payload),
            state_json=encode_state(
                lost_transition=body.get("transition"),
                lost_detected_at=body.get("detected_at"),
                lost_message=body.get("message"),
                lost_raw=payload,
            ),
            detail="the pending-page commit failed and was rolled back; this "
            "page was never delivered and will never be re-detected",
        )
    return now_iso


async def _process_suppression_reversals(
    db: Database, settings, pre_state: dict, post_state: dict
) -> list[dict]:
    """Fire the §12b operator alert for every combo whose 30d suppression state
    reversed operator-favorable (active) state, INCLUDING pages still owed from
    an earlier refresh.

    Covers the two write sites the frozen-refresh permanent-suppression alert
    (#424) does NOT: the initial ``unsuppressed → suppressed`` latch and the
    parole-exhausted re-suppression. gainers_early was combo-suppressed
    2026-06-12 with no operator alert and sat dark 7.5 weeks — this closes that
    gap at the transition itself.

    Two phases, in this order:

    1. record every newly detected transition as PENDING (durable, pre-delivery)
    2. attempt every pending page exactly once, clearing only what is confirmed

    Doing all the recording first is what makes "exactly once per refresh" true
    for a combo that both carries an old pending page and transitions again in
    the same run: the second write supersedes the first, and the single delivery
    pass then sees one row.

    Each alert is plain text (``parse_mode=None``, set in the sender) so the
    underscore-laden combo/signal names and ``revive_signal_with_baseline`` are
    not mangled by MarkdownV1. Dispatched/delivered/failed logs bracket the send
    so a successful delivery is not silent. A delivery failure never breaks
    refresh; the page simply stays pending and the next refresh re-attempts.

    Returns the list of ``{"combo_key", "transition"}`` DELIVERED this run —
    which may include pages first detected on an earlier run.
    """
    run_iso = await _record_pending_reversals(db, settings, pre_state, post_state)

    cur = await db._conn.execute(
        "SELECT combo_key, reversal_alert_pending_json FROM combo_performance "
        "WHERE window = '30d' AND reversal_alert_pending_json IS NOT NULL "
        "ORDER BY combo_key"
    )
    pending = await cur.fetchall()

    alerted: list[dict] = []
    for combo, raw in pending:
        try:
            payload = json.loads(raw)
            message = payload["message"]
            transition = payload["transition"]
        except (ValueError, TypeError, KeyError) as exc:
            # Corrupt durable state. Deliberately left PENDING rather than
            # cleared: an unreadable page is a real lost alert, and a row that
            # complains on every refresh is a far better failure mode than one
            # that quietly deletes itself.
            log.error(
                "suppression_reversal_pending_unreadable",
                combo_key=combo,
                err=str(exc),
                err_type=type(exc).__name__,
                detail="pending payload could not be decoded; left in place",
            )
            await record_alert_event(
                db,
                event_type="marker_anomaly",
                combo_key=combo,
                transition="reversal_alert_pending_json",
                delivery_result="pending_unreadable",
                payload_hash=payload_digest(raw) if isinstance(raw, str) else None,
                detail=f"{type(exc).__name__}: {exc}",
            )
            continue

        # A page recorded on an EARLIER refresh outlives the state it describes:
        # the suppression may have been cleared since — by a later refresh, or by
        # the operator doing exactly what the page instructed. Without a
        # timestamp the body is byte-indistinguishable from a fresh one, so a
        # days-late page reads as current. Stamp only the retries: first-attempt
        # bodies stay byte-identical to #424.
        detected_at = payload.get("detected_at")
        is_retry = detected_at != run_iso
        if is_retry:
            # A payload missing only this key would otherwise render the literal
            # "[detected None]". Classification is unchanged — a missing key is
            # still a retry, it just cannot say when.
            stamp = detected_at if detected_at else "unknown"
            message = f"{message} [detected {stamp}]"

        # `payload_digest(message)` — the body as STAMPED, after the retry
        # suffix is appended. Hashing the pre-stamp text would prove which page
        # was rendered, not which page was sent.
        digest = payload_digest(message)
        log.info(
            "suppression_reversal_alert_dispatched",
            combo_key=combo,
            transition=transition,
            detected_at=detected_at,
            retry=is_retry,
        )
        await record_alert_event(
            db,
            event_type="alert_dispatched",
            combo_key=combo,
            alert_source="combo_refresh_suppression_reversal",
            transition=transition,
            detected_at=detected_at,
            retry=is_retry,
            payload_hash=digest,
        )
        try:
            await _send_suppression_reversal_alert(settings, message)
        except Exception as exc:
            # Never breaks refresh, and no longer loses the page: the marker is
            # untouched, so the next refresh re-attempts this same body.
            log.exception(
                "suppression_reversal_alert_failed",
                combo_key=combo,
                transition=transition,
                err=str(exc),
                err_type=type(exc).__name__,
                detail="page stays pending; the next refresh re-attempts",
            )
            await record_alert_event(
                db,
                event_type="alert_failed",
                combo_key=combo,
                alert_source="combo_refresh_suppression_reversal",
                transition=transition,
                detected_at=detected_at,
                delivery_result=f"error:{type(exc).__name__}",
                retry=is_retry,
                payload_hash=digest,
                detail="page stays pending; the next refresh re-attempts",
            )
            continue
        log.info(
            "suppression_reversal_alert_delivered",
            combo_key=combo,
            transition=transition,
        )
        await record_alert_event(
            db,
            event_type="alert_delivered",
            combo_key=combo,
            alert_source="combo_refresh_suppression_reversal",
            transition=transition,
            detected_at=detected_at,
            delivery_result="ok",
            retry=is_retry,
            payload_hash=digest,
        )

        # Clear ONLY after a confirmed send, and only if the payload is still
        # the one just delivered. Delivery ran unlocked, so a concurrent refresh
        # could have superseded it mid-flight; a blind clear would drop that
        # newer page. Matching on the exact payload makes the newer one survive.
        try:
            async with db._txn_lock:
                cur = await db._conn.execute(
                    "UPDATE combo_performance "
                    "SET reversal_alert_pending_json = NULL "
                    "WHERE combo_key = ? AND window = '30d' "
                    "  AND reversal_alert_pending_json = ?",
                    (combo, raw),
                )
                # THIS statement's affected-row count — never
                # `Connection.total_changes`, which is connection-wide and
                # cumulative, so an unrelated write would read as success.
                changed = cur.rowcount
                if changed:
                    await record_alert_event(
                        db,
                        event_type="marker_cleared",
                        combo_key=combo,
                        transition="reversal_alert_pending_json",
                        detected_at=detected_at,
                        payload_hash=digest,
                        managed_txn=True,
                    )
                await db._conn.commit()
            if changed == 0:
                # Deliberately NEUTRAL. rowcount 0 means only that the payload
                # this pass delivered is no longer the one on the row — it may
                # have been superseded by a newer transition (still pending) OR
                # already cleared by a concurrent pass (nothing pending). The
                # old wording asserted the first, which is false half the time.
                log.info(
                    "suppression_reversal_pending_not_cleared",
                    combo_key=combo,
                    detail="payload no longer present on the row (superseded or "
                    "already cleared); not cleared by this pass",
                )
                await record_alert_event(
                    db,
                    event_type="marker_anomaly",
                    combo_key=combo,
                    transition="reversal_alert_pending_json",
                    detected_at=detected_at,
                    delivery_result="pending_not_cleared",
                    payload_hash=digest,
                    detail="payload no longer present on the row (superseded or "
                    "already cleared); not cleared by this pass",
                )
        except aiosqlite.Error as exc:
            # Delivered but not cleared: the next refresh re-sends. A duplicate
            # page is the safe direction; silence is not.
            log.exception(
                "suppression_reversal_pending_clear_failed",
                combo_key=combo,
                err=str(exc),
            )
        alerted.append({"combo_key": combo, "transition": transition})

    return alerted


async def _send_suppression_reversal_alert(settings, message: str) -> None:
    """Deliver a §12b suppression-reversal alert via a one-shot Telegram send.

    Mirrors :func:`_send_permanent_suppression_alert`: ``aiohttp`` +
    ``scout.alerter`` are imported lazily (a module-level ``import aiohttp``
    aborts the interpreter on Windows dev boxes via OpenSSL Applink, and is
    pointless on any run with no reversal). Tests monkeypatch this function so
    the real aiohttp import never runs.

    ``raise_on_failure=True`` is LOAD-BEARING, not decoration. The alerter
    defaults to ``False`` and merely LOGS a non-200 or a network error, so
    without it a rejected page returns normally: the caller then emits
    ``suppression_reversal_alert_delivered`` and counts the combo as alerted.
    The reversal is detected by diffing suppression state ACROSS one refresh, so
    the transition never recurs — a swallowed rejection silences that alert
    permanently, not just for this run.
    """
    import aiohttp  # deferred — module-level import aborts on Windows

    from scout import alerter  # deferred — pulls aiohttp at import time

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15)
    ) as session:
        await alerter.send_telegram_message(
            message,
            session,
            settings,
            parse_mode=None,
            source="combo_refresh_suppression_reversal",
            raise_on_failure=True,
        )


async def _process_permanent_suppression(
    db: Database, settings, window_cutoff: str
) -> list[str]:
    """Detect combos entering the suppressed-and-idle state and fire the §12b
    operator alert once per entry.

    A combo is *suppressed and idle* when its 30d row is ``suppressed = 1`` AND
    it has NO trade opened within the refresh window — i.e. it survives in the
    refresh set only because of the widening in ``refresh_all``.

    This used to be called *permanent* suppression, and that word was wrong in
    the way that matters: the state DOES clear, on a passing parole retest
    (D4). What makes it worth paging is that nothing will start that retest on
    its own — the combo is not trading, so it accrues no evidence, so it stays
    suppressed. Calling it permanent told the operator the wrong remedy.

    Dedup via ``perm_suppression_alerted_at``: fire once, re-arm only when the
    combo leaves the state (becomes unsuppressed OR trades again inside the
    window). Alert-delivery failures never break refresh — the dedup marker is
    set only after a confirmed send, so a transient Telegram outage re-attempts
    on the next run rather than silently dropping the notification.

    Returns the combos newly alerted this run.
    """
    conn = db._conn
    now_iso = datetime.now(timezone.utc).isoformat()

    # `_txn_lock` held for the whole read→write→commit sequence, matching both
    # sibling marker sites. This block previously committed on the shared
    # connection WITHOUT the lock, which is the interleaving hazard the lock
    # exists to prevent — a concurrent writer's half-finished statements could
    # ride out on this commit.
    async with db._txn_lock:
        # Named BEFORE the UPDATE clears them: the re-arm is a state change the
        # operator cares about (a combo that left suppressed-and-idle gets its
        # page back), and afterwards the evidence is gone. Same stamped-marker
        # gating as the retest sibling — only rows that actually HELD a marker
        # are re-arms; the UPDATE's WHERE already requires IS NOT NULL, so this
        # SELECT is the exact set it will clear.
        cur = await conn.execute(
            "SELECT combo_key, perm_suppression_alerted_at FROM combo_performance "
            "WHERE window = '30d' "
            "  AND perm_suppression_alerted_at IS NOT NULL "
            "  AND (suppressed = 0 OR EXISTS ("
            "        SELECT 1 FROM paper_trades pt "
            "        WHERE pt.signal_combo = combo_performance.combo_key "
            "          AND pt.opened_at >= ?))",
            (window_cutoff,),
        )
        rearmed = await cur.fetchall()

        # Re-arm: clear the dedup marker for any combo that has LEFT the
        # suppressed-and-idle state since it was last alerted, so a future
        # re-entry alerts again.
        await conn.execute(
            "UPDATE combo_performance "
            "SET perm_suppression_alerted_at = NULL "
            "WHERE window = '30d' "
            "  AND perm_suppression_alerted_at IS NOT NULL "
            "  AND (suppressed = 0 OR EXISTS ("
            "        SELECT 1 FROM paper_trades pt "
            "        WHERE pt.signal_combo = combo_performance.combo_key "
            "          AND pt.opened_at >= ?))",
            (window_cutoff,),
        )
        for rearm_combo, stamped_at in rearmed:
            await record_alert_event(
                db,
                event_type="suppression_transition",
                combo_key=rearm_combo,
                transition="page_rearm",
                detected_at=now_iso,
                state_json=encode_state(
                    marker="perm_suppression_alerted_at",
                    cleared_stamp=stamped_at,
                ),
                detail="perm_suppression_alerted_at",
                managed_txn=True,
            )

        # Pending = suppressed, no recent trade, not yet alerted for this entry.
        cur = await conn.execute(
            "SELECT combo_key FROM combo_performance cp "
            "WHERE cp.window = '30d' "
            "  AND cp.suppressed = 1 "
            "  AND cp.perm_suppression_alerted_at IS NULL "
            "  AND NOT EXISTS ("
            "        SELECT 1 FROM paper_trades pt "
            "        WHERE pt.signal_combo = cp.combo_key "
            "          AND pt.opened_at >= ?)",
            (window_cutoff,),
        )
        pending = [r[0] for r in await cur.fetchall()]
        await conn.commit()

    window_days = settings.FEEDBACK_REFRESH_WINDOW_DAYS
    alerted: list[str] = []
    for combo in pending:
        message = (
            f"combo {combo} is SUPPRESSED AND IDLE: no trades in "
            f">{window_days}d and still suppressed, so it is not accumulating "
            f"the retest evidence that would clear it.\n"
            "This is not permanent — it clears on a passing parole retest — "
            "but nothing will start that retest on its own.\n"
            "TWO INDEPENDENT gates can hold a signal down, and they clear "
            "differently:\n"
            "- combo suppression (combo_performance.suppressed): clears ONLY "
            "when a parole retest completes and passes. Operator action does "
            "not clear it directly.\n"
            "- signal suspension (signal_params.enabled=0): clears ONLY via "
            "db.revive_signal_with_baseline(signal_type=...).\n"
            f"On a BASE combo (combo_key == a signal_type, i.e. {combo} has no "
            "'+'), revive_signal_with_baseline ALSO re-opens this combo's "
            "parole window and refills its retest allowance — it does not "
            "clear the suppression, it lets the retest begin. On a "
            "multi-signal combo it does nothing to the combo axis at all.\n"
            "See docs/runbook_gainers_early_revival.md"
        )
        # §12b: dispatched/delivered/failed logs bracket the send so a
        # successful delivery is NOT silent. parse_mode=None is set inside the
        # sender — the body carries signal names + revive_signal_with_baseline
        # whose underscores MarkdownV1 would mangle without an error.
        digest = payload_digest(message)
        log.info("permanent_suppression_alert_dispatched", combo_key=combo)
        await record_alert_event(
            db,
            event_type="alert_dispatched",
            combo_key=combo,
            alert_source="combo_refresh_permanent_suppression",
            detected_at=now_iso,
            retry=0,
            payload_hash=digest,
        )
        try:
            await _send_permanent_suppression_alert(settings, message)
        except Exception as exc:
            # Alert failure must never break refresh. Leave the dedup marker
            # NULL so the next run re-attempts — the operator MUST eventually be
            # told; the failed log surfaces the outage in the meantime.
            log.exception(
                "permanent_suppression_alert_failed",
                combo_key=combo,
                err=str(exc),
                err_type=type(exc).__name__,
            )
            await record_alert_event(
                db,
                event_type="alert_failed",
                combo_key=combo,
                alert_source="combo_refresh_permanent_suppression",
                detected_at=now_iso,
                delivery_result=f"error:{type(exc).__name__}",
                retry=0,
                payload_hash=digest,
                detail="dedup marker left NULL; the next run re-attempts",
            )
            continue
        log.info("permanent_suppression_alert_delivered", combo_key=combo)
        await record_alert_event(
            db,
            event_type="alert_delivered",
            combo_key=combo,
            alert_source="combo_refresh_permanent_suppression",
            detected_at=now_iso,
            delivery_result="ok",
            retry=0,
            payload_hash=digest,
        )

        # Set the dedup marker only after a confirmed send. `_txn_lock` held for
        # the write+commit, matching both sibling marker sites — only the
        # NETWORK call above belongs outside it.
        try:
            async with db._txn_lock:
                await conn.execute(
                    "UPDATE combo_performance SET perm_suppression_alerted_at = ? "
                    "WHERE combo_key = ? AND window = '30d'",
                    (now_iso, combo),
                )
                await record_alert_event(
                    db,
                    event_type="marker_stamped",
                    combo_key=combo,
                    transition="perm_suppression_alerted_at",
                    detected_at=now_iso,
                    payload_hash=digest,
                    managed_txn=True,
                )
                await conn.commit()
        except aiosqlite.Error as exc:
            log.exception(
                "permanent_suppression_marker_update_failed",
                combo_key=combo,
                err=str(exc),
            )
        alerted.append(combo)

    return alerted


async def _send_permanent_suppression_alert(settings, message: str) -> None:
    """Deliver the §12b permanent-suppression alert via a one-shot Telegram send.

    ``aiohttp`` + ``scout.alerter`` are imported lazily: a module-level
    ``import aiohttp`` aborts the interpreter on Windows dev boxes (OpenSSL
    Applink), and importing it is pointless on any run with no
    permanent-suppression combo. Tests monkeypatch this function so the real
    aiohttp import never runs.

    ``raise_on_failure=True`` is LOAD-BEARING, not decoration. The alerter
    defaults to ``False`` and merely LOGS a non-200 or a network error, so
    without it the caller would emit ``permanent_suppression_alert_delivered``
    and stamp ``perm_suppression_alerted_at`` for a page Telegram rejected — and
    that dedup marker then suppresses every future attempt until the combo
    leaves the state. "Set the dedup marker only after a confirmed send" is only
    a true statement if failure propagates.
    """
    import aiohttp  # deferred — module-level import aborts on Windows

    from scout import alerter  # deferred — pulls aiohttp at import time

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15)
    ) as session:
        await alerter.send_telegram_message(
            message,
            session,
            settings,
            parse_mode=None,
            source="combo_refresh_permanent_suppression",
            raise_on_failure=True,
        )
