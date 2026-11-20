"""The declared state machines of gecko-alpha.

Each entry was written from the code as it stood at registration and then
checked by independent review. What the tests can and cannot hold in place:

- STATE LISTS are compared with the SQLite CHECK constraint, the Enum or the
  Literal where one exists, with the status literals in scout/trading for
  ``paper_trade``, and with the dashboard's copies for ``signal_lane`` and
  ``tg_alert_outcome``. For the machines whose column has none of these the
  list is a reviewed description that no test can verify.
- TRANSITIONS are compared with the code only for ``solana_execution`` (the
  lane's own table). Elsewhere a test checks that the function named in
  ``via`` still exists, not that it still performs that move.
- MEANINGS, GUARD KINDS and CAVEATS are prose; nothing checks them.

So: treat an entry as a well-sourced map, and read the function it points at
before relying on a detail. If you change a status value, a transition or
what a state means, update the entry in the same change and run
``python -m scout.statemachines render --write``.

DESCRIPTIVE, NOT PRESCRIPTIVE. Nothing in the trading, alerting or execution
paths imports this module, so declaring a machine here changes no behavior.
It must never be used to define an outcome population — see the note above
``VALID_TERMINAL_OUTCOME_SQL`` in scout/trading/paper.py.
"""

from __future__ import annotations

from scout.statemachines.model import START, Invariant, Machine, State, Transition


def _t(
    srcs: str | tuple[str, ...],
    dsts: str | tuple[str, ...],
    via: str,
    trigger: str,
    guard: str = "none",
    *,
    include_self: bool = False,
) -> tuple[Transition, ...]:
    """Expand a many-to-many transition into individual rows.

    A same-state pair is a real thing — code that rewrites the state a row
    is already in — but in a many-to-many expansion it is usually noise, so
    it is kept only when asked for (``include_self``) or when the call names
    exactly one source and one destination.
    """
    explicit_pair = isinstance(srcs, str) and isinstance(dsts, str)
    src_list = (srcs,) if isinstance(srcs, str) else srcs
    dst_list = (dsts,) if isinstance(dsts, str) else dsts
    return tuple(
        Transition(src=s, dst=d, via=via, trigger=trigger, guard=guard)
        for s in src_list
        for d in dst_list
        if s != d or include_self or explicit_pair
    )


# --------------------------------------------------------------------------
# Paper trading
# --------------------------------------------------------------------------

_PAPER = "scout/trading/paper.py"

_PAPER_CLOSES: tuple[tuple[str, str, str, str], ...] = (
    # (state, label, meaning, trigger)
    (
        "closed_tp",
        "Closed — take profit",
        "Reached the take-profit target. Only the pre-ladder exit rules "
        "produce it; a laddered trade scales out through its legs instead.",
        "price reached take-profit (legacy exit rules)",
    ),
    (
        "closed_sl",
        "Closed — stop loss",
        "Fell to the stop-loss before the first ladder leg filled.",
        "price fell to stop-loss",
    ),
    (
        "closed_expired",
        "Closed — max hold time",
        "Held for the maximum duration. Also the status of forced closes made "
        "with no price or a stale price, so check exit_reason and "
        "exit_provenance before trusting the PnL.",
        "max duration elapsed, or forced close on missing/stale price",
    ),
    (
        "closed_trailing_stop",
        "Closed — trailing stop",
        "Gave back the configured share of its peak gain.",
        "trailing-stop drawdown from peak",
    ),
    (
        "closed_moonshot_trail",
        "Closed — moonshot trail",
        "Trailing stop fired after the moonshot threshold widened the trail.",
        "trailing-stop drawdown after moonshot was armed",
    ),
    (
        "closed_floor",
        "Closed — breakeven floor",
        "Fell back to entry after the first ladder leg had armed the floor.",
        "price returned to entry with the floor armed",
    ),
    (
        "closed_peak_fade",
        "Closed — peak fade",
        "Faded from its peak at both observation checkpoints.",
        "peak-fade rule",
    ),
    (
        "closed_high_peak_fade",
        "Closed — high-peak fade",
        "Retraced from a high peak past the high-peak-fade threshold.",
        "high-peak-fade rule (when not in dry-run)",
    ),
    (
        "closed_momentum_death",
        "Closed — momentum death",
        "Momentum collapsed after entry.",
        "momentum-death rule (when not in dry-run)",
    ),
    (
        "closed_time_death",
        "Closed — time death",
        "Went nowhere for too long.",
        "time-death rule (when not in dry-run)",
    ),
    (
        "closed_stale_onset",
        "Closed — price went stale",
        "The price feed went stale; closed at the last good cached price.",
        "stale-onset exit",
    ),
    (
        "closed_manual",
        "Closed — manual",
        "Closed by the operator. Also what a close with an unrecognised "
        "reason becomes (logged at ERROR as paper_close_reason_unmapped). "
        "Excluded from combo win-rate statistics.",
        "operator close, or a close reason missing from status_map",
    ),
)

_ANCHORS = ("kraken_pilot_anchor", "solana_lane_anchor")
_NOT_ANCHOR = "status NOT IN ('kraken_pilot_anchor', 'solana_lane_anchor')"

PAPER_TRADE = Machine(
    name="paper_trade",
    title="Paper trade lifecycle",
    entity="paper_trades.status",
    storage="column",
    table="paper_trades",
    column="status",
    summary=(
        "One simulated position from open to close. A trade opens once and "
        "closes once; the close status records which exit rule fired."
    ),
    vocabulary="none",
    enforcement="guarded",
    states=(
        State(
            "open",
            "Open",
            "Position is held. The evaluator re-checks it on every pass.",
            initial=True,
        ),
    )
    + tuple(
        State(name, label, meaning, terminal=True)
        for name, label, meaning, _ in _PAPER_CLOSES
    ),
    transitions=_t(
        START, "open", f"{_PAPER}::execute_buy", "a signal opens a trade", "insert"
    )
    + tuple(
        Transition("open", name, f"{_PAPER}::execute_sell", trigger, "cas")
        for name, _, _, trigger in _PAPER_CLOSES
    ),
    foreign_values=_ANCHORS,
    invariants=(
        Invariant(
            "closed_without_closed_at",
            "A closed paper trade must carry its close time.",
            "SELECT COUNT(*) FROM paper_trades "
            "WHERE status LIKE 'closed_%' AND closed_at IS NULL",
            ("paper_trades",),
        ),
        Invariant(
            "open_with_closed_at",
            "An open paper trade must not carry a close time.",
            "SELECT COUNT(*) FROM paper_trades "
            "WHERE status = 'open' AND closed_at IS NOT NULL",
            ("paper_trades",),
        ),
    ),
    caveats=(
        "The column has no CHECK constraint; the close statuses are spelled "
        "at the call sites in scout/trading/evaluator.py and in status_map "
        "in scout/trading/paper.py.",
        "execute_sell maps an unrecognised reason to closed_manual when no "
        "status_override is passed, and closed_manual is excluded from the "
        "combo statistics. The close still happens; it logs "
        "paper_close_reason_unmapped at ERROR so it does not go unnoticed.",
        "'Closed' is re-derived with different predicates across modules "
        "(status != 'open', LIKE 'closed_%', LIKE 'closed%', "
        "VALID_TERMINAL_OUTCOME_SQL); they do not select the same rows.",
        "The two anchor values are sentinel rows inserted by the Kraken pilot "
        "and the Solana lane so their live rows have a paper parent; they are "
        "not trades.",
    ),
)

_LADDER_ON = "remaining_qty IS NOT NULL"
EXIT_LADDER = Machine(
    name="exit_ladder",
    title="Exit ladder (partial take-profit legs and breakeven floor)",
    entity="paper_trades.leg_1_filled_at / leg_2_filled_at / floor_armed",
    storage="derived",
    table="paper_trades",
    scope=_NOT_ANCHOR,
    summary=(
        "How far a trade has scaled out, for open and closed trades alike. "
        "Filling leg 1 arms the breakeven floor, which replaces the "
        "stop-loss from then on."
    ),
    vocabulary="none",
    enforcement="guarded",
    states=(
        State(
            "legacy",
            "No ladder",
            "Opened before the ladder cut-over (no remaining_qty); handled "
            "by the legacy exit rules.",
            terminal=True,
            written=False,
            predicate="remaining_qty IS NULL",
        ),
        State(
            "pre_leg_1",
            "No leg filled",
            "No ladder leg has filled. While such a trade is open the "
            "stop-loss is its downside exit.",
            initial=True,
            predicate=(
                f"{_LADDER_ON} AND leg_1_filled_at IS NULL "
                "AND leg_2_filled_at IS NULL AND COALESCE(floor_armed, 0) = 0"
            ),
        ),
        State(
            "leg_1_filled",
            "Leg 1 filled, floor armed",
            "First partial taken and the breakeven floor armed. The "
            "stop-loss no longer applies. From here on the floor exit, the "
            "trailing (or moonshot) stop, the fade and momentum-death rules, "
            "max hold time, a forced close on a stale or missing price, and "
            "a manual close can all close it (time-death only applies "
            "before leg 1).",
            predicate=(
                f"{_LADDER_ON} AND leg_1_filled_at IS NOT NULL "
                "AND leg_2_filled_at IS NULL AND COALESCE(floor_armed, 0) = 1"
            ),
        ),
        State(
            "leg_2_filled",
            "Leg 2 filled",
            "Both partials taken; the remainder rides the trailing stop.",
            terminal=True,
            predicate=(
                f"{_LADDER_ON} AND leg_1_filled_at IS NOT NULL "
                "AND leg_2_filled_at IS NOT NULL AND COALESCE(floor_armed, 0) = 1"
            ),
        ),
    ),
    transitions=_t(
        START, "pre_leg_1", f"{_PAPER}::execute_buy", "trade opened", "insert"
    )
    + _t(
        "pre_leg_1",
        "leg_1_filled",
        f"{_PAPER}::execute_partial_sell",
        "gain reached the leg-1 threshold",
        "cas",
    )
    + _t(
        "leg_1_filled",
        "leg_2_filled",
        f"{_PAPER}::execute_partial_sell",
        "gain reached the leg-2 threshold",
        "cas",
    ),
    caveats=(
        "execute_partial_sell guards on the leg column being NULL but not on "
        "status = 'open', and it accepts either leg; the order (leg 1 before "
        "leg 2) is enforced only by the caller in evaluate_paper_trades.",
        "A row matching none of the four predicates (for example floor armed "
        "with leg 1 empty) is representable and is what the auditor reports.",
        "The anchor sentinel rows are out of scope: they have no "
        "remaining_qty and would otherwise be counted as legacy trades.",
    ),
)

# --------------------------------------------------------------------------
# Live and shadow ledgers
# --------------------------------------------------------------------------

_ENGINE = "scout/live/engine.py"
_KRAKEN = "scout/live/kraken_pilot.py"
_SOLANA = "scout/live/solana_lane.py"
_LIVE_EVAL = "scout/live/live_evaluator.py"
_RECON = "scout/live/reconciliation.py"

LIVE_TRADE = Machine(
    name="live_trade",
    title="Live trade ledger",
    entity="live_trades.status",
    storage="column",
    table="live_trades",
    column="status",
    summary=(
        "The money ledger for real orders on every venue (Binance engine, "
        "Kraken pilot, Solana lane). One row per order intent."
    ),
    vocabulary="db_check",
    enforcement="declared",
    states=(
        State(
            "open",
            "Open",
            "An order intent is on record: about to be submitted, resting, "
            "or filled and held. The Solana lane records it before the "
            "authorization prompt, so 'open' does not by itself mean "
            "anything was sent.",
            initial=True,
        ),
        State(
            "rejected",
            "Rejected",
            "No position resulted: refused before submission, definitively "
            "refused by Kraken, proved never submitted or failed on chain "
            "(Solana), or cancelled with nothing filled. On the Binance "
            "engine a venue-rejected buy becomes needs_manual_review, not "
            "this.",
            initial=True,
            terminal=True,
        ),
        State(
            "closed_tp",
            "Closed — take profit",
            "Exit filled at take-profit.",
            terminal=True,
        ),
        State(
            "closed_sl",
            "Closed — stop loss",
            "Exit filled at stop-loss.",
            terminal=True,
        ),
        State(
            "closed_duration",
            "Closed — max hold time",
            "Exit filled after the maximum hold time.",
            terminal=True,
        ),
        State(
            "closed_via_reconciliation",
            "Closed — by reconciliation",
            "A supervised Kraken exit or a Solana reverse swap was "
            "reconciled and the position is gone.",
            terminal=True,
        ),
        State(
            "needs_manual_review",
            "Needs manual review",
            "Automation could not establish what happened on the venue. "
            "Check the venue account and the evidence file. Apart from the "
            "Solana lane's own paths and the Kraken cancel command, nothing "
            "in the code moves a row out of this state: it is settled by "
            "hand.",
            attention=True,
        ),
    ),
    transitions=_t(
        START,
        "rejected",
        f"{_ENGINE}::_write_live_reject",
        "refused before submission (mandate, routing, gate)",
        "insert",
    )
    + _t(
        START,
        "open",
        "scout/live/idempotency.py::record_pending_order",
        "recorded just before the venue submit (Binance, Kraken)",
        "insert",
    )
    + _t(
        START,
        "open",
        f"{_SOLANA}::record_solana_intent",
        "recorded before the authorization prompt (Solana)",
        "insert",
    )
    + _t(
        "open",
        ("closed_tp", "closed_sl", "closed_duration"),
        f"{_LIVE_EVAL}::_close_live_trade",
        "exit level crossed and the sell filled",
    )
    + _t(
        "open",
        "closed_via_reconciliation",
        f"{_KRAKEN}::_reconcile_and_close",
        "supervised Kraken exit reconciled",
    )
    + _t(
        "open",
        "closed_via_reconciliation",
        f"{_SOLANA}::_close_reverse_row",
        "Solana reverse swap reconciled",
    )
    + _t(
        "open",
        "needs_manual_review",
        f"{_ENGINE}::_dispatch_live",
        "entry ended without a clean fill (partial, rejected or timed out)",
        "cas",
    )
    + _t(
        "open",
        "needs_manual_review",
        f"{_LIVE_EVAL}::_mark_exit_needs_review",
        "exit sell did not fill",
        "cas",
    )
    + _t(
        "open",
        "needs_manual_review",
        f"{_RECON}::_flag_live_review",
        "boot reconciliation could not match the venue",
        "cas",
    )
    + _t(
        "open",
        "needs_manual_review",
        f"{_KRAKEN}::_resolve_ambiguity",
        "Kraken submission could not be resolved",
    )
    + _t(
        "open",
        "needs_manual_review",
        f"{_KRAKEN}::_resolve_exit_ambiguity",
        "Kraken exit submission could not be resolved",
    )
    + _t(
        "open",
        "needs_manual_review",
        f"{_KRAKEN}::_settle_exit_outcome",
        "Kraken exit order ended with an unreadable or zero fill",
    )
    + _t(
        "open",
        "needs_manual_review",
        f"{_KRAKEN}::_record_partial_exit",
        "a Kraken partial exit fill was already applied once",
    )
    + _t(
        "open",
        "needs_manual_review",
        f"{_KRAKEN}::_reconcile_and_close",
        "Kraken exit reconciliation did not match",
    )
    + _t(
        "open",
        "needs_manual_review",
        f"{_SOLANA}::_handle_ambiguity",
        "Solana submission outcome unknown",
    )
    + _t(
        "open",
        "needs_manual_review",
        f"{_SOLANA}::_handle_reverse_ambiguity",
        "Solana reverse submission outcome unknown",
    )
    + _t(
        "open",
        "rejected",
        f"{_KRAKEN}::place",
        "kill switch found active on the re-check after approval",
    )
    + _t(
        "open",
        "rejected",
        f"{_KRAKEN}::_submit_and_resolve",
        "Kraken definitively refused the order",
    )
    + _t(
        "open",
        "rejected",
        f"{_KRAKEN}::_resolve_ambiguity",
        "resolution proved Kraken never accepted the order",
    )
    + _t(
        "open",
        "rejected",
        f"{_KRAKEN}::_await_and_reconcile",
        "Kraken order ended with nothing filled",
    )
    + _t(
        ("open", "needs_manual_review"),
        "rejected",
        f"{_KRAKEN}::cancel",
        "operator cancel with nothing filled",
        "cas",
    )
    + _t(
        ("open", "needs_manual_review"),
        "rejected",
        f"{_SOLANA}::retire_row",
        "Solana run proved never submitted, or failed on chain",
    )
    + _t(
        "needs_manual_review",
        "open",
        f"{_SOLANA}::_apply_reverse_verdict",
        "reverse swap proved not submitted; the position is still held",
        "python",
    ),
    invariants=(
        Invariant(
            "closed_without_closed_at",
            "A closed live trade must carry its close time. Every code path "
            "sets it; a row settled by hand without closed_at shows up here.",
            "SELECT COUNT(*) FROM live_trades "
            "WHERE status LIKE 'closed_%' AND closed_at IS NULL",
            ("live_trades",),
        ),
        Invariant(
            "open_with_closed_at",
            "An open live trade must not carry a close time.",
            "SELECT COUNT(*) FROM live_trades "
            "WHERE status = 'open' AND closed_at IS NOT NULL",
            ("live_trades",),
        ),
    ),
    caveats=(
        "Most writes are UPDATE ... WHERE id = ? with no check of the prior "
        "status in the statement or the function; where a check exists it "
        "is usually in the caller. Only the transitions marked "
        "compare-and-swap filter on the prior status in the SQL itself.",
        "PilotRunner.cancel (Kraken) always sends the venue cancel, whatever the "
        "row's status, because that reduces exposure. Its ledger write "
        "applies only while the row is open or needs_manual_review, no "
        "exit is recorded and, for the write of rejected, the ledger "
        "records no entry fill; otherwise the row is left as it was and the "
        "evidence records cancel_ledger_unchanged. It exits for review in "
        "two cases: a fill reported on a rejected row, and a row whose "
        "ledger records a fill while the venue shows none.",
        "Manual settlement of needs_manual_review rows is done with SQL "
        "outside the code, so those moves cannot appear in this table.",
        "needs_manual_review means different things per venue: the Binance "
        "engine uses it so a row stops counting as open exposure, while the "
        "Kraken pilot and the Solana lane treat it as blocking the lane.",
        "'Closed' is re-derived three ways: LIKE 'closed_%' "
        "(scout/live/kill_switch.py), LIKE 'closed%' "
        "(scout/live/services/dormancy.py, approval_thresholds.py) and an "
        "explicit tuple (scout/live/mandate.py).",
    ),
)

_SHADOW_EVAL = "scout/live/shadow_evaluator.py"
SHADOW_TRADE = Machine(
    name="shadow_trade",
    title="Shadow trade ledger",
    entity="shadow_trades.status",
    storage="column",
    table="shadow_trades",
    column="status",
    summary=(
        "What the live engine would have done, recorded without sending an "
        "order. It reuses the live ledger's status names; the meanings are "
        "its own."
    ),
    vocabulary="db_check",
    enforcement="declared",
    states=(
        State(
            "open",
            "Open (simulated)",
            "A simulated position: the engine would have entered here. No "
            "order was sent.",
            initial=True,
        ),
        State(
            "rejected",
            "Not entered",
            "A pre-trade gate refused the entry, or order-book depth could "
            "not be fetched.",
            initial=True,
            terminal=True,
        ),
        State(
            "closed_tp",
            "Closed — take profit (simulated)",
            "Take-profit level crossed; exit simulated against the order book.",
            terminal=True,
        ),
        State(
            "closed_sl",
            "Closed — stop loss (simulated)",
            "Stop-loss level crossed; exit simulated against the order book.",
            terminal=True,
        ),
        State(
            "closed_duration",
            "Closed — max hold time (simulated)",
            "Maximum hold time reached; exit simulated against the order book.",
            terminal=True,
        ),
        State(
            "closed_via_reconciliation",
            "Closed — at boot",
            "An exit level was found already crossed when checked at boot.",
            terminal=True,
        ),
        State(
            "needs_manual_review",
            "Review backoff",
            "Could not be evaluated (price or exit depth unavailable) three "
            "times over the row's life. It is picked up again once its "
            "next_review_at has passed and closes normally if a level is "
            "then crossed, so unlike the live ledger this is not a dead end.",
        ),
    ),
    transitions=_t(
        START,
        ("rejected", "open"),
        f"{_ENGINE}::on_paper_trade_opened",
        "the live engine processes a paper-trade open",
        "insert",
    )
    + _t(
        ("open", "needs_manual_review"),
        ("closed_tp", "closed_sl", "closed_duration"),
        f"{_SHADOW_EVAL}::_close_shadow_trade",
        "exit level crossed",
    )
    + _t(
        "open",
        "closed_via_reconciliation",
        f"{_RECON}::_close_crossed_row",
        "exit level found already crossed at boot",
    )
    + _t(
        "open",
        "needs_manual_review",
        f"{_SHADOW_EVAL}::_bump_review",
        "third evaluation failure (review_retries is never reset)",
    )
    + _t(
        "needs_manual_review",
        "needs_manual_review",
        f"{_SHADOW_EVAL}::_bump_review",
        "re-evaluation failed again; next_review_at is pushed out",
    ),
    caveats=(
        "Every UPDATE is WHERE id = ? with no check of the prior status.",
        "Gate-refused and depth-failure rows are written before the engine "
        "branches on mode, so they appear here in live mode as well.",
    ),
)

# --------------------------------------------------------------------------
# Execution lanes
# --------------------------------------------------------------------------

_REC = f"{_SOLANA}::record_execution_state"


def _sol(src: str, dsts: tuple[str, ...], trigger: str) -> tuple[Transition, ...]:
    return _t(src, dsts, _REC, trigger, "table")


SOLANA_EXECUTION = Machine(
    name="solana_execution",
    title="Solana execution run",
    entity="solana_executions.state",
    storage="column",
    table="solana_executions",
    column="state",
    summary=(
        "One swap attempt on the Solana lane, from quote to reconciled "
        "balances. This is the reference implementation: the lane declares "
        "its own transition table and refuses any write that is not in it. "
        "The registry mirrors that table and a test keeps the two equal."
    ),
    vocabulary="db_check",
    enforcement="enforced",
    states=(
        State("quote_created", "Quote created", "A quote was fetched.", initial=True),
        State(
            "transaction_built",
            "Transaction built",
            "The swap transaction was built.",
        ),
        State(
            "simulation_passed",
            "Simulation passed",
            "The exact transaction simulated cleanly.",
        ),
        State(
            "awaiting_authorization",
            "Awaiting authorization",
            "Waiting for the operator (or the mandate) to approve.",
            attention=True,
        ),
        State("authorized", "Authorized", "Approved; not yet signed."),
        State("signed", "Signed", "Signed locally; nothing has been sent."),
        State(
            "submission_attempted",
            "Submission attempted",
            "A send was attempted, so a transaction may exist. The lane is "
            "blocked until the cluster says what happened.",
        ),
        State("landed", "Landed", "The cluster has seen the transaction."),
        State(
            "confirmed",
            "Confirmed",
            "Confirmed by the cluster. Permitted by the table; no code "
            "path writes it today.",
            written=False,
        ),
        State("finalized", "Finalized", "Finalized by the cluster."),
        State(
            "submission_unknown",
            "Submission unknown",
            "The send's outcome could not be determined. Run the lane's "
            "resolve command; do not rebuild or resubmit.",
            attention=True,
        ),
        State(
            "reconciled",
            "Reconciled",
            "Balances were checked against the swap and explained.",
            terminal=True,
        ),
        State(
            "failed",
            "Failed",
            "The run is over. On a forward run no position exists. On a "
            "reverse (exit) run the position is still held and its ledger "
            "row stays open.",
            terminal=True,
        ),
    ),
    transitions=_t(START, "quote_created", _REC, "run starts", "table")
    + _sol("quote_created", ("transaction_built", "failed"), "build")
    + _sol("transaction_built", ("simulation_passed", "failed"), "simulate")
    + _sol(
        "simulation_passed", ("awaiting_authorization", "failed"), "request approval"
    )
    + _sol("awaiting_authorization", ("authorized", "failed"), "approval decision")
    + _sol("authorized", ("signed", "failed"), "sign")
    + _sol("signed", ("submission_attempted", "failed"), "submit")
    + _sol(
        "submission_attempted",
        ("landed", "confirmed", "finalized", "submission_unknown", "failed"),
        "what the cluster reports",
    )
    + _sol("landed", ("confirmed", "finalized", "failed"), "what the cluster reports")
    + _sol(
        "confirmed", ("finalized", "reconciled", "failed"), "finality / reconciliation"
    )
    + _sol("finalized", ("reconciled", "failed"), "reconciliation")
    + _sol(
        "submission_unknown",
        ("landed", "confirmed", "finalized", "failed"),
        "resolver verdict",
    ),
    caveats=(
        "The guard reads the current state and then writes; the read is not "
        "part of the same SQL statement as the write.",
        "When the confirmation poll times out and the resolver then adopts "
        "the transaction as landed, both paths first record the catch-up "
        "state (finalized or landed). An adoption that is not finalized is "
        "a reconciliation mismatch: the run stops at landed for review.",
        "record_execution_state replaces the stored detail JSON whenever a "
        "detail is passed; callers that need to keep earlier keys must merge "
        "them first.",
    ),
)

_EVM = "scout/live/evm/execution.py"
EVM_EXECUTION = Machine(
    name="evm_execution",
    title="EVM submission receipt",
    entity="scout.live.evm.execution.EvmReceipt.state",
    storage="memory",
    summary=(
        "The submission state of one 0x AllowanceHolder transaction. Not "
        "used in production: no production code constructs a receipt, and "
        "the only wired broadcaster is the disabled one."
    ),
    vocabulary="enum",
    enforcement="guarded",
    states=(
        State(
            "not_submitted",
            "Not submitted",
            "Nothing was sent; rebuilding is safe.",
            initial=True,
        ),
        State("pending", "Pending", "Sent; awaiting inclusion."),
        State(
            "unknown",
            "Unknown",
            "The send raised ambiguously. Rebuilding is forbidden: a "
            "transaction may exist.",
            attention=True,
        ),
        State("confirmed", "Confirmed", "Included in a block.", written=False),
        State("finalized", "Finalized", "Final.", terminal=True, written=False),
        State(
            "reverted",
            "Reverted",
            "Executed and reverted.",
            terminal=True,
            written=False,
        ),
        State(
            "replaced",
            "Replaced",
            "Replaced by another transaction with the same nonce.",
            attention=True,
            written=False,
        ),
    ),
    transitions=_t(
        START,
        "not_submitted",
        f"{_EVM}::EvmReceipt",
        "a caller constructs the receipt (only tests do today)",
        "insert",
    )
    + _t(
        "not_submitted",
        ("pending", "unknown"),
        f"{_EVM}::submit_once",
        "broadcast succeeded / raised ambiguously",
        "python",
    ),
    caveats=(
        "There is no transition table; the only guard is submit_once "
        "refusing anything that is not not_submitted.",
        "confirmed, finalized, reverted and replaced can only come from a "
        "Broadcaster.lookup implementation, and none is wired, so pending "
        "and unknown have nowhere to go.",
        "EvmReceipt.is_terminal counts not_submitted as terminal; the "
        "registry does not, because submit_once moves it on.",
    ),
)

# --------------------------------------------------------------------------
# Controls: kill switch, signal lanes, combo suppression
# --------------------------------------------------------------------------

_KS = "scout/live/kill_switch.py"
_KS_LIVE_EVENT = (
    "active_kill_event_id IS NOT NULL AND EXISTS ("
    "SELECT 1 FROM kill_events k "
    "WHERE k.id = live_control.active_kill_event_id "
    # julianday keeps sub-second precision; is_active() treats a kill as
    # expired only when killed_until is strictly before now.
    "AND k.cleared_at IS NULL AND julianday(k.killed_until) {op} julianday('now'))"
)
KILL_SWITCH = Machine(
    name="kill_switch",
    title="Live kill switch",
    entity="live_control.active_kill_event_id (+ kill_events)",
    storage="derived",
    table="live_control",
    summary=(
        "Whether live order placement is halted. Engaged by the daily loss "
        "cap, by the engine on a Binance auth or IP-ban failure, or by the "
        "operator; it stops halting orders when killed_until passes and is "
        "released when cleared."
    ),
    vocabulary="none",
    enforcement="guarded",
    states=(
        State(
            "inactive",
            "Inactive",
            "Live orders are allowed.",
            initial=True,
            predicate="active_kill_event_id IS NULL",
        ),
        State(
            "engaged",
            "Engaged",
            "Live orders are halted until killed_until.",
            attention=True,
            predicate=_KS_LIVE_EVENT.format(op=">="),
        ),
        State(
            "expired_uncleared",
            "Expired, not yet cleared",
            "killed_until has passed, so orders are allowed again, but the "
            "pointer is still set. The per-tick auto-clear normally removes "
            "this at once, and a new kill releases it itself. If it lingers, "
            "the auto-clear tick is not running.",
            attention=True,
            predicate=_KS_LIVE_EVENT.format(op="<"),
        ),
    ),
    transitions=_t(
        START,
        "inactive",
        "scout/db.py::_migrate_live_trading_schema",
        "schema installed",
        "insert",
    )
    + _t(
        "inactive",
        "engaged",
        f"{_KS}::trigger",
        "daily loss cap tripped, automatic engine kill, or operator kill",
        "cas",
    )
    + _t(
        "expired_uncleared",
        "engaged",
        f"{_KS}::trigger",
        "a new kill arrives; the expired event is stamped cleared and the "
        "pointer re-claimed in one transaction",
        "txn",
    )
    + _t(
        "engaged",
        "expired_uncleared",
        f"{_KS}::is_active",
        "killed_until passes (no write: is_active starts answering inactive)",
    )
    + _t(
        ("engaged", "expired_uncleared"),
        "inactive",
        f"{_KS}::clear",
        "operator clear",
    )
    + _t(
        "expired_uncleared",
        "inactive",
        f"{_KS}::auto_clear_if_expired",
        "automatic clear on the next tick or at boot",
        "cas",
    )
    + _t(
        "expired_uncleared",
        "inactive",
        "scripts/clear_expired_kills.py::_apply_clear",
        "the clear-expired-kills script (its caller checks the expiry first)",
        "cas",
    ),
    caveats=(
        "trigger is a real compare-and-swap on the pointer. The operator's "
        "clear releases the kill it read as active; if another process moved "
        "the pointer in between it clears nothing and returns None (the CLI "
        "then says NOT cleared). The automatic "
        "clears (auto_clear_if_expired and the clear-expired-kills script) "
        "apply only while the pointer still names the event they judged "
        "expired.",
        "Before claiming, trigger releases a pointer whose event is "
        "expired, already cleared or missing, so a dead pointer does not "
        "block a new kill. A kill still in force wins the compare-and-swap "
        "as before. A killed_until that cannot be parsed is left in force "
        "and logged at ERROR (live_kill_dead_pointer_unreadable).",
        "Writers that do not take the transaction lock can roll back the "
        "shared connection mid-trigger. Each trigger attempt commits, then "
        "reads the committed pointer back, and reports a win only for a "
        "kill durably in force. An undone attempt is retried, three attempts "
        "in all; an attempt whose commits all fail is never counted as a win. "
        "If none engages a kill it logs live_kill_trigger_unconfirmed at "
        "ERROR, sends a NOT ENGAGED alert and returns (None, False) — or "
        "re-raises the last database error, if there was one. The kill CLI "
        "then exits 1.",
        "A pointer left on an already-cleared event matches no state: the "
        "switch reads as inactive. No automatic clear releases it; the next "
        "trigger does, and so does an operator clear. The auditor reports "
        "that row.",
    ),
)

SIGNAL_LANE = Machine(
    name="signal_lane",
    title="Signal lane (enabled / suspended)",
    entity="signal_params.enabled + suspended_at",
    storage="derived",
    table="signal_params",
    summary=(
        "Whether a signal type may open paper trades. Auto-suspend turns a "
        "losing lane off; only the operator turns it back on."
    ),
    vocabulary="none",
    enforcement="declared",
    states=(
        State(
            "enabled",
            "Enabled",
            "The lane opens trades.",
            initial=True,
            predicate="enabled = 1 AND suspended_at IS NULL",
        ),
        State(
            "disabled",
            "Disabled",
            "Turned off by the operator (no suspension record).",
            written=False,
            predicate="enabled = 0 AND suspended_at IS NULL",
        ),
        State(
            "suspended",
            "Suspended",
            "Turned off automatically after losses; suspended_reason says "
            "which rule fired. Revive it with the revival runbook.",
            attention=True,
            predicate="enabled = 0 AND suspended_at IS NOT NULL",
        ),
    ),
    transitions=_t(
        START,
        "enabled",
        "scout/db.py::_migrate_signal_params_schema",
        "lane seeded",
        "insert",
    )
    + _t(
        "enabled",
        "suspended",
        "scout/trading/auto_suspend.py::_suspend",
        "hard-loss or PnL-threshold rule (the caller selected enabled lanes)",
    )
    + _t(
        ("suspended", "disabled"),
        "enabled",
        "scout/db.py::revive_signal_with_baseline",
        "operator revive",
    ),
    caveats=(
        "No code writes 'disabled'; it only arises from operator SQL.",
        "revive_signal_with_baseline does not require the lane to be off, so "
        "reviving an enabled lane re-stamps its drawdown baseline and "
        "re-paroles its combo.",
        "scout/trading/params.py answers enabled=True without reading the "
        "row when the params table is switched off, the database is closed "
        "or the row is missing. For a signal type that is not a known "
        "default, params_for_signal also answers enabled=True while "
        "get_params raises UnknownSignalType. Table reads and the "
        "missing-row answer are cached per process for five minutes, or "
        "until bump_cache_version is called.",
        "_suspend itself is UPDATE ... WHERE signal_type = ? with no check "
        "that the lane is still enabled; that filter is the caller's earlier "
        "SELECT.",
        "dashboard/lane_status.py derives these three states plus 'unknown'. "
        "A row with enabled = 0 and an unreadable or future-dated "
        "suspended_at is suspended here and unknown there. "
        "SignalTrustTab.jsx shows SUSPENDED for both disabled and suspended.",
        "enabled = 1 with suspended_at set matches no state; the dashboard "
        "calls it conflicting_suspension_metadata and the auditor reports it.",
    ),
)

_COMBO = "scout/trading/combo_refresh.py::_refresh_combo_locked"
_REVIVE = "scout/db.py::revive_signal_with_baseline"
COMBO_SUPPRESSION = Machine(
    name="combo_suppression",
    title="Signal-combo suppression and parole",
    entity="combo_performance (30-day row): suppressed + parole_at + parole_trades_remaining",
    storage="derived",
    table="combo_performance",
    scope="window = '30d'",
    summary=(
        "A signal combination with a poor 30-day win rate is suppressed, "
        "then allowed a limited number of retest trades (parole) to earn "
        "its way back."
    ),
    vocabulary="none",
    enforcement="guarded",
    states=(
        State(
            "active",
            "Not suppressed",
            "Trades on this combination are admitted normally.",
            initial=True,
            predicate="suppressed = 0",
        ),
        State(
            "suppressed",
            "Suppressed, parole slots left",
            "Blocked until parole_at; after that each admitted retest trade "
            "spends one parole slot.",
            initial=True,
            predicate=(
                "suppressed = 1 AND parole_at IS NOT NULL "
                "AND COALESCE(parole_trades_remaining, 0) > 0"
            ),
        ),
        State(
            "parole_exhausted",
            "Suppressed, parole spent",
            "All retest slots are used. Normally the nightly refresh then "
            "judges the retest; a generation whose retest can never "
            "complete stays here until the operator acts.",
            predicate=(
                "suppressed = 1 AND parole_at IS NOT NULL "
                "AND COALESCE(parole_trades_remaining, 0) <= 0"
            ),
        ),
    ),
    transitions=_t(
        START, ("active", "suppressed"), _COMBO, "first nightly refresh", "insert"
    )
    + _t("active", "suppressed", _COMBO, "30-day win rate below threshold", "txn")
    + _t(
        "suppressed",
        "parole_exhausted",
        "scout/trading/suppression.py::_open_gate",
        "last parole slot spent",
        "txn",
    )
    + _t(
        "parole_exhausted",
        "suppressed",
        "scout/trading/suppression.py::_refund_parole_slot",
        "a reserved slot was refunded",
        "cas",
    )
    + _t(
        "parole_exhausted",
        "suppressed",
        _COMBO,
        "retest failed; a fresh suppression generation starts",
        "txn",
    )
    + _t(
        "parole_exhausted",
        "suppressed",
        _REVIVE,
        "operator revive re-opens parole",
        "cas",
    )
    + _t(
        "suppressed",
        "suppressed",
        _REVIVE,
        "operator revive restarts parole from now, ending any lock early",
        "cas",
    )
    + _t("parole_exhausted", "active", _COMBO, "retest passed", "txn"),
    caveats=(
        "Only the 30-day row is the entity. 7-day rows share the columns "
        "and are always written suppressed = 0, so they are out of scope.",
        "suppressed = 1 with parole_at NULL matches no state: the gate "
        "denies it and the retest pager skips it until an operator revive "
        "gives it a parole date. The auditor reports such rows.",
        "An unparsable parole_at makes the gate admit the trade "
        "(db_error_fallback_allow) without spending a slot.",
        "parole_trades_remaining = NULL on a suppressed row is read as "
        "exhausted by the gate, as not exhausted by the refresh classifier "
        "and as 1 by the retest pager. No current writer produces it.",
    ),
)

# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------

_TGD = "scout/trading/tg_alert_dispatch.py"
_SURF = "scout/trading/trade_surface_alerts.py::_send_claimed_alert"
_DET = "scout/trading/detection_alert.py::notify_early_detections"

TG_ALERT_OUTCOME = Machine(
    name="tg_alert_outcome",
    title="Telegram trade-alert outcome",
    entity="tg_alert_log.outcome",
    storage="column",
    table="tg_alert_log",
    column="outcome",
    summary=(
        "What happened to one attempt to alert about a trade or detection. "
        "Rows are written once; the only later change is sent -> "
        "dispatch_failed when a send fails after the row was written."
    ),
    vocabulary="db_check",
    enforcement="declared",
    states=(
        State(
            "sent",
            "Sent",
            "Delivered — except on the paper-trade lane, where the row is "
            "committed before the send and means 'claimed'.",
            initial=True,
        ),
        State(
            "blocked_eligibility",
            "Blocked — not eligible",
            "The signal or token is not eligible for Telegram alerts.",
            initial=True,
            terminal=True,
        ),
        State(
            "blocked_dedup_24h",
            "Blocked — already alerted",
            "The token was alerted within the dedup window.",
            initial=True,
            terminal=True,
        ),
        State(
            "blocked_cooldown",
            "Blocked — cooldown",
            "Written today only by the detection lane, where it also means "
            "the token was alerted within the last 24 hours.",
            initial=True,
            terminal=True,
        ),
        State(
            "dispatch_failed",
            "Dispatch failed",
            "The Telegram send failed or was cancelled.",
            initial=True,
            terminal=True,
        ),
        State(
            "announcement_sent",
            "Announcement sent",
            "One-time marker: the alerts-enabled announcement went out.",
            initial=True,
            terminal=True,
        ),
        State(
            "m1_5c_announcement_sent",
            "M1.5c announcement sent",
            "One-time marker for the M1.5c announcement.",
            initial=True,
            terminal=True,
        ),
    ),
    transitions=_t(
        START,
        ("sent", "blocked_eligibility", "blocked_dedup_24h"),
        f"{_TGD}::notify_paper_trade_opened",
        "paper trade opened",
        "insert",
    )
    + _t(
        START,
        ("sent", "blocked_dedup_24h"),
        _SURF,
        "trade surfaced (the sent row is meant to be committed after the send)",
        "insert",
    )
    + _t(
        "sent",
        "dispatch_failed",
        _SURF,
        "the send failed or was cancelled; normally inside the still "
        "uncommitted transaction, so readers see only dispatch_failed",
    )
    + _t(
        START,
        ("sent", "blocked_eligibility", "blocked_cooldown", "dispatch_failed"),
        _DET,
        "early detection",
        "insert",
    )
    + _t(
        START,
        "announcement_sent",
        "scout/main.py::_maybe_announce_tg_alerts",
        "first boot with alerts enabled",
        "insert",
    )
    + _t(
        START,
        "m1_5c_announcement_sent",
        "scout/main.py::_maybe_announce_m1_5c",
        "first boot after M1.5c",
        "insert",
    )
    + _t(
        "sent",
        "dispatch_failed",
        f"{_TGD}::notify_paper_trade_opened",
        "the send failed after the row was claimed",
    )
    + _t(
        "sent",
        "dispatch_failed",
        f"{_TGD}::_demote_sent_row",
        "the send was cancelled after the row was claimed",
    ),
    caveats=(
        "sent is committed before the send on the paper-trade lane, so a "
        "crash in between leaves a sent row for an alert that never went out "
        "and blocks that token for the dedup window.",
        "The detection lane records its 24-hour dedup as blocked_cooldown "
        "while the other lanes use blocked_dedup_24h, so counts of either "
        "value alone understate deduplication.",
        "The demotion UPDATEs are WHERE id = ? without AND outcome = 'sent'.",
        "The surface lane inserts its sent row uncommitted and awaits the "
        "Telegram call holding only the transaction lock. Other writers on "
        "the shared connection commit without that lock, so one of them can "
        "make the provisional sent row durable before the send finishes.",
        "Both announcement helpers call send_telegram_message without "
        "raise_on_failure, so the marker row is written even when Telegram "
        "rejects the message.",
    ),
)

_OP_ACTIONS = ("acted", "useful", "ignored", "false_positive")
_OP_VIA = "scout/db.py::record_tg_alert_operator_action"
OPERATOR_ACTION = Machine(
    name="operator_action",
    title="Operator feedback on a Telegram alert",
    entity="tg_alert_operator_actions.action",
    storage="column",
    table="tg_alert_operator_actions",
    column="action",
    summary="The operator's latest label for a sent alert; may be relabelled freely.",
    vocabulary="db_check",
    enforcement="declared",
    states=tuple(
        State(name, label, meaning, initial=True)
        for name, label, meaning in (
            ("acted", "Acted", "The operator traded on the alert."),
            ("useful", "Useful", "Informative, not traded."),
            ("ignored", "Ignored", "Seen and passed over."),
            ("false_positive", "False positive", "The alert was wrong."),
        )
    ),
    transitions=_t(START, _OP_ACTIONS, _OP_VIA, "operator labels the alert", "insert")
    + _t(
        _OP_ACTIONS,
        _OP_ACTIONS,
        _OP_VIA,
        "operator relabels the alert",
        include_self=True,
    ),
    caveats=(
        "Relabelling overwrites the row; earlier labels are not kept.",
        "Only an alert whose tg_alert_log row is 'sent' can be labelled; "
        "that is checked in Python before the upsert.",
    ),
)

# --------------------------------------------------------------------------
# Outcome labelling and source calls
# --------------------------------------------------------------------------

_LEDGER = "scout/outcome_ledger.py"
OUTCOME_LABEL = Machine(
    name="outcome_label",
    title="Signal outcome labelling",
    entity="signal_outcome_ledger.label_status",
    storage="column",
    table="signal_outcome_ledger",
    column="label_status",
    summary=(
        "Every emitted signal is labelled with forward returns as the "
        "horizons (15m to 7d) come due."
    ),
    vocabulary="db_check",
    enforcement="guarded",
    states=(
        State(
            "pending",
            "Pending",
            "No label stored yet: no horizon is due, no price was found for "
            "a due horizon, the row has no price anchor, or it is still "
            "waiting its turn in the labelling batch.",
            initial=True,
        ),
        State(
            "partial",
            "Partly labelled",
            "Some horizons labelled; 7 days not yet reached.",
        ),
        State(
            "complete",
            "Labelled",
            "Seven days passed and at least one label exists. Individual "
            "horizons can still be empty.",
            terminal=True,
        ),
        State(
            "unlabelable",
            "Unlabelable",
            "Seven days passed with no label, or the emission time is unreadable.",
            terminal=True,
        ),
    ),
    transitions=_t(
        START,
        "pending",
        f"{_LEDGER}::record_emission_with_status",
        "signal emitted",
        "insert",
    )
    + _t(
        "pending",
        "partial",
        f"{_LEDGER}::label_pending",
        "first horizon labelled",
        "python",
    )
    + _t(
        "pending",
        ("complete", "unlabelable"),
        f"{_LEDGER}::label_pending",
        "7-day horizon reached",
        "python",
    )
    + _t(
        "partial",
        "complete",
        f"{_LEDGER}::label_pending",
        "7-day horizon reached",
        "python",
    ),
)

_SQ = "scout/source_quality/ledger.py"
_SC_STATES = ("pending", "partial", "complete", "unresolvable")
SOURCE_CALL_OUTCOME = Machine(
    name="source_call_outcome",
    title="Source-call outcome",
    entity="source_calls.outcome_status",
    storage="column",
    table="source_calls",
    column="outcome_status",
    summary=(
        "How much of a Telegram/X call's forward performance could be "
        "computed. Recomputed for every priceable call on every refresh."
    ),
    vocabulary="db_check",
    enforcement="declared",
    states=(
        State(
            "pending",
            "Pending",
            "At least one forward window has not opened yet (the last opens "
            "24h after the call and closes at 28h); earlier windows may "
            "already be computed. Also where a call with no priceable "
            "identity stays.",
            initial=True,
        ),
        State(
            "partial",
            "Partial",
            "Every window has opened and price rows exist, but not every "
            "forward field could be computed.",
        ),
        State("complete", "Complete", "All forward fields computed."),
        State("unresolvable", "Unresolvable", "No price rows exist for the token."),
    ),
    transitions=_t(
        START, "pending", f"{_SQ}::backfill_source_calls", "call ingested", "insert"
    )
    + _t(
        _SC_STATES,
        _SC_STATES,
        f"{_SQ}::refresh_source_call_outcomes",
        "recomputed from the price data present at refresh time",
        include_self=True,
    ),
    caveats=(
        "No state is terminal: the refresh recomputes the status of every "
        "priceable call from the price data present at that moment, so a "
        "row is not guaranteed to stay complete.",
        "Calls with no priceable identity, or an unreadable call time, are "
        "never recomputed and stay at their insert-time pending.",
    ),
)

_SC_SOURCE_VALUES = (
    "RESOLVED",
    "UNRESOLVED_TRANSIENT",
    "UNRESOLVED_TERMINAL",
    "resolved",
    "unresolved",
)
SOURCE_CALL_RESOLUTION = Machine(
    name="source_call_resolution",
    title="Source-call token resolution",
    entity="source_calls.resolved_state",
    storage="column",
    table="source_calls",
    column="resolved_state",
    summary=(
        "Whether the token a call names was identified. The column carries "
        "three vocabularies at once: Telegram's (upper case), X's (lower "
        "case) and the refresh's own."
    ),
    vocabulary="none",
    enforcement="declared",
    states=(
        State(
            "RESOLVED",
            "Resolved (Telegram)",
            "Copied from the Telegram resolver.",
            initial=True,
        ),
        State(
            "UNRESOLVED_TRANSIENT",
            "Unresolved, transient (Telegram)",
            "Copied from the Telegram resolver.",
            initial=True,
        ),
        State(
            "UNRESOLVED_TERMINAL",
            "Unresolved, final (Telegram)",
            "Copied from the Telegram resolver.",
            initial=True,
        ),
        State(
            "resolved",
            "Resolved (X)",
            "The X call resolved to a coin id.",
            initial=True,
        ),
        State(
            "unresolved",
            "Unresolved",
            "No token identified at ingest, or (set by the refresh) no "
            "priceable identity: neither a CoinGecko coin id nor a contract "
            "address.",
            initial=True,
        ),
        State(
            "eligible_contract",
            "Priceable contract",
            "A contract address that the snapshot writer can price.",
        ),
    ),
    transitions=_t(
        START,
        _SC_SOURCE_VALUES,
        f"{_SQ}::backfill_source_calls",
        "call ingested (value copied from the source)",
        "insert",
    )
    + _t(
        _SC_SOURCE_VALUES + ("eligible_contract",),
        ("eligible_contract", "unresolved"),
        f"{_SQ}::refresh_source_call_outcomes",
        "refresh reclassifies contract-keyed and identity-less rows",
        include_self=True,
    )
    + _t(
        ("eligible_contract", "unresolved"),
        _SC_SOURCE_VALUES,
        f"{_SQ}::backfill_source_calls",
        "the next backfill overwrites the value from the source again",
        include_self=True,
    ),
    caveats=(
        "No CHECK constraint. The backfill and the refresh each overwrite "
        "the column; the live writer runs them back to back, so between "
        "runs a row holds the refresh's answer and during a run it flips.",
    ),
)

# --------------------------------------------------------------------------
# Discovery, social, narrative, chains
# --------------------------------------------------------------------------

_PONS = "scout/ingestion/rh_pons.py"
_PONS_RECON = "scout/db.py::reconcile_curve_launch_projection"
_CURVE_WRITTEN = ("on_curve", "on_v4", "unknown")
CURVE_LAUNCH = Machine(
    name="curve_launch",
    title="Robinhood/Pons curve-launch lifecycle",
    entity="curve_launch_discoveries.lifecycle_status",
    storage="column",
    table="curve_launch_discoveries",
    column="lifecycle_status",
    summary=(
        "Where a launched token is: still on its bonding curve, or graduated "
        "to a Uniswap v4 pool. For each token touched by a collection pass "
        "it is re-derived from the surviving on-chain evidence."
    ),
    vocabulary="db_check",
    enforcement="declared",
    states=(
        State("on_curve", "On curve", "Trading on its bonding curve.", initial=True),
        State(
            "graduating",
            "Graduating",
            "Curve swept, pool not yet created. In the vocabulary but no "
            "code writes it.",
            written=False,
        ),
        State("on_v4", "On Uniswap v4", "Graduated to a pool.", initial=True),
        State(
            "rescued",
            "Rescued",
            "Owner-rescued after a failed graduation. In the vocabulary but "
            "no code writes it.",
            written=False,
        ),
        State(
            "unknown",
            "Unknown",
            "No surviving evidence (for example after a chain reorg removed "
            "the launch log). Dropped from trade polling.",
        ),
    ),
    transitions=_t(
        START,
        ("on_curve", "on_v4"),
        f"{_PONS}::collect_from_logs",
        "TokenLaunched seen / PoolGraduated seen for an unseen token",
        "insert",
    )
    + _t(
        "on_curve",
        "on_v4",
        f"{_PONS}::advance_lifecycle",
        "PoolGraduated for a known token",
        "python",
    )
    + _t(
        _CURVE_WRITTEN,
        _CURVE_WRITTEN,
        _PONS_RECON,
        "projection rebuilt from canonical evidence (reorgs can move it back)",
        include_self=True,
    ),
    caveats=(
        "advance_lifecycle is forward-only, but the end-of-pass reconcile "
        "rewrites the status from evidence without consulting that order, "
        "so for a touched token reconcile is the effective source of truth "
        "and can move a row backwards.",
        "graduating and rescued are in the CHECK constraint and the ordering "
        "map but no code writes them.",
    ),
)

_TGL = "scout/social/telegram/listener.py"
TG_RESOLUTION = Machine(
    name="tg_resolution",
    title="Telegram message token resolution",
    entity="tg_social_signals.resolution_state",
    storage="column",
    table="tg_social_signals",
    column="resolution_state",
    summary=(
        "Whether a token mentioned in a Telegram message was identified. "
        "Rows are insert-only: a successful retry adds a second row instead "
        "of updating the first."
    ),
    vocabulary="enum",
    enforcement="declared",
    states=(
        State(
            "RESOLVED",
            "Resolved",
            "Token identified. For a cashtag-only message that is the top "
            "search candidate, with no contract address.",
            initial=True,
            terminal=True,
        ),
        State(
            "UNRESOLVED_TRANSIENT",
            "Unresolved — retry was scheduled",
            "Not found on the first attempt and one in-memory retry was "
            "scheduled. The row never changes, so it cannot tell a pending "
            "retry from one that failed or was lost.",
            initial=True,
            terminal=True,
        ),
        State(
            "UNRESOLVED_TERMINAL",
            "Unresolved — final",
            "Not found on the first attempt and the miss is not retryable. "
            "Alert only, no trade. A failed retry writes no row.",
            initial=True,
            terminal=True,
        ),
    ),
    transitions=_t(
        START,
        ("RESOLVED", "UNRESOLVED_TRANSIENT", "UNRESOLVED_TERMINAL"),
        f"{_TGL}::_replay_post_resolution",
        "message resolved (first attempt, or a successful retry)",
        "insert",
    ),
    caveats=(
        "The column has no CHECK. The Enum in "
        "scout/social/telegram/models.py is the Python spelling; SQL readers "
        "in scout/social/telegram/shadow.py, dashboard/db.py and "
        "scripts/check_tg_shadow_lag.py compare against the raw string "
        "'RESOLVED'.",
        "A transient row is never updated: a successful retry inserts a "
        "second row, a failed retry writes nothing, and the retry itself is "
        "an in-memory task that does not survive a restart.",
    ),
)

_LSN = f"{_TGL}::_set_listener_state"
_LISTENER_STATES = (
    "running",
    "auth_lost",
    "crashed",
    "stopped",
    "disabled_floodwait",
)
TG_LISTENER = Machine(
    name="tg_listener",
    title="Telegram listener health",
    entity="tg_social_health.listener_state",
    storage="column",
    table="tg_social_health",
    column="listener_state",
    summary=(
        "Last recorded health of the Telegram listener. The same column is "
        "also written, always as 'running', by per-channel and shadow-scan "
        "heartbeat rows, so a count of 'running' rows says nothing about "
        "the listener itself."
    ),
    vocabulary="module_constants",
    enforcement="declared",
    states=(
        State(
            "running",
            "Running",
            "The listener connected and verified its session. Also what the "
            "row still says after a normal shutdown or a hard kill.",
            initial=True,
        ),
        State(
            "auth_lost",
            "Could not connect / not authorised",
            "The session was revoked, or connecting at start failed for any "
            "reason (every start-up connect failure is recorded this way).",
            initial=True,
            attention=True,
        ),
        State(
            "crashed",
            "Crashed",
            "The listener task died with an exception.",
            attention=True,
        ),
        State(
            "stopped",
            "Disconnected",
            "The Telegram connection ended on its own. A normal pipeline "
            "shutdown does not write this.",
        ),
        State(
            "disabled_floodwait",
            "Disabled — flood wait",
            "Telegram rate-limited the account past the circuit-breaker " "threshold.",
            attention=True,
        ),
    ),
    transitions=_t(
        START,
        ("running", "auth_lost"),
        _LSN,
        "first start: session verified / connect failed",
        "insert",
    )
    + _t(
        START,
        "running",
        f"{_TGL}::_persist_message_with_watermark",
        "per-channel heartbeat row",
        "insert",
    )
    + _t(
        START,
        "running",
        "scout/social/telegram/shadow.py::_upsert_health_row",
        "shadow-scan heartbeat row",
        "insert",
    )
    + _t(
        _LISTENER_STATES,
        "running",
        _LSN,
        "listener (re)started and verified its session",
        include_self=True,
    )
    + _t(
        ("running", "crashed", "stopped", "disabled_floodwait"),
        "auth_lost",
        _LSN,
        "session revoked while running, or connect failed at restart",
    )
    + _t(
        "running",
        ("crashed", "stopped", "disabled_floodwait"),
        _LSN,
        "exception / connection ended on its own / flood wait",
    )
    + _t(
        "disabled_floodwait",
        "crashed",
        _LSN,
        "a start-up flood wait re-raises and is then recorded as a crash",
    ),
    caveats=(
        "Writes are unconditional upserts. A normal shutdown cancels the "
        "task without writing, and a hard kill writes nothing either, so "
        "both leave the row at running with nothing to age it out.",
        "ListenerState in scout/social/telegram/listener.py includes "
        "crashed; the copy in scout/social/telegram/models.py does not.",
    ),
)

_TGCLI = "scout/social/telegram/cli.py"
TG_CHANNEL = Machine(
    name="tg_channel",
    title="Telegram channel subscription",
    entity="tg_social_channels.removed_at",
    storage="derived",
    table="tg_social_channels",
    summary="Whether a curated Telegram channel is meant to be listened to.",
    vocabulary="none",
    enforcement="declared",
    states=(
        State(
            "active",
            "Active",
            "Should be listened to. The listener picks changes up at its "
            "next channel reload (or restart, if reloading is off).",
            initial=True,
            predicate="removed_at IS NULL",
        ),
        State(
            "removed",
            "Removed",
            "Should be ignored: removed by the operator, or automatically "
            "after the account lost access. Takes effect at the next reload.",
            predicate="removed_at IS NOT NULL",
        ),
    ),
    transitions=_t(START, "active", f"{_TGCLI}::cmd_add", "operator adds", "insert")
    + _t(
        START,
        "active",
        f"{_TGCLI}::cmd_sync_channels",
        "channels.yml sync adds a new channel",
        "insert",
    )
    + _t(
        "active",
        "active",
        f"{_TGCLI}::cmd_add",
        "operator re-adds an active channel (the row is replaced)",
    )
    + _t("active", "removed", f"{_TGCLI}::cmd_remove", "operator removes", "cas")
    + _t(
        "active",
        "removed",
        f"{_TGL}::_mark_channel_removed_and_alert",
        "channel access lost",
    )
    + _t(
        "removed",
        "removed",
        f"{_TGL}::_mark_channel_removed_and_alert",
        "access lost again (removed_at is re-stamped)",
    )
    + _t(
        "removed",
        "active",
        f"{_TGCLI}::cmd_sync_channels",
        "channels.yml sync lists it again",
    )
    + _t("removed", "active", f"{_TGCLI}::cmd_add", "operator re-adds"),
    caveats=(
        "cmd_add uses INSERT OR REPLACE, which resets columns it does not "
        "set (such as cashtag_trade_eligible) and changes the row id.",
        "A sync reactivates channels the listener removed for lost access.",
    ),
)

PREDICTION_OUTCOME = Machine(
    name="prediction_outcome",
    title="Narrative prediction evaluation",
    entity="predictions.outcome_class",
    storage="column",
    table="predictions",
    column="outcome_class",
    null_state="pending",
    summary=(
        "A narrative prediction is checked at 6, 24 and 48 hours; the final "
        "class is the result of the first evaluation at or after 48 hours."
    ),
    vocabulary="none",
    enforcement="guarded",
    states=(
        State(
            "pending",
            "Pending",
            "Not finally evaluated (stored as NULL). The 6h and 24h "
            "checkpoints fill in first.",
            initial=True,
        ),
        State(
            "HIT",
            "Hit",
            "Up by at least the hit threshold at the 48h evaluation.",
            terminal=True,
        ),
        State(
            "MISS",
            "Miss",
            "Down by at least the miss threshold at the 48h evaluation.",
            terminal=True,
        ),
        State(
            "NEUTRAL",
            "Neutral",
            "Between the thresholds at the 48h evaluation.",
            terminal=True,
        ),
        State(
            "UNRESOLVED",
            "Unresolved",
            "Three evaluation passes found no price.",
            terminal=True,
        ),
    ),
    transitions=_t(
        START,
        "pending",
        "scout/narrative/predictor.py::store_predictions",
        "prediction stored",
        "insert",
    )
    + _t(
        "pending",
        ("HIT", "MISS", "NEUTRAL", "UNRESOLVED"),
        "scout/narrative/evaluator.py::evaluate_pending",
        "first evaluation at or after 48h / third pass without a price",
        "python",
    ),
    caveats=(
        "eval_retry_count is cumulative and is also incremented when the "
        "price is missing because of the call budget, a rate limit or a "
        "network error, so UNRESOLVED can be written before 48 hours and "
        "without a real price failure.",
        "A prediction whose price_at_prediction is zero or negative is "
        "skipped on every pass and stays pending.",
        "scout/chains/tracker.py reads this column and treats NEUTRAL as a " "miss.",
    ),
)

_TRACK = "scout/chains/tracker.py"
CHAIN_MATCH_OUTCOME = Machine(
    name="chain_match_outcome",
    title="Chain-match outcome",
    entity="chain_matches.outcome_class",
    storage="column",
    table="chain_matches",
    column="outcome_class",
    null_state="pending",
    summary="Whether a completed (or expired) signal chain was followed by a real move.",
    vocabulary="none",
    enforcement="guarded",
    states=(
        State(
            "pending",
            "Pending",
            "Not evaluated (stored as NULL). Evaluation is attempted from "
            "48 hours after completion; the row stays pending for as long "
            "as no outcome source is available.",
            initial=True,
        ),
        State(
            "hit",
            "Hit",
            "Judged a hit: for a narrative chain the coin's latest resolved "
            "prediction was a hit; for a memecoin chain its current FDV is "
            "at least the configured percentage above the market cap stored "
            "at completion (or, when none was stored, its latest recorded "
            "price change is positive).",
            terminal=True,
        ),
        State("miss", "Miss", "Judged not a hit.", terminal=True),
        State(
            "dust_skipped",
            "Skipped — dust",
            "Market cap too small to evaluate.",
            terminal=True,
        ),
        State(
            "EXPIRED",
            "Expired (legacy)",
            "Legacy value that no code writes any more; a migration resets "
            "it to pending for narrative rows.",
            written=False,
        ),
    ),
    transitions=_t(
        START, "pending", f"{_TRACK}::_record_completion", "chain completed", "insert"
    )
    + _t(
        START,
        "pending",
        f"{_TRACK}::_record_expired_chain",
        "chain expired",
        "insert",
    )
    + _t(
        "pending",
        ("hit", "miss", "dust_skipped"),
        f"{_TRACK}::_update_chain_outcomes_inner",
        "evaluated, 48 hours or more after completion",
        "python",
    )
    + _t(
        "EXPIRED",
        "pending",
        "scout/db.py::_migrate_feedback_loop_schema",
        "migration resets legacy narrative rows",
        "cas",
    ),
    caveats=(
        "An expired chain and a completed chain write the same kind of row; "
        "they can only be told apart by conviction_boost = 0 and the step "
        "count, and both put the token and pattern into cooldown.",
    ),
)

_NARR_MIG = "scout/db.py::_migrate_narrative_resolution_status_v1"
NARRATIVE_RESOLUTION = Machine(
    name="narrative_resolution",
    title="Narrative alert resolution",
    entity="narrative_alerts_inbound.resolution_status",
    storage="column",
    table="narrative_alerts_inbound",
    column="resolution_status",
    null_state="unclassified",
    summary="Whether an inbound narrative alert carried a contract that resolved to a coin.",
    vocabulary="none",
    enforcement="guarded",
    states=(
        State(
            "unclassified",
            "Unclassified",
            "Stored before the column existed (NULL); classified the next "
            "time the database is initialised.",
            written=False,
        ),
        State(
            "cashtag_only",
            "Cashtag only",
            "No contract address in the alert; nothing to resolve.",
            initial=True,
            terminal=True,
        ),
        State(
            "ca_resolved",
            "Contract resolved",
            "The contract mapped to a coin.",
            initial=True,
            terminal=True,
        ),
        State(
            "ca_unresolved",
            "Contract unresolved",
            "A contract was present but no coin was found. Retried every "
            "time the database is initialised.",
            initial=True,
        ),
    ),
    transitions=_t(
        START,
        ("cashtag_only", "ca_resolved", "ca_unresolved"),
        "scout/api/narrative_resolver.py::insert_narrative_alert",
        "alert received",
        "insert",
    )
    + _t(
        "unclassified",
        ("cashtag_only", "ca_resolved", "ca_unresolved"),
        _NARR_MIG,
        "backfill on database initialisation",
        "cas",
    )
    + _t(
        "ca_unresolved",
        "ca_resolved",
        _NARR_MIG,
        "retro-resolve on database initialisation found the coin",
        "cas",
    ),
    caveats=(
        "The backfill and retro-resolve are not gated: they run on every "
        "Database.initialize(), which includes the pipeline, the dashboard "
        "and CLI commands.",
    ),
)

# --------------------------------------------------------------------------
# In-memory machines worth naming
# --------------------------------------------------------------------------

_HB = "scout/heartbeat.py"
_INGEST_STATES = ("healthy", "counting", "starved")
INGEST_SOURCE = Machine(
    name="ingest_source",
    title="Ingestion source starvation watchdog",
    entity="scout.heartbeat._ingest_watchdog_state[source] (+ ingest_watchdog_state)",
    storage="memory",
    summary=(
        "Per data source: is it returning rows? One page is attempted when "
        "a source has been empty for the configured number of cycles."
    ),
    vocabulary="none",
    enforcement="guarded",
    states=(
        State("healthy", "Healthy", "Returned rows on the last cycle.", initial=True),
        State(
            "counting",
            "Empty, below threshold",
            "Empty for fewer cycles than the threshold.",
            initial=True,
        ),
        State(
            "starved",
            "Starved",
            "Empty for the threshold number of cycles. A page was attempted "
            "once for this episode; it is not retried if it failed, not "
            "sent in dry-run, and not re-sent after a restart.",
            initial=True,
            attention=True,
        ),
    ),
    transitions=_t(
        START,
        _INGEST_STATES,
        f"{_HB}::hydrate_ingest_watchdog_state",
        "boot: restored from the persisted miss counter",
        "insert",
    )
    + _t(
        START,
        _INGEST_STATES,
        f"{_HB}::observe_ingest_sources",
        "source first observed (empty counts at once; starved if threshold is 1)",
        "insert",
    )
    + _t(
        "healthy",
        ("counting", "starved"),
        f"{_HB}::observe_ingest_sources",
        "a cycle returned nothing",
        "python",
    )
    + _t(
        "counting",
        "starved",
        f"{_HB}::observe_ingest_sources",
        "empty cycles reached the threshold",
        "python",
    )
    + _t(
        ("counting", "starved"),
        "healthy",
        f"{_HB}::observe_ingest_sources",
        "rows returned again",
        "python",
    ),
    caveats=(
        "The source is marked alerted before the Telegram page is sent, so a "
        "failed page is not retried during that outage.",
        "The persisted table means three different things depending on the "
        "source key: a miss counter, a poll heartbeat (dex_discovery, "
        "rh_pons) or a failure streak (rh_pons_attempt).",
    ),
)


MACHINES: tuple[Machine, ...] = (
    PAPER_TRADE,
    EXIT_LADDER,
    LIVE_TRADE,
    SHADOW_TRADE,
    SOLANA_EXECUTION,
    EVM_EXECUTION,
    KILL_SWITCH,
    SIGNAL_LANE,
    COMBO_SUPPRESSION,
    TG_ALERT_OUTCOME,
    OPERATOR_ACTION,
    OUTCOME_LABEL,
    SOURCE_CALL_OUTCOME,
    SOURCE_CALL_RESOLUTION,
    CURVE_LAUNCH,
    TG_RESOLUTION,
    TG_LISTENER,
    TG_CHANNEL,
    PREDICTION_OUTCOME,
    CHAIN_MATCH_OUTCOME,
    NARRATIVE_RESOLUTION,
    INGEST_SOURCE,
)
