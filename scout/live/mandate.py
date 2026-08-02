"""The cross-venue execution mandate — one authority, every venue.

What was missing
----------------
``scout/live/solana_lane.py`` has a three-lock promotion gate: the mode, a second
explicit flag, and N supervised executions counted from the ledger. It is careful,
and it is **Solana-only**.

``scout/live/engine.py::_dispatch_live`` has none of that. A paper-trade signal fires,
the engine routes, and an order is placed — no human anywhere in the path. It is
already the most autonomous execution surface in the tree, and the only things
standing in front of it are three boolean flags whose job is "is live trading wired
up", not "is autonomous execution authorized".

So this module is the authority both families ask. Solana keeps its own three locks
and gains this one on top (strictly additive: it can refuse a promotion the lane would
have allowed, never permit one the lane would have refused). The CEX path gets its
first autonomy gate.

Every default refuses
---------------------
``LIVE_EXECUTION_MANDATE_MODE`` defaults to ``DISABLED``, the enable flag defaults to
``False``, the venue and family allowlists default to EMPTY, and every envelope limit
defaults to ``0``. An unconfigured mandate permits nothing — not "permits everything
because nothing was restricted". This is the same discipline ``VenueCapabilities``
applies to capabilities and for the same reason: undeclared is absent.

Entry-only, deliberately
------------------------
*** THE MANDATE GATES OPENING EXPOSURE. IT NEVER GATES CLOSING IT. ***
``LiveEngine.__init__`` already refuses to boot "can buy, cannot sell" because that
orphans real money. A mandate that could block the closer would recreate exactly that
state at runtime, from a config value, with positions already open. So a
``reduce_only`` intent is REFUSED here rather than approved — not because exits are
forbidden, but because the mandate is not the thing that authorizes them, and an
``authorize()`` that returned ``permitted=True`` for an exit would read as though it
were. This mirrors the Solana lane, where the daily cap is an ENTRY cap and the exit
carries its own ``OperatorExitBinding``.

Why the supervised count is not imported from the Solana lane
--------------------------------------------------------------
``solana_lane.count_supervised_reconciled`` does exactly this query. It is
reimplemented here rather than imported because
``tests/live/test_solana_reverse_has_no_autonomous_path.py`` asserts that NOTHING in
``scout/`` imports ``scout.live.solana_lane`` — a single import is the difference
between an operator-only command and a scheduled one, and that guarantee is worth more
than the duplication. The two queries are pinned together by
``tests/live/test_execution_mandate.py``, which asserts the constants match the lane's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:  # pragma: no cover
    from scout.db import Database
    from scout.live.intent import TradeIntent

log = structlog.get_logger(__name__)

__all__ = [
    "ExecutionMandate",
    "MandateDecision",
    "MandateEnvelope",
    "MandateRefused",
    "EXECUTING_MODES",
]

MODE_DISABLED = "DISABLED"
MODE_SIMULATION_ONLY = "SIMULATION_ONLY"
MODE_SUPERVISED_LIVE = "SUPERVISED_LIVE"
MODE_BOUNDED_AUTONOMOUS = "BOUNDED_AUTONOMOUS"

#: The two modes under which an order may actually be placed. ``DISABLED`` and
#: ``SIMULATION_ONLY`` are not in it, so membership — not inequality against
#: ``DISABLED`` — is the test. A new mode added later is refused until someone
#: deliberately adds it here.
EXECUTING_MODES = (MODE_SUPERVISED_LIVE, MODE_BOUNDED_AUTONOMOUS)

#: Mirrored from ``scout.live.solana_lane`` (see the module docstring for why they are
#: not imported). Pinned by test.
SOLANA_STATE_RECONCILED = "reconciled"

#: Venue families this mandate knows how to count supervised history for. A family
#: absent from here cannot reach ``BOUNDED_AUTONOMOUS`` at all, because there would be
#: no ledger to settle the claim "we did the supervised trades".
_SUPERVISED_LEDGERS = ("cex", "dex")


class MandateRefused(Exception):
    """Execution is not authorized. Carries WHICH gate refused and what would fix it.

    ``gate`` is a short stable token (``mode``, ``enable_flag``, ``venue``,
    ``expiry``, ``envelope``, ``supervised_history``, ``scope``) so callers can record
    a machine-readable reject reason without parsing prose, while the message stays
    long enough for an operator to act on. A refusal nobody can act on is a refusal
    somebody will work around.
    """

    def __init__(self, gate: str, message: str) -> None:
        super().__init__(message)
        self.gate = gate
        self.message = message


@dataclass(frozen=True)
class MandateEnvelope:
    """The bounded box autonomous execution runs inside.

    Every field must be present and finite. ``Decimal("Infinity") > 0`` is ``True``,
    so a positivity check alone would accept an infinite "maximum" — a field named
    maximum that imposes no maximum. Finiteness is therefore checked first, exactly as
    ``TradeIntent`` does.
    """

    per_trade_max_notional_usd: Decimal
    daily_max_notional_usd: Decimal
    max_open_positions: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "per_trade_max_notional_usd": str(self.per_trade_max_notional_usd),
            "daily_max_notional_usd": str(self.daily_max_notional_usd),
            "max_open_positions": self.max_open_positions,
        }


@dataclass(frozen=True)
class MandateDecision:
    """A recorded authorization. Only ever constructed on the permitted path.

    There is no ``permitted: bool`` field, and that is on purpose: a decision object
    carrying ``permitted=False`` invites ``if decision.permitted`` at the call site,
    and a caller that forgets the ``if`` gets execution. Refusal is an exception, so
    the only way to hold a ``MandateDecision`` is to have been authorized.
    """

    mode: str
    venue: str
    venue_family: str
    intent_hash: str
    envelope: MandateEnvelope
    supervised_reconciled: int | None
    decided_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "venue": self.venue,
            "venue_family": self.venue_family,
            "intent_hash": self.intent_hash,
            "envelope": self.envelope.as_dict(),
            "supervised_reconciled": self.supervised_reconciled,
            "decided_at": self.decided_at.isoformat(),
        }


def _csv_set(raw: Any) -> frozenset[str]:
    """Parse a comma-separated allowlist. Empty / unset yields the EMPTY set.

    Empty means "nothing is allowed", never "everything is allowed". A settings
    typo therefore closes the gate rather than opening it.
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, (list, tuple, set, frozenset)):
        items = [str(v).strip() for v in raw]
    else:
        items = [part.strip() for part in str(raw).split(",")]
    return frozenset(item for item in items if item)


def _finite_decimal(value: Any) -> Decimal | None:
    """Coerce to a finite positive-or-zero ``Decimal``, or ``None`` if impossible.

    ``None`` is the fail-closed answer for every unusable input: NaN (which raises
    ``InvalidOperation`` — an ``ArithmeticError``, not a ``ValueError`` — on
    comparison), infinities, and anything unparseable.
    """
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not dec.is_finite():
        return None
    return dec


class ExecutionMandate:
    """Venue-neutral authority over whether an intent may be executed.

    Construct from settings; call :meth:`authorize` with the intent. Refusal raises
    :class:`MandateRefused`.
    """

    def __init__(self, *, settings: Any, db: "Database | None" = None) -> None:
        self._s = settings
        self._db = db

    # ------------------------------------------------------------------
    @property
    def mode(self) -> str:
        return str(getattr(self._s, "LIVE_EXECUTION_MANDATE_MODE", MODE_DISABLED))

    @property
    def is_active(self) -> bool:
        """Both locks, and nothing else. Deliberately cheap and side-effect free so
        boot-time reporting can call it without a database."""
        return self.mode in EXECUTING_MODES and bool(
            getattr(self._s, "LIVE_EXECUTION_MANDATE_ENABLED", False)
        )

    # ------------------------------------------------------------------
    def precheck(self) -> MandateEnvelope:
        """The venue-independent locks, checkable before any routing work happens.

        Exists so an inactive mandate refuses BEFORE the caller does on-demand venue
        metadata fetches, depth probes or anything else with a side effect. It is a
        strict subset of :meth:`authorize` — the same three gates, run by the same
        code — so it cannot drift into permitting something ``authorize`` would
        refuse.

        Returns the envelope so a caller that wants to report the configured bounds
        at boot does not have to reach for private state.
        """
        mode = self.mode
        if mode not in EXECUTING_MODES:
            raise MandateRefused(
                "mode",
                f"LIVE_EXECUTION_MANDATE_MODE is {mode!r}; execution requires one of "
                f"{list(EXECUTING_MODES)}.",
            )
        if not bool(getattr(self._s, "LIVE_EXECUTION_MANDATE_ENABLED", False)):
            raise MandateRefused(
                "enable_flag",
                f"LIVE_EXECUTION_MANDATE_MODE is {mode} but "
                "LIVE_EXECUTION_MANDATE_ENABLED is False. The mode alone does not "
                "authorize execution — that takes two deliberate settings, on purpose.",
            )
        return self._read_envelope()

    # ------------------------------------------------------------------
    async def authorize(
        self, intent: "TradeIntent", *, at: datetime | None = None
    ) -> MandateDecision:
        """Authorize ``intent`` for execution, or raise :class:`MandateRefused`.

        Gates run in a fixed order, cheapest and most-fundamental first, so a
        misconfigured deployment gets the most useful refusal rather than the first
        incidental one.
        """
        now = at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("`at` must be timezone-aware")

        # -- 0. The intent must still be what it says it is ------------------
        # Everything below reasons about the intent's terms. If the hash does not
        # match them, those terms are unauthenticated and no gate result means
        # anything.
        if not intent.verify():
            raise MandateRefused(
                "integrity",
                f"intent {intent.intent_id} fails verify(): its terms no longer match "
                "its content hash. Rebuild the intent; do not execute this one.",
            )

        # -- 1. Scope: the mandate authorizes ENTRIES only -------------------
        if intent.reduce_only:
            raise MandateRefused(
                "scope",
                "reduce_only intents are not mandate-gated. The mandate bounds "
                "OPENING exposure; blocking a close from a config value would orphan "
                "an open position, which is the state LiveEngine.__init__ already "
                "refuses to boot into. Route exits through the closer, not here.",
            )

        # -- 2 + 3 + 7. Mode, second lock, bounded envelope ------------------
        # Shared with precheck() so the two can never disagree about what an
        # inactive mandate means.
        envelope = self.precheck()
        mode = self.mode

        # -- 4. The intent must be asking for the mode we are in -------------
        # An intent minted under SUPERVISED_LIVE carries that in its hashed terms.
        # Executing it under BOUNDED_AUTONOMOUS would be executing something nobody
        # authorized in that regime, and the hash is what makes the difference
        # detectable.
        #
        # STATED PLAINLY: on the live engine's own path this check cannot fail.
        # `LiveEngine._build_entry_intent` sets `mode=self._mandate.mode`, so both
        # sides of the comparison come from one settings read moments apart. It is
        # not dead weight — it is the check that bites at every OTHER entry point:
        # an intent reconstructed from a `live_trades` row, replayed from evidence,
        # or produced by a future non-engine caller, any of which can carry a mode
        # that was current when it was minted and is not current now. That is the
        # same reason `verify()` exists above: both guard the deserialization
        # boundary, not the freshly-minted one.
        if intent.mode != mode:
            raise MandateRefused(
                "mode",
                f"intent was minted under mode {intent.mode!r} but the mandate is "
                f"{mode!r}; the mode is part of the intent's hashed terms and must "
                "match.",
            )

        # -- 5. Venue + family allowlists ------------------------------------
        families = _csv_set(getattr(self._s, "LIVE_EXECUTION_MANDATE_FAMILIES", ""))
        venues = _csv_set(getattr(self._s, "LIVE_EXECUTION_MANDATE_VENUES", ""))
        if intent.venue_family not in families:
            raise MandateRefused(
                "venue",
                f"venue_family {intent.venue_family!r} is not in "
                f"LIVE_EXECUTION_MANDATE_FAMILIES ({sorted(families) or 'empty'}). "
                "An empty allowlist permits nothing.",
            )
        if intent.preferred_venue not in venues:
            raise MandateRefused(
                "venue",
                f"venue {intent.preferred_venue!r} is not in "
                f"LIVE_EXECUTION_MANDATE_VENUES ({sorted(venues) or 'empty'}). "
                "An empty allowlist permits nothing.",
            )

        # -- 6. Lifetime -----------------------------------------------------
        if intent.is_expired(at=now):
            raise MandateRefused(
                "expiry",
                f"intent {intent.intent_id} expired at "
                f"{intent.expires_at.isoformat()} (now {now.isoformat()}).",
            )
        if now > intent.execution_deadline:
            raise MandateRefused(
                "expiry",
                f"intent {intent.intent_id} passed its execution_deadline "
                f"{intent.execution_deadline.isoformat()} (now {now.isoformat()}).",
            )

        # -- 7. The intent must fit the envelope -----------------------------
        # The intent's own declared ceiling must fit inside the mandate's. Comparing
        # maximum_notional (not exact_quantity) because the two can be denominated
        # differently — exact_quantity may be base units, and a base quantity cannot
        # be compared against a USD cap without a price the mandate does not hold.
        if intent.maximum_notional > envelope.per_trade_max_notional_usd:
            raise MandateRefused(
                "envelope",
                f"intent maximum_notional {intent.maximum_notional} exceeds the "
                f"mandate per-trade cap {envelope.per_trade_max_notional_usd}.",
            )

        # -- 8. Recorded supervised history (BOUNDED_AUTONOMOUS only) ---------
        supervised: int | None = None
        if mode == MODE_BOUNDED_AUTONOMOUS:
            supervised = await self._count_supervised(intent.venue_family)
            required = int(
                getattr(
                    self._s,
                    "LIVE_EXECUTION_MANDATE_MIN_SUPERVISED_EXECUTIONS",
                    3,
                )
            )
            if required < 1:
                raise MandateRefused(
                    "supervised_history",
                    "LIVE_EXECUTION_MANDATE_MIN_SUPERVISED_EXECUTIONS is "
                    f"{required}; autonomous promotion with no recorded supervised "
                    "history is refused. Set it to 1 or more.",
                )
            if supervised < required:
                raise MandateRefused(
                    "supervised_history",
                    f"BOUNDED_AUTONOMOUS requires {required} completed supervised "
                    f"execution(s) recorded for venue_family "
                    f"{intent.venue_family!r}; there "
                    f"{'is' if supervised == 1 else 'are'} {supervised}. Run under "
                    "SUPERVISED_LIVE until that many have reached a reconciled "
                    "terminal state. This is counted from the ledger and cannot be "
                    "satisfied by configuration.",
                )

        decision = MandateDecision(
            mode=mode,
            venue=intent.preferred_venue,
            venue_family=intent.venue_family,
            intent_hash=intent.intent_hash,
            envelope=envelope,
            supervised_reconciled=supervised,
            decided_at=now,
        )
        log.info("execution_mandate_authorized", **decision.as_dict())
        return decision

    # ------------------------------------------------------------------
    def _read_envelope(self) -> MandateEnvelope:
        """Build the envelope, refusing anything unset, non-finite or non-positive."""
        per_trade = _finite_decimal(
            getattr(self._s, "LIVE_EXECUTION_MANDATE_PER_TRADE_MAX_USD", 0)
        )
        daily = _finite_decimal(
            getattr(self._s, "LIVE_EXECUTION_MANDATE_DAILY_MAX_USD", 0)
        )
        try:
            max_open = int(
                getattr(self._s, "LIVE_EXECUTION_MANDATE_MAX_OPEN_POSITIONS", 0)
            )
        except (TypeError, ValueError):
            max_open = 0

        missing = [
            name
            for name, value in (
                ("LIVE_EXECUTION_MANDATE_PER_TRADE_MAX_USD", per_trade),
                ("LIVE_EXECUTION_MANDATE_DAILY_MAX_USD", daily),
            )
            if value is None or value <= 0
        ]
        if max_open <= 0:
            missing.append("LIVE_EXECUTION_MANDATE_MAX_OPEN_POSITIONS")
        if missing:
            raise MandateRefused(
                "envelope",
                "the mandate requires a bounded envelope; these limits are unset, "
                f"non-positive or non-finite: {', '.join(missing)}.",
            )
        assert per_trade is not None and daily is not None  # narrowed by `missing`
        if per_trade > daily:
            raise MandateRefused(
                "envelope",
                f"per-trade cap {per_trade} exceeds the daily cap {daily}; a single "
                "trade could exhaust more than a day's budget, so the daily cap would "
                "bound nothing.",
            )
        return MandateEnvelope(
            per_trade_max_notional_usd=per_trade,
            daily_max_notional_usd=daily,
            max_open_positions=max_open,
        )

    # ------------------------------------------------------------------
    async def _count_supervised(self, venue_family: str) -> int:
        """Completed supervised executions recorded in the ledger for this family.

        The precondition a flag cannot fake. Each family reads its own ledger:

        * ``dex`` → ``solana_executions`` rows at ``state='reconciled'`` under
          ``mode='SUPERVISED_LIVE'``. Reconciled, not merely finalized: a swap that
          landed but whose accounting could not be explained is exactly the history
          that should NOT count toward promotion.
        * ``cex`` → ``live_trades`` rows written under ``mandate_mode='SUPERVISED_LIVE'``
          that carry a real entry fill. There is no ``mandate_mode`` on any row that
          predates this module, so the count starts at zero and the only way to raise
          it is to actually run supervised executions through this path. That is the
          intended property, not a migration gap.

        A missing table, an unreadable ledger or an unknown family all return a
        refusal rather than a zero-that-looks-like-a-count — "the database could not
        answer" and "the answer is none" are the same verdict here (both refuse) but
        they are not the same fact, and conflating them is how a promotion gets
        granted against a ledger nobody read.
        """
        if venue_family not in _SUPERVISED_LEDGERS:
            raise MandateRefused(
                "supervised_history",
                f"venue_family {venue_family!r} has no supervised-history ledger; "
                f"known families are {list(_SUPERVISED_LEDGERS)}. BOUNDED_AUTONOMOUS "
                "cannot be reached without one.",
            )
        db = self._db
        if db is None or getattr(db, "_conn", None) is None:
            raise MandateRefused(
                "supervised_history",
                "BOUNDED_AUTONOMOUS requires reading the execution ledger, and no "
                "database is wired to the mandate.",
            )
        if venue_family == "dex":
            sql = (
                "SELECT COUNT(*) FROM solana_executions " "WHERE state = ? AND mode = ?"
            )
            params: tuple[Any, ...] = (SOLANA_STATE_RECONCILED, MODE_SUPERVISED_LIVE)
        else:
            sql = (
                "SELECT COUNT(*) FROM live_trades "
                "WHERE mandate_mode = ? AND entry_fill_qty IS NOT NULL"
            )
            params = (MODE_SUPERVISED_LIVE,)
        try:
            cur = await db._conn.execute(sql, params)
            row = await cur.fetchone()
        except Exception as exc:
            raise MandateRefused(
                "supervised_history",
                f"could not read the supervised-execution ledger for "
                f"{venue_family!r}: {type(exc).__name__}: {exc}. Refusing rather than "
                "treating an unreadable ledger as an empty one.",
            ) from exc
        return int(row[0]) if row is not None else 0
