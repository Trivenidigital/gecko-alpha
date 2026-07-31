"""Kraken supervised pilot runner — PR-K3.

The operator-invoked CLI that executes ONE supervised Kraken spot limit
trade behind a manual approval boundary. It composes PR-K1/K2's
``KrakenSpotAdapter`` with the existing kill switch and ``live_trades``
ledger; it is NOT wired into the signal-driven live engine and nothing in
the pipeline imports it.

Usage::

    python -m scout.live.kraken_pilot place --side buy --price 100000.0 \\
        --volume 0.0002 [--validate-only --yes-i-am-rehearsing]
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

**One pilot process at a time, enforced by the filesystem.** Two runs in two
terminals are two live orders: each clears its own one-order-at-a-time check
before either writes a row, and each reads a daily-gross total the other is
about to invalidate. ``O_CREAT | O_EXCL`` on a lock file beside the database
is the atomic test-and-set that closes both windows at the only point the two
processes share. A held lock is never broken automatically.

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
"""

from __future__ import annotations

import argparse
import asyncio
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
from scout.live.idempotency import lookup_existing_order_id, record_pending_order
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

    if args.command == "place":
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

    # One pilot process at a time, machine-wide (see acquire_pilot_lock).
    try:
        lock_fd, lock_path = acquire_pilot_lock(db_path)
    except PilotLockHeld as held:
        print(
            f"REFUSED [lock]: another pilot run holds {held.lock_path}\n"
            f"  holder: {held.holder}\n"
            "  Two runs are two live orders. If that process is gone, confirm "
            "the venue\n"
            "  state (`status`, or the Kraken web UI) and then delete the lock "
            "file by hand."
        )
        return EXIT_BLOCKED

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
        if args.command == "cancel":
            return await runner.cancel(decision_id=args.decision_id)
        return await runner.status()
    finally:
        await adapter.close()
        await db.close()
        release_pilot_lock(lock_fd, lock_path)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
