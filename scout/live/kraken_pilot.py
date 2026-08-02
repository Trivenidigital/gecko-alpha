"""Kraken supervised pilot runner — PR-K3.

The operator-invoked CLI that executes ONE supervised Kraken spot limit
trade behind a manual approval boundary. It composes PR-K1/K2's
``KrakenSpotAdapter`` with the existing kill switch and ``live_trades``
ledger; it is NOT wired into the signal-driven live engine and nothing in
the pipeline imports it.

Usage::

    python -m scout.live.kraken_pilot place --side buy --price 100000.0 \\
        --volume 0.0002 [--validate-only --yes-i-am-rehearsing]
    python -m scout.live.kraken_pilot exit --live-trade-id 7 --price 101000.0 \\
        [--validate-only --yes-i-am-rehearsing]
    python -m scout.live.kraken_pilot cancel --decision-id <uuid>
    python -m scout.live.kraken_pilot status

Place flow, in mechanical order — every step is printed, logged and
appended to the evidence file before the next one starts:

1.  envelope gate (KRAKEN_PILOT_ENABLED, pair configured, keys present)
2.  kill-switch check
3.  preflight (auth + withdrawal capability EXCLUDED, or abort)
4.  startup reconciliation — a restart must not forget an order
5.  market rules (tick / lot / ordermin / costmin)
6.  caps (per-order notional, daily gross, price-deviation warning)
7.  balance cover
8.  identity (decision id == cl_ord_id) + collision check
9.  MANUAL APPROVAL BOUNDARY
10. persist the intent row, immediately before submission
11. kill-switch RE-check, post-approval
12. submit
13. resolve the outcome: fills, or ambiguity resolution, then reconcile

Design decisions this module had to make, and why
-------------------------------------------------
**A limit price bounds the price, not the size.** Every notional cap is
checked against BOTH ``price * volume`` and ``mid * volume``, because a
marketable order does not transact at its limit — a sell priced below the mid
sells into the book from the top down, so bounding the limit notional bounds
nothing on that side. The larger figure is what the daily gross counts and
what ``size_usd`` records, so the ledger holds exposure taken rather than
exposure requested.

**One PLACEMENT at a time, enforced by the filesystem.** Two ``place`` runs in
two terminals are two live orders: each clears its own one-order-at-a-time
check before either writes a row, and each reads a daily-gross total the other
is about to invalidate. ``O_CREAT | O_EXCL`` on a lock file beside the database
is the atomic test-and-set that closes both windows at the only point the two
processes share. A held lock is never broken automatically.

The lock covers ``place`` and nothing else. ``status`` is read-only and
``cancel`` reduces exposure, so neither can create the second order the lock
exists to prevent — and locking them would be actively harmful, because a
stale lock arises exactly when a run died with an order possibly resting,
which is the moment those two commands are most needed.

**The database must already exist.** SQLite creates one for any path it is
handed, so running from the wrong directory does not fail — it silently
succeeds against an empty file where the kill switch is clear, no prior orders
need reconciling and the daily gross is zero. Every safety mechanism reads as
"all clear" precisely because it is reading the wrong database.

**Both directions of reconciliation, ledger↔venue.** Ledger rows are looked up
at the venue by client id, AND the venue's full resting-order list is checked
against the ledger. The second direction is the one a client-id lookup
structurally cannot cover: an order placed from the web UI has no cl_ord_id we
would know to ask about, so a ledger-driven check reports a clear lane while
the account holds a live order. An unreadable listing is a blocker, because
"no open orders" is what licenses a placement.

**The ledger row is written immediately before the POST, never earlier.**
``live_trades.status='open'`` means "an order may exist at the venue", which
is exactly what is true from the instant the POST leaves. Writing it earlier
(at approval, say) would mean an abort between approval and submission leaves
a row asserting an order that provably does not exist — and step 4 blocks the
whole lane on such a row. This reuses ``idempotency.record_pending_order``
unchanged: same "insert 'open' immediately before venue submit" contract the
Binance path uses, same ``db._txn_lock`` discipline, same UNIQUE
client_order_id backstop.

**A ``--validate-only`` rehearsal writes NO ledger row.** Kraken's
``validate=true`` cannot trade (adapter fact 12), so a rehearsal can never
leave an order behind — and a row that says otherwise is a phantom that
blocks the next run at step 4. Writing the row and then updating it to
'rejected' would still leave a crash window in which a rehearsal blocks the
real trade, which is the failure this ordering exists to prevent. The
rehearsal's evidence file is the record; it is complete, and it is the same
code path in every other respect (signing, encoding, precision, minimums).

**``paper_trade_id`` is satisfied by a pilot ANCHOR row, not a per-trade
paper trade.** ``live_trades.paper_trade_id`` is ``NOT NULL REFERENCES
paper_trades(id)`` and ``PRAGMA foreign_keys=ON`` is set on every connection
(scout/db.py), so a pilot order physically cannot be recorded without some
paper_trades row to point at. There is no signal behind an operator-invoked
trade, so this module creates ONE sentinel row — token_id 'kraken-pilot',
signal_type 'kraken_pilot', status 'kraken_pilot_anchor', opened_at epoch —
and every pilot order references it. Chosen over a per-order synthetic paper
trade because it keeps the pollution at exactly one row, ever. The three
field values are each load-bearing against a specific reader: the status is
not 'open' so no open-position loop scans it (every such query filters
``status = 'open'``), the epoch ``opened_at`` keeps it out of the daily
digest's ``date(opened_at) = ?`` count and the analytics windows' ``opened_at
>= cutoff``, and ``closed_at`` stays NULL so the closed-trade digest never
sees it. No schema migration.

**``reject_reason`` uses only values already in the CHECK constraint.** The
enum has no "operator cancelled" or "rehearsal" member and the constraint is
NOT widened here: a cancel and a plain venue refusal write ``NULL`` (the
constraint permits it) with the real reason in the evidence file and the log.
A kill-switch abort writes 'kill_switch', a resolved-as-never-accepted
submission writes 'venue_unavailable', and a venue refusal naming insufficient
funds writes 'insufficient_balance'.

**Evidence is JSON Lines** — one JSON object per step, appended, flushed and
fsynced before the step returns, with the file reopened per record. A crash
at any point therefore leaves every completed step on disk; there is no
whole-file rewrite that can be interrupted halfway. The filename keeps the
``.json`` extension the pilot spec fixed; the content is line-delimited.

**The decision id is minted at the start of the run**, not at step 8, so the
evidence file has its name — and every earlier step has somewhere to be
recorded — before the first gate runs. Its role is unchanged: it IS the
``cl_ord_id`` (a dashed uuid4, which is Kraken's documented long-UUID form;
``idempotency.make_client_order_id`` targets Binance's 28 chars and does NOT
fit Kraken, adapter fact 10) and the first 8 characters are what the operator
types to authorize.

The ``exit`` command — closing a position, not opening one
----------------------------------------------------------
``exit`` CLOSES one existing ``live_trades`` row, selected by id. It is a
sibling of ``place``, not a mode of it: ``place --side sell`` is blocked by
any open row and would book a SECOND row, which is the opposite of what
closing a position means.

Exit flow: envelope → row selection + eligibility → lane state → venue state
(balance covers the sale, nothing already resting) → account fee tier →
cost scenarios → MANUAL APPROVAL → durable intent → submit once → resolve →
reconcile → close.

**The exit is NOT gated on ``KRAKEN_PILOT_ENABLED`` or the kill switch.**
Same reasoning that leaves ``cancel`` ungated: both are levers that REDUCE
exposure, and a master switch able to strand an open position is a safety
mechanism pointed the wrong way. Both states are recorded in the evidence
either way. ``LIVE_USE_REAL_SIGNED_REQUESTS`` IS still checked, not as a
policy choice but because ``place_limit_order`` refuses outright under it —
catching that before the approval prompt beats discovering it after.

**The exit client order id is deterministic**, not a uuid4:
``idempotency.make_exit_client_order_id`` gives ``gecko-x-<live_trade_id>``,
which fits Kraken's 18-character free-text form (adapter fact 10). Determinism
is the crash-recovery mechanism — a rerun after an interrupted exit computes
the SAME id, finds the order resting in ``OpenOrders``, and refuses instead of
placing a second sale of the same coins. That check is why the venue-state
step refuses on the exit id specifically as well as on the pair.

**The typed authorization is bound to the order, not to the run.** ``place``
has the operator type a decision-id prefix, which identifies the RUN; that is
sufficient there because the run's parameters came from the same command line.
An exit's quantity comes from the ledger row, so an authorization that
survived a change of price or quantity would be an approval of something the
operator never read. The token is a digest over
(pair, side, volume, price, cl_ord_id) — change any one and the token the
operator was shown no longer matches.

**Reconciliation gates the close, not the other way round.** ``status``,
``exit_fill_price``, ``realized_pnl_usd`` and ``closed_at`` are written only
after the venue's balance move and per-fill records agree with the fill the
order reported. When they do not, the row is marked ``needs_manual_review``
and left non-terminal: a closed row asserts the position is gone, and
asserting that on unreconciled evidence is how a phantom position is created.

**A partial fill never closes the row.** It reduces ``entry_fill_qty`` — the
lane's held-quantity field — by what sold, records the exit txid and fill
price, leaves the status ``open``, and tells the operator. The remainder is a
real position; a later ``exit`` run sells exactly what is left because it
reads the reduced quantity.

**The close status is ``closed_via_reconciliation``.** The ``live_trades``
CHECK enum has no operator-exit member and this module does not widen it (same
discipline as ``reject_reason`` above). ``closed_tp`` / ``closed_sl`` would
fabricate a threshold trigger that never fired and ``closed_duration`` a
hold-time expiry that never elapsed; ``closed_via_reconciliation`` is the
vocabulary's out-of-band close, and nothing in-tree writes it to
``live_trades`` today, so the value is unambiguous there: it means a
supervised pilot exit. The real economics live in ``realized_pnl_usd`` /
``realized_pnl_pct`` and the evidence file.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

import structlog

from scout.config import Settings, load_settings
from scout.db import Database
from scout.live.idempotency import (
    lookup_existing_order_id,
    make_exit_client_order_id,
    record_pending_order,
)
from scout.live.kill_switch import KillSwitch
from scout.live.kraken_adapter import KrakenAmbiguousSubmissionError, KrakenSpotAdapter

log = structlog.get_logger(__name__)

# Exit codes. Distinct rather than a blanket 1 because the operator's next
# action differs per class, and a wrapper script should be able to tell them
# apart without parsing stdout.
#
#   0  EXIT_OK        nothing outstanding, or a limit order left resting
#                     (which is a normal outcome, not a problem)
#   1  EXIT_REFUSED   a gate said no, the operator did not authorize, or the
#                     venue definitively refused. No order exists.
#   2  EXIT_BLOCKED   the lane is blocked — an unresolved prior ledger row, a
#                     resting order the ledger does not know about, or another
#                     pilot process holding the lock. Nothing was attempted.
#   3  EXIT_ESCALATE  a submission could not be resolved, or the run failed
#                     unexpectedly. State is UNKNOWN; a human must look.
#   4  EXIT_REVIEW    the order completed but the post-trade reconciliation
#                     could not explain the balance move. Money moved in a way
#                     the runner cannot account for — check before the next run.
EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_BLOCKED = 2
EXIT_ESCALATE = 3
EXIT_REVIEW = 4

VENUE = "kraken"

# The pilot anchor (see module docstring). Values are matched exactly on
# lookup, so they are constants rather than anything derived at runtime.
_ANCHOR_TOKEN_ID = "kraken-pilot"
_ANCHOR_SIGNAL_TYPE = "kraken_pilot"
_ANCHOR_STATUS = "kraken_pilot_anchor"
_ANCHOR_OPENED_AT = "1970-01-01T00:00:00+00:00"

# live_trades statuses that mean "an order may still exist at the venue".
# Any row in one of these blocks a new placement (one live order at a time).
_BLOCKING_STATUSES = ("open", "needs_manual_review")

# Kraken's published default taker fee, used only when AssetPairs carries no
# fee schedule for the pair. Always labelled ESTIMATE in the approval block.
_DEFAULT_TAKER_FEE_PCT = Decimal("0.26")

# What `exit` writes into live_trades.status on a reconciled close. See the
# module docstring for why this member and not closed_tp / closed_sl.
_EXIT_CLOSED_STATUS = "closed_via_reconciliation"

# Characters of the authorization digest the operator retypes. Eight hex
# characters is 32 bits — far beyond anything a slip of the hand reproduces,
# and short enough to be copied off a screen without error.
_EXIT_AUTH_TOKEN_CHARS = 8

# Bound into the authorization digest so a token minted for the exit lane can
# never authorize something else that happens to hash the same tuple.
_EXIT_AUTH_DOMAIN = "kraken-pilot-exit-v1"

# Redacted before anything is written to the evidence file or the log. The
# runner never handles credentials itself — the adapter owns signing — so this
# is a backstop against a future field carrying one, not a live concern.
# Deliberately does NOT match 'signal_type' or 'client_order_id'.
_SECRET_KEY_RE = re.compile(
    r"(?i)(secret|passw|api[-_]?key|api[-_]?sign|authorization|bearer)"
)
_REDACTED = "[REDACTED]"


class PilotAbort(Exception):
    """A gate refused. Carries the evidence step name and the exit code."""

    def __init__(self, stage: str, reason: str, *, exit_code: int = EXIT_REFUSED):
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.exit_code = exit_code


class PilotLockHeld(Exception):
    """Another pilot process holds the lock. Carries the holder's record."""

    def __init__(self, lock_path: Path, holder: str) -> None:
        super().__init__(f"pilot lock held: {lock_path}")
        self.lock_path = lock_path
        self.holder = holder


# ----------------------------------------------------------------------
# Single-process lock
# ----------------------------------------------------------------------
def pilot_lock_path(db_path: str | Path) -> Path:
    """Lock file for the pilot lane, beside the database it guards."""
    return Path(f"{db_path}.pilot.lock")


def acquire_pilot_lock(db_path: str | Path) -> tuple[int, Path]:
    """Take the pilot lane's exclusive lock, or raise ``PilotLockHeld``.

    Two pilot runs in two terminals are two live orders: each passes its own
    one-order-at-a-time check before either writes a ledger row, so the
    in-process check cannot see the other, and the daily-gross total each
    reads is stale by the time the other commits. ``O_CREAT | O_EXCL`` is the
    filesystem's atomic test-and-set, which closes both windows at the only
    point they share — the machine.

    A held lock is NEVER broken automatically, even if the recorded PID is
    long gone. A stale lock means some earlier run did not reach its cleanup,
    which is exactly the state where an order may be resting unrecorded; an
    auto-break would step straight past that. The operator is told the PID and
    the file to delete.
    """
    lock_path = pilot_lock_path(db_path)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        try:
            holder = lock_path.read_text(encoding="utf-8").strip()
        except OSError:
            holder = "(unreadable)"
        raise PilotLockHeld(lock_path, holder or "(empty)") from exc
    os.write(
        fd,
        f"pid={os.getpid()} acquired_at={datetime.now(timezone.utc).isoformat()}\n".encode(),
    )
    return fd, lock_path


def release_pilot_lock(fd: int, lock_path: Path) -> None:
    """Close and remove the lock. Safe to call on a partially-taken lock."""
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        lock_path.unlink()
    except OSError:
        log.warning("kraken_pilot_lock_not_removed", lock_path=str(lock_path))


# ----------------------------------------------------------------------
# Evidence
# ----------------------------------------------------------------------
def _scrub(value: Any) -> Any:
    """Recursively redact values under secret-looking keys."""
    if isinstance(value, dict):
        return {
            key: (_REDACTED if _SECRET_KEY_RE.search(str(key)) else _scrub(val))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


class EvidenceLog:
    """Append-only JSON-Lines evidence file for one pilot decision.

    One object per step, flushed and fsynced before ``record`` returns, with
    the handle opened and closed per record. A crash mid-run therefore keeps
    every step that completed — there is no partially-rewritten file state.

    ``record`` is also the single structlog call site, so a step cannot reach
    one sink and miss the other.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, step: str, **fields: Any) -> dict[str, Any]:
        entry = {
            "step": step,
            "at": datetime.now(timezone.utc).isoformat(),
            **_scrub(fields),
        }
        line = json.dumps(entry, default=str)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        log.info("kraken_pilot_step", step=step, **_scrub(fields))
        return entry


# ----------------------------------------------------------------------
# Small numeric helpers — Kraken and the ledger both speak strings.
# ----------------------------------------------------------------------
def _dec(value: Any) -> Decimal:
    """Decimal via str(), so a float's binary repr never leaks in."""
    return Decimal(str(value))


def _dec_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _fmt(value: Decimal | None) -> str | None:
    """Fixed-point string; ``str(Decimal('1E-8'))`` would be exponential."""
    return None if value is None else format(value, "f")


def _canonical_amount(value: Decimal) -> str:
    """One spelling per numeric value: trailing zeros stripped, fixed-point.

    ``normalize()`` collapses ``0.1500`` and ``0.15`` to the same Decimal but
    renders integers exponentially (``Decimal('100').normalize()`` is
    ``1E+2``), so the ``f`` format is still what puts it on the wire.
    """
    return format(value.normalize(), "f")


def _first_fee_pct(field: Any) -> Decimal | None:
    """First-tier percent from an AssetPairs ``fees`` / ``fees_maker`` table.

    The schedule is ``[[volume, percent], ...]`` ascending by volume, so tier
    zero is the rate that applies at pilot size.
    """
    if isinstance(field, list) and field:
        tier = field[0]
        if isinstance(tier, (list, tuple)) and len(tier) >= 2:
            return _dec_or_none(tier[1])
    return None


# ----------------------------------------------------------------------
# Ledger helpers
# ----------------------------------------------------------------------
async def ensure_pilot_anchor(db: Database) -> int:
    """Return the pilot anchor ``paper_trades.id``, creating it if absent.

    See the module docstring for why the anchor exists and why its
    status / opened_at are what they are. Idempotent: the natural key is
    ``UNIQUE(token_id, signal_type, opened_at)``, so a concurrent creator
    loses the INSERT race and re-reads the winner's row.
    """
    if db._conn is None or db._txn_lock is None:
        raise RuntimeError("Database not initialized.")

    select_sql = (
        "SELECT id FROM paper_trades "
        "WHERE token_id = ? AND signal_type = ? AND opened_at = ?"
    )
    params = (_ANCHOR_TOKEN_ID, _ANCHOR_SIGNAL_TYPE, _ANCHOR_OPENED_AT)
    cur = await db._conn.execute(select_sql, params)
    row = await cur.fetchone()
    if row is not None:
        return int(row[0])

    signal_data = json.dumps(
        {
            "note": "Anchor row for scout.live.kraken_pilot. Not a trade.",
            "why": "live_trades.paper_trade_id is NOT NULL REFERENCES "
            "paper_trades(id) and the supervised pilot has no signal behind it.",
        }
    )
    try:
        async with db._txn_lock:
            cur = await db._conn.execute(
                """INSERT INTO paper_trades
                   (token_id, symbol, name, chain, signal_type, signal_data,
                    entry_price, amount_usd, quantity, tp_price, sl_price,
                    status, opened_at)
                   VALUES (?, 'KRAKEN-PILOT', 'Kraken supervised pilot anchor',
                           'kraken', ?, ?, 0, 0, 0, 0, 0, ?, ?)""",
                (
                    _ANCHOR_TOKEN_ID,
                    _ANCHOR_SIGNAL_TYPE,
                    signal_data,
                    _ANCHOR_STATUS,
                    _ANCHOR_OPENED_AT,
                ),
            )
            await db._conn.commit()
            anchor_id = int(cur.lastrowid or 0)
        log.info("kraken_pilot_anchor_created", paper_trade_id=anchor_id)
        return anchor_id
    except sqlite3.IntegrityError:
        # Lost the race on the natural key — the other creator's row is the
        # one to use.
        cur = await db._conn.execute(select_sql, params)
        row = await cur.fetchone()
        if row is None:  # pragma: no cover - only reachable on a torn DB
            raise
        return int(row[0])


async def update_live_trade(db: Database, live_trade_id: int, **columns: Any) -> None:
    """UPDATE one ``live_trades`` row under ``db._txn_lock``.

    Column names come from this module's own call sites, never from operator
    input, so interpolating them into the SET clause carries no injection
    surface; the values stay parameterized.
    """
    if db._conn is None or db._txn_lock is None:
        raise RuntimeError("Database not initialized.")
    if not columns:
        return
    assignments = ", ".join(f"{name} = ?" for name in columns)
    async with db._txn_lock:
        await db._conn.execute(
            f"UPDATE live_trades SET {assignments} WHERE id = ?",
            (*columns.values(), live_trade_id),
        )
        await db._conn.commit()


async def fetch_blocking_rows(db: Database) -> list[dict[str, Any]]:
    """Kraken ledger rows that mean an order may still be live."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    placeholders = ", ".join("?" for _ in _BLOCKING_STATUSES)
    cur = await db._conn.execute(
        "SELECT id, client_order_id, entry_order_id, status, pair, symbol, "
        "       size_usd, entry_fill_qty, created_at "
        f"FROM live_trades WHERE venue = ? AND status IN ({placeholders}) "
        "ORDER BY id",
        (VENUE, *_BLOCKING_STATUSES),
    )
    rows = await cur.fetchall()
    return [
        {
            "live_trade_id": r[0],
            "client_order_id": r[1],
            "entry_order_id": r[2],
            "status": r[3],
            "pair": r[4],
            "symbol": r[5],
            "size_usd": r[6],
            "entry_fill_qty": r[7],
            "created_at": r[8],
        }
        for r in rows
    ]


async def fetch_row_by_decision_id(
    db: Database, decision_id: str
) -> dict[str, Any] | None:
    """The ledger row whose ``client_order_id`` is this decision id."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    cur = await db._conn.execute(
        "SELECT id, client_order_id, entry_order_id, status, pair, symbol, "
        "       size_usd, entry_fill_price, entry_fill_qty, created_at "
        "FROM live_trades WHERE venue = ? AND client_order_id = ?",
        (VENUE, decision_id),
    )
    r = await cur.fetchone()
    if r is None:
        return None
    return {
        "live_trade_id": r[0],
        "client_order_id": r[1],
        "entry_order_id": r[2],
        "status": r[3],
        "pair": r[4],
        "symbol": r[5],
        "size_usd": r[6],
        "entry_fill_price": r[7],
        "entry_fill_qty": r[8],
        "created_at": r[9],
    }


async def fetch_live_trade_row(
    db: Database, live_trade_id: int
) -> dict[str, Any] | None:
    """One ``live_trades`` row by primary key, or ``None``.

    Deliberately NOT filtered by venue. ``exit`` has to be able to tell "no
    such row" apart from "that row belongs to another venue" — a venue filter
    collapses both into ``None`` and the operator is told their row does not
    exist when it does.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    cur = await db._conn.execute(
        "SELECT id, client_order_id, entry_order_id, status, venue, pair, symbol, "
        "       size_usd, entry_fill_price, entry_fill_qty, exit_order_id, "
        "       exit_fill_price, realized_pnl_usd, realized_pnl_pct, created_at, "
        "       closed_at "
        "FROM live_trades WHERE id = ?",
        (live_trade_id,),
    )
    r = await cur.fetchone()
    if r is None:
        return None
    return {
        "live_trade_id": r[0],
        "client_order_id": r[1],
        "entry_order_id": r[2],
        "status": r[3],
        "venue": r[4],
        "pair": r[5],
        "symbol": r[6],
        "size_usd": r[7],
        "entry_fill_price": r[8],
        "entry_fill_qty": r[9],
        "exit_order_id": r[10],
        "exit_fill_price": r[11],
        "realized_pnl_usd": r[12],
        "realized_pnl_pct": r[13],
        "created_at": r[14],
        "closed_at": r[15],
    }


async def daily_gross_usd(db: Database, day: str) -> Decimal:
    """Sum of today's kraken notionals, excluding rejected rows.

    ``substr(created_at, 1, 10)`` rather than SQLite's ``date()``: every
    writer in this tree stamps ``datetime.now(timezone.utc).isoformat()``, so
    the first ten characters ARE the UTC date, and the prefix compare cannot
    be thrown by an offset suffix the way date-function parsing can. Summed
    in Python as Decimal because ``size_usd`` is TEXT and ``SUM(CAST(... AS
    REAL))`` would round the money.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    # (status IS NULL OR status != 'rejected') rather than the bare inequality:
    # SQL three-valued logic drops NULL-status rows from `status != 'rejected'`
    # entirely, which would silently exclude a row from the day's exposure.
    # The column is NOT NULL today; this keeps the accounting correct if that
    # ever changes rather than under-counting the cap.
    cur = await db._conn.execute(
        "SELECT size_usd FROM live_trades "
        "WHERE venue = ? AND (status IS NULL OR status != 'rejected') "
        "AND substr(created_at, 1, 10) = ?",
        (VENUE, day),
    )
    total = Decimal("0")
    for (size_usd,) in await cur.fetchall():
        value = _dec_or_none(size_usd)
        if value is not None:
            total += value
    return total


# ----------------------------------------------------------------------
# Operator I/O
# ----------------------------------------------------------------------
def exit_authorization_token(
    *,
    pair: str,
    side: str,
    volume: Decimal,
    price: Decimal,
    client_order_id: str,
) -> str:
    """The token the operator retypes to authorize ONE specific exit order.

    A digest over the exact tuple that defines the order — pair, side,
    quantity, limit price, client order id — so the token is an approval of
    THAT order and nothing else. Change the price by one tick, or run against
    a row whose quantity moved, and the token the operator was shown no longer
    matches what they would be typing for.

    That is the whole difference from ``place``'s decision-id prefix, which
    identifies the RUN. On ``place`` the parameters came from the operator's
    own command line, so run-identity is enough. An exit's quantity is read
    out of the ledger, which the operator did not type — binding to the run
    would let a changed quantity ride an authorization given for a different
    one.

    Numbers are canonicalized before digesting — trailing zeros stripped, then
    rendered fixed-point — so ``0.1500`` and ``0.15`` are one order and not
    two. That matters because the quantity arrives as free-form TEXT from the
    ledger: without it, a row whose quantity was stored with different padding
    would refuse an authorization that is economically identical, which trains
    the operator to retype tokens without reading them.
    """
    payload = "|".join(
        [
            _EXIT_AUTH_DOMAIN,
            pair.strip().upper(),
            side.strip().lower(),
            _canonical_amount(volume),
            _canonical_amount(price),
            client_order_id,
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest[:_EXIT_AUTH_TOKEN_CHARS].upper()


def exit_economics(
    *,
    entry_price: Decimal,
    held_qty: Decimal,
    exit_price: Decimal,
    entry_fee: Decimal,
    fee_pct: Decimal,
) -> dict[str, Any]:
    """Fees, break-even and P&L for ONE fee assumption (maker OR taker).

    Called once per scenario rather than blended, because the two rates are
    not two guesses at one number — they are two different things that can
    happen to the same order, and averaging them would describe an outcome
    that cannot occur.

    ``entry_fee`` is a single historical number: it was already paid, and it
    does not depend on how the EXIT executes. Only the exit fee varies with
    ``fee_pct``.

    Break-even is the exit price at which realised P&L is exactly zero::

        proceeds = P * q * (1 - r)          cost_basis = entry_price * q + entry_fee
        P_be     = cost_basis / (q * (1 - r))

    i.e. the fee is paid on the exit notional, so the break-even sits above
    the naive ``entry + fees / q`` by the fee charged on the fee.
    """
    rate = fee_pct / Decimal("100")
    exit_notional = exit_price * held_qty
    exit_fee = exit_notional * rate
    cost_basis = entry_price * held_qty + entry_fee
    denominator = held_qty * (Decimal("1") - rate)
    break_even = cost_basis / denominator if denominator > 0 else None
    proceeds = exit_notional - exit_fee
    pnl = proceeds - cost_basis
    pnl_pct = pnl / cost_basis * Decimal("100") if cost_basis > 0 else None
    return {
        "fee_pct": _fmt(fee_pct),
        "exit_notional": _fmt(exit_notional),
        "exit_fee": _fmt(exit_fee),
        "entry_fee": _fmt(entry_fee),
        "round_trip_fee": _fmt(entry_fee + exit_fee),
        "cost_basis": _fmt(cost_basis),
        "break_even_price": _fmt(break_even),
        "proceeds": _fmt(proceeds),
        "realized_pnl_usd": _fmt(pnl),
        "realized_pnl_pct": _fmt(pnl_pct),
    }


def read_authorization(expected: str) -> tuple[bool, str]:
    """Read the operator's typed authorization from stdin.

    Returns ``(authorized, outcome)``. Every non-match is a refusal:
    a mismatch, an empty line, and EOF (a non-interactive stdin, a closed
    pipe, ``</dev/null``) all abort the run.
    """
    try:
        typed = input("authorize> ")
    except (EOFError, KeyboardInterrupt, OSError):
        return False, "no_input"
    typed = typed.strip()
    if not typed:
        return False, "empty"
    if typed != expected:
        return False, "mismatch"
    return True, "authorized"


def _print_block(title: str, lines: list[str]) -> None:
    bar = "=" * 68
    print(bar)
    print(title)
    print(bar)
    for line in lines:
        print(line)
    print(bar)


# ----------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------
class PilotRunner:
    """One supervised Kraken pilot invocation.

    Constructed with everything it needs so tests can drive it against a
    tmp_path DB and a mocked venue; ``main`` wires the production instance.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        adapter: KrakenSpotAdapter,
        kill_switch: KillSwitch,
    ) -> None:
        self._settings = settings
        self._db = db
        self._adapter = adapter
        self._ks = kill_switch

    # ---------------- shared gates ----------------
    def _evidence_for(self, decision_id: str) -> EvidenceLog:
        directory = Path(self._settings.KRAKEN_PILOT_EVIDENCE_DIR)
        return EvidenceLog(directory / f"kraken_pilot_{decision_id}.json")

    def _secret_present(self, field: str) -> bool:
        raw = getattr(self._settings, field, None)
        if raw is None:
            return False
        value = raw.get_secret_value() if hasattr(raw, "get_secret_value") else str(raw)
        return bool(value)

    def _check_credentials(self) -> None:
        missing = [
            field
            for field in ("KRAKEN_API_KEY", "KRAKEN_API_SECRET")
            if not self._secret_present(field)
        ]
        if missing:
            raise PilotAbort(
                "envelope_gate", f"missing credentials: {', '.join(missing)}"
            )

    def _check_envelope(self, *, validate_only: bool) -> dict[str, Any]:
        """Step 1 — the pilot is enabled, a pair is approved, keys exist.

        Placement only. ``cancel`` deliberately runs a narrower gate — see
        ``_check_cancel_envelope``.
        """
        if not self._settings.KRAKEN_PILOT_ENABLED:
            raise PilotAbort(
                "envelope_gate",
                "KRAKEN_PILOT_ENABLED is False — the pilot lane is off. Set it "
                "in .env only for the supervised session.",
            )
        pair = (self._settings.KRAKEN_PILOT_PAIR or "").strip().upper()
        if not pair:
            raise PilotAbort(
                "envelope_gate",
                "KRAKEN_PILOT_PAIR is empty — name the ONE approved base symbol "
                "(e.g. KRAKEN_PILOT_PAIR=BTC) before running the pilot.",
            )
        self._check_credentials()
        if not validate_only and not self._settings.LIVE_USE_REAL_SIGNED_REQUESTS:
            # Checked here rather than left to the adapter so the refusal lands
            # BEFORE the operator is asked to authorize an order that could not
            # have been placed either way.
            raise PilotAbort(
                "envelope_gate",
                "LIVE_USE_REAL_SIGNED_REQUESTS is False — the emergency-revert "
                "posture blocks every real order. Use --validate-only to "
                "rehearse, or flip the flag for the supervised session.",
            )
        return {
            "pilot_enabled": True,
            "pair": pair,
            "quote": self._settings.KRAKEN_PILOT_QUOTE.upper(),
            "credentials_present": True,
            "validate_only": validate_only,
        }

    def _check_cancel_envelope(self) -> dict[str, Any]:
        """Credentials only — cancel is NOT gated on the pilot envelope.

        Requiring ``KRAKEN_PILOT_ENABLED`` here would mean that turning the
        master gate off — the operator's instinctive first move when something
        looks wrong — also takes away the lever that pulls a resting order.
        Same reasoning as the adapter leaving ``cancel_order`` outside
        ``LIVE_USE_REAL_SIGNED_REQUESTS``: a gate must not disable the thing
        that reduces exposure. The approved-pair setting is not needed either;
        the ledger row carries the pair.
        """
        self._check_credentials()
        return {
            "credentials_present": True,
            "pilot_enabled": self._settings.KRAKEN_PILOT_ENABLED,
            "note": "cancel is not gated on KRAKEN_PILOT_ENABLED — it reduces "
            "exposure, so the master gate must not be able to disable it",
        }

    async def _check_kill_switch(self, stage: str) -> dict[str, Any]:
        """Steps 2 and 11 — refuse while the live kill switch is engaged."""
        state = await self._ks.is_active()
        if state is None:
            return {"kill_active": False}
        raise PilotAbort(
            stage,
            f"kill switch ACTIVE (event #{state.kill_event_id}, by "
            f"{state.triggered_by}, until {state.killed_until.isoformat()}): "
            f"{state.reason}",
        )

    async def _reconcile_open_rows(self) -> dict[str, Any]:
        """Step 4 — reconcile the ledger and the venue, in BOTH directions.

        Ledger → venue: every non-terminal row is looked up by client id. A
        ``needs_manual_review`` row always blocks; an ``open`` row always
        blocks too (one live order at a time), and the lookup is there to tell
        the operator WHAT it is rather than to decide whether it counts.

        Venue → ledger: the account's full resting-order list is fetched, and
        any order the ledger cannot account for is its own blocker. This is
        the direction a client-id lookup structurally cannot cover — an order
        placed from the Kraken web UI, or left by a run whose ledger row never
        landed, has no cl_ord_id we would think to ask about, so a
        ledger-driven check reports a clear lane while the account holds a
        live order. Fails CLOSED: if the listing cannot be read, that is a
        blocker too, because "no open orders" is what licenses a placement.

        Returns a dict with ``rows`` / ``venue_open_orders`` / ``unknown_orders``
        / ``blockers`` (the total the caller gates on) / ``venue_open_count``.

        Nothing is auto-mutated here. A filled entry order leaves a real
        position, and 'closed_*' in this ledger means the trade is closed —
        so silently retiring the row would assert the position is gone.
        """
        rows = await fetch_blocking_rows(self._db)
        for row in rows:
            cid = row.get("client_order_id")
            if not cid:
                row["venue"] = {
                    "outcome": "unresolvable",
                    "detail": "ledger row has no client_order_id to look up",
                }
                continue
            try:
                conf = await self._adapter.fetch_order_by_client_id(
                    pair=row.get("pair") or "", client_order_id=cid
                )
            except Exception as exc:
                row["venue"] = {
                    "outcome": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                continue
            if conf is None:
                row["venue"] = {"outcome": "absent"}
            else:
                row["venue"] = {
                    "outcome": "found",
                    "status": conf.status,
                    "txid": conf.venue_order_id,
                    "filled_qty": conf.filled_qty,
                    "fill_price": conf.fill_price,
                }

        # Known = the identities of the rows we are already blocking on. An
        # order matching one of those is accounted for by its row; anything
        # else resting at the venue is not accounted for at all — including an
        # order whose ledger row says it is terminal, which is its own alarm.
        known_ids = {row["client_order_id"] for row in rows if row["client_order_id"]}
        known_ids |= {row["entry_order_id"] for row in rows if row["entry_order_id"]}

        venue_open_orders: list[dict[str, Any]] = []
        unknown_orders: list[dict[str, Any]] = []
        listing_error: dict[str, Any] | None = None
        try:
            venue_open_orders = await self._adapter.fetch_open_orders()
        except Exception as exc:
            listing_error = {
                "error_type": type(exc).__name__,
                "error": str(exc),
                "detail": "could not read the account's resting orders; refusing "
                "to treat an unreadable listing as an empty one",
            }
            log.warning(
                "kraken_pilot_open_orders_unreadable",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        else:
            unknown_orders = [
                order
                for order in venue_open_orders
                if order.get("client_order_id") not in known_ids
                and order.get("txid") not in known_ids
            ]

        blockers = len(rows) + len(unknown_orders) + (1 if listing_error else 0)
        return {
            "rows": rows,
            "venue_open_orders": venue_open_orders,
            "venue_open_count": len(venue_open_orders),
            "unknown_orders": unknown_orders,
            "listing_error": listing_error,
            "blockers": blockers,
        }

    async def _resolve_market_rules(self, canonical: str) -> tuple[Any, dict[str, Any]]:
        """Step 5 — venue metadata plus the raw AssetPairs row (for fees)."""
        meta = await self._adapter.fetch_venue_metadata(canonical)
        if meta is None:
            raise PilotAbort(
                "market_rules",
                f"Kraken does not list a tradable {canonical}/"
                f"{self._settings.KRAKEN_PILOT_QUOTE} spot book (or its status is "
                "not 'online')",
            )
        expected_quote = self._settings.KRAKEN_PILOT_QUOTE.strip().upper()
        if meta.quote != expected_quote:
            raise PilotAbort(
                "market_rules",
                f"resolved book {meta.venue_pair} is quoted in {meta.quote}, not "
                f"the approved KRAKEN_PILOT_QUOTE={expected_quote}",
            )
        row = await self._adapter.fetch_exchange_info_row(meta.venue_pair)
        return meta, (row or {})

    @staticmethod
    def _check_against_rules(
        *, meta: Any, price: Decimal, volume: Decimal
    ) -> list[str]:
        """Validate CLI price/volume against tick / lot / ordermin / costmin.

        The adapter re-validates all of this before it builds a body — this
        pass exists so the operator gets one clean error listing everything
        wrong, rather than discovering the problems one exception at a time.
        """
        problems: list[str] = []
        notional = price * volume
        tick = _dec_or_none(meta.tick_size)
        if tick is not None and tick > 0 and price % tick != 0:
            problems.append(
                f"price {_fmt(price)} is not a multiple of tick_size {_fmt(tick)}"
            )
        lot = _dec_or_none(meta.lot_size)
        if lot is not None and lot > 0 and volume % lot != 0:
            problems.append(
                f"volume {_fmt(volume)} is not a multiple of lot_size {_fmt(lot)}"
            )
        min_size = _dec_or_none(meta.min_size)
        if min_size is not None and volume < min_size:
            problems.append(f"volume {_fmt(volume)} is below ordermin {_fmt(min_size)}")
        min_cost = _dec_or_none(meta.min_cost)
        if min_cost is not None and notional < min_cost:
            problems.append(
                f"notional {_fmt(notional)} is below costmin {_fmt(min_cost)} "
                "(Kraken enforces costmin independently of ordermin)"
            )
        return problems

    async def _check_caps(
        self,
        *,
        notional: Decimal,
        price: Decimal,
        volume: Decimal,
        side: str,
        venue_pair: str,
    ) -> dict[str, Any]:
        """Step 6 — per-order bounds, daily gross, and price sanity.

        Two notionals, because the limit price alone does not bound what an
        order can transact. A limit is a WORST-price instruction, not a size
        one: a sell priced below the mid does not sell for its limit, it sells
        into the book from the top down, so ``volume`` base units leave the
        account at roughly the mid. Bounding only ``price * volume`` therefore
        bounds nothing on the marketable side — a sell of 1.5 BTC priced at
        $10 reads as a $15 order and disposes of $150 at a $100 mid.

        So both are computed and both must clear the per-order ceiling:
        ``limit_notional = price * volume`` and
        ``market_notional = mid * volume``. Checked on BOTH sides rather than
        just the sell: a buy is bounded by its limit in principle, but that
        assumes the fill price is the one field of the order that cannot
        surprise us, and this cap exists precisely for the case where an
        assumption about price is wrong.

        The larger of the two is what gets counted against the daily gross and
        persisted as ``size_usd``, so the ledger records exposure actually
        taken rather than exposure nominally requested.

        Note the asymmetry, which is deliberate: the MAXIMUM is checked
        against both notionals, the MINIMUM against the limit notional only.
        The floor is an economic "is this trade worth doing" bound on what the
        operator asked for, not a safety bound — nothing dangerous happens
        when an order turns out smaller than intended, and the venue's own
        ``costmin`` is enforced separately in ``_check_against_rules``.
        Applying the floor to the market notional too would refuse orders for
        being too small at the current mid, which is a refusal footgun with no
        safety payoff. Do not "fix" the asymmetry.
        """
        min_usd = _dec(self._settings.KRAKEN_PILOT_MIN_ORDER_USD)
        max_usd = _dec(self._settings.KRAKEN_PILOT_MAX_ORDER_USD)
        if notional < min_usd or notional > max_usd:
            raise PilotAbort(
                "caps",
                f"limit notional {_fmt(notional)} is outside the approved "
                f"per-order band [{_fmt(min_usd)}, {_fmt(max_usd)}]",
            )

        mid = await self._adapter.fetch_price(venue_pair)
        market_notional = mid * volume if mid > 0 else Decimal("0")
        if mid > 0 and market_notional > max_usd:
            raise PilotAbort(
                "caps",
                f"at the current mid {_fmt(mid)} this order transacts "
                f"{_fmt(market_notional)} ({_fmt(volume)} units), over "
                f"KRAKEN_PILOT_MAX_ORDER_USD={_fmt(max_usd)}. A limit price "
                f"bounds the price, not the size — the {_fmt(notional)} limit "
                "notional is not what this order is worth.",
            )
        effective_notional = max(notional, market_notional)

        day = datetime.now(timezone.utc).date().isoformat()
        gross_before = await daily_gross_usd(self._db, day)
        gross_after = gross_before + effective_notional
        max_gross = _dec(self._settings.KRAKEN_PILOT_MAX_DAILY_GROSS_USD)
        if gross_after > max_gross:
            raise PilotAbort(
                "caps",
                f"daily gross would reach {_fmt(gross_after)} "
                f"({_fmt(gross_before)} already today + "
                f"{_fmt(effective_notional)}), over "
                f"KRAKEN_PILOT_MAX_DAILY_GROSS_USD={_fmt(max_gross)}",
            )

        # Price sanity is a WARNING, never a block: a marketable limit is a
        # legitimate supervised choice. It has to be a deliberate one, so it
        # gets surfaced in the approval block instead of being swallowed.
        warnings: list[str] = []
        deviation_pct = Decimal("0")
        if mid > 0:
            deviation_pct = abs(price - mid) / mid * Decimal("100")
            limit_pct = _dec(self._settings.KRAKEN_PILOT_PRICE_DEVIATION_WARN_PCT)
            if deviation_pct > limit_pct:
                warnings.append(
                    f"limit price {_fmt(price)} deviates {_fmt(deviation_pct)}% "
                    f"from mid {_fmt(mid)} (warn threshold {_fmt(limit_pct)}%)"
                )
        crossing = (side == "buy" and price > mid) or (side == "sell" and price < mid)
        if crossing:
            warnings.append(
                f"limit {_fmt(price)} CROSSES the mid {_fmt(mid)} on a {side} — "
                "this order is marketable and may fill immediately as taker"
            )
        for warning in warnings:
            log.warning("kraken_pilot_price_warning", detail=warning)
        return {
            "notional": _fmt(notional),
            "market_notional": _fmt(market_notional),
            "effective_notional": _fmt(effective_notional),
            "per_order_band": [_fmt(min_usd), _fmt(max_usd)],
            "daily_gross_before": _fmt(gross_before),
            "daily_gross_after": _fmt(gross_after),
            "daily_gross_cap": _fmt(max_gross),
            "mid": _fmt(mid),
            "deviation_pct": _fmt(deviation_pct),
            "crossing": crossing,
            "warnings": warnings,
        }

    async def _check_balance(
        self, *, side: str, meta: Any, notional: Decimal, volume: Decimal
    ) -> dict[str, Any]:
        """Step 7 — the funding side covers the order plus fee headroom."""
        headroom = _dec(self._settings.KRAKEN_PILOT_FEE_HEADROOM_PCT) / Decimal("100")
        if side == "buy":
            asset = meta.quote
            required = notional * (Decimal("1") + headroom)
        else:
            asset = meta.canonical
            required = volume
        available = _dec(await self._adapter.fetch_account_balance(asset=asset))
        if available < required:
            raise PilotAbort(
                "balance",
                f"available {asset} balance {_fmt(available)} does not cover the "
                f"required {_fmt(required)} (order + "
                f"{self._settings.KRAKEN_PILOT_FEE_HEADROOM_PCT}% fee headroom)",
            )
        return {
            "asset": asset,
            "available": _fmt(available),
            "required": _fmt(required),
        }

    # ---------------- place ----------------
    async def place(
        self,
        *,
        side: str,
        price: Decimal,
        volume: Decimal,
        validate_only: bool = False,
    ) -> int:
        decision_id = str(uuid4())
        # The decision id IS the cl_ord_id: a dashed uuid4 is Kraken's
        # documented long-UUID form (adapter fact 10), so one identifier ties
        # the operator's authorization, the ledger row and the venue order
        # together with nothing to reconcile between them.
        client_order_id = decision_id
        evidence = self._evidence_for(decision_id)
        evidence.record(
            "run_started",
            command="place",
            decision_id=decision_id,
            side=side,
            price=_fmt(price),
            volume=_fmt(volume),
            validate_only=validate_only,
            evidence_path=str(evidence.path),
            # Which database this run read its safety state from. Without it,
            # an evidence pack cannot distinguish "the kill switch was clear"
            # from "we were looking at the wrong database".
            db_path=str(Path(self._settings.DB_PATH).resolve()),
        )

        live_trade_id: int | None = None
        try:
            envelope = self._check_envelope(validate_only=validate_only)
            evidence.record("envelope_gate", **envelope)

            kill = await self._check_kill_switch("kill_switch_check")
            evidence.record("kill_switch_check", **kill)

            try:
                preflight = await self._adapter.preflight_credentials_check()
            except Exception as exc:
                raise PilotAbort(
                    "preflight",
                    f"{type(exc).__name__}: {exc}",
                ) from exc
            evidence.record("preflight", **preflight)

            reconciliation = await self._reconcile_open_rows()
            evidence.record(
                "startup_reconciliation",
                blocking_rows=reconciliation["rows"],
                venue_open_orders=reconciliation["venue_open_orders"],
                venue_open_count=reconciliation["venue_open_count"],
                unknown_orders=reconciliation["unknown_orders"],
                listing_error=reconciliation["listing_error"],
                blocker_count=reconciliation["blockers"],
            )
            if reconciliation["blockers"]:
                self._print_blocked(reconciliation)
                raise PilotAbort(
                    "startup_reconciliation",
                    f"{reconciliation['blockers']} unresolved item(s) block the "
                    f"lane ({len(reconciliation['rows'])} ledger row(s), "
                    f"{len(reconciliation['unknown_orders'])} unknown venue "
                    f"order(s))",
                    exit_code=EXIT_BLOCKED,
                )

            canonical = envelope["pair"]
            meta, pair_row = await self._resolve_market_rules(canonical)
            problems = self._check_against_rules(meta=meta, price=price, volume=volume)
            evidence.record(
                "market_rules",
                canonical=meta.canonical,
                venue_pair=meta.venue_pair,
                quote=meta.quote,
                tick_size=meta.tick_size,
                lot_size=meta.lot_size,
                min_size=meta.min_size,
                min_cost=meta.min_cost,
                problems=problems,
            )
            if problems:
                raise PilotAbort("market_rules", "; ".join(problems))

            notional = price * volume
            caps = await self._check_caps(
                notional=notional,
                price=price,
                volume=volume,
                side=side,
                venue_pair=meta.venue_pair,
            )
            evidence.record("caps", **caps)
            # What the ledger records is exposure TAKEN, not exposure asked
            # for: on the marketable side those differ (see _check_caps).
            effective_notional = _dec_or_none(caps["effective_notional"]) or notional

            balance = await self._check_balance(
                side=side, meta=meta, notional=notional, volume=volume
            )
            evidence.record("balance", **balance)

            existing = await lookup_existing_order_id(self._db, client_order_id)
            if existing is not None:  # pragma: no cover - uuid4 collision
                raise PilotAbort(
                    "identity",
                    f"decision id {decision_id} already exists in live_trades",
                )
            evidence.record(
                "identity", decision_id=decision_id, client_order_id=client_order_id
            )

            taker_pct = _first_fee_pct(pair_row.get("fees"))
            maker_pct = _first_fee_pct(pair_row.get("fees_maker"))
            fee_estimated = taker_pct is None
            if taker_pct is None:
                taker_pct = _DEFAULT_TAKER_FEE_PCT
            estimated_fee = notional * taker_pct / Decimal("100")

            authorized, outcome = self._request_authorization(
                decision_id=decision_id,
                client_order_id=client_order_id,
                meta=meta,
                side=side,
                price=price,
                volume=volume,
                notional=notional,
                taker_pct=taker_pct,
                maker_pct=maker_pct,
                fee_estimated=fee_estimated,
                estimated_fee=estimated_fee,
                balance=balance,
                caps=caps,
                venue_open_count=reconciliation["venue_open_count"],
                validate_only=validate_only,
            )
            evidence.record(
                "authorization",
                outcome="authorized" if authorized else "authorization_refused",
                detail=outcome,
                expected_prefix_len=8,
            )
            if not authorized:
                raise PilotAbort(
                    "authorization",
                    f"operator did not authorize ({outcome}) — nothing was sent",
                )

            if not validate_only:
                # Re-check immediately before the write. The step-4 scan ran
                # before the approval prompt, which is unbounded operator
                # time — long enough for another run to have written a row.
                # The lock in main() makes a second pilot process impossible,
                # so this closes the same window against anything else that
                # writes the ledger (the live engine, a manual fix mid-run).
                late_rows = await fetch_blocking_rows(self._db)
                if late_rows:
                    evidence.record(
                        "pre_write_recheck",
                        outcome="blocked",
                        blocking_rows=late_rows,
                    )
                    raise PilotAbort(
                        "pre_write_recheck",
                        f"{len(late_rows)} kraken ledger row(s) appeared while the "
                        "approval was pending — nothing was sent",
                        exit_code=EXIT_BLOCKED,
                    )
                evidence.record("pre_write_recheck", outcome="clear")

                # size_usd here is the AUTHORIZED CEILING — the larger of the
                # limit and market notionals, i.e. the most this order was
                # approved to transact. It is NOT executed notional, and it is
                # written before the order exists, so nothing has executed yet.
                # What actually happened lives in entry_fill_price /
                # entry_fill_qty, the per-fill rows, and the evidence file. Any
                # reader totalling size_usd is measuring authorized exposure
                # (which is what the daily cap governs), not realised volume.
                live_trade_id = await record_pending_order(
                    self._db,
                    client_order_id=client_order_id,
                    paper_trade_id=await ensure_pilot_anchor(self._db),
                    coin_id=f"kraken-pilot-{meta.canonical.lower()}",
                    symbol=meta.canonical,
                    venue=VENUE,
                    pair=meta.venue_pair,
                    signal_type=_ANCHOR_SIGNAL_TYPE,
                    size_usd=_fmt(effective_notional),
                    mid_at_entry=caps.get("mid"),
                )
                evidence.record(
                    "intent_persisted",
                    live_trade_id=live_trade_id,
                    status="open",
                    note="written immediately before submission; 'open' means an "
                    "order may exist at the venue from this instant",
                )
            else:
                evidence.record(
                    "intent_skipped",
                    reason="validate_only rehearsal cannot create an order, so no "
                    "ledger row is written (a row would be a phantom that blocks "
                    "the next run's startup reconciliation)",
                )

            # Step 11 — post-approval kill re-check. Handled inline rather than
            # through PilotAbort because the intent row already exists and must
            # be retired: it asserts "an order may exist" and nothing was sent.
            kill_state = await self._ks.is_active()
            if kill_state is not None:
                if live_trade_id is not None:
                    await update_live_trade(
                        self._db,
                        live_trade_id,
                        status="rejected",
                        reject_reason="kill_switch",
                        closed_at=datetime.now(timezone.utc).isoformat(),
                    )
                evidence.record(
                    "kill_switch_recheck",
                    kill_active=True,
                    kill_event_id=kill_state.kill_event_id,
                    reason=kill_state.reason,
                    live_trade_id=live_trade_id,
                    ledger_status="rejected",
                )
                raise PilotAbort(
                    "kill_switch_recheck",
                    f"kill switch engaged between approval and submission "
                    f"(event #{kill_state.kill_event_id}) — nothing was sent",
                )
            evidence.record("kill_switch_recheck", kill_active=False)

            return await self._submit_and_resolve(
                evidence=evidence,
                decision_id=decision_id,
                client_order_id=client_order_id,
                live_trade_id=live_trade_id,
                meta=meta,
                side=side,
                price=price,
                volume=volume,
                notional=notional,
                validate_only=validate_only,
                balance_before=_dec_or_none(balance["available"]),
                balance_asset=balance["asset"],
            )

        except PilotAbort as abort:
            evidence.record(
                "aborted",
                stage=abort.stage,
                reason=abort.reason,
                exit_code=abort.exit_code,
            )
            print(f"REFUSED [{abort.stage}]: {abort.reason}")
            print(f"evidence: {evidence.path}")
            return abort.exit_code
        except Exception as exc:
            # Anything unanticipated. Recorded and turned into an escalation
            # rather than a traceback: this can fire AFTER an order exists, and
            # a stack trace on stderr is not an evidence record. The ledger row
            # is deliberately left as it stands — whatever it says is what was
            # last known to be true.
            log.error(
                "kraken_pilot_unexpected_error",
                decision_id=decision_id,
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=True,
            )
            evidence.record(
                "unexpected_error",
                error_type=type(exc).__name__,
                error=str(exc),
                live_trade_id=live_trade_id,
            )
            _print_block(
                "ESCALATE — UNEXPECTED FAILURE",
                [
                    f"  {type(exc).__name__}: {exc}",
                    f"  decision ID     : {decision_id}",
                    f"  live_trades row : {live_trade_id}",
                    "  Confirm the venue state before any further action.",
                    f"  evidence        : {evidence.path}",
                ],
            )
            return EXIT_ESCALATE

    def _request_authorization(
        self,
        *,
        decision_id: str,
        client_order_id: str,
        meta: Any,
        side: str,
        price: Decimal,
        volume: Decimal,
        notional: Decimal,
        taker_pct: Decimal,
        maker_pct: Decimal | None,
        fee_estimated: bool,
        estimated_fee: Decimal,
        balance: dict[str, Any],
        caps: dict[str, Any],
        venue_open_count: int,
        validate_only: bool,
    ) -> tuple[bool, str]:
        """Step 9 — print the decision block and require the typed prefix.

        Every figure here is one the operator is being asked to accept, so
        each is a value read this run, not a constant. In particular the
        open-order count comes from the venue's own listing rather than from
        our ledger: a ledger fact printed as if it were a venue fact is
        exactly the sort of reassurance that survives being wrong.

        The numeric lines use ASCII operators (``->``) rather than typographic
        ones. This block is read on whatever console the operator has, and a
        cp1252 terminal renders a stray arrow as a mojibake glyph sitting
        between two money figures.
        """
        fee_label = " (ESTIMATE - AssetPairs published no fee schedule)"
        maker_line = f"{_fmt(maker_pct)}%" if maker_pct is not None else "unpublished"
        market_notional = caps.get("market_notional")
        effective_notional = caps.get("effective_notional")
        lines = [
            f"  pair                : {meta.canonical} "
            f"(venue altname {meta.venue_pair}, quote {meta.quote})",
            f"  side                : {side.upper()}",
            f"  limit price         : {_fmt(price)} {meta.quote}",
            f"  quantity            : {_fmt(volume)} {meta.canonical}",
            f"  limit notional      : {_fmt(notional)} {meta.quote} "
            f"(price x quantity)",
            f"  at current mid      : {market_notional} {meta.quote} "
            f"(what this transacts if it fills at the mid)",
            f"  exposure counted    : {effective_notional} {meta.quote} "
            f"(the larger of the two; this is what the ledger records)",
            f"  estimated fee       : {_fmt(estimated_fee)} {meta.quote} "
            f"(taker {_fmt(taker_pct)}%, maker {maker_line})"
            f"{fee_label if fee_estimated else ''}",
            f"  decision ID         : {decision_id}",
            f"  client order ID     : {client_order_id}",
            f"  database            : {Path(self._settings.DB_PATH).resolve()}",
            f"  available balance   : {balance['available']} {balance['asset']}",
            f"  daily gross now     : {caps['daily_gross_before']} "
            f"-> {caps['daily_gross_after']} of {caps['daily_gross_cap']}",
            f"  open kraken orders  : {venue_open_count} "
            f"(from the venue's own OpenOrders listing)",
            f"  current mid         : {caps['mid']} "
            f"(limit deviates {caps['deviation_pct']}%)",
        ]
        for warning in caps.get("warnings", []):
            lines.append(f"  ** WARNING          : {warning}")
        if validate_only:
            lines.append(
                "  ** REHEARSAL        : validate=true — Kraken will parse and "
                "check this order but NOT place it"
            )
        _print_block("KRAKEN SUPERVISED PILOT — MANUAL APPROVAL REQUIRED", lines)
        print(
            "Type the first 8 characters of the decision ID to authorize. "
            "Anything else — including an empty line or a closed stdin — aborts."
        )
        return read_authorization(decision_id[:8])

    async def _submit_and_resolve(
        self,
        *,
        evidence: EvidenceLog,
        decision_id: str,
        client_order_id: str,
        live_trade_id: int | None,
        meta: Any,
        side: str,
        price: Decimal,
        volume: Decimal,
        notional: Decimal,
        validate_only: bool,
        balance_before: Decimal | None,
        balance_asset: str,
    ) -> int:
        """Steps 12-13 — submit, then resolve whatever came back."""
        txid: str | None = None
        try:
            result = await self._adapter.place_limit_order(
                pair=meta.venue_pair,
                side=side,
                price=price,
                volume=volume,
                client_order_id=client_order_id,
                validate_only=validate_only,
            )
        except KrakenAmbiguousSubmissionError as exc:
            evidence.record(
                "submission_ambiguous",
                error_type=type(exc).__name__,
                error=str(exc),
                note="NEVER resend — resolving by client id instead",
            )
            code, txid = await self._resolve_ambiguity(
                evidence=evidence,
                client_order_id=client_order_id,
                live_trade_id=live_trade_id,
                meta=meta,
            )
            if code is not None:
                return code
        except Exception as exc:
            # Definitive refusal (or a validate-only failure, which the adapter
            # never reports as ambiguous). The order does not exist.
            reject_reason = _reject_reason_for_error(str(exc))
            if live_trade_id is not None:
                await update_live_trade(
                    self._db,
                    live_trade_id,
                    status="rejected",
                    reject_reason=reject_reason,
                    closed_at=datetime.now(timezone.utc).isoformat(),
                )
            evidence.record(
                "submission_refused",
                error_type=type(exc).__name__,
                error=str(exc),
                live_trade_id=live_trade_id,
                ledger_status=None if live_trade_id is None else "rejected",
                reject_reason=reject_reason,
            )
            print(f"REFUSED [submit]: {type(exc).__name__}: {exc}")
            print(f"evidence: {evidence.path}")
            return EXIT_REFUSED
        else:
            if validate_only:
                evidence.record(
                    "validate_only_accepted",
                    descr=result.get("descr"),
                    price_sent=result.get("price"),
                    volume_sent=result.get("volume"),
                    note="Kraken validated the order and placed nothing; no "
                    "ledger row exists by design",
                )
                _print_block(
                    "REHEARSAL COMPLETE — no order was placed",
                    [
                        f"  decision ID : {decision_id}",
                        f"  descr       : {result.get('descr')}",
                        f"  evidence    : {evidence.path}",
                    ],
                )
                return EXIT_OK
            txids = result.get("txid") or []
            txid = str(txids[0]) if txids else None
            evidence.record(
                "submitted",
                txid=txid,
                all_txids=txids,
                descr=result.get("descr"),
                price_sent=result.get("price"),
                volume_sent=result.get("volume"),
            )

        if live_trade_id is not None and txid:
            await update_live_trade(self._db, live_trade_id, entry_order_id=txid)
            evidence.record("txid_persisted", live_trade_id=live_trade_id, txid=txid)

        return await self._await_and_reconcile(
            evidence=evidence,
            decision_id=decision_id,
            client_order_id=client_order_id,
            live_trade_id=live_trade_id,
            txid=txid,
            meta=meta,
            side=side,
            notional=notional,
            volume=volume,
            balance_before=balance_before,
            balance_asset=balance_asset,
        )

    async def _resolve_ambiguity(
        self,
        *,
        evidence: EvidenceLog,
        client_order_id: str,
        live_trade_id: int | None,
        meta: Any,
    ) -> tuple[int | None, str | None]:
        """Turn a ``KrakenAmbiguousSubmissionError`` into a decided state.

        Returns ``(exit_code, txid)`` — an exit code when the run is over, or
        ``(None, txid)`` when the order was adopted and the caller should
        continue into fill confirmation.
        """
        detail = await self._adapter.resolve_order_submission_detail(
            client_order_id=client_order_id
        )
        verdict = detail.get("verdict")
        evidence.record("ambiguity_resolution", **detail)

        if verdict == "accepted":
            txid = None
            conf = await self._adapter.fetch_order_by_client_id(
                pair=meta.venue_pair, client_order_id=client_order_id
            )
            if conf is not None and conf.venue_order_id:
                txid = str(conf.venue_order_id)
            elif detail.get("txid"):
                txid = str(detail["txid"][0])
            evidence.record(
                "ambiguity_adopted",
                txid=txid,
                venue_status=None if conf is None else conf.status,
                note="the order DID land — adopting it rather than resending",
            )
            return None, txid

        if verdict == "not_accepted":
            if live_trade_id is not None:
                await update_live_trade(
                    self._db,
                    live_trade_id,
                    status="rejected",
                    reject_reason="venue_unavailable",
                    closed_at=datetime.now(timezone.utc).isoformat(),
                )
            evidence.record(
                "ambiguity_not_accepted",
                live_trade_id=live_trade_id,
                ledger_status="rejected",
                reject_reason="venue_unavailable",
            )
            _print_block(
                "SUBMISSION DID NOT LAND — no order exists",
                [
                    "  Two clean sweeps of OpenOrders + ClosedOrders found no",
                    "  order carrying this client id. The ledger row is rejected.",
                    f"  evidence: {evidence.path}",
                ],
            )
            return EXIT_REFUSED, None

        # unresolved — the dangerous one. STOP. Never resend.
        if live_trade_id is not None:
            await update_live_trade(
                self._db, live_trade_id, status="needs_manual_review"
            )
        evidence.record(
            "ambiguity_unresolved",
            live_trade_id=live_trade_id,
            ledger_status="needs_manual_review",
        )
        _print_block(
            "ESCALATE — SUBMISSION UNRESOLVED, DO NOT RESEND",
            [
                "  Kraken could not confirm whether this order exists.",
                f"  client order id : {client_order_id}",
                f"  live_trades row : {live_trade_id} (needs_manual_review)",
                "",
                "  The pilot lane is now BLOCKED: every future `place` run will",
                "  refuse at startup reconciliation until this row is resolved.",
                "",
                "  Next steps, in order:",
                "   1. Check the order in the Kraken web UI (Orders + Trades).",
                "   2. If it exists and is resting, cancel it:",
                "        python -m scout.live.kraken_pilot cancel "
                f"--decision-id {client_order_id}",
                "   3. If it filled, you hold a position — close it manually,",
                "      then update the row's status by hand with the outcome.",
                f"   4. Evidence: {evidence.path}",
            ],
        )
        return EXIT_ESCALATE, None

    async def _await_and_reconcile(
        self,
        *,
        evidence: EvidenceLog,
        decision_id: str,
        client_order_id: str,
        live_trade_id: int | None,
        txid: str | None,
        meta: Any,
        side: str,
        notional: Decimal,
        volume: Decimal,
        balance_before: Decimal | None,
        balance_asset: str,
    ) -> int:
        """Step 13a — wait for the fill, persist it, then reconcile."""
        timeout_sec = float(self._settings.KRAKEN_PILOT_FILL_TIMEOUT_SEC)
        # Say so before going quiet. Without this the operator watches a
        # blank terminal for up to two minutes immediately after authorizing
        # a live order, which is the exact moment to wonder whether it hung.
        print(
            f"order submitted; waiting for fill, up to {timeout_sec:g}s "
            f"(polling every {self._settings.KRAKEN_FILL_POLL_INTERVAL_SEC:g}s) ..."
        )
        conf = await self._adapter.await_fill_confirmation(
            venue_order_id=txid or "",
            client_order_id=client_order_id,
            timeout_sec=timeout_sec,
        )
        evidence.record(
            "fill_confirmation",
            status=conf.status,
            txid=conf.venue_order_id,
            filled_qty=conf.filled_qty,
            fill_price=conf.fill_price,
        )

        fills: list[dict[str, Any]] = []
        if conf.status in ("filled", "partial") and (txid or conf.venue_order_id):
            fills = await self._adapter.fetch_order_fills(
                txid=str(txid or conf.venue_order_id)
            )
            evidence.record("fills", fill_count=len(fills), fills=fills)
            if live_trade_id is not None:
                await update_live_trade(
                    self._db,
                    live_trade_id,
                    entry_fill_price=_fmt(_dec_or_none(conf.fill_price)),
                    entry_fill_qty=_fmt(_dec_or_none(conf.filled_qty)),
                )
        elif conf.status == "rejected":
            # The venue is telling us this order is terminal with nothing
            # executed — no position exists.
            if live_trade_id is not None:
                await update_live_trade(
                    self._db,
                    live_trade_id,
                    status="rejected",
                    closed_at=datetime.now(timezone.utc).isoformat(),
                )
            evidence.record(
                "ledger_updated",
                live_trade_id=live_trade_id,
                ledger_status="rejected",
                note="venue reported a terminal zero-fill order",
            )
        elif conf.status == "timeout":
            # Expected and FINE for a limit order: it is still resting. The
            # row stays 'open' because that is exactly what is true.
            evidence.record(
                "order_resting",
                live_trade_id=live_trade_id,
                txid=txid,
                timeout_sec=float(self._settings.KRAKEN_PILOT_FILL_TIMEOUT_SEC),
                note="limit order did not reach a terminal state inside the "
                "window; it is still working. Use `status` to watch it and "
                "`cancel --decision-id` to pull it.",
            )

        summary = await self._reconcile(
            evidence=evidence,
            live_trade_id=live_trade_id,
            client_order_id=client_order_id,
            conf=conf,
            fills=fills,
            side=side,
            notional=notional,
            volume=volume,
            balance_before=balance_before,
            balance_asset=balance_asset,
        )

        lines = [
            f"  decision ID  : {decision_id}",
            f"  txid         : {txid or conf.venue_order_id}",
            f"  venue status : {conf.status}",
            f"  filled       : {conf.filled_qty} @ {conf.fill_price}",
            f"  fills        : {len(fills)}",
            f"  reconcile    : {summary['verdict']}",
            f"  evidence     : {evidence.path}",
        ]
        if summary["verdict"] == "review":
            lines.extend(
                ["", "  RECONCILIATION COULD NOT EXPLAIN THE BALANCE MOVE:"]
                + [f"   - {m}" for m in summary["mismatches"]]
                + ["  Check the account before running the pilot again."]
            )
        _print_block(f"PILOT ORDER {conf.status.upper()}", lines)
        # An unexplained balance move exits non-zero even though the order
        # itself completed: money moved in a way the runner cannot account
        # for, and a 0 here would tell a wrapper script everything is fine.
        # A resting limit order is NOT this — that is a normal outcome and
        # keeps EXIT_OK.
        return EXIT_REVIEW if summary["verdict"] == "review" else EXIT_OK

    async def _reconcile(
        self,
        *,
        evidence: EvidenceLog,
        live_trade_id: int | None,
        client_order_id: str,
        conf: Any,
        fills: list[dict[str, Any]],
        side: str,
        notional: Decimal,
        volume: Decimal,
        balance_before: Decimal | None,
        balance_asset: str,
    ) -> dict[str, Any]:
        """Compare the venue's view, the ledger row and the balance delta.

        The sampled balance is the FUNDING side — quote on a buy, base on a
        sell — and ``fetch_account_balance`` reports what is AVAILABLE, i.e.
        net of open-order holds. So the expected move is negative in every
        non-terminal case and differs only in magnitude: a filled buy spends
        cost+fee, a resting buy has the notional held, a filled sell gives up
        the executed base quantity, a resting sell has the volume held, and a
        terminal zero-fill order moves nothing.

        A deviation beyond the fee-headroom band comes back as ``review``.
        The runner never silently accepts a mismatch it cannot explain.
        """
        balance_after = _dec(
            await self._adapter.fetch_account_balance(asset=balance_asset)
        )
        ledger = (
            None
            if live_trade_id is None
            else await fetch_row_by_decision_id(self._db, client_order_id)
        )

        fee_total = Decimal("0")
        cost_total = Decimal("0")
        for fill in fills:
            fee_total += _dec_or_none(fill.get("fee")) or Decimal("0")
            cost_total += _dec_or_none(fill.get("cost")) or Decimal("0")
        filled_qty = _dec_or_none(conf.filled_qty)

        # Scale of the funding asset: quote units on a buy, base units on a
        # sell. Every threshold below is expressed in the sampled asset.
        full_size = notional if side == "buy" else volume
        if conf.status == "rejected":
            magnitude = Decimal("0")
        elif conf.status in ("filled", "partial"):
            if side == "buy":
                magnitude = cost_total + fee_total if cost_total > 0 else notional
            else:
                magnitude = filled_qty if filled_qty is not None else volume
        else:  # still resting — the funding asset is held, not yet spent
            magnitude = full_size
        expected_delta = -magnitude
        observed_delta = (
            None if balance_before is None else balance_after - balance_before
        )
        headroom_pct = _dec(self._settings.KRAKEN_PILOT_FEE_HEADROOM_PCT)
        # Relative, never a fixed absolute: the sampled asset is base units on
        # a sell, where a flat "$0.01" floor would be an enormous band.
        tolerance = full_size * headroom_pct / Decimal("100")
        if tolerance <= 0:
            tolerance = full_size * Decimal("0.0001")

        mismatches: list[str] = []
        if observed_delta is None:
            mismatches.append("no pre-trade balance sample to compare against")
        elif abs(observed_delta - expected_delta) > tolerance:
            mismatches.append(
                f"balance delta {_fmt(observed_delta)} differs from expected "
                f"{_fmt(expected_delta)} by more than {_fmt(tolerance)}"
            )
        if live_trade_id is not None and ledger is None:
            mismatches.append("ledger row could not be re-read after the trade")
        if (
            ledger is not None
            and conf.venue_order_id
            and ledger.get("entry_order_id") != conf.venue_order_id
        ):
            mismatches.append(
                f"ledger entry_order_id {ledger.get('entry_order_id')!r} does not "
                f"match venue txid {conf.venue_order_id!r}"
            )

        # The account's resting orders AFTER the trade, from the venue rather
        # than from our ledger. This is what makes "one order at a time"
        # checkable from the evidence pack alone: a reader can see the account
        # held exactly the orders this run accounts for, without trusting the
        # same ledger the run was writing.
        try:
            venue_open_orders: Any = await self._adapter.fetch_open_orders()
        except Exception as exc:
            venue_open_orders = {
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            mismatches.append(
                f"could not read the venue's open orders after the trade "
                f"({type(exc).__name__})"
            )

        summary = {
            "verdict": "review" if mismatches else "pass",
            "mismatches": mismatches,
            "venue": {
                "status": conf.status,
                "txid": conf.venue_order_id,
                "filled_qty": conf.filled_qty,
                "fill_price": conf.fill_price,
                "fill_count": len(fills),
                "fees_paid": _fmt(fee_total),
                "cost": _fmt(cost_total),
                "open_orders_after": venue_open_orders,
            },
            "ledger": ledger,
            "balance": {
                "asset": balance_asset,
                "before": _fmt(balance_before),
                "after": _fmt(balance_after),
                "observed_delta": _fmt(observed_delta),
                "expected_delta": _fmt(expected_delta),
                "tolerance": _fmt(tolerance),
            },
        }
        evidence.record("reconciliation", **summary)
        if mismatches:
            log.warning("kraken_pilot_reconciliation_review", mismatches=mismatches)
        return summary

    # ---------------- exit ----------------
    def _evidence_for_exit(self, live_trade_id: int, decision_id: str) -> EvidenceLog:
        """Evidence file for one exit attempt.

        The ROW ID leads the filename so every attempt against a position is
        one glob away (``kraken_pilot_exit_7_*.json``). After a crash the
        operator knows the row id — it is what they typed — and not the
        decision id, which was minted inside the run that died.
        """
        directory = Path(self._settings.KRAKEN_PILOT_EVIDENCE_DIR)
        return EvidenceLog(
            directory / f"kraken_pilot_exit_{live_trade_id}_{decision_id}.json"
        )

    def _check_exit_envelope(self, *, validate_only: bool) -> dict[str, Any]:
        """Narrower than ``place``'s gate — see the module docstring.

        Credentials are required (nothing can be signed without them) and
        ``LIVE_USE_REAL_SIGNED_REQUESTS`` is required for a real order, because
        ``place_limit_order`` raises under it regardless and a refusal is
        clearer before the approval prompt than after.

        ``KRAKEN_PILOT_ENABLED`` and ``KRAKEN_PILOT_PAIR`` are NOT checked. The
        master gate exists to stop new exposure and must not be able to strand
        an open position, and the approved-pair setting is irrelevant here —
        the pair comes from the ledger row, which is the pair the account
        actually holds.
        """
        self._check_credentials()
        if not validate_only and not self._settings.LIVE_USE_REAL_SIGNED_REQUESTS:
            raise PilotAbort(
                "envelope_gate",
                "LIVE_USE_REAL_SIGNED_REQUESTS is False — the emergency-revert "
                "posture blocks every real order, including this exit. Use "
                "--validate-only to rehearse, or flip the flag for the "
                "supervised session.",
            )
        return {
            "credentials_present": True,
            "validate_only": validate_only,
            "pilot_enabled": self._settings.KRAKEN_PILOT_ENABLED,
            "note": "exit is not gated on KRAKEN_PILOT_ENABLED — it reduces "
            "exposure, so the master gate must not be able to strand a position",
        }

    async def _select_exit_row(self, live_trade_id: int) -> dict[str, Any]:
        """Step 1 — load the row and prove it is a closeable kraken position."""
        row = await fetch_live_trade_row(self._db, live_trade_id)
        if row is None:
            raise PilotAbort(
                "row_selection",
                f"no live_trades row with id={live_trade_id}",
            )
        if row["venue"] != VENUE:
            raise PilotAbort(
                "row_selection",
                f"live_trades #{live_trade_id} is a {row['venue']!r} row, not "
                f"{VENUE!r} — this command only closes kraken positions",
            )
        if row["status"] != "open":
            raise PilotAbort(
                "row_selection",
                f"live_trades #{live_trade_id} status is {row['status']!r}, not "
                "'open'. Only an open position can be closed; a "
                "needs_manual_review row must be resolved by hand first, and a "
                "terminal row is already closed.",
            )
        entry_price = _dec_or_none(row["entry_fill_price"])
        held_qty = _dec_or_none(row["entry_fill_qty"])
        if entry_price is None or held_qty is None:
            raise PilotAbort(
                "row_selection",
                f"live_trades #{live_trade_id} has no recorded entry fill "
                f"(entry_fill_price={row['entry_fill_price']!r}, "
                f"entry_fill_qty={row['entry_fill_qty']!r}). Without both there "
                "is no proven position to sell and no basis to price it — "
                "confirm the entry at the venue and record it before exiting.",
            )
        if entry_price <= 0 or held_qty <= 0:
            raise PilotAbort(
                "row_selection",
                f"live_trades #{live_trade_id} records a non-positive entry fill "
                f"(price={_fmt(entry_price)} qty={_fmt(held_qty)}) — there is "
                "nothing to sell",
            )
        if not row["pair"]:
            raise PilotAbort(
                "row_selection",
                f"live_trades #{live_trade_id} has no pair recorded",
            )

        # Every OTHER non-terminal kraken row. This row being open is the
        # precondition, not a blocker — but a second in-flight row means the
        # lane's one-order-at-a-time invariant is already broken, and adding a
        # sell to that is not the moment to find out what the other row is.
        others = [
            other
            for other in await fetch_blocking_rows(self._db)
            if other["live_trade_id"] != live_trade_id
        ]
        if others:
            raise PilotAbort(
                "lane_state",
                f"{len(others)} other non-terminal kraken ledger row(s) are "
                f"in flight ({', '.join(str(o['live_trade_id']) for o in others)})"
                " — resolve them before closing this position",
            )
        return {
            "row": row,
            "entry_price": entry_price,
            "held_qty": held_qty,
            "other_blocking_rows": others,
        }

    async def _check_exit_venue_state(
        self, *, pair: str, base_asset: str, volume: Decimal, client_order_id: str
    ) -> dict[str, Any]:
        """Step 2 — the account holds the coins and nothing is already resting.

        Both halves fail CLOSED. An unreadable ``OpenOrders`` is a refusal
        rather than an empty list, for the same reason it blocks ``place``:
        "there is no order resting" is the fact that licenses placing one.
        """
        available = _dec(await self._adapter.fetch_account_balance(asset=base_asset))
        if available < volume:
            raise PilotAbort(
                "venue_balance",
                f"available {base_asset} balance {_fmt(available)} does not cover "
                f"the {_fmt(volume)} this row says is held. The ledger and the "
                "account disagree about the position — confirm at the venue "
                "before selling anything.",
            )

        try:
            open_orders = await self._adapter.fetch_open_orders()
        except Exception as exc:
            raise PilotAbort(
                "venue_open_orders",
                f"could not read the account's resting orders "
                f"({type(exc).__name__}: {exc}) — refusing to treat an unreadable "
                "listing as an empty one",
            ) from exc

        conflicts = [
            order
            for order in open_orders
            if order.get("client_order_id") == client_order_id
            or str(order.get("pair") or "").upper() == pair.upper()
        ]
        if conflicts:
            raise PilotAbort(
                "venue_open_orders",
                f"{len(conflicts)} order(s) already rest for {pair} or for exit "
                f"client id {client_order_id} "
                f"({', '.join(str(o.get('txid')) for o in conflicts)}). An exit "
                "for this position may already be working — never place a "
                "second one. Check the venue, then cancel or let it fill.",
            )
        return {
            "asset": base_asset,
            "available": _fmt(available),
            "required": _fmt(volume),
            "venue_open_count": len(open_orders),
            "venue_open_orders": open_orders,
        }

    async def _exit_fee_tier(
        self, pair: str
    ) -> tuple[Decimal, Decimal, dict[str, Any]]:
        """Step 3 — THIS account's maker/taker rates, or refuse.

        No default and no fallback to the public AssetPairs schedule. Every
        number on the approval screen is a number the operator is being asked
        to accept; a guessed fee rate is worse than no screen at all, because
        it is wrong in a way that looks authoritative.
        """
        try:
            tier = await self._adapter.fetch_fee_tier(pair=pair)
        except Exception as exc:
            raise PilotAbort(
                "fee_tier",
                f"could not read this account's fee tier for {pair} "
                f"({type(exc).__name__}: {exc}). Refusing rather than guessing — "
                "an authorization screen carrying a made-up fee is worse than no "
                "screen.",
            ) from exc
        maker_pct = _dec_or_none(tier.get("maker_pct"))
        taker_pct = _dec_or_none(tier.get("taker_pct"))
        if maker_pct is None or taker_pct is None:
            raise PilotAbort(
                "fee_tier",
                f"TradeVolume returned no usable rates for {pair} "
                f"(maker={tier.get('maker_pct')!r} taker={tier.get('taker_pct')!r})",
            )
        return maker_pct, taker_pct, tier

    async def _entry_fee(
        self, *, row: dict[str, Any], entry_notional: Decimal, taker_pct: Decimal
    ) -> tuple[Decimal, str]:
        """What the ENTRY cost in fees — measured if possible, derived if not.

        ``live_trades`` has no fee column, so the fee is recovered from the
        entry order's own fills when the txid is on the row. That is a fact
        rather than an estimate, and it matters: the pilot's first live trade
        paid roughly double the headline rate, which a derived figure would
        have hidden.

        The fallback derives it from the row at the TAKER rate — the higher of
        the two — so an unmeasurable entry fee understates P&L rather than
        overstating it.
        """
        txid = row.get("entry_order_id")
        if txid:
            try:
                fills = await self._adapter.fetch_order_fills(txid=str(txid))
            except Exception as exc:
                log.warning(
                    "kraken_pilot_entry_fee_unreadable",
                    live_trade_id=row.get("live_trade_id"),
                    entry_order_id=txid,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            else:
                parsed = [_dec_or_none(fill.get("fee")) for fill in fills]
                if parsed and any(value is not None for value in parsed):
                    return (
                        sum(
                            (value for value in parsed if value is not None),
                            Decimal("0"),
                        ),
                        "actual_entry_fills",
                    )
        return entry_notional * taker_pct / Decimal("100"), "estimated_at_taker_rate"

    async def exit_position(
        self,
        *,
        live_trade_id: int,
        price: Decimal,
        validate_only: bool = False,
    ) -> int:
        """Close ONE open kraken ``live_trades`` row with a supervised sell."""
        decision_id = str(uuid4())
        client_order_id = make_exit_client_order_id(live_trade_id)
        evidence = self._evidence_for_exit(live_trade_id, decision_id)
        evidence.record(
            "run_started",
            command="exit",
            decision_id=decision_id,
            live_trade_id=live_trade_id,
            client_order_id=client_order_id,
            price=_fmt(price),
            validate_only=validate_only,
            evidence_path=str(evidence.path),
            db_path=str(Path(self._settings.DB_PATH).resolve()),
        )

        try:
            evidence.record(
                "envelope_gate",
                **self._check_exit_envelope(validate_only=validate_only),
            )

            # Observed, never gating — an exit reduces exposure (module
            # docstring). Recorded so the evidence pack shows the kill state
            # the sale was made under.
            kill_state = await self._ks.is_active()
            evidence.record(
                "kill_switch_observed",
                kill_active=kill_state is not None,
                kill_event_id=None if kill_state is None else kill_state.kill_event_id,
                note="exit is not gated on the kill switch — it reduces exposure",
            )

            selection = await self._select_exit_row(live_trade_id)
            row = selection["row"]
            entry_price: Decimal = selection["entry_price"]
            held_qty: Decimal = selection["held_qty"]
            pair = str(row["pair"])
            base_asset = str(row["symbol"] or "").upper()
            evidence.record(
                "row_selected",
                live_trade_id=live_trade_id,
                status=row["status"],
                venue=row["venue"],
                pair=pair,
                symbol=base_asset,
                entry_client_order_id=row["client_order_id"],
                entry_order_id=row["entry_order_id"],
                entry_fill_price=_fmt(entry_price),
                entry_fill_qty=_fmt(held_qty),
                exit_client_order_id=client_order_id,
                other_blocking_rows=selection["other_blocking_rows"],
            )

            venue_state = await self._check_exit_venue_state(
                pair=pair,
                base_asset=base_asset,
                volume=held_qty,
                client_order_id=client_order_id,
            )
            evidence.record("venue_state", **venue_state)
            balance_before = _dec_or_none(venue_state["available"])

            maker_pct, taker_pct, tier = await self._exit_fee_tier(pair)
            evidence.record(
                "fee_tier",
                pair=pair,
                maker_pct=_fmt(maker_pct),
                taker_pct=_fmt(taker_pct),
                volume=tier.get("volume"),
                currency=tier.get("currency"),
                source="/0/private/TradeVolume (this account's tier)",
            )

            entry_notional = entry_price * held_qty
            entry_fee, entry_fee_source = await self._entry_fee(
                row=row, entry_notional=entry_notional, taker_pct=taker_pct
            )
            scenarios = {
                "maker": exit_economics(
                    entry_price=entry_price,
                    held_qty=held_qty,
                    exit_price=price,
                    entry_fee=entry_fee,
                    fee_pct=maker_pct,
                ),
                "taker": exit_economics(
                    entry_price=entry_price,
                    held_qty=held_qty,
                    exit_price=price,
                    entry_fee=entry_fee,
                    fee_pct=taker_pct,
                ),
            }
            # Best effort: a public Ticker outage must not stop a position
            # being closed, so the mid decorates the screen rather than gating
            # it. The taker warning is printed either way.
            mid: Decimal | None = None
            try:
                mid = await self._adapter.fetch_price(pair)
            except Exception as exc:
                log.warning(
                    "kraken_pilot_exit_mid_unavailable",
                    pair=pair,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            evidence.record(
                "cost_scenarios",
                entry_notional=_fmt(entry_notional),
                entry_fee=_fmt(entry_fee),
                entry_fee_source=entry_fee_source,
                mid=_fmt(mid),
                crosses_book=None if mid is None else price < mid,
                maker=scenarios["maker"],
                taker=scenarios["taker"],
            )

            token = exit_authorization_token(
                pair=pair,
                side="sell",
                volume=held_qty,
                price=price,
                client_order_id=client_order_id,
            )
            authorized, outcome = self._request_exit_authorization(
                live_trade_id=live_trade_id,
                decision_id=decision_id,
                client_order_id=client_order_id,
                pair=pair,
                base_asset=base_asset,
                entry_price=entry_price,
                held_qty=held_qty,
                price=price,
                entry_notional=entry_notional,
                entry_fee=entry_fee,
                entry_fee_source=entry_fee_source,
                maker_pct=maker_pct,
                taker_pct=taker_pct,
                scenarios=scenarios,
                mid=mid,
                venue_state=venue_state,
                token=token,
                validate_only=validate_only,
            )
            evidence.record(
                "authorization",
                outcome="authorized" if authorized else "authorization_refused",
                detail=outcome,
                bound_to=["pair", "side", "volume", "price", "client_order_id"],
            )
            if not authorized:
                raise PilotAbort(
                    "authorization",
                    f"operator did not authorize ({outcome}) — nothing was sent",
                )

            # Durable intent, immediately before the submission. Fsynced by
            # EvidenceLog.record, so a crash in the next few milliseconds
            # leaves a record naming exactly what was about to be sent — and
            # the client order id in it is deterministic, so the recovery run
            # finds the order if it landed instead of selling twice.
            evidence.record(
                "exit_intent_persisted",
                decision_id=decision_id,
                live_trade_id=live_trade_id,
                pair=pair,
                side="sell",
                volume=_fmt(held_qty),
                price=_fmt(price),
                client_order_id=client_order_id,
                validate_only=validate_only,
                note="written and fsynced BEFORE submission; if the run dies "
                "after this record the order may exist — look it up by this "
                "client order id, never resend",
            )

            return await self._submit_exit(
                evidence=evidence,
                decision_id=decision_id,
                live_trade_id=live_trade_id,
                client_order_id=client_order_id,
                pair=pair,
                base_asset=base_asset,
                entry_price=entry_price,
                entry_fee=entry_fee,
                entry_fee_source=entry_fee_source,
                held_qty=held_qty,
                price=price,
                balance_before=balance_before,
                validate_only=validate_only,
            )

        except PilotAbort as abort:
            evidence.record(
                "aborted",
                stage=abort.stage,
                reason=abort.reason,
                exit_code=abort.exit_code,
            )
            print(f"REFUSED [{abort.stage}]: {abort.reason}")
            print(f"evidence: {evidence.path}")
            return abort.exit_code
        except Exception as exc:
            log.error(
                "kraken_pilot_unexpected_error",
                command="exit",
                decision_id=decision_id,
                live_trade_id=live_trade_id,
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=True,
            )
            evidence.record(
                "unexpected_error",
                error_type=type(exc).__name__,
                error=str(exc),
                live_trade_id=live_trade_id,
            )
            _print_block(
                "ESCALATE — UNEXPECTED FAILURE DURING EXIT",
                [
                    f"  {type(exc).__name__}: {exc}",
                    f"  decision ID     : {decision_id}",
                    f"  live_trades row : {live_trade_id}",
                    f"  exit client id  : {client_order_id}",
                    "  A sell order may exist. Confirm the venue state before",
                    "  any further action, and NEVER resend.",
                    f"  evidence        : {evidence.path}",
                ],
            )
            return EXIT_ESCALATE

    def _request_exit_authorization(
        self,
        *,
        live_trade_id: int,
        decision_id: str,
        client_order_id: str,
        pair: str,
        base_asset: str,
        entry_price: Decimal,
        held_qty: Decimal,
        price: Decimal,
        entry_notional: Decimal,
        entry_fee: Decimal,
        entry_fee_source: str,
        maker_pct: Decimal,
        taker_pct: Decimal,
        scenarios: dict[str, dict[str, Any]],
        mid: Decimal | None,
        venue_state: dict[str, Any],
        token: str,
        validate_only: bool,
    ) -> tuple[bool, str]:
        """Step 5 — print BOTH cost scenarios, then require the bound token.

        Both rates are shown in full because a limit order does not choose
        which one it pays: it pays maker if it rests and taker if it crosses,
        and which of those happens is decided by the book at the instant the
        order arrives, not by the order type. Showing one number would be
        showing the operator a fee they may not be charged.

        ASCII operators only (``->``), same reason as ``place``'s block: this
        is read on whatever console the operator has.
        """
        entry_fee_label = (
            "ACTUAL, from the entry order's fills"
            if entry_fee_source == "actual_entry_fills"
            else "ESTIMATE at the taker rate - the entry fills could not be read"
        )
        lines = [
            f"  position            : live_trades #{live_trade_id} "
            f"({base_asset} on {pair})",
            "  side                : SELL (closing the position)",
            f"  quantity            : {_fmt(held_qty)} {base_asset} "
            f"(entry_fill_qty - the whole recorded position)",
            f"  entry fill price    : {_fmt(entry_price)}",
            f"  entry notional      : {_fmt(entry_notional)}",
            f"  entry fee           : {_fmt(entry_fee)} ({entry_fee_label})",
            f"  limit price         : {_fmt(price)}",
            f"  exit notional       : {scenarios['maker']['exit_notional']} "
            f"(price x quantity)",
            "  current mid         : "
            + (_fmt(mid) or "" if mid is not None else "unavailable"),
            f"  account fee tier    : maker {_fmt(maker_pct)}% / "
            f"taker {_fmt(taker_pct)}% (this account, from TradeVolume)",
            f"  decision ID         : {decision_id}",
            f"  exit client order ID: {client_order_id}",
            f"  database            : {Path(self._settings.DB_PATH).resolve()}",
            f"  available balance   : {venue_state['available']} "
            f"{venue_state['asset']}",
            f"  open kraken orders  : {venue_state['venue_open_count']} "
            f"(from the venue's own OpenOrders listing)",
        ]
        headings = {
            "maker": f"IF THIS FILLS AS MAKER (the limit rests and does not cross "
            f"the book, {_fmt(maker_pct)}%):",
            "taker": f"IF THIS FILLS AS TAKER (the limit crosses the book, "
            f"{_fmt(taker_pct)}%):",
        }
        for assumption in ("maker", "taker"):
            scenario = scenarios[assumption]
            lines.extend(
                [
                    "",
                    f"  {headings[assumption]}",
                    f"    exit fee          : {scenario['exit_fee']} "
                    f"(exit notional x {scenario['fee_pct']}%)",
                    f"    round-trip fee    : {scenario['round_trip_fee']} "
                    f"(entry {_fmt(entry_fee)} + this exit fee)",
                    f"    break-even price  : {scenario['break_even_price']} "
                    f"(the exit price at which realised P&L is zero, "
                    f"{assumption} assumption)",
                    f"    realised P&L      : {scenario['realized_pnl_usd']} "
                    f"({scenario['realized_pnl_pct']}%) at the "
                    f"{_fmt(price)} limit, {assumption} assumption",
                ]
            )
        lines.extend(
            [
                "",
                "  ** WARNING          : a LIMIT order is not a MAKER guarantee. "
                "If this",
                "                        price crosses the book when it reaches "
                "the matching",
                "                        engine it executes immediately as TAKER, "
                "at the taker",
                "                        figures above.",
            ]
        )
        if mid is not None and price < mid:
            lines.append(
                f"  ** WARNING          : limit {_fmt(price)} is BELOW the mid "
                f"{_fmt(mid)} — this sell is marketable and will very likely pay "
                "the TAKER rate"
            )
        if validate_only:
            lines.append(
                "  ** REHEARSAL        : validate=true — Kraken will parse and "
                "check this order but NOT place it"
            )
        _print_block("KRAKEN SUPERVISED PILOT — EXIT APPROVAL REQUIRED", lines)
        print(
            f"Type this exact token to authorize:  {token}\n"
            "It is derived from the pair, side, quantity, limit price and client "
            "order id.\n"
            "Change any one of them and this token no longer authorizes the "
            "order. Anything\n"
            "else — including an empty line or a closed stdin — aborts."
        )
        return read_authorization(token)

    async def _submit_exit(
        self,
        *,
        evidence: EvidenceLog,
        decision_id: str,
        live_trade_id: int,
        client_order_id: str,
        pair: str,
        base_asset: str,
        entry_price: Decimal,
        entry_fee: Decimal,
        entry_fee_source: str,
        held_qty: Decimal,
        price: Decimal,
        balance_before: Decimal | None,
        validate_only: bool,
    ) -> int:
        """Steps 7-8 — submit exactly once, then decide what came back."""
        txid: str | None = None
        try:
            result = await self._adapter.place_limit_order(
                pair=pair,
                side="sell",
                price=price,
                volume=held_qty,
                client_order_id=client_order_id,
                validate_only=validate_only,
            )
        except KrakenAmbiguousSubmissionError as exc:
            evidence.record(
                "submission_ambiguous",
                error_type=type(exc).__name__,
                error=str(exc),
                note="NEVER resend — resolving by client id instead",
            )
            code, txid = await self._resolve_exit_ambiguity(
                evidence=evidence,
                live_trade_id=live_trade_id,
                client_order_id=client_order_id,
                pair=pair,
            )
            if code is not None:
                return code
        except Exception as exc:
            # Definitive refusal. Nothing was placed, so the position is
            # untouched and the row stays exactly as it was — writing
            # 'rejected' here would retire a row that still holds coins.
            evidence.record(
                "submission_refused",
                error_type=type(exc).__name__,
                error=str(exc),
                live_trade_id=live_trade_id,
                ledger_status="open",
                note="no order was created; the position is still held and the "
                "row is unchanged",
            )
            print(f"REFUSED [submit]: {type(exc).__name__}: {exc}")
            print(f"  live_trades #{live_trade_id} is unchanged — you still hold ")
            print(f"  {_fmt(held_qty)} {base_asset}.")
            print(f"evidence: {evidence.path}")
            return EXIT_REFUSED
        else:
            if validate_only:
                evidence.record(
                    "validate_only_accepted",
                    descr=result.get("descr"),
                    price_sent=result.get("price"),
                    volume_sent=result.get("volume"),
                    note="Kraken validated the exit order and placed nothing; the "
                    "ledger row is untouched by design",
                )
                _print_block(
                    "EXIT REHEARSAL COMPLETE — no order was placed",
                    [
                        f"  live_trades row : {live_trade_id} (unchanged, still open)",
                        f"  decision ID     : {decision_id}",
                        f"  descr           : {result.get('descr')}",
                        f"  evidence        : {evidence.path}",
                    ],
                )
                return EXIT_OK
            txids = result.get("txid") or []
            txid = str(txids[0]) if txids else None
            evidence.record(
                "submitted",
                txid=txid,
                all_txids=txids,
                descr=result.get("descr"),
                price_sent=result.get("price"),
                volume_sent=result.get("volume"),
            )

        if txid:
            # Persisted the instant it is known: from here the ledger names the
            # venue order even if everything after this line fails.
            await update_live_trade(self._db, live_trade_id, exit_order_id=txid)
            evidence.record(
                "exit_txid_persisted", live_trade_id=live_trade_id, txid=txid
            )

        return await self._await_and_close(
            evidence=evidence,
            decision_id=decision_id,
            live_trade_id=live_trade_id,
            client_order_id=client_order_id,
            txid=txid,
            pair=pair,
            base_asset=base_asset,
            entry_price=entry_price,
            entry_fee=entry_fee,
            entry_fee_source=entry_fee_source,
            held_qty=held_qty,
            price=price,
            balance_before=balance_before,
        )

    async def _resolve_exit_ambiguity(
        self,
        *,
        evidence: EvidenceLog,
        live_trade_id: int,
        client_order_id: str,
        pair: str,
    ) -> tuple[int | None, str | None]:
        """Turn an ambiguous exit submission into a decided state. Never resends.

        Returns ``(exit_code, txid)`` — a code when the run is over, or
        ``(None, txid)`` when the order was adopted and the caller continues
        into fill confirmation.
        """
        detail = await self._adapter.resolve_order_submission_detail(
            client_order_id=client_order_id
        )
        verdict = detail.get("verdict")
        evidence.record("ambiguity_resolution", **detail)

        if verdict == "accepted":
            txid = None
            conf = await self._adapter.fetch_order_by_client_id(
                pair=pair, client_order_id=client_order_id
            )
            if conf is not None and conf.venue_order_id:
                txid = str(conf.venue_order_id)
            elif detail.get("txid"):
                txid = str(detail["txid"][0])
            evidence.record(
                "ambiguity_adopted",
                txid=txid,
                venue_status=None if conf is None else conf.status,
                note="the sell DID land — adopting it rather than resending",
            )
            return None, txid

        if verdict == "not_accepted":
            evidence.record(
                "ambiguity_not_accepted",
                live_trade_id=live_trade_id,
                ledger_status="open",
                note="no sell order exists; the position is still held and the "
                "row is unchanged",
            )
            _print_block(
                "EXIT DID NOT LAND — no order exists",
                [
                    "  Two clean sweeps of OpenOrders + ClosedOrders found no",
                    "  order carrying this exit client id. The position is still",
                    f"  held and live_trades #{live_trade_id} is unchanged.",
                    f"  evidence: {evidence.path}",
                ],
            )
            return EXIT_REFUSED, None

        # unresolved — STOP. Never resend a sell that may already be working.
        await update_live_trade(self._db, live_trade_id, status="needs_manual_review")
        evidence.record(
            "ambiguity_unresolved",
            live_trade_id=live_trade_id,
            ledger_status="needs_manual_review",
        )
        _print_block(
            "ESCALATE — EXIT SUBMISSION UNRESOLVED, DO NOT RESEND",
            [
                "  Kraken could not confirm whether this sell order exists.",
                f"  exit client id  : {client_order_id}",
                f"  live_trades row : {live_trade_id} (needs_manual_review)",
                "",
                "  Next steps, in order:",
                "   1. Check the order in the Kraken web UI (Orders + Trades).",
                "   2. If it is resting and you do not want it, cancel it there.",
                "   3. If it filled, record the outcome on the row by hand.",
                f"   4. Evidence: {evidence.path}",
            ],
        )
        return EXIT_ESCALATE, None

    async def _await_and_close(
        self,
        *,
        evidence: EvidenceLog,
        decision_id: str,
        live_trade_id: int,
        client_order_id: str,
        txid: str | None,
        pair: str,
        base_asset: str,
        entry_price: Decimal,
        entry_fee: Decimal,
        entry_fee_source: str,
        held_qty: Decimal,
        price: Decimal,
        balance_before: Decimal | None,
    ) -> int:
        """Steps 9-10 — wait for the fill, then partial / close / review."""
        timeout_sec = float(self._settings.KRAKEN_PILOT_FILL_TIMEOUT_SEC)
        print(
            f"exit order submitted; waiting for fill, up to {timeout_sec:g}s "
            f"(polling every {self._settings.KRAKEN_FILL_POLL_INTERVAL_SEC:g}s) ..."
        )
        conf = await self._adapter.await_fill_confirmation(
            venue_order_id=txid or "",
            client_order_id=client_order_id,
            timeout_sec=timeout_sec,
        )
        evidence.record(
            "fill_confirmation",
            status=conf.status,
            txid=conf.venue_order_id,
            filled_qty=conf.filled_qty,
            fill_price=conf.fill_price,
        )
        lookup_txid = str(txid or conf.venue_order_id or "")

        if conf.status == "timeout":
            # Normal for a limit order: it is still working. The row stays
            # open, which is exactly what is true — the coins are still ours
            # until it fills.
            evidence.record(
                "exit_order_resting",
                live_trade_id=live_trade_id,
                txid=lookup_txid or None,
                timeout_sec=timeout_sec,
            )
            _print_block(
                "EXIT ORDER STILL RESTING",
                [
                    f"  live_trades row : {live_trade_id} (still open — the "
                    "position is unsold)",
                    f"  exit txid       : {lookup_txid or '(unknown)'}",
                    f"  exit client id  : {client_order_id}",
                    f"  limit price     : {_fmt(price)}",
                    "",
                    "  The order did not reach a terminal state inside the window.",
                    "  It is working. Re-run this command later to reconcile it, or",
                    "  cancel it in the Kraken web UI — `kraken_pilot cancel` keys",
                    "  off the ENTRY client id and cannot pull an exit order.",
                    f"  evidence        : {evidence.path}",
                ],
            )
            return EXIT_OK

        if conf.status == "rejected":
            evidence.record(
                "exit_order_terminal_zero_fill",
                live_trade_id=live_trade_id,
                txid=lookup_txid or None,
                ledger_status="open",
                note="the venue reported a terminal order with nothing executed; "
                "the position is still held",
            )
            _print_block(
                "EXIT ORDER ENDED WITHOUT SELLING ANYTHING",
                [
                    f"  live_trades row : {live_trade_id} (unchanged, still open)",
                    f"  exit txid       : {lookup_txid or '(unknown)'}",
                    f"  You still hold {_fmt(held_qty)} {base_asset}.",
                    f"  evidence        : {evidence.path}",
                ],
            )
            return EXIT_REFUSED

        filled_qty = _dec_or_none(conf.filled_qty)
        if filled_qty is None or filled_qty <= 0:
            # The venue called it filled/partial but gave no readable quantity.
            # Nothing may be concluded about the position from that.
            await update_live_trade(
                self._db, live_trade_id, status="needs_manual_review"
            )
            evidence.record(
                "exit_fill_unreadable",
                live_trade_id=live_trade_id,
                ledger_status="needs_manual_review",
                filled_qty=conf.filled_qty,
            )
            _print_block(
                "ESCALATE — EXIT FILL COULD NOT BE READ",
                [
                    f"  The venue reported status {conf.status!r} but no usable",
                    f"  filled quantity ({conf.filled_qty!r}).",
                    f"  live_trades row : {live_trade_id} (needs_manual_review)",
                    f"  exit txid       : {lookup_txid or '(unknown)'}",
                    f"  evidence        : {evidence.path}",
                ],
            )
            return EXIT_ESCALATE

        fills: list[dict[str, Any]] = []
        if lookup_txid:
            fills = await self._adapter.fetch_order_fills(txid=lookup_txid)
        evidence.record("fills", fill_count=len(fills), fills=fills)

        exit_vwap = _dec_or_none(conf.fill_price)
        fill_qty_total = Decimal("0")
        fill_cost_total = Decimal("0")
        exit_fee_total = Decimal("0")
        fee_readable = False
        for fill in fills:
            fill_qty_total += _dec_or_none(fill.get("vol")) or Decimal("0")
            fill_cost_total += _dec_or_none(fill.get("cost")) or Decimal("0")
            fee = _dec_or_none(fill.get("fee"))
            if fee is not None:
                fee_readable = True
                exit_fee_total += fee
        if exit_vwap is None and fill_qty_total > 0:
            exit_vwap = fill_cost_total / fill_qty_total

        # Strict, with no tolerance band. The error directions are not
        # symmetric: treating a partial as complete closes a row while coins
        # remain, and treating a dust remainder as a partial merely leaves an
        # open row for the operator to look at. Only one of those can lose
        # track of a position.
        if filled_qty < held_qty:
            return await self._record_partial_exit(
                evidence=evidence,
                live_trade_id=live_trade_id,
                txid=lookup_txid or None,
                base_asset=base_asset,
                held_qty=held_qty,
                filled_qty=filled_qty,
                exit_vwap=exit_vwap,
                fills=fills,
            )

        return await self._reconcile_and_close(
            evidence=evidence,
            decision_id=decision_id,
            live_trade_id=live_trade_id,
            txid=lookup_txid or None,
            pair=pair,
            base_asset=base_asset,
            entry_price=entry_price,
            entry_fee=entry_fee,
            entry_fee_source=entry_fee_source,
            held_qty=held_qty,
            filled_qty=filled_qty,
            exit_vwap=exit_vwap,
            exit_fee_total=exit_fee_total,
            fee_readable=fee_readable,
            fill_qty_total=fill_qty_total,
            fill_cost_total=fill_cost_total,
            fills=fills,
            balance_before=balance_before,
        )

    async def _record_partial_exit(
        self,
        *,
        evidence: EvidenceLog,
        live_trade_id: int,
        txid: str | None,
        base_asset: str,
        held_qty: Decimal,
        filled_qty: Decimal,
        exit_vwap: Decimal | None,
        fills: list[dict[str, Any]],
    ) -> int:
        """Step 9 — a partial fill leaves a real position. Never close on it.

        ``entry_fill_qty`` is the lane's held-quantity field, so it is reduced
        by what sold: the remainder is what the account still holds and what a
        later ``exit`` run must sell. No P&L is written — the trade is not over,
        and a realised figure on an unfinished position would be read as final.
        """
        remaining = held_qty - filled_qty
        await update_live_trade(
            self._db,
            live_trade_id,
            exit_order_id=txid,
            exit_fill_price=_fmt(exit_vwap),
            entry_fill_qty=_fmt(remaining),
        )
        evidence.record(
            "exit_partial_fill",
            live_trade_id=live_trade_id,
            ledger_status="open",
            txid=txid,
            requested_qty=_fmt(held_qty),
            filled_qty=_fmt(filled_qty),
            remaining_qty=_fmt(remaining),
            exit_fill_price=_fmt(exit_vwap),
            fill_count=len(fills),
            note="POSITION STILL EXISTS — row stays open with the remaining "
            "quantity; no realised P&L is written for an unfinished exit",
        )
        _print_block(
            "EXIT PARTIALLY FILLED — POSITION STILL OPEN",
            [
                f"  live_trades row : {live_trade_id} (still open)",
                f"  exit txid       : {txid or '(unknown)'}",
                f"  sold            : {_fmt(filled_qty)} {base_asset} @ "
                f"{_fmt(exit_vwap)}",
                f"  still held      : {_fmt(remaining)} {base_asset} "
                "(entry_fill_qty has been reduced to this)",
                "",
                "  DECISION REQUIRED. The rest of the order may still be resting.",
                "  Check the venue, then either let it work, cancel it in the",
                "  Kraken web UI, or re-run this command for the remainder once",
                "  nothing is resting for this pair.",
                f"  evidence        : {evidence.path}",
            ],
        )
        return EXIT_REVIEW

    async def _reconcile_and_close(
        self,
        *,
        evidence: EvidenceLog,
        decision_id: str,
        live_trade_id: int,
        txid: str | None,
        pair: str,
        base_asset: str,
        entry_price: Decimal,
        entry_fee: Decimal,
        entry_fee_source: str,
        held_qty: Decimal,
        filled_qty: Decimal,
        exit_vwap: Decimal | None,
        exit_fee_total: Decimal,
        fee_readable: bool,
        fill_qty_total: Decimal,
        fill_cost_total: Decimal,
        fills: list[dict[str, Any]],
        balance_before: Decimal | None,
    ) -> int:
        """Step 10 — reconcile, and close ONLY if the reconciliation confirms.

        Three independent facts have to agree before the row is retired: the
        base balance fell by what sold, the per-fill records account for the
        quantity the order says it filled, and there are per-fill records at
        all. A close asserts the position is gone; asserting that on evidence
        we could not corroborate is how a phantom position gets created, and
        the row is the only durable claim about what the account holds.
        """
        mismatches: list[str] = []
        balance_after: Decimal | None = None
        try:
            balance_after = _dec(
                await self._adapter.fetch_account_balance(asset=base_asset)
            )
        except Exception as exc:
            mismatches.append(
                f"could not re-read the {base_asset} balance after the sale "
                f"({type(exc).__name__})"
            )

        headroom_pct = _dec(self._settings.KRAKEN_PILOT_FEE_HEADROOM_PCT)
        tolerance = held_qty * headroom_pct / Decimal("100")
        if tolerance <= 0:
            tolerance = held_qty * Decimal("0.0001")

        observed_delta: Decimal | None = None
        expected_delta = -filled_qty
        if balance_before is None:
            mismatches.append("no pre-trade balance sample to compare against")
        elif balance_after is not None:
            observed_delta = balance_after - balance_before
            if abs(observed_delta - expected_delta) > tolerance:
                mismatches.append(
                    f"{base_asset} balance delta {_fmt(observed_delta)} differs "
                    f"from the expected {_fmt(expected_delta)} by more than "
                    f"{_fmt(tolerance)}"
                )

        if not fills:
            mismatches.append(
                "the venue reported a completed fill but produced no per-fill "
                "records to corroborate it"
            )
        elif abs(fill_qty_total - filled_qty) > tolerance:
            mismatches.append(
                f"per-fill quantities total {_fmt(fill_qty_total)}, which does "
                f"not match the order's reported {_fmt(filled_qty)}"
            )
        if fills and not fee_readable:
            mismatches.append("no readable fee on any exit fill")
        if exit_vwap is None or exit_vwap <= 0:
            mismatches.append("no usable exit fill price")

        proceeds = (
            fill_cost_total - exit_fee_total
            if fill_cost_total > 0
            else (exit_vwap or Decimal("0")) * filled_qty - exit_fee_total
        )
        cost_basis = entry_price * held_qty + entry_fee
        pnl = proceeds - cost_basis
        pnl_pct = pnl / cost_basis * Decimal("100") if cost_basis > 0 else None

        summary = {
            "verdict": "review" if mismatches else "pass",
            "mismatches": mismatches,
            "txid": txid,
            "filled_qty": _fmt(filled_qty),
            "exit_fill_price": _fmt(exit_vwap),
            "exit_fee": _fmt(exit_fee_total),
            "exit_proceeds_net": _fmt(proceeds),
            "entry_fee": _fmt(entry_fee),
            "entry_fee_source": entry_fee_source,
            "cost_basis": _fmt(cost_basis),
            "realized_pnl_usd": _fmt(pnl),
            "realized_pnl_pct": _fmt(pnl_pct),
            "balance": {
                "asset": base_asset,
                "before": _fmt(balance_before),
                "after": _fmt(balance_after),
                "observed_delta": _fmt(observed_delta),
                "expected_delta": _fmt(expected_delta),
                "tolerance": _fmt(tolerance),
            },
            "fill_count": len(fills),
        }
        evidence.record("exit_reconciliation", **summary)

        if mismatches:
            log.warning(
                "kraken_pilot_exit_reconciliation_review",
                live_trade_id=live_trade_id,
                mismatches=mismatches,
            )
            await update_live_trade(
                self._db,
                live_trade_id,
                status="needs_manual_review",
                exit_order_id=txid,
                exit_fill_price=_fmt(exit_vwap),
            )
            evidence.record(
                "exit_not_closed",
                live_trade_id=live_trade_id,
                ledger_status="needs_manual_review",
                note="reconciliation did not confirm the sale; the row is NOT "
                "closed and no realised P&L is written",
            )
            _print_block(
                "EXIT NOT CLOSED — RECONCILIATION DID NOT CONFIRM THE SALE",
                [
                    f"  live_trades row : {live_trade_id} (needs_manual_review)",
                    f"  exit txid       : {txid or '(unknown)'}",
                    "",
                    "  The venue says the order filled, but the account did not",
                    "  move the way that fill implies:",
                ]
                + [f"   - {m}" for m in mismatches]
                + [
                    "",
                    "  The row has NOT been closed and no realised P&L has been",
                    "  written. Money may have moved in a way this runner cannot",
                    "  account for. Check the account before anything else.",
                    f"  evidence        : {evidence.path}",
                ],
            )
            return EXIT_REVIEW

        closed_at = datetime.now(timezone.utc).isoformat()
        await update_live_trade(
            self._db,
            live_trade_id,
            status=_EXIT_CLOSED_STATUS,
            exit_order_id=txid,
            exit_fill_price=_fmt(exit_vwap),
            realized_pnl_usd=_fmt(pnl),
            realized_pnl_pct=_fmt(pnl_pct),
            closed_at=closed_at,
        )
        evidence.record(
            "exit_closed",
            live_trade_id=live_trade_id,
            ledger_status=_EXIT_CLOSED_STATUS,
            exit_order_id=txid,
            exit_fill_price=_fmt(exit_vwap),
            realized_pnl_usd=_fmt(pnl),
            realized_pnl_pct=_fmt(pnl_pct),
            closed_at=closed_at,
        )
        _print_block(
            "POSITION CLOSED",
            [
                f"  live_trades row : {live_trade_id} ({_EXIT_CLOSED_STATUS})",
                f"  decision ID     : {decision_id}",
                f"  exit txid       : {txid or '(unknown)'}",
                f"  sold            : {_fmt(filled_qty)} {base_asset} @ "
                f"{_fmt(exit_vwap)} on {pair}",
                f"  exit fee        : {_fmt(exit_fee_total)}",
                f"  entry fee       : {_fmt(entry_fee)} ({entry_fee_source})",
                f"  cost basis      : {_fmt(cost_basis)}",
                f"  realised P&L    : {_fmt(pnl)} ({_fmt(pnl_pct)}%)",
                "  reconcile       : pass",
                f"  evidence        : {evidence.path}",
            ],
        )
        return EXIT_OK

    # ---------------- cancel ----------------
    async def cancel(self, *, decision_id: str) -> int:
        """Cancel the resting pilot order identified by ``decision_id``.

        Deliberately NOT gated on the kill switch. Cancel is the lever that
        REDUCES exposure — the same reasoning that keeps ``cancel_order``
        ungated in the adapter — so a kill that made the order unpullable
        would disable the safety mechanism. The kill state is recorded in the
        evidence either way.
        """
        evidence = self._evidence_for(decision_id)
        evidence.record("run_started", command="cancel", decision_id=decision_id)
        try:
            evidence.record("envelope_gate", **self._check_cancel_envelope())
            kill_state = await self._ks.is_active()
            evidence.record(
                "kill_switch_observed",
                kill_active=kill_state is not None,
                note="cancel is not gated on the kill switch — it reduces exposure",
            )

            row = await fetch_row_by_decision_id(self._db, decision_id)
            if row is None:
                raise PilotAbort(
                    "lookup",
                    f"no kraken live_trades row with client_order_id={decision_id}",
                )
            evidence.record("ledger_row", **row)

            balance_asset = self._settings.KRAKEN_PILOT_QUOTE.upper()
            balance_before = _dec(
                await self._adapter.fetch_account_balance(asset=balance_asset)
            )

            txid = row.get("entry_order_id")
            cancel_result = await self._adapter.cancel_order(
                txid=txid or None,
                client_order_id=None if txid else decision_id,
            )
            evidence.record(
                "cancel_requested",
                by="txid" if txid else "client_order_id",
                count=cancel_result.get("count"),
                pending=cancel_result.get("pending"),
                already_gone=cancel_result.get("already_gone"),
            )

            conf = await self._adapter.fetch_order_by_client_id(
                pair=row.get("pair") or "", client_order_id=decision_id
            )
            evidence.record(
                "post_cancel_lookup",
                found=conf is not None,
                status=None if conf is None else conf.status,
                txid=None if conf is None else conf.venue_order_id,
                filled_qty=None if conf is None else conf.filled_qty,
                fill_price=None if conf is None else conf.fill_price,
            )

            filled_qty = _dec_or_none(None if conf is None else conf.filled_qty)
            partial = filled_qty is not None and filled_qty > 0
            fills: list[dict[str, Any]] = []
            if partial:
                # A partially-filled cancel leaves REAL inventory. The row
                # stays 'open' because a position exists; 'rejected' would
                # assert nothing happened while the account holds coins.
                lookup_txid = txid or (conf.venue_order_id if conf else None)
                if lookup_txid:
                    fills = await self._adapter.fetch_order_fills(txid=str(lookup_txid))
                await update_live_trade(
                    self._db,
                    row["live_trade_id"],
                    entry_fill_price=_fmt(
                        _dec_or_none(None if conf is None else conf.fill_price)
                    ),
                    entry_fill_qty=_fmt(filled_qty),
                )
                evidence.record(
                    "cancel_partial_fill",
                    live_trade_id=row["live_trade_id"],
                    ledger_status="open",
                    filled_qty=_fmt(filled_qty),
                    fills=fills,
                    note="POSITION EXISTS — row stays open",
                )
            else:
                # Zero-fill cancel: the entry never happened. 'rejected' is the
                # vocabulary's term for an entry that produced no position.
                # reject_reason stays NULL — the CHECK enum has no
                # operator-cancel member and this module does not widen it.
                await update_live_trade(
                    self._db,
                    row["live_trade_id"],
                    status="rejected",
                    closed_at=datetime.now(timezone.utc).isoformat(),
                )
                evidence.record(
                    "cancel_zero_fill",
                    live_trade_id=row["live_trade_id"],
                    ledger_status="rejected",
                    reject_reason=None,
                    note="reject_reason left NULL: the CHECK enum has no "
                    "operator-cancel value and the constraint is not widened here",
                )

            balance_after = _dec(
                await self._adapter.fetch_account_balance(asset=balance_asset)
            )
            evidence.record(
                "cancel_reconciliation",
                asset=balance_asset,
                before=_fmt(balance_before),
                after=_fmt(balance_after),
                delta=_fmt(balance_after - balance_before),
                partial_fill=partial,
            )

            _print_block(
                "PILOT ORDER CANCELLED" + (" — PARTIAL FILL" if partial else ""),
                [
                    f"  decision ID : {decision_id}",
                    f"  ledger row  : {row['live_trade_id']} "
                    f"({'open (position exists)' if partial else 'rejected'})",
                    f"  filled qty  : {_fmt(filled_qty) if partial else '0'}",
                    f"  balance     : {_fmt(balance_before)} -> "
                    f"{_fmt(balance_after)} {balance_asset}",
                    f"  evidence    : {evidence.path}",
                ],
            )
            if partial:
                print(
                    "ACTION REQUIRED: a partial position exists and the ledger row "
                    "is still open. It will block the next `place` run until you "
                    "close the position and resolve the row."
                )
            return EXIT_OK

        except PilotAbort as abort:
            evidence.record(
                "aborted",
                stage=abort.stage,
                reason=abort.reason,
                exit_code=abort.exit_code,
            )
            print(f"REFUSED [{abort.stage}]: {abort.reason}")
            return abort.exit_code
        except Exception as exc:
            # A cancel that fails partway is an exposure question, so it gets
            # the same recorded escalation as a failed place rather than a
            # traceback: the order may still be resting.
            log.error(
                "kraken_pilot_unexpected_error",
                decision_id=decision_id,
                command="cancel",
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=True,
            )
            evidence.record(
                "unexpected_error",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            _print_block(
                "ESCALATE — CANCEL DID NOT COMPLETE",
                [
                    f"  {type(exc).__name__}: {exc}",
                    f"  decision ID : {decision_id}",
                    "  The order may still be resting. Confirm in the Kraken web",
                    "  UI before retrying.",
                    f"  evidence    : {evidence.path}",
                ],
            )
            return EXIT_ESCALATE

    # ---------------- status ----------------
    async def status(self) -> int:
        """Read-only lane report. Writes no evidence file — nothing decided."""
        lines: list[str] = []
        settings = self._settings
        lines.append(f"  KRAKEN_PILOT_ENABLED     : {settings.KRAKEN_PILOT_ENABLED}")
        lines.append(
            f"  approved pair            : "
            f"{settings.KRAKEN_PILOT_PAIR or '(none)'}/{settings.KRAKEN_PILOT_QUOTE}"
        )
        lines.append(
            f"  per-order band           : "
            f"{settings.KRAKEN_PILOT_MIN_ORDER_USD} - "
            f"{settings.KRAKEN_PILOT_MAX_ORDER_USD} USD"
        )
        lines.append(
            f"  real signed requests     : {settings.LIVE_USE_REAL_SIGNED_REQUESTS}"
        )
        lines.append(f"  database                 : {Path(settings.DB_PATH).resolve()}")

        kill_state = await self._ks.is_active()
        lines.append(
            "  kill switch              : "
            + (
                "INACTIVE"
                if kill_state is None
                else f"ACTIVE (#{kill_state.kill_event_id}, {kill_state.reason})"
            )
        )

        try:
            preflight = await self._adapter.preflight_credentials_check()
            lines.append(
                f"  preflight                : auth_ok={preflight['auth_ok']} "
                f"withdrawal_excluded={preflight['withdrawal_excluded']}"
            )
        except Exception as exc:
            lines.append(f"  preflight                : FAILED {type(exc).__name__}")

        quote = settings.KRAKEN_PILOT_QUOTE.upper()
        for asset in dict.fromkeys([quote, (settings.KRAKEN_PILOT_PAIR or "").upper()]):
            if not asset:
                continue
            try:
                balance = await self._adapter.fetch_account_balance(asset=asset)
                lines.append(f"  available {asset:<14} : {balance}")
            except Exception as exc:
                lines.append(
                    f"  available {asset:<14} : unavailable ({type(exc).__name__})"
                )

        day = datetime.now(timezone.utc).date().isoformat()
        gross = await daily_gross_usd(self._db, day)
        lines.append(
            f"  daily gross ({day}): {_fmt(gross)} of "
            f"{settings.KRAKEN_PILOT_MAX_DAILY_GROSS_USD} USD"
        )

        reconciliation = await self._reconcile_open_rows()
        rows = reconciliation["rows"]
        lines.append(f"  blocking ledger rows     : {len(rows)}")
        for row in rows:
            venue = row.get("venue", {})
            lines.append(
                f"    #{row['live_trade_id']} {row['status']} "
                f"cid={row['client_order_id']} txid={row['entry_order_id']} "
                f"pair={row['pair']} size={row['size_usd']} "
                f"venue={venue.get('outcome')}"
                + (f"/{venue.get('status')}" if venue.get("status") else "")
            )
        if reconciliation["listing_error"]:
            lines.append(
                "  venue open orders        : UNREADABLE "
                f"({reconciliation['listing_error']['error_type']})"
            )
        else:
            lines.append(
                f"  venue open orders        : {reconciliation['venue_open_count']}"
            )
            for order in reconciliation["venue_open_orders"]:
                lines.append(
                    f"    {order['txid']} {order['pair']} {order['status']} "
                    f"vol={order['vol']} exec={order['vol_exec']} "
                    f"price={order['price']} cid={order['client_order_id']}"
                )
        if reconciliation["unknown_orders"]:
            lines.append(
                f"  UNKNOWN venue orders     : "
                f"{len(reconciliation['unknown_orders'])} "
                "(resting, not accounted for by any ledger row)"
            )

        _print_block("KRAKEN PILOT STATUS", lines)
        if reconciliation["blockers"]:
            print(
                "The lane is BLOCKED: `place` will refuse until everything above "
                "is resolved."
            )
        return EXIT_OK

    def _print_blocked(self, reconciliation: dict[str, Any]) -> None:
        lines = [
            "  A restart must not forget an order, and the account must not hold",
            "  one the ledger cannot name. The lane refuses to place anything new",
            "  until each item below is resolved.",
            "",
        ]
        for row in reconciliation["rows"]:
            venue = row.get("venue", {})
            lines.append(
                f"    live_trades #{row['live_trade_id']}  status={row['status']}"
            )
            lines.append(f"      client_order_id : {row['client_order_id']}")
            lines.append(f"      txid            : {row['entry_order_id']}")
            lines.append(f"      pair / size     : {row['pair']} / {row['size_usd']}")
            lines.append(f"      created_at      : {row['created_at']}")
            lines.append(f"      venue says      : {venue}")
        for order in reconciliation["unknown_orders"]:
            lines.append(
                f"    UNKNOWN venue order  txid={order['txid']} "
                f"pair={order['pair']}"
            )
            lines.append(f"      client_order_id : {order['client_order_id']}")
            lines.append(
                f"      vol / executed  : {order['vol']} / {order['vol_exec']}"
            )
            lines.append(f"      limit price     : {order['price']}")
            lines.append("      No ledger row accounts for this order — it was placed")
            lines.append("      outside the pilot, or by a run whose row never landed.")
        if reconciliation["listing_error"]:
            lines.append(
                f"    COULD NOT READ the venue's open orders: "
                f"{reconciliation['listing_error']['error_type']}: "
                f"{reconciliation['listing_error']['error']}"
            )
            lines.append("      An unreadable listing is treated as a blocker, because")
            lines.append(
                "      'no open orders' is what would license a new placement."
            )
        lines.extend(
            [
                "",
                "  To clear:",
                "   - resting order you want gone:",
                "       python -m scout.live.kraken_pilot cancel --decision-id <cid>",
                "   - filled order: close the position, then set the row's status",
                "     by hand to record how it ended.",
                "   - needs_manual_review: confirm the venue state first (Kraken",
                "     web UI), then resolve the row. NEVER resend the order.",
                "   - unknown venue order: cancel it in the Kraken web UI, or",
                "     record it in live_trades if it was a pilot order.",
            ]
        )
        _print_block("PILOT LANE BLOCKED", lines)


def _reject_reason_for_error(message: str) -> str | None:
    """Map a venue refusal to a ``live_trades.reject_reason`` CHECK member.

    Only maps what the enum can express. Everything else returns ``None`` —
    permitted by the constraint — with the real error preserved in the
    evidence file and the structured log. Inventing a closer-sounding value
    would put a wrong reason in the durable ledger.
    """
    lowered = message.lower()
    if "insufficient funds" in lowered or "insufficient_funds" in lowered:
        return "insufficient_balance"
    return None


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _decimal_arg(raw: str) -> Decimal:
    """argparse converter for a price or a quantity.

    Three rejections, all of which would otherwise reach the trading logic:

    - ``Decimal('x')`` raises InvalidOperation, an ArithmeticError, which is
      NOT one of the exceptions argparse converts into a usage error — a
      typo'd price would surface as a traceback rather than a usage message.
    - ``Decimal('NaN')`` and ``Decimal('Infinity')`` PARSE. NaN then makes
      every comparison in the cap checks False, so a NaN order size passes
      every bound it is tested against; Infinity poisons the arithmetic
      instead. Both must die at the boundary.
    - Zero and negatives are not orders. The adapter rejects them too, but by
      then the operator has already been shown an approval block.
    """
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a decimal number") from exc
    if not value.is_finite():
        raise argparse.ArgumentTypeError(
            f"{raw!r} is not a finite number — NaN and Infinity parse as Decimals "
            "but compare false against every cap"
        )
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{raw!r} must be greater than zero")
    return value


_DECISION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-" r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _decision_id_arg(raw: str) -> str:
    """argparse converter for ``--decision-id``.

    Strips surrounding whitespace (these get copied out of a terminal or an
    evidence file, and a trailing space silently turns a lookup into "no such
    row") and requires the dashed-UUID shape the runner mints, so a mangled
    paste fails as a usage error rather than as a ledger miss during an
    incident.
    """
    value = raw.strip()
    if not _DECISION_ID_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"{raw!r} is not a pilot decision id (expected a dashed UUID, e.g. "
            "6d1b345e-2821-40e2-ad83-4ecb18a06876)"
        )
    return value


def _live_trade_id_arg(raw: str) -> int:
    """argparse converter for ``--live-trade-id``.

    A ledger row is selected by exact primary key, so anything that is not a
    positive integer is a typo that would otherwise become a "no such row"
    refusal after several venue reads.
    """
    try:
        value = int(raw.strip())
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f"{raw!r} is not an integer live_trades id"
        ) from exc
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{raw!r} must be a positive live_trades id")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kraken_pilot",
        description="Execute ONE supervised Kraken spot limit trade.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    place = sub.add_parser("place", help="place one supervised limit order")
    place.add_argument("--side", choices=("buy", "sell"), required=True)
    place.add_argument("--price", type=_decimal_arg, required=True, help="limit price")
    place.add_argument(
        "--volume", type=_decimal_arg, required=True, help="base-asset quantity"
    )
    place.add_argument(
        "--validate-only",
        action="store_true",
        help="venue-side dry run (Kraken validate=true); places nothing",
    )
    place.add_argument(
        "--yes-i-am-rehearsing",
        action="store_true",
        help="required with --validate-only; refused without it",
    )

    exit_cmd = sub.add_parser(
        "exit", help="close ONE open supervised kraken position by ledger row id"
    )
    exit_cmd.add_argument(
        "--live-trade-id",
        type=_live_trade_id_arg,
        required=True,
        help="live_trades.id of the open kraken position to close",
    )
    exit_cmd.add_argument(
        "--price", type=_decimal_arg, required=True, help="limit price for the sell"
    )
    exit_cmd.add_argument(
        "--validate-only",
        action="store_true",
        help="venue-side dry run (Kraken validate=true); places nothing",
    )
    exit_cmd.add_argument(
        "--yes-i-am-rehearsing",
        action="store_true",
        help="required with --validate-only; refused without it",
    )

    cancel = sub.add_parser("cancel", help="cancel a resting pilot order")
    cancel.add_argument("--decision-id", type=_decision_id_arg, required=True)

    sub.add_parser("status", help="read-only lane report")
    return parser


async def main(argv: list[str] | None = None) -> int:
    # The approval block carries money figures, and a console that cannot
    # encode a character raises UnicodeEncodeError mid-print — losing the rest
    # of the block, including the line the operator is about to act on. Replace
    # is the right failure here: a mangled glyph is survivable, a truncated
    # decision block is not. The numeric lines use ASCII operators regardless.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (ValueError, OSError):  # pragma: no cover - exotic stdout
            pass

    args = build_parser().parse_args(argv)

    if args.command in ("place", "exit"):
        if args.validate_only and not args.yes_i_am_rehearsing:
            print(
                "REFUSED [args]: --validate-only requires --yes-i-am-rehearsing, "
                "so a rehearsal is never mistaken for the real run."
            )
            return EXIT_REFUSED
        if args.yes_i_am_rehearsing and not args.validate_only:
            # Refused rather than ignored: an operator who typed the rehearsal
            # flag believes they are rehearsing, and silently placing a real
            # order under that belief is the worst outcome available here.
            print(
                "REFUSED [args]: --yes-i-am-rehearsing was passed without "
                "--validate-only. This would be a REAL order."
            )
            return EXIT_REFUSED

    settings = load_settings()

    # The DB must already exist. sqlite3 CREATES a database for any path it is
    # handed, so running this from the wrong directory does not fail — it
    # silently succeeds against an empty file, and every safety mechanism that
    # reads state finds nothing to object to: no kill switch, no prior rows to
    # reconcile, zero daily gross. The lane would be wide open precisely
    # because it is looking at the wrong database.
    db_path = Path(settings.DB_PATH)
    if not db_path.exists():
        print(
            f"REFUSED [database]: no database at {db_path.resolve()}\n"
            "  DB_PATH resolves relative to the current directory, so this is "
            "almost always\n"
            "  the wrong working directory. Run the pilot from the deployment "
            "root.\n"
            "  Creating one here would disable the kill switch, the startup "
            "reconciliation\n"
            "  and the daily-gross cap all at once, silently."
        )
        return EXIT_REFUSED

    # The lock guards ORDER SUBMISSION, and is taken for `place` and `exit`.
    #
    # It exists to stop two processes creating two live orders. `exit` submits
    # an AddOrder just as `place` does — two concurrent exits are two sells of
    # the same coins, each clearing its own venue-state check before either
    # submits — so it takes the same lock. `status` is read-only and `cancel`
    # reduces exposure without placing anything, so neither can do the thing
    # the lock prevents — and gating them would be actively harmful: a stale
    # lock arises exactly when an earlier run died with an order possibly
    # resting, which is the moment the operator most needs to look and to pull
    # it. A lock that blocks its own recovery path is worse than no lock.
    lock_fd: int | None = None
    lock_path: Path | None = None
    if args.command in ("place", "exit"):
        try:
            lock_fd, lock_path = acquire_pilot_lock(db_path)
        except PilotLockHeld as held:
            print(
                f"REFUSED [lock]: another pilot run holds {held.lock_path}\n"
                f"  holder: {held.holder}\n"
                "  Two runs are two live orders, so `place` and `exit` are "
                "blocked.\n"
                "  `status` and `cancel` still work — use them to see what is "
                "resting\n"
                "  and to pull it. Once the venue state is confirmed and that "
                "process\n"
                "  is gone, delete the lock file by hand."
            )
            return EXIT_BLOCKED

    # Everything past the acquire is inside the finally that releases it.
    # Database construction and initialize() can BOTH raise — a migration can
    # exceed busy_timeout, and a slow one invites Ctrl+C — and a lock leaked
    # there is permanent, blocking every later placement on a machine where
    # nothing is actually resting.
    try:
        db = Database(db_path, busy_timeout_ms=settings.SQLITE_BUSY_TIMEOUT_MS)
        await db.initialize()
        adapter = KrakenSpotAdapter(settings, db=db)
        runner = PilotRunner(
            settings=settings, db=db, adapter=adapter, kill_switch=KillSwitch(db)
        )
        try:
            if args.command == "place":
                return await runner.place(
                    side=args.side,
                    price=args.price,
                    volume=args.volume,
                    validate_only=args.validate_only,
                )
            if args.command == "exit":
                return await runner.exit_position(
                    live_trade_id=args.live_trade_id,
                    price=args.price,
                    validate_only=args.validate_only,
                )
            if args.command == "cancel":
                return await runner.cancel(decision_id=args.decision_id)
            return await runner.status()
        finally:
            await adapter.close()
            await db.close()
    finally:
        if lock_fd is not None and lock_path is not None:
            release_pilot_lock(lock_fd, lock_path)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
