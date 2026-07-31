"""Solana supervised pilot runner — PR-S2.

The operator-invoked CLI that executes ONE supervised SOL->USDC swap on
mainnet behind a manual approval boundary. It composes PR-S1's venue clients
(``scout.live.solana``) with the existing kill switch and ``live_trades``
ledger; it is NOT wired into the signal-driven live engine and nothing in the
pipeline imports it.

Usage::

    python -m scout.live.solana_pilot place --sol 0.05 \\
        [--simulate-only --yes-i-am-rehearsing]
    python -m scout.live.solana_pilot status
    python -m scout.live.solana_pilot resolve --decision-id <uuid>

Place flow, in mechanical order — every step is printed, logged and appended
to the evidence file before the next one starts:

1.  envelope gate (SOLANA_PILOT_ENABLED, keypair path, mainnet hosts)
2.  kill-switch check
3.  keypair custody — load at call time, derive the signer pubkey
4.  startup reconciliation — a restart must not forget a signature
5.  Jupiter quote
6.  quote envelope (USD band on the USDC out, price impact, slippage, mints)
7.  Jupiter build (unsigned) with the Jito tip requested
8.  tx_inspector — every check it can make against the transaction's own bytes
9.  balance cover (swap + fees + ATA rent + headroom)
10. independent RPC simulation
11. LOCAL SIGNING, in memory — this derives the expected signature
12. persist the intent (ledger row carrying the expected signature) + evidence
13. MANUAL APPROVAL BOUNDARY — the operator types the signature prefix
14. kill-switch RE-check, post-approval
15. blockhash-validity RE-check, post-approval
16. submit through Jito, exactly once
17. confirmation, then finalization
18. getTransaction + balance reconciliation

Design decisions this module had to make, and why
-------------------------------------------------
**Signing precedes authorization, and that is deliberate.** The approval
screen is required to show the expected transaction signature, and that
signature does not exist until the message has been signed — it is a pure
function of the message bytes and the key, computed locally with no network
involved (``solana.signer``). So the order is build -> inspect -> simulate ->
SIGN -> persist -> approve -> submit.

Nothing leaves the process between signing and authorization. What the
operator's authorization gates is BROADCAST, not signature creation: an
unbroadcast signed transaction is inert bytes in memory, indistinguishable in
effect from no transaction at all. Signing first is what makes the
authorization bind to one exact transaction rather than to an intention — the
operator types the first 8 characters of the signature they are looking at,
so a rebuilt transaction (different blockhash, different signature) cannot be
submitted under an authorization given for a different one. There is no
approve-then-sign ordering that preserves that property, because approving a
transaction whose identity is not yet fixed is approving a description.

**The expected signature IS the idempotency key**, and it is persisted before
submission. Solana has no client-order-id and no server-side dedup. A
transaction is identified by its signature, the signature is derivable before
the POST, and a REBUILT transaction is a second independent swap rather than a
retry. So the runner writes the signature into ``live_trades.entry_order_id``
before the bytes are sent, and NEVER rebuilds inside a run. Everything after
an ambiguous submission is a question about that one signature.

**No cancel subcommand.** Solana has no cancellation primitive: a submitted
transaction either lands before ``lastValidBlockHeight`` or it can never land.
The equivalent lever is time plus the resolver, which is what ``resolve``
runs. A ``cancel`` that could only ever print "wait" would be worse than its
absence.

**``lastValidBlockHeight`` is persisted in the evidence file, not the ledger.**
The resolver needs it: without it, ``definitively_not_submitted`` is
unreachable and an absent signature stays ``unresolved`` forever.
``live_trades`` has no column that could hold a block height without abusing
one (``mid_at_entry`` is a price, ``entry_slippage_bps`` is basis points), and
a schema migration for a single Solana-only integer is a heavier change than
this lane warrants. The evidence file is already the lane's durable audit
substrate — JSON Lines, fsynced per record, written before the approval
prompt — so ``read_persisted_intent`` reads the height back from it. When the
file is missing or unreadable the height is ``None``, which makes the resolver
fail CLOSED (``unresolved``, lane blocked) rather than degrade quietly.

**One PLACEMENT at a time, enforced by the filesystem.** Same reasoning and
same ``O_CREAT | O_EXCL`` mechanism as the Kraken lane, on its own lock file:
two ``place`` runs in two terminals are two swaps, each clearing its own
one-at-a-time check before either writes a row. The lock covers ``place``
only — ``status`` and ``resolve`` are how an operator investigates a stale
lock, so locking them would block the recovery path. The lock is separate
from the Kraken pilot's because the two lanes hold different assets at
different venues and neither's envelope bounds the other.

**The database must already exist.** SQLite creates one for any path it is
handed, so running from the wrong directory does not fail — it silently
succeeds against an empty file where the kill switch is clear and no prior
signature needs reconciling. Every safety mechanism reads "all clear"
precisely because it is reading the wrong database.

**``paper_trade_id`` is satisfied by a pilot ANCHOR row.**
``live_trades.paper_trade_id`` is ``NOT NULL REFERENCES paper_trades(id)`` with
``PRAGMA foreign_keys=ON``, and there is no signal behind an operator-invoked
swap. One sentinel row — token_id 'solana-pilot', signal_type 'solana_pilot',
status 'solana_pilot_anchor', opened_at epoch — is created once and referenced
by every pilot swap. The three field values are each load-bearing against a
specific reader: the status is not 'open' so no open-position loop scans it,
the epoch ``opened_at`` keeps it out of the daily digest's
``date(opened_at) = ?`` count and the analytics windows' ``opened_at >=
cutoff``, and ``closed_at`` stays NULL so the closed-trade digest never sees
it. No schema migration.

**A landed swap leaves the row 'open'.** 'open' here means "this pilot holds
the resulting position", which is what is true after a swap executes, and it
blocks the next ``place`` run — correct for a supervised lane that trades once
per authorization. The operator disposes of the USDC and resolves the row by
hand, exactly as on the Kraken lane.

**A ``--simulate-only`` rehearsal writes NO ledger row.** It runs the whole
flow including the approval prompt and the local signing, and stops before
submission. A row would be a phantom asserting a swap that provably never
happened, and step 4 blocks the whole lane on such a row. The evidence file is
the record, and it carries the would-be expected signature.

**Evidence is JSON Lines** — one object per step, appended, flushed and
fsynced before the step returns, with the file reopened per record. A crash at
any point leaves every completed step on disk.

**These primitives are deliberately duplicated from ``kraken_pilot``** rather
than extracted into a shared module: the evidence format, the lock protocol
and the approval prompt are part of each lane's audit contract, and a change
made for one venue must not silently alter the other's recorded history.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import aiohttp
import structlog
from solders.keypair import Keypair

from scout.config import Settings, load_settings
from scout.db import Database
from scout.live.kill_switch import KillSwitch
from scout.live.solana.constants import (
    ATA_RENT_LAMPORTS_FALLBACK,
    JITO_TIP_ACCOUNTS_FALLBACK,
    LAMPORTS_PER_SIGNATURE,
    LAMPORTS_PER_SOL,
    SOL_MINT,
    TOKEN_ACCOUNT_DATA_LENGTH,
    USDC_DECIMALS,
    USDC_MINT,
)
from scout.live.solana.exceptions import (
    SolanaAmbiguousSubmissionError,
    SolanaAPIError,
    SolanaKeypairError,
)
from scout.live.solana.jito_client import JitoClient
from scout.live.solana.jupiter_client import JupiterClient, SolanaQuote, SwapBuild
from scout.live.solana.resolver import ResolutionReport, resolve_submission
from scout.live.solana.rpc_client import SolanaRpcClient
from scout.live.solana.signer import SignedTransaction, load_keypair, sign_transaction
from scout.live.solana.tx_inspector import VerificationReport, verify_swap_transaction

log = structlog.get_logger(__name__)

# Exit codes. Distinct rather than a blanket 1 because the operator's next
# action differs per class, and a wrapper script should be able to tell them
# apart without parsing stdout.
#
#   0  EXIT_OK        the swap landed and reconciled, or nothing was pending.
#   1  EXIT_REFUSED   a gate said no, the operator did not authorize, or the
#                     transaction definitively did not execute. No position.
#   2  EXIT_BLOCKED   the lane is blocked — an unresolved prior signature, or
#                     another pilot process holding the lock. Nothing tried.
#   3  EXIT_ESCALATE  a submission could not be resolved, or the run failed
#                     unexpectedly. State is UNKNOWN; a human must look.
#   4  EXIT_REVIEW    the swap executed but the post-trade reconciliation could
#                     not explain the balance move.
EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_BLOCKED = 2
EXIT_ESCALATE = 3
EXIT_REVIEW = 4

VENUE = "solana"
PAIR = "SOL/USDC"
BASE_SYMBOL = "SOL"

# The pilot anchor (see module docstring). Values are matched exactly on
# lookup, so they are constants rather than anything derived at runtime.
_ANCHOR_TOKEN_ID = "solana-pilot"
_ANCHOR_SIGNAL_TYPE = "solana_pilot"
_ANCHOR_STATUS = "solana_pilot_anchor"
_ANCHOR_OPENED_AT = "1970-01-01T00:00:00+00:00"

# live_trades statuses that mean "a signature may still be live, or a position
# may exist". Any such row blocks a new placement (one swap at a time).
_BLOCKING_STATUSES = ("open", "needs_manual_review")

# Substrings that mark a non-mainnet cluster in an RPC or block-engine URL.
# A cheap mechanical check, not a proof — the mints in `constants` are mainnet
# addresses, so a wrong-cluster build also fails simulation. Both exist because
# the envelope is "mainnet only" and a typo in .env is the realistic way to
# leave it.
_NON_MAINNET_MARKERS = ("devnet", "testnet", "localhost", "127.0.0.1")

# Public RPC hosts known to sit behind a load balancer. The resolver must not
# read from one — see ``resolver_endpoint``. Matched as a substring against a
# lowercased URL, so a provider path or port does not defeat the check.
_ROUND_ROBIN_RPC_HOSTS = (
    "api.mainnet-beta.solana.com",
    "api.devnet.solana.com",
    "api.testnet.solana.com",
    "solana-api.projectserum.com",
    "rpc.ankr.com/solana",
)

# Redacted before anything reaches the evidence file or the log. The runner
# never handles key material itself — ``signer`` loads, uses and drops it — so
# this is a backstop against a future field carrying one.
# Deliberately does NOT match 'signal_type', 'client_order_id' or 'signature'.
_SECRET_KEY_RE = re.compile(
    r"(?i)(secret|passw|api[-_]?key|keypair|private|authorization|bearer)"
)
_REDACTED = "[REDACTED]"

# Evidence step that carries the durable intent — see the module docstring on
# why lastValidBlockHeight lives here.
_INTENT_STEP = "intent_persisted"


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
        super().__init__(f"solana pilot lock held: {lock_path}")
        self.lock_path = lock_path
        self.holder = holder


# ----------------------------------------------------------------------
# Single-process lock
# ----------------------------------------------------------------------
def pilot_lock_path(db_path: str | Path) -> Path:
    """Lock file for the Solana pilot lane, beside the database it guards.

    Distinct from the Kraken pilot's lock: the two lanes hold different assets
    at different venues, and neither's envelope bounds the other's exposure.
    """
    return Path(f"{db_path}.solana_pilot.lock")


def acquire_pilot_lock(db_path: str | Path) -> tuple[int, Path]:
    """Take the Solana pilot lane's exclusive lock, or raise ``PilotLockHeld``.

    Two pilot runs in two terminals are two swaps: each passes its own
    one-at-a-time check before either writes a ledger row, so the in-process
    check cannot see the other. ``O_CREAT | O_EXCL`` is the filesystem's
    atomic test-and-set, which closes the window at the only point the two
    processes share — the machine.

    A held lock is NEVER broken automatically, even if the recorded PID is long
    gone. A stale lock means some earlier run did not reach its cleanup, which
    is exactly the state where a signature may be in flight unrecorded; an
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
        log.warning("solana_pilot_lock_not_removed", lock_path=str(lock_path))


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
    if isinstance(value, bytes):
        # Never render raw bytes: the only bytes in this lane are transaction
        # material, and a base64 blob in an evidence file is unreadable noise
        # at best.
        return f"<{len(value)} bytes>"
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
        log.info("solana_pilot_step", step=step, **_scrub(fields))
        return entry


def resolver_endpoint(settings: Settings) -> tuple[str, bool]:
    """The URL the §7 resolver reads from, and whether it is safely PINNED.

    The resolver reaches ``definitively_not_submitted`` — the one verdict that
    licenses clearing the lane — by combining two facts read in separate RPC
    calls: the signature is absent, AND the chain's block height has passed
    ``lastValidBlockHeight``. Behind a load balancer those two calls can land
    on different nodes, and a node that is simultaneously AHEAD on height and
    missing the signature from its status cache manufactures a false
    ``definitively_not_submitted``. The runner would retire a row describing a
    transaction that is still in flight, and the operator would rerun: two
    swaps against one authorization.

    So the endpoint has to be one consistent node. ``SOLANA_RESOLVER_RPC_URL``
    names it; when unset the main RPC URL is used, but only if that is not
    itself a known round-robin.

    Returns ``(url, pinned)``. Callers that are about to act irreversibly on a
    verdict must refuse when ``pinned`` is False; callers that only REPORT a
    verdict may proceed and say the reading is degraded.
    """
    configured = (settings.SOLANA_RESOLVER_RPC_URL or "").strip()
    url = configured or settings.SOLANA_RPC_URL
    lowered = url.lower()
    pinned = not any(host in lowered for host in _ROUND_ROBIN_RPC_HOSTS)
    return url, pinned


def evidence_path_for(evidence_dir: str | Path, decision_id: str) -> Path:
    """Evidence file for one decision. One naming rule, one call site each."""
    return Path(evidence_dir) / f"solana_pilot_{decision_id}.json"


def read_persisted_intent(
    evidence_dir: str | Path, decision_id: str
) -> dict[str, Any] | None:
    """Read back the durable intent record for ``decision_id``, or None.

    The ``lastValidBlockHeight`` the resolver needs lives here rather than in
    ``live_trades`` — see the module docstring. Returns the LAST intent record
    in the file; a run writes exactly one, and taking the last is the correct
    reading if a future change ever writes more.

    Every failure mode returns None (missing directory, missing file,
    unreadable file, malformed line, no intent record). None makes the caller
    resolve with ``last_valid_block_height=None``, under which
    ``definitively_not_submitted`` is unreachable and the row stays blocking —
    the fail-closed reading of "we cannot prove this transaction expired".
    """
    path = evidence_path_for(evidence_dir, decision_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    found: dict[str, Any] | None = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("step") == _INTENT_STEP:
            found = entry
    return found


# ----------------------------------------------------------------------
# Small numeric helpers — the chain speaks integers, the ledger speaks text.
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


def lamports_from_sol(sol: Decimal) -> int:
    """Exact lamports for a SOL amount, or ``ValueError`` if inexact.

    A fractional lamport is not a rounding question: the amount goes into the
    quote as a raw integer, so anything below 1e-9 SOL would be silently
    truncated into a swap the operator did not ask for.
    """
    scaled = sol * LAMPORTS_PER_SOL
    if scaled != scaled.to_integral_value():
        raise ValueError(
            f"{sol} SOL is not a whole number of lamports (1 lamport = 1e-9 SOL)"
        )
    return int(scaled)


def sol_from_lamports(lamports: int | None) -> Decimal | None:
    return None if lamports is None else _dec(lamports) / _dec(LAMPORTS_PER_SOL)


def usdc_from_raw(raw: int | None) -> Decimal | None:
    return None if raw is None else _dec(raw) / _dec(10**USDC_DECIMALS)


def quote_price_impact_pct(quote: SolanaQuote) -> Decimal:
    """The quote's price impact as a PERCENT.

    Jupiter reports ``priceImpactPct`` as a decimal FRACTION — "0.0025" is
    0.25%, not 0.0025% — so the value is multiplied by 100 before it is
    compared against ``SOLANA_PILOT_MAX_PRICE_IMPACT_PCT`` or shown to the
    operator. That direction is also the conservative one: reading a fraction
    as if it were already a percent would make a 2% impact look like 0.02% and
    pass a ceiling it breaches by two orders of magnitude.

    An unparseable value is not zero. It comes back as a NaN-free sentinel of
    100% so the ceiling check fails closed on a field we could not read.
    """
    parsed = _dec_or_none(quote.price_impact_pct)
    if parsed is None or not parsed.is_finite():
        return Decimal("100")
    return abs(parsed) * Decimal("100")


# ----------------------------------------------------------------------
# Ledger helpers
# ----------------------------------------------------------------------
async def ensure_pilot_anchor(db: Database) -> int:
    """Return the pilot anchor ``paper_trades.id``, creating it if absent.

    See the module docstring for why the anchor exists and why its status /
    opened_at are what they are. Idempotent: the natural key is
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
            "note": "Anchor row for scout.live.solana_pilot. Not a trade.",
            "why": "live_trades.paper_trade_id is NOT NULL REFERENCES "
            "paper_trades(id) and the supervised swap has no signal behind it.",
        }
    )
    try:
        async with db._txn_lock:
            cur = await db._conn.execute(
                """INSERT INTO paper_trades
                   (token_id, symbol, name, chain, signal_type, signal_data,
                    entry_price, amount_usd, quantity, tp_price, sl_price,
                    status, opened_at)
                   VALUES (?, 'SOLANA-PILOT', 'Solana supervised pilot anchor',
                           'solana', ?, ?, 0, 0, 0, 0, 0, ?, ?)""",
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
        log.info("solana_pilot_anchor_created", paper_trade_id=anchor_id)
        return anchor_id
    except sqlite3.IntegrityError:
        # Lost the race on the natural key — the other creator's row is the
        # one to use.
        cur = await db._conn.execute(select_sql, params)
        row = await cur.fetchone()
        if row is None:  # pragma: no cover - only reachable on a torn DB
            raise
        return int(row[0])


async def record_solana_intent(
    db: Database,
    *,
    decision_id: str,
    expected_signature: str,
    paper_trade_id: int,
    size_usd: str,
    sol_price_usdc: str | None,
) -> int:
    """Insert the 'open' ledger row carrying the expected signature.

    Deliberately NOT ``idempotency.record_pending_order``. That helper inserts
    without ``entry_order_id`` and every caller fills it in afterwards, which
    on this lane would leave a window where the row exists and the signature
    does not — an 'open' row with no signature is unresolvable, so a crash in
    that window would block the lane permanently on a transaction nobody can
    ask about. The signature is the idempotency key here, so it goes in the
    same INSERT.

    Everything else follows the shared contract: one write under
    ``db._txn_lock`` (the connection is shared, and an unlocked commit would
    also commit another coroutine's half-built transaction), status 'open'
    meaning "a transaction may exist from this instant", and the UNIQUE
    ``client_order_id`` index as the DB-layer backstop.

    ``size_usd`` is the quote's USDC output — the AUTHORIZED value of the
    swap, recorded before anything executes. It is not realised proceeds;
    those land in ``entry_fill_qty`` / ``entry_fill_price`` after
    reconciliation.
    """
    if db._conn is None or db._txn_lock is None:
        raise RuntimeError("Database not initialized.")
    now_iso = datetime.now(timezone.utc).isoformat()
    async with db._txn_lock:
        cur = await db._conn.execute(
            """INSERT INTO live_trades
               (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
                size_usd, mid_at_entry, entry_order_id, status,
                client_order_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
            (
                paper_trade_id,
                _ANCHOR_TOKEN_ID,
                BASE_SYMBOL,
                VENUE,
                PAIR,
                _ANCHOR_SIGNAL_TYPE,
                size_usd,
                sol_price_usdc,
                expected_signature,
                decision_id,
                now_iso,
            ),
        )
        await db._conn.commit()
        return int(cur.lastrowid or 0)


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


_ROW_COLUMNS = (
    "id, client_order_id, entry_order_id, status, pair, symbol, size_usd, "
    "entry_fill_price, entry_fill_qty, created_at"
)


def _row_dict(row: Any) -> dict[str, Any]:
    return {
        "live_trade_id": row[0],
        "client_order_id": row[1],
        "entry_order_id": row[2],
        "status": row[3],
        "pair": row[4],
        "symbol": row[5],
        "size_usd": row[6],
        "entry_fill_price": row[7],
        "entry_fill_qty": row[8],
        "created_at": row[9],
    }


async def fetch_blocking_rows(db: Database) -> list[dict[str, Any]]:
    """Solana ledger rows that mean a signature or a position is outstanding."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    placeholders = ", ".join("?" for _ in _BLOCKING_STATUSES)
    cur = await db._conn.execute(
        f"SELECT {_ROW_COLUMNS} FROM live_trades "
        f"WHERE venue = ? AND status IN ({placeholders}) ORDER BY id",
        (VENUE, *_BLOCKING_STATUSES),
    )
    return [_row_dict(r) for r in await cur.fetchall()]


async def fetch_row_by_decision_id(
    db: Database, decision_id: str
) -> dict[str, Any] | None:
    """The ledger row whose ``client_order_id`` is this decision id."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    cur = await db._conn.execute(
        f"SELECT {_ROW_COLUMNS} FROM live_trades "
        "WHERE venue = ? AND client_order_id = ?",
        (VENUE, decision_id),
    )
    row = await cur.fetchone()
    return None if row is None else _row_dict(row)


async def retire_row(
    db: Database, live_trade_id: int | None, *, reject_reason: str | None = None
) -> None:
    """Mark a row 'rejected' — no swap executed, nothing is outstanding.

    ``reject_reason`` uses only values already in the CHECK constraint. The
    enum has no "operator declined" or "blockhash expired" member and the
    constraint is NOT widened here: those write NULL (which the constraint
    permits) with the real reason in the evidence file and the log.
    """
    if live_trade_id is None:
        return
    await update_live_trade(
        db,
        live_trade_id,
        status="rejected",
        reject_reason=reject_reason,
        closed_at=datetime.now(timezone.utc).isoformat(),
    )


# ----------------------------------------------------------------------
# Operator I/O
# ----------------------------------------------------------------------
def read_authorization(expected: str) -> tuple[bool, str]:
    """Read the operator's typed authorization from stdin.

    Returns ``(authorized, outcome)``. Every non-match is a refusal: a
    mismatch, an empty line, and EOF (a non-interactive stdin, a closed pipe,
    ``</dev/null``) all abort the run. The comparison is case-SENSITIVE
    because base58 is a case-sensitive alphabet — 'A' and 'a' are different
    signatures.
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
    """One supervised Solana pilot invocation.

    Constructed with everything it needs so tests can drive it against a
    tmp_path DB and mocked venues; ``main`` wires the production instance.

    ``keypair_loader`` is injected rather than called directly because
    ``signer.load_keypair`` refuses to run on a non-POSIX platform (it cannot
    verify file mode and ownership there), and the test suite runs on
    developer machines as well as the deployment host. Production passes the
    real loader; tests pass a fixture keypair. The key material itself is
    never a parameter, never cached, and never reaches this module's state.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        jupiter: JupiterClient,
        rpc: SolanaRpcClient,
        jito: JitoClient,
        kill_switch: KillSwitch,
        keypair_loader: Callable[[], Keypair] | None = None,
        resolver_rpc: SolanaRpcClient | None = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._jupiter = jupiter
        self._rpc = rpc
        self._jito = jito
        self._ks = kill_switch
        self._load_keypair = keypair_loader or (lambda: load_keypair(settings))
        # Both sweeps of every resolution read from THIS client, which must be
        # bound to one consistent node — see ``resolver_endpoint``. Defaults to
        # the main client so a test (or a deployment whose single RPC is
        # already dedicated) needs no second wiring.
        self._resolver_rpc = resolver_rpc or rpc

    # ---------------- shared gates ----------------
    def _evidence_for(self, decision_id: str) -> EvidenceLog:
        return EvidenceLog(
            evidence_path_for(self._settings.SOLANA_PILOT_EVIDENCE_DIR, decision_id)
        )

    def _check_envelope(self, *, simulate_only: bool) -> dict[str, Any]:
        """Step 1 — the pilot is enabled, a key is configured, hosts are mainnet."""
        if not self._settings.SOLANA_PILOT_ENABLED:
            raise PilotAbort(
                "envelope_gate",
                "SOLANA_PILOT_ENABLED is False — the pilot lane is off. Set it "
                "in .env only for the supervised session.",
            )
        if not (self._settings.SOLANA_PILOT_KEYPAIR_PATH or "").strip():
            raise PilotAbort(
                "envelope_gate",
                "SOLANA_PILOT_KEYPAIR_PATH is empty — name the id.json holding "
                "the ONE approved pilot key before running the pilot.",
            )
        # "Mainnet only" is part of the approved envelope, and the realistic
        # way to leave it is an .env host left over from a devnet experiment.
        for field in ("SOLANA_RPC_URL", "JITO_BLOCK_ENGINE_URL"):
            url = str(getattr(self._settings, field, "")).lower()
            hit = next((m for m in _NON_MAINNET_MARKERS if m in url), None)
            if hit is not None:
                raise PilotAbort(
                    "envelope_gate",
                    f"{field} points at a non-mainnet host (contains {hit!r}). "
                    "The approved envelope is mainnet only.",
                )
        # The resolver's endpoint is an envelope property, not an operational
        # detail: a placement that cannot be RESOLVED afterwards is a placement
        # whose recovery path is broken before it starts.
        resolver_url, pinned = resolver_endpoint(self._settings)
        if not pinned:
            raise PilotAbort(
                "envelope_gate",
                f"the resolver would read from {resolver_url}, which is a "
                "load-balanced public endpoint. Two calls can land on two "
                "nodes, and a node that is ahead on block height while missing "
                "the signature manufactures a false "
                "'definitively_not_submitted' — which clears the lane and "
                "invites a rerun of a swap that is still in flight. Set "
                "SOLANA_RESOLVER_RPC_URL to a single dedicated node.",
            )
        return {
            "pilot_enabled": True,
            "keypair_path_configured": True,
            "input_mint": SOL_MINT,
            "output_mint": USDC_MINT,
            "rpc_url": self._settings.SOLANA_RPC_URL,
            "resolver_rpc_url": resolver_url,
            "resolver_endpoint_pinned": True,
            "block_engine_url": self._settings.JITO_BLOCK_ENGINE_URL,
            "simulate_only": simulate_only,
        }

    async def _check_kill_switch(self, stage: str) -> dict[str, Any]:
        """Steps 2 and 14 — refuse while the live kill switch is engaged."""
        state = await self._ks.is_active()
        if state is None:
            return {"kill_active": False}
        raise PilotAbort(
            stage,
            f"kill switch ACTIVE (event #{state.kill_event_id}, by "
            f"{state.triggered_by}, until {state.killed_until.isoformat()}): "
            f"{state.reason}",
        )

    def _custody_check(self) -> tuple[Keypair, dict[str, Any]]:
        """Step 3 — load the key at call time and derive the signer pubkey.

        The loader enforces the file's mode and ownership
        (``signer.enforce_keyfile_security``). A refusal here is an envelope
        refusal, not an escalation: nothing has been built and nothing sent.
        """
        try:
            keypair = self._load_keypair()
        except SolanaKeypairError as exc:
            raise PilotAbort("keypair_custody", str(exc)) from exc
        pubkey = str(keypair.pubkey())
        return keypair, {
            "signer_pubkey": pubkey,
            "keypair_path": self._settings.SOLANA_PILOT_KEYPAIR_PATH,
            "loaded_at_call_time": True,
        }

    async def _reconcile_open_rows(self, *, auto_retire: bool) -> dict[str, Any]:
        """Step 4 — decide what every outstanding signature actually did.

        For each blocking row the persisted expected signature is put to the
        cluster through the §7 resolver. The four verdicts get four different
        treatments, and only one of them clears the lane:

        ``definitively_not_submitted``
            The signature is absent AND its blockhash has expired, so the
            transaction can never land. Auto-retired to 'rejected' when
            ``auto_retire`` — this is safe precisely because the verdict is
            about impossibility rather than about absence.
        ``landed``
            A swap executed. The row keeps blocking: a position exists and the
            operator has to dispose of it, and silently retiring the row would
            assert the opposite.
        ``failed_on_chain``
            The transaction ran and failed; a fee was paid. Blocks, because
            somebody has to look at what it cost.
        ``unresolved``
            We could not tell. Blocks — this is the fail-closed default.

        ``auto_retire`` is False for ``status``, which the spec keeps strictly
        read-only; the same scan then reports what ``place`` WOULD do.
        """
        rows = await fetch_blocking_rows(self._db)
        blockers = 0
        for row in rows:
            signature = row.get("entry_order_id")
            decision_id = row.get("client_order_id") or ""
            if not signature:
                # An 'open' row with no signature cannot be asked about. It
                # should be unreachable (the INSERT writes both), so it is
                # reported rather than repaired.
                row["resolution"] = {
                    "verdict": "unresolved",
                    "detail": "ledger row carries no expected signature",
                }
                row["blocking"] = True
                blockers += 1
                continue

            intent = read_persisted_intent(
                self._settings.SOLANA_PILOT_EVIDENCE_DIR, decision_id
            )
            last_valid = (
                None if intent is None else intent.get("last_valid_block_height")
            )
            try:
                report = await resolve_submission(
                    expected_signature=signature,
                    last_valid_block_height=last_valid,
                    rpc_client=self._resolver_rpc,
                    settings=self._settings,
                )
            except Exception as exc:  # pragma: no cover - resolver swallows its own
                row["resolution"] = {
                    "verdict": "unresolved",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
                row["blocking"] = True
                blockers += 1
                continue

            row["resolution"] = _resolution_summary(report)
            row["resolution"]["last_valid_block_height_source"] = (
                "evidence_file" if intent is not None else "unavailable"
            )
            if report.verdict == "definitively_not_submitted":
                if not resolver_endpoint(self._settings)[1]:
                    # The verdict itself is not trustworthy from a
                    # load-balanced endpoint, so it neither retires the row nor
                    # unblocks the lane. `place` refuses at the envelope before
                    # reaching this, which makes it defence in depth rather
                    # than a live path — and that is exactly why it must not
                    # quietly clear the row if that gate is ever moved.
                    row["blocking"] = True
                    row["auto_retired"] = False
                    row["resolution"][
                        "not_actionable"
                    ] = "verdict read from a load-balanced endpoint"
                    blockers += 1
                    continue
                row["blocking"] = False
                if auto_retire:
                    await retire_row(
                        self._db,
                        row["live_trade_id"],
                        reject_reason="venue_unavailable",
                    )
                    row["auto_retired"] = True
                    log.info(
                        "solana_pilot_row_auto_retired",
                        live_trade_id=row["live_trade_id"],
                        signature=signature,
                    )
                else:
                    row["auto_retired"] = False
            else:
                row["blocking"] = True
                blockers += 1

        return {"rows": rows, "blockers": blockers}

    # ---------------- quote / build / inspect ----------------
    async def _quote_and_check(
        self, *, amount_lamports: int
    ) -> tuple[SolanaQuote, dict[str, Any]]:
        """Steps 5-6 — quote, then the envelope checks that read the quote.

        The USD band is enforced on the QUOTE's USDC output, not on the SOL
        input: USDC is the dollar-denominated leg (1 USDC ~ 1 USD), so what
        the operator's $5-$10 envelope actually bounds is what comes back. A
        band applied to the SOL side would need a price feed of its own and
        would drift out of the envelope every time SOL moved.
        """
        quote = await self._jupiter.get_quote(amount=amount_lamports)

        problems: list[str] = []
        if quote.swap_mode != "ExactIn":
            # otherAmountThreshold means "minimum output" under ExactIn and
            # "maximum input" under ExactOut. Every downstream guarantee in
            # this lane reads it as the former.
            problems.append(
                f"quote swapMode is {quote.swap_mode!r}, not 'ExactIn' — "
                "otherAmountThreshold would not be a minimum-output bound"
            )
        if quote.in_amount != amount_lamports:
            problems.append(
                f"quote inAmount {quote.in_amount} != requested "
                f"{amount_lamports} lamports"
            )
        approved_bps = int(self._settings.SOLANA_PILOT_SLIPPAGE_BPS)
        if quote.slippage_bps > approved_bps:
            problems.append(
                f"quote slippageBps {quote.slippage_bps} exceeds the approved "
                f"SOLANA_PILOT_SLIPPAGE_BPS={approved_bps}"
            )

        out_usd = usdc_from_raw(quote.out_amount) or Decimal("0")
        min_usd = _dec(self._settings.SOLANA_PILOT_MIN_ORDER_USD)
        max_usd = _dec(self._settings.SOLANA_PILOT_MAX_ORDER_USD)
        if out_usd < min_usd or out_usd > max_usd:
            problems.append(
                f"quoted output {_fmt(out_usd)} USDC is outside the approved "
                f"per-swap band [{_fmt(min_usd)}, {_fmt(max_usd)}] USD"
            )

        impact_pct = quote_price_impact_pct(quote)
        max_impact = _dec(self._settings.SOLANA_PILOT_MAX_PRICE_IMPACT_PCT)
        if impact_pct > max_impact:
            problems.append(
                f"price impact {_fmt(impact_pct)}% exceeds "
                f"SOLANA_PILOT_MAX_PRICE_IMPACT_PCT={_fmt(max_impact)}%"
            )

        detail = {
            "in_amount_lamports": quote.in_amount,
            "in_amount_sol": _fmt(sol_from_lamports(quote.in_amount)),
            "out_amount_raw": quote.out_amount,
            "out_amount_usdc": _fmt(out_usd),
            "min_out_amount_raw": quote.min_out_amount,
            "min_out_amount_usdc": _fmt(usdc_from_raw(quote.min_out_amount)),
            "slippage_bps": quote.slippage_bps,
            "price_impact_pct": _fmt(impact_pct),
            "price_impact_raw": quote.price_impact_pct,
            "swap_mode": quote.swap_mode,
            "route": _route_summary(quote),
            "context_slot": quote.context_slot,
            "band_usd": [_fmt(min_usd), _fmt(max_usd)],
            "problems": problems,
        }
        if problems:
            raise PilotAbort("quote_envelope", "; ".join(problems))
        return quote, detail

    async def _inspect(
        self,
        *,
        build: SwapBuild,
        signer_pubkey: str,
        ata_rent_lamports: int,
        ata_rent_source: str,
    ) -> tuple[VerificationReport, dict[str, Any]]:
        """Step 8 — the inspector's verdict on the bytes Jupiter sent back.

        Three things are handed in rather than left to defaults, and each
        one would silently degrade a check if it were not:

        **The LIVE Jito tip-account list.** The tip check is an exact-address
        comparison and Jito rotates the accounts, so a stale allowlist
        rejects a legitimately-tipped transaction. ``fetch_tip_accounts``
        falls back to the static list on failure, which is a DEGRADED mode —
        the evidence records which list was actually used.

        **A real ``rpc_client``.** Without one, any route that uses address
        lookup tables fails ``lookup_tables_resolved`` by construction, and
        with it the program-allowlist and mint-presence checks that read the
        resolved keys. Jupiter routes commonly use ALTs, so a runner that
        omitted this could never approve one.

        **The live rent figure.** The inspector counts ATA rent against the
        total-fee ceiling. Left to its fallback constant it would price the
        ceiling off a number that is not this cluster's.
        """
        tip_accounts = await self._jito.fetch_tip_accounts()
        # `fetch_tip_accounts` returns the module-level fallback OBJECT when
        # the block engine could not be read, and a fresh frozenset
        # otherwise — so identity, not equality, distinguishes the two.
        tip_source = (
            "static_fallback"
            if tip_accounts is JITO_TIP_ACCOUNTS_FALLBACK
            else "live_block_engine"
        )
        if tip_source == "static_fallback":
            log.warning("solana_pilot_tip_accounts_degraded", count=len(tip_accounts))
        report = await verify_swap_transaction(
            tx_b64=build.swap_transaction_b64,
            expected_signer=signer_pubkey,
            settings=self._settings,
            rpc_client=self._rpc,
            tip_accounts=tip_accounts,
            ata_rent_lamports=ata_rent_lamports,
        )
        detail = {
            "passed": report.passed,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in report.checks
            ],
            "failed_checks": [c.name for c in report.failures],
            "message_sha256": report.message_sha256,
            "fee_payer": report.fee_payer,
            "num_required_signatures": report.num_required_signatures,
            "recent_blockhash": report.recent_blockhash,
            "jito_tip_lamports": report.jito_tip_lamports,
            "jito_tip_destination": report.jito_tip_destination,
            "priority_fee_lamports": report.priority_fee_lamports,
            "total_fee_lamports": report.total_fee_lamports,
            "compute_unit_limit": report.compute_unit_limit,
            "compute_unit_price_micro_lamports": (
                report.compute_unit_price_micro_lamports
            ),
            "program_ids": list(report.program_ids),
            "unresolved_lookup_tables": list(report.unresolved_lookup_tables),
            "tip_accounts_checked": len(tip_accounts),
            "tip_accounts_source": tip_source,
            "ata_create_count": report.ata_create_count,
            "ata_rent_lamports": report.ata_rent_lamports,
            "ata_rent_per_account_lamports": ata_rent_lamports,
            "ata_rent_source": ata_rent_source,
            "rpc_client_supplied": True,
        }
        return report, detail

    async def _ata_rent_lamports(self) -> tuple[int, str]:
        """Rent-exempt minimum for the output ATA, and where the figure came from.

        Asked of the cluster rather than hardcoded, because rent is a cluster
        parameter. The documented fallback keeps the pilot runnable through an
        RPC hiccup, and the source is recorded so an evidence reader can tell
        which number the balance gate actually used.
        """
        try:
            value = await self._rpc.get_minimum_balance_for_rent_exemption(
                TOKEN_ACCOUNT_DATA_LENGTH
            )
        except Exception as exc:
            log.warning(
                "solana_pilot_ata_rent_fallback",
                error_type=type(exc).__name__,
                error=str(exc),
                fallback=ATA_RENT_LAMPORTS_FALLBACK,
            )
            return ATA_RENT_LAMPORTS_FALLBACK, "documented_fallback"
        return int(value), "rpc"

    async def _check_balance(
        self,
        *,
        signer_pubkey: str,
        amount_lamports: int,
        report: VerificationReport,
    ) -> dict[str, Any]:
        """Step 9 — SOL covers the swap, every cost the bytes carry, and headroom.

        ``report.total_fee_lamports`` is the whole outgoing side: base
        signature fee + priority fee + Jito tip + ATA RENT, every term
        re-derived from the transaction's own bytes rather than from what we
        asked Jupiter for. The rent is in there because it is real money
        leaving the wallet — a key that has never held USDC pays the
        rent-exempt minimum for the account the swap creates, and a gate that
        ignored it would pass a transaction the chain then refuses for
        insufficient funds, after the operator had authorized it.

        The rent is therefore NOT added again here. It is counted once, by
        the inspector, from the actual number of ATA-create instructions in
        the transaction — which is also why a wallet that already holds USDC
        is not charged for an account it does not need.
        """
        required = amount_lamports + report.total_fee_lamports
        # 10% headroom over the whole requirement, so a priority-fee market
        # that moves between the build and the block cannot turn a
        # just-affordable swap into an on-chain failure.
        required_with_headroom = int(_dec(required) * Decimal("1.10"))

        sol_lamports = await self._rpc.get_balance(signer_pubkey)
        usdc_raw = await self._rpc.get_token_balance(signer_pubkey, USDC_MINT)

        detail = {
            "sol_balance_lamports": sol_lamports,
            "sol_balance": _fmt(sol_from_lamports(sol_lamports)),
            "usdc_balance_raw": usdc_raw,
            "usdc_balance": _fmt(usdc_from_raw(usdc_raw)),
            "swap_lamports": amount_lamports,
            "total_fee_lamports": report.total_fee_lamports,
            "ata_rent_lamports": report.ata_rent_lamports,
            "ata_create_count": report.ata_create_count,
            "required_lamports": required,
            "required_with_headroom_lamports": required_with_headroom,
            "headroom_pct": 10,
        }
        if sol_lamports < required_with_headroom:
            raise PilotAbort(
                "balance",
                f"SOL balance {_fmt(sol_from_lamports(sol_lamports))} does not "
                f"cover the required {_fmt(sol_from_lamports(required_with_headroom))} "
                f"(swap {_fmt(sol_from_lamports(amount_lamports))} + "
                f"{report.total_fee_lamports} lamports of fees and rent "
                f"[{report.ata_create_count} ATA create(s) = "
                f"{report.ata_rent_lamports} lamports] + 10% headroom)",
            )
        return detail

    # ---------------- place ----------------
    async def place(
        self,
        *,
        sol: Decimal,
        simulate_only: bool = False,
    ) -> int:
        decision_id = str(uuid4())
        evidence = self._evidence_for(decision_id)
        try:
            amount_lamports = lamports_from_sol(sol)
        except ValueError as exc:
            evidence.record("aborted", stage="args", reason=str(exc))
            print(f"REFUSED [args]: {exc}")
            return EXIT_REFUSED

        evidence.record(
            "run_started",
            command="place",
            decision_id=decision_id,
            sol=_fmt(sol),
            amount_lamports=amount_lamports,
            simulate_only=simulate_only,
            evidence_path=str(evidence.path),
            # Which database this run read its safety state from. Without it,
            # an evidence pack cannot distinguish "the kill switch was clear"
            # from "we were looking at the wrong database".
            db_path=str(Path(self._settings.DB_PATH).resolve()),
        )

        live_trade_id: int | None = None
        try:
            envelope = self._check_envelope(simulate_only=simulate_only)
            evidence.record("envelope_gate", **envelope)

            kill = await self._check_kill_switch("kill_switch_check")
            evidence.record("kill_switch_check", **kill)

            keypair, custody = self._custody_check()
            signer_pubkey = custody["signer_pubkey"]
            evidence.record("keypair_custody", **custody)

            reconciliation = await self._reconcile_open_rows(auto_retire=True)
            evidence.record(
                "startup_reconciliation",
                rows=reconciliation["rows"],
                blocker_count=reconciliation["blockers"],
            )
            if reconciliation["blockers"]:
                self._print_blocked(reconciliation)
                raise PilotAbort(
                    "startup_reconciliation",
                    f"{reconciliation['blockers']} unresolved signature(s) block "
                    "the lane",
                    exit_code=EXIT_BLOCKED,
                )

            quote, quote_detail = await self._quote_and_check(
                amount_lamports=amount_lamports
            )
            evidence.record("quote", **quote_detail)

            tip = int(self._settings.SOLANA_PILOT_JITO_TIP_LAMPORTS)
            build = await self._jupiter.build_swap_transaction(
                quote=quote, user_pubkey=signer_pubkey, jito_tip_lamports=tip
            )
            evidence.record(
                "swap_built",
                last_valid_block_height=build.last_valid_block_height,
                requested_jito_tip_lamports=build.requested_jito_tip_lamports,
                prioritization_fee_lamports=build.prioritization_fee_lamports,
                compute_unit_limit=build.compute_unit_limit,
                simulation_error=build.simulation_error,
                tx_b64_len=len(build.swap_transaction_b64),
            )
            if build.simulation_error is not None:
                raise PilotAbort(
                    "swap_built",
                    f"Jupiter reported a simulation error on its own build: "
                    f"{build.simulation_error}",
                )

            # The rent figure is read BEFORE inspection because the
            # inspector prices it into the total-fee ceiling. Asking
            # afterwards would leave the ceiling evaluated against a
            # constant rather than this cluster's actual rent parameters.
            ata_rent, ata_rent_source = await self._ata_rent_lamports()
            report, inspect_detail = await self._inspect(
                build=build,
                signer_pubkey=signer_pubkey,
                ata_rent_lamports=ata_rent,
                ata_rent_source=ata_rent_source,
            )
            evidence.record("tx_inspection", **inspect_detail)
            if not report.passed:
                raise PilotAbort(
                    "tx_inspection",
                    "; ".join(f"{c.name}: {c.detail}" for c in report.failures),
                )

            balance = await self._check_balance(
                signer_pubkey=signer_pubkey,
                amount_lamports=amount_lamports,
                report=report,
            )
            evidence.record("balance", **balance)

            sim = await self._rpc.simulate_transaction(build.swap_transaction_b64)
            evidence.record(
                "simulation",
                ok=sim.ok,
                err=sim.err,
                units_consumed=sim.units_consumed,
                slot=sim.slot,
                logs_tail=list(sim.logs[-20:]),
            )
            if not sim.ok:
                raise PilotAbort(
                    "simulation",
                    f"independent simulation failed against current chain state: "
                    f"err={sim.err}. Submitting anyway would burn a fee to land "
                    "a failure.",
                )

            # Step 11 — LOCAL SIGNING. Nothing leaves the process here; this
            # exists to derive the signature the operator is about to
            # authorize. See the module docstring on the ordering.
            signed = sign_transaction(
                build.swap_transaction_b64, keypair, expected_signer=signer_pubkey
            )
            evidence.record(
                "signed_in_memory",
                expected_signature=signed.signature,
                message_sha256=signed.message_sha256,
                signer_pubkey=signed.signer_pubkey,
                note="signed locally and held in memory; nothing has been sent",
            )
            if signed.message_sha256 != report.message_sha256:
                # The bytes that were inspected must be the bytes that were
                # signed. A divergence means the report on the approval screen
                # describes a different transaction than the signature does.
                raise PilotAbort(
                    "signed_in_memory",
                    f"signed message digest {signed.message_sha256} does not "
                    f"match the inspected digest {report.message_sha256}",
                )

            sol_price = None
            out_usd = usdc_from_raw(quote.out_amount) or Decimal("0")
            if amount_lamports:
                sol_price = out_usd / (_dec(amount_lamports) / _dec(LAMPORTS_PER_SOL))

            if not simulate_only:
                live_trade_id = await record_solana_intent(
                    self._db,
                    decision_id=decision_id,
                    expected_signature=signed.signature,
                    paper_trade_id=await ensure_pilot_anchor(self._db),
                    size_usd=_fmt(out_usd) or "0",
                    sol_price_usdc=_fmt(sol_price),
                )
                # This record is the durable home of lastValidBlockHeight (see
                # the module docstring). It is fsynced before the approval
                # prompt, so a crash at the prompt still leaves the signature
                # resolvable on the next run.
                evidence.record(
                    _INTENT_STEP,
                    live_trade_id=live_trade_id,
                    decision_id=decision_id,
                    expected_signature=signed.signature,
                    last_valid_block_height=build.last_valid_block_height,
                    status="open",
                    note="written BEFORE the approval prompt; the signature is "
                    "the idempotency key and must be durable before anything "
                    "can be submitted",
                )
            else:
                evidence.record(
                    "intent_skipped",
                    reason="--simulate-only rehearsal never submits, so no ledger "
                    "row is written (a row would be a phantom that blocks the "
                    "next run's startup reconciliation)",
                    would_be_expected_signature=signed.signature,
                    would_be_last_valid_block_height=build.last_valid_block_height,
                )

            authorized, outcome = await self._request_authorization(
                decision_id=decision_id,
                signed=signed,
                quote=quote,
                build=build,
                report=report,
                balance=balance,
                sim=sim,
                simulate_only=simulate_only,
            )
            evidence.record(
                "authorization",
                outcome="authorized" if authorized else "authorization_refused",
                detail=outcome,
                expected_prefix_len=8,
                bound_to_signature=signed.signature,
            )
            if not authorized:
                await retire_row(self._db, live_trade_id)
                if live_trade_id is not None:
                    evidence.record(
                        "intent_retired",
                        live_trade_id=live_trade_id,
                        ledger_status="rejected",
                        note="authorization refused before submission — the row "
                        "asserted a transaction that was never sent",
                    )
                raise PilotAbort(
                    "authorization",
                    f"operator did not authorize ({outcome}) — nothing was sent",
                )

            if simulate_only:
                evidence.record(
                    "rehearsal_complete",
                    would_be_expected_signature=signed.signature,
                    simulation_ok=sim.ok,
                    units_consumed=sim.units_consumed,
                    note="--simulate-only: the submission step is skipped "
                    "entirely; no transaction was sent and no ledger row exists",
                )
                _print_block(
                    "REHEARSAL COMPLETE — nothing was submitted",
                    [
                        f"  decision ID        : {decision_id}",
                        f"  would-be signature : {signed.signature}",
                        f"  simulation         : ok, "
                        f"{sim.units_consumed} compute units",
                        f"  evidence           : {evidence.path}",
                    ],
                )
                return EXIT_OK

            # Step 14 — post-approval kill re-check. Handled inline rather than
            # through PilotAbort because the intent row already exists and must
            # be retired: it asserts "a transaction may exist" and nothing was
            # sent.
            kill_state = await self._ks.is_active()
            if kill_state is not None:
                await retire_row(self._db, live_trade_id, reject_reason="kill_switch")
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

            freshness = await self._recheck_blockhash(build=build)
            evidence.record("blockhash_recheck", **freshness)
            if not freshness["valid"]:
                await retire_row(self._db, live_trade_id)
                evidence.record(
                    "authorization_invalidated",
                    live_trade_id=live_trade_id,
                    ledger_status="rejected",
                    note="the authorization was given for numbers that went "
                    "stale before submission",
                )
                _print_block(
                    "AUTHORIZATION INVALIDATED — nothing was sent",
                    [
                        f"  {freshness['detail']}",
                        "",
                        "  The quote, the transaction and the signature you",
                        "  authorized are all stale. Nothing was submitted and the",
                        "  ledger row is rejected.",
                        "",
                        "  Rerun the command. The next run does the whole loop",
                        "  again: a NEW quote, a NEW signature, and a NEW",
                        "  authorization bound to that new signature.",
                        f"  evidence: {evidence.path}",
                    ],
                )
                return EXIT_REFUSED

            return await self._submit_and_resolve(
                evidence=evidence,
                decision_id=decision_id,
                live_trade_id=live_trade_id,
                signed=signed,
                build=build,
                quote=quote,
                report=report,
                balance=balance,
                amount_lamports=amount_lamports,
                signer_pubkey=signer_pubkey,
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
            # rather than a traceback: this can fire AFTER a transaction
            # exists, and a stack trace on stderr is not an evidence record.
            # The ledger row is deliberately left as it stands — whatever it
            # says is what was last known to be true.
            log.error(
                "solana_pilot_unexpected_error",
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
                    "  Confirm the on-chain state before any further action.",
                    "  NEVER rebuild — resolve the persisted signature.",
                    f"  evidence        : {evidence.path}",
                ],
            )
            return EXIT_ESCALATE

    async def _recheck_blockhash(self, *, build: SwapBuild) -> dict[str, Any]:
        """Step 15 — is the authorized build still landable?

        The approval prompt is unbounded operator time, and a Solana build is
        perishable: it carries a blockhash that stops being valid once the
        chain passes ``lastValidBlockHeight``. Submitting past that point
        cannot land, and submitting just short of it very likely cannot either,
        so the check refuses inside a configurable safety margin.

        An RPC failure here counts as INVALID. We cannot show the transaction
        is still fresh, and the whole point of the re-check is that a stale
        authorization must not be spent.
        """
        margin = int(self._settings.SOLANA_PILOT_BLOCKHASH_SAFETY_MARGIN_BLOCKS)
        deadline = build.last_valid_block_height - margin
        try:
            height = await self._rpc.get_block_height()
        except Exception as exc:
            return {
                "valid": False,
                "current_block_height": None,
                "last_valid_block_height": build.last_valid_block_height,
                "safety_margin_blocks": margin,
                "detail": (
                    f"could not read the current block height "
                    f"({type(exc).__name__}: {exc}) — refusing to submit a build "
                    "whose freshness cannot be shown"
                ),
            }
        valid = height <= deadline
        return {
            "valid": valid,
            "current_block_height": height,
            "last_valid_block_height": build.last_valid_block_height,
            "safety_margin_blocks": margin,
            "blocks_remaining": build.last_valid_block_height - height,
            "detail": (
                f"block height {height} of {build.last_valid_block_height} "
                f"(margin {margin})"
                if valid
                else f"block height {height} is past the safe submission "
                f"deadline {deadline} "
                f"(lastValidBlockHeight {build.last_valid_block_height} minus a "
                f"{margin}-block margin)"
            ),
        }

    async def _request_authorization(
        self,
        *,
        decision_id: str,
        signed: SignedTransaction,
        quote: SolanaQuote,
        build: SwapBuild,
        report: VerificationReport,
        balance: dict[str, Any],
        sim: Any,
        simulate_only: bool,
    ) -> tuple[bool, str]:
        """Step 13 — print the decision block and require the typed prefix.

        The phrase is the first 8 characters of the EXPECTED TRANSACTION
        SIGNATURE, not of the decision id. That is what binds the
        authorization to one exact transaction: any rebuild produces different
        bytes, therefore a different signature, therefore a different phrase,
        so an authorization can never be carried across a rebuild.

        Every figure here is a value read this run. The numeric lines use
        ASCII operators (``->``) rather than typographic ones: this block is
        read on whatever console the operator has, and a cp1252 terminal
        renders a stray arrow as a mojibake glyph between two money figures.
        """
        out_usd = usdc_from_raw(quote.out_amount)
        min_out_usd = usdc_from_raw(quote.min_out_amount)
        impact = quote_price_impact_pct(quote)
        # Blockhash age is read HERE, at display time, rather than carried
        # forward from the build. The operator is being asked "is this
        # transaction still landable", and a number computed a minute ago
        # answers a question about a moment that has passed.
        age = await self._blockhash_age(build)
        lines = [
            f"  pair                : SOL -> USDC",
            f"  input mint          : {quote.input_mint}",
            f"  output mint         : {quote.output_mint}",
            f"  exact input         : {_fmt(sol_from_lamports(quote.in_amount))} SOL "
            f"({quote.in_amount} lamports)",
            f"  expected output     : {_fmt(out_usd)} USDC "
            f"({quote.out_amount} raw)",
            f"  minimum acceptable  : {_fmt(min_out_usd)} USDC "
            f"({quote.min_out_amount} raw, otherAmountThreshold; this is the "
            f"bound enforced on-chain)",
            f"  slippage            : {quote.slippage_bps} bps",
            f"  price impact        : {_fmt(impact)}%",
            f"  route               : {_route_summary(quote)}",
            f"  priority fee        : {report.priority_fee_lamports} lamports",
            f"  jito tip            : {report.jito_tip_lamports} lamports "
            f"-> {report.jito_tip_destination}",
            f"  max total fee       : {report.total_fee_lamports} lamports "
            f"(base + priority + tip, of "
            f"{self._settings.SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS} allowed)",
            f"  blockhash           : {report.recent_blockhash}",
            f"  blockhash age       : {age}",
            f"  simulation          : err={sim.err}, "
            f"{sim.units_consumed} compute units consumed",
            f"  signer              : {signed.signer_pubkey}",
            f"  current SOL         : {balance['sol_balance']} SOL",
            f"  current USDC        : {balance['usdc_balance']} USDC",
            f"  message sha256      : {signed.message_sha256}",
            f"  EXPECTED SIGNATURE  : {signed.signature}",
            f"  database            : {Path(self._settings.DB_PATH).resolve()}",
            f"  decision ID         : {decision_id}",
        ]
        if simulate_only:
            lines.append(
                "  ** REHEARSAL        : --simulate-only — the submission step "
                "is skipped entirely and NOTHING is sent"
            )
        _print_block("SOLANA SUPERVISED PILOT — MANUAL APPROVAL REQUIRED", lines)
        print(
            "Type the first 8 characters of the EXPECTED SIGNATURE to authorize. "
            "Anything else — including an empty line or a closed stdin — aborts. "
            "The phrase is case-sensitive."
        )
        return read_authorization(signed.signature[:8])

    async def _blockhash_age(self, build: SwapBuild) -> str:
        """One display line: how many blocks of life the build has left.

        Never raises. If the height cannot be read the line says so — an
        approval screen that silently omits a field the operator is meant to
        weigh is worse than one that admits the gap. The binding check runs
        after authorization in ``_recheck_blockhash``, which fails closed on
        the same error.
        """
        try:
            height = await self._rpc.get_block_height()
        except Exception as exc:
            return (
                f"UNKNOWN — could not read the current block height "
                f"({type(exc).__name__}); lastValidBlockHeight "
                f"{build.last_valid_block_height}"
            )
        return (
            f"{build.last_valid_block_height - height} blocks remaining "
            f"(height {height} of lastValidBlockHeight "
            f"{build.last_valid_block_height})"
        )

    # ---------------- submit ----------------
    async def _submit_and_resolve(
        self,
        *,
        evidence: EvidenceLog,
        decision_id: str,
        live_trade_id: int | None,
        signed: SignedTransaction,
        build: SwapBuild,
        quote: SolanaQuote,
        report: VerificationReport,
        balance: dict[str, Any],
        amount_lamports: int,
        signer_pubkey: str,
    ) -> int:
        """Steps 16-18 — submit exactly once, then decide what happened."""
        try:
            receipt = await self._jito.submit_transaction(
                signed.signed_tx_b64,
                expected_signature=signed.signature,
                last_valid_block_height=build.last_valid_block_height,
                bundle_only=True,
            )
        except SolanaAmbiguousSubmissionError as exc:
            evidence.record(
                "submission_ambiguous",
                error_type=type(exc).__name__,
                error=str(exc),
                expected_signature=exc.expected_signature,
                note="NEVER rebuild — resolving the persisted signature instead",
            )
            code, _resolution = await self._handle_ambiguity(
                evidence=evidence,
                decision_id=decision_id,
                live_trade_id=live_trade_id,
                signed=signed,
                build=build,
                signer_pubkey=signer_pubkey,
            )
            if code is not None:
                return code
        except SolanaAPIError as exc:
            # A JSON-RPC error member IS the block engine deciding. Definitive
            # — the transaction was not accepted, so nothing is outstanding.
            await retire_row(self._db, live_trade_id, reject_reason="venue_unavailable")
            evidence.record(
                "submission_refused",
                error_type=type(exc).__name__,
                error=str(exc),
                live_trade_id=live_trade_id,
                ledger_status=None if live_trade_id is None else "rejected",
                reject_reason="venue_unavailable",
            )
            print(f"REFUSED [submit]: {type(exc).__name__}: {exc}")
            print(f"evidence: {evidence.path}")
            return EXIT_REFUSED
        else:
            evidence.record(
                "submitted",
                expected_signature=receipt.signature,
                returned_signature=receipt.returned_signature,
                bundle_id=receipt.bundle_id,
                bundle_only=receipt.bundle_only,
            )

        return await self._await_and_reconcile(
            evidence=evidence,
            decision_id=decision_id,
            live_trade_id=live_trade_id,
            signed=signed,
            build=build,
            quote=quote,
            report=report,
            balance=balance,
            amount_lamports=amount_lamports,
            signer_pubkey=signer_pubkey,
        )

    async def _handle_ambiguity(
        self,
        *,
        evidence: EvidenceLog,
        decision_id: str,
        live_trade_id: int | None,
        signed: SignedTransaction,
        build: SwapBuild,
        signer_pubkey: str,
    ) -> tuple[int | None, ResolutionReport]:
        """Turn an ambiguous submission into a decided state.

        Returns ``(exit_code, resolution)`` — an exit code when the run is
        over, or ``(None, resolution)`` when the transaction turns out to have
        landed and the caller should continue into the confirmation path.

        No branch here rebuilds or resubmits. A rebuild produces a different
        blockhash and a different signature, and BOTH can land — two swaps
        where the operator authorized one.
        """
        resolution = await resolve_submission(
            expected_signature=signed.signature,
            last_valid_block_height=build.last_valid_block_height,
            rpc_client=self._resolver_rpc,
            settings=self._settings,
            owner_pubkey=signer_pubkey,
        )
        evidence.record("ambiguity_resolution", **_resolution_summary(resolution))

        if resolution.verdict == "landed":
            evidence.record(
                "ambiguity_adopted",
                signature=signed.signature,
                slot=resolution.slot,
                confirmation_status=resolution.confirmation_status,
                note="the transaction DID land — adopting it rather than " "rebuilding",
            )
            return None, resolution

        if resolution.verdict == "failed_on_chain":
            await retire_row(self._db, live_trade_id)
            evidence.record(
                "ambiguity_failed_on_chain",
                live_trade_id=live_trade_id,
                ledger_status="rejected",
                on_chain_err=resolution.on_chain_err,
                note="the transaction ran and failed; the fee was paid and the "
                "outcome is final. Nothing to resubmit.",
            )
            _print_block(
                "TRANSACTION FAILED ON CHAIN — no position, fee paid",
                [
                    f"  signature   : {signed.signature}",
                    f"  on-chain err: {resolution.on_chain_err}",
                    f"  slot        : {resolution.slot}",
                    "  The swap did not execute. The transaction fee was still",
                    "  charged. The ledger row is rejected.",
                    f"  evidence    : {evidence.path}",
                ],
            )
            return EXIT_REFUSED, resolution

        if resolution.verdict == "definitively_not_submitted":
            await retire_row(self._db, live_trade_id, reject_reason="venue_unavailable")
            evidence.record(
                "ambiguity_not_submitted",
                live_trade_id=live_trade_id,
                ledger_status="rejected",
                note="signature absent from the cluster AND its blockhash has "
                "expired, so it can never land",
            )
            _print_block(
                "SUBMISSION DID NOT LAND — no transaction exists",
                [
                    f"  signature : {signed.signature}",
                    "  The signature is absent from the cluster and its blockhash",
                    "  has expired, so it can never land. The ledger row is",
                    "  rejected and the lane is clear.",
                    "",
                    "  Rerunning is safe. It will build a NEW transaction with a",
                    "  new signature and ask for a new authorization.",
                    f"  evidence  : {evidence.path}",
                ],
            )
            return EXIT_REFUSED, resolution

        # unresolved — the dangerous one. STOP. Never rebuild.
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
            "ESCALATE — SUBMISSION UNRESOLVED, DO NOT REBUILD",
            [
                "  The cluster could not tell us whether this transaction",
                "  exists. It may still be in flight.",
                f"  signature       : {signed.signature}",
                f"  decision ID     : {decision_id}",
                f"  live_trades row : {live_trade_id} (needs_manual_review)",
                "",
                "  *** DO NOT REBUILD AND DO NOT RESUBMIT. ***",
                "  A rebuild produces a different blockhash and a different",
                "  signature, and BOTH can land — two swaps where one was",
                "  authorized.",
                "",
                "  The pilot lane is now BLOCKED: every future `place` run will",
                "  refuse at startup reconciliation until this row resolves.",
                "",
                "  Next steps, in order:",
                "   1. Re-run the resolver as the blockhash expires:",
                "        python -m scout.live.solana_pilot resolve "
                f"--decision-id {decision_id}",
                "   2. Once it reports definitively_not_submitted the row is",
                "      retired automatically and the lane clears.",
                "   3. If it reports landed, you hold USDC — dispose of it and",
                "      resolve the row by hand.",
                f"   4. Evidence: {evidence.path}",
            ],
        )
        return EXIT_ESCALATE, resolution

    async def _await_confirmation(self, signature: str) -> dict[str, Any]:
        """Poll the cluster to 'confirmed', then to 'finalized'.

        Two waits rather than one, because they answer different questions.
        'confirmed' says a supermajority voted on the block; 'finalized' says
        it is rooted and cannot be rolled back. A supervised pilot reports the
        second, so a report of a completed swap is not something a fork can
        take back.

        Returns an outcome dict; never raises. A poll error is recorded and
        the loop continues, because one unreachable RPC read is not evidence
        about the transaction.
        """
        poll = float(self._settings.SOLANA_PILOT_POLL_INTERVAL_SEC)
        confirm_deadline = time.monotonic() + float(
            self._settings.SOLANA_PILOT_CONFIRM_TIMEOUT_SEC
        )
        finalize_deadline = confirm_deadline + float(
            self._settings.SOLANA_PILOT_FINALIZE_TIMEOUT_SEC
        )
        polls = 0
        last_status: Any = None
        errors: list[str] = []
        seen_known = False
        outcome = "confirm_timeout"

        while True:
            polls += 1
            try:
                statuses = await self._rpc.get_signature_statuses([signature])
                last_status = statuses[0] if statuses else None
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                last_status = None
            else:
                if last_status is not None and last_status.known:
                    seen_known = True
                    if last_status.err is not None:
                        outcome = "failed_on_chain"
                        break
                    if last_status.confirmation_status == "finalized":
                        outcome = "finalized"
                        break
                    # Landed but not yet rooted: keep polling to the finalize
                    # deadline rather than reporting a reversible outcome.
                    if time.monotonic() >= finalize_deadline:
                        outcome = "finalize_timeout"
                        break
                    await asyncio.sleep(poll)
                    continue

            if time.monotonic() >= confirm_deadline:
                outcome = "confirm_timeout"
                break
            await asyncio.sleep(poll)

        if outcome == "confirm_timeout" and seen_known:
            # We DID see the cluster acknowledge this signature; only the
            # later polls stopped answering. Calling that a confirmation
            # timeout would send the caller off to resolve a transaction we
            # already know landed, and would read in the evidence as "never
            # seen" when the opposite was observed.
            outcome = "finalize_timeout"

        return {
            "outcome": outcome,
            "polls": polls,
            "poll_errors": errors,
            "known": bool(last_status is not None and last_status.known),
            "slot": None if last_status is None else last_status.slot,
            "confirmation_status": (
                None if last_status is None else last_status.confirmation_status
            ),
            "err": None if last_status is None else last_status.err,
            "confirm_timeout_sec": float(
                self._settings.SOLANA_PILOT_CONFIRM_TIMEOUT_SEC
            ),
            "finalize_timeout_sec": float(
                self._settings.SOLANA_PILOT_FINALIZE_TIMEOUT_SEC
            ),
        }

    async def _await_and_reconcile(
        self,
        *,
        evidence: EvidenceLog,
        decision_id: str,
        live_trade_id: int | None,
        signed: SignedTransaction,
        build: SwapBuild,
        quote: SolanaQuote,
        report: VerificationReport,
        balance: dict[str, Any],
        amount_lamports: int,
        signer_pubkey: str,
    ) -> int:
        """Steps 17-18 — wait for finality, then reconcile the balances."""
        print(
            "submitted; waiting for confirmation then finalization, up to "
            f"{self._settings.SOLANA_PILOT_CONFIRM_TIMEOUT_SEC:g}s + "
            f"{self._settings.SOLANA_PILOT_FINALIZE_TIMEOUT_SEC:g}s ..."
        )
        confirmation = await self._await_confirmation(signed.signature)
        evidence.record("confirmation", **confirmation)

        if confirmation["outcome"] == "failed_on_chain":
            await retire_row(self._db, live_trade_id)
            evidence.record(
                "failed_on_chain",
                live_trade_id=live_trade_id,
                ledger_status="rejected",
                err=confirmation["err"],
                note="the transaction executed and failed; the fee was paid and "
                "no position exists",
            )
            _print_block(
                "TRANSACTION FAILED ON CHAIN — no position, fee paid",
                [
                    f"  signature   : {signed.signature}",
                    f"  on-chain err: {confirmation['err']}",
                    f"  slot        : {confirmation['slot']}",
                    f"  evidence    : {evidence.path}",
                ],
            )
            return EXIT_REFUSED

        if confirmation["outcome"] == "confirm_timeout":
            # Never seen by the cluster inside the window. That is exactly the
            # question the resolver answers, and it is NOT a licence to
            # rebuild.
            evidence.record(
                "confirmation_timeout",
                note="signature not seen inside the confirmation window — "
                "resolving rather than assuming",
            )
            code, resolution = await self._handle_ambiguity(
                evidence=evidence,
                decision_id=decision_id,
                live_trade_id=live_trade_id,
                signed=signed,
                build=build,
                signer_pubkey=signer_pubkey,
            )
            if code is not None:
                return code
            # Adopted: the resolver found the transaction the confirmation
            # poll never saw. Carry its facts forward so the reconciliation
            # and the final block describe the landing rather than the
            # timeout that preceded it.
            confirmation["outcome"] = "landed_after_resolution"
            confirmation["slot"] = resolution.slot
            confirmation["confirmation_status"] = resolution.confirmation_status

        transaction = None
        try:
            transaction = await self._rpc.get_transaction(signed.signature)
        except Exception as exc:
            evidence.record(
                "get_transaction_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        meta = (transaction or {}).get("meta") or {}
        evidence.record(
            "transaction_record",
            found=transaction is not None,
            slot=(transaction or {}).get("slot"),
            fee_lamports=meta.get("fee"),
            on_chain_err=meta.get("err"),
        )

        summary = await self._reconcile(
            evidence=evidence,
            live_trade_id=live_trade_id,
            decision_id=decision_id,
            signature=signed.signature,
            quote=quote,
            report=report,
            balance=balance,
            amount_lamports=amount_lamports,
            signer_pubkey=signer_pubkey,
            confirmation=confirmation,
            transaction=transaction,
        )

        lines = [
            f"  decision ID  : {decision_id}",
            f"  signature    : {signed.signature}",
            f"  status       : {confirmation['outcome']} "
            f"({confirmation['confirmation_status']})",
            f"  slot         : {confirmation['slot']}",
            f"  SOL spent    : {summary['balance']['sol_spent']} SOL",
            f"  USDC received: {summary['balance']['usdc_received']} USDC",
            f"  reconcile    : {summary['verdict']}",
            f"  ledger row   : {live_trade_id} (open — the position is yours to "
            f"dispose of)",
            f"  evidence     : {evidence.path}",
        ]
        if summary["verdict"] == "review":
            lines.extend(
                ["", "  RECONCILIATION COULD NOT EXPLAIN THE BALANCE MOVE:"]
                + [f"   - {m}" for m in summary["mismatches"]]
                + ["  Check the wallet before running the pilot again."]
            )
        _print_block(f"PILOT SWAP {confirmation['outcome'].upper()}", lines)
        # An unexplained balance move exits non-zero even though the swap
        # itself completed: money moved in a way the runner cannot account
        # for, and a 0 here would tell a wrapper script everything is fine.
        return EXIT_REVIEW if summary["verdict"] == "review" else EXIT_OK

    async def _reconcile(
        self,
        *,
        evidence: EvidenceLog,
        live_trade_id: int | None,
        decision_id: str,
        signature: str,
        quote: SolanaQuote,
        report: VerificationReport,
        balance: dict[str, Any],
        amount_lamports: int,
        signer_pubkey: str,
        confirmation: dict[str, Any],
        transaction: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Compare the chain's view, the ledger row and the balance deltas.

        The USDC floor is ``otherAmountThreshold``, not ``outAmount``: the
        former is what the on-chain program enforces, the latter is an
        estimate, and reconciling against an estimate would report a review on
        every swap that filled inside its slippage band.

        The SOL side is a RANGE rather than a point. The swap amount is exact,
        the fees are known from the inspected bytes, and the ATA rent is
        conditional — it leaves the wallet only if the output token account had
        to be created — so anything from "swap + fees" to "swap + fees + rent"
        is fully explained. Outside that range, the runner says so rather than
        silently accepting a move it cannot account for.
        """
        sol_after = None
        usdc_after = None
        probe_errors: list[str] = []
        try:
            sol_after = await self._rpc.get_balance(signer_pubkey)
        except Exception as exc:
            probe_errors.append(f"get_balance: {type(exc).__name__}: {exc}")
        try:
            usdc_after = await self._rpc.get_token_balance(signer_pubkey, USDC_MINT)
        except Exception as exc:
            probe_errors.append(f"get_token_balance: {type(exc).__name__}: {exc}")

        sol_before = int(balance["sol_balance_lamports"])
        usdc_before = int(balance["usdc_balance_raw"])
        ata_rent = int(balance["ata_rent_lamports"])

        sol_spent = None if sol_after is None else sol_before - sol_after
        usdc_received = None if usdc_after is None else usdc_after - usdc_before

        mismatches: list[str] = list(probe_errors)

        # *** This comparison IS the slippage guarantee. ***
        # tx_inspector does not and cannot check the minimum-output bound:
        # the route and its otherAmountThreshold are encoded inside
        # Jupiter's instruction data, which is an opaque blob to a static
        # inspector. What protects the trade is Jupiter's on-chain program
        # enforcing that threshold, and THIS check confirming after the fact
        # that it did. Recorded as an explicit boolean rather than inferred
        # from the absence of a mismatch, so an evidence reader can see the
        # guarantee was tested rather than assumed.
        meets_minimum_output: bool | None = None
        if usdc_received is None:
            mismatches.append("no post-trade USDC balance to compare against")
        else:
            meets_minimum_output = usdc_received >= quote.min_out_amount
            if not meets_minimum_output:
                mismatches.append(
                    f"received {usdc_received} raw USDC, below the on-chain "
                    f"minimum {quote.min_out_amount} (otherAmountThreshold) — "
                    "the slippage bound did not hold"
                )

        # Tolerance of one base signature fee absorbs dust from the
        # wrap/unwrap round trip without widening the band enough to hide a
        # real discrepancy. Deliberately a fixed protocol constant rather than
        # anything derived from the fees: a slack that scaled with the tip
        # would widen the band exactly when the transaction got expensive,
        # which is when an unexplained move matters most.
        slack = LAMPORTS_PER_SIGNATURE
        # The band spans "the swap alone" to "the swap plus everything the
        # inspected bytes can charge". Its width is the rent, and that is not
        # slop: rent is a rent-exempt DEPOSIT, and the temporary wSOL account
        # Jupiter opens is closed back to the owner inside the same
        # transaction — so whether a given account's rent comes back is a
        # property of the route, not something knowable here. Both ends are
        # explained; anything outside is not.
        #
        # ``report.total_fee_lamports`` ALREADY includes the ATA rent, so it is
        # not added a second time.
        low = amount_lamports - slack
        high = amount_lamports + report.total_fee_lamports + slack
        if sol_spent is None:
            mismatches.append("no post-trade SOL balance to compare against")
        elif not (low <= sol_spent <= high):
            mismatches.append(
                f"SOL spent {sol_spent} lamports is outside the explained range "
                f"[{low}, {high}] (swap {amount_lamports} + fees and rent "
                f"{report.total_fee_lamports}, of which {ata_rent} is "
                f"recoverable ATA rent)"
            )

        on_chain_err = ((transaction or {}).get("meta") or {}).get("err")
        if on_chain_err is not None:
            mismatches.append(f"the finalized transaction carries err={on_chain_err}")
        if confirmation["outcome"] == "finalize_timeout":
            mismatches.append(
                "the transaction confirmed but was not finalized inside the "
                "window — it is landed but not yet rooted"
            )

        ledger = None
        if live_trade_id is not None:
            if usdc_received is not None and usdc_received > 0:
                sol_units = _dec(amount_lamports) / _dec(LAMPORTS_PER_SOL)
                usdc_units = usdc_from_raw(usdc_received) or Decimal("0")
                await update_live_trade(
                    self._db,
                    live_trade_id,
                    entry_fill_qty=_fmt(sol_units),
                    entry_fill_price=_fmt(
                        usdc_units / sol_units if sol_units else Decimal("0")
                    ),
                )
            ledger = await fetch_row_by_decision_id(self._db, decision_id)
            if ledger is None:
                mismatches.append("ledger row could not be re-read after the swap")
            elif ledger.get("entry_order_id") != signature:
                mismatches.append(
                    f"ledger entry_order_id {ledger.get('entry_order_id')!r} does "
                    f"not match the submitted signature {signature!r}"
                )

        summary = {
            "verdict": "review" if mismatches else "pass",
            "mismatches": mismatches,
            "chain": {
                "signature": signature,
                "slot": confirmation["slot"],
                "confirmation_status": confirmation["confirmation_status"],
                "fee_lamports": ((transaction or {}).get("meta") or {}).get("fee"),
                "on_chain_err": on_chain_err,
                "transaction_found": transaction is not None,
            },
            "ledger": ledger,
            "balance": {
                "sol_before_lamports": sol_before,
                "sol_after_lamports": sol_after,
                "sol_spent_lamports": sol_spent,
                "sol_spent": _fmt(sol_from_lamports(sol_spent)),
                "sol_spent_explained_range": [low, high],
                "usdc_before_raw": usdc_before,
                "usdc_after_raw": usdc_after,
                "usdc_received_raw": usdc_received,
                "usdc_received": _fmt(usdc_from_raw(usdc_received)),
                "usdc_minimum_raw": quote.min_out_amount,
                "usdc_minimum": _fmt(usdc_from_raw(quote.min_out_amount)),
                "usdc_quoted_raw": quote.out_amount,
                # The slippage guarantee, stated rather than implied.
                "meets_minimum_output": meets_minimum_output,
            },
        }
        evidence.record("reconciliation", **summary)
        if mismatches:
            log.warning("solana_pilot_reconciliation_review", mismatches=mismatches)
        return summary

    # ---------------- resolve ----------------
    async def resolve(self, *, decision_id: str) -> int:
        """Put one persisted signature to the cluster and report the verdict.

        Read-only except for the one safe mutation: a
        ``definitively_not_submitted`` verdict retires the row, because that
        verdict means the transaction can never land and the lane is genuinely
        clear. Every other verdict leaves the row exactly as it is.

        Deliberately NOT gated on the kill switch or on SOLANA_PILOT_ENABLED.
        Turning the master gate off is an operator's instinctive first move
        when something looks wrong, and it must not take away the tool that
        explains what happened.
        """
        evidence = self._evidence_for(decision_id)
        evidence.record("run_started", command="resolve", decision_id=decision_id)
        try:
            row = await fetch_row_by_decision_id(self._db, decision_id)
            if row is None:
                raise PilotAbort(
                    "lookup",
                    f"no solana live_trades row with client_order_id={decision_id}",
                )
            evidence.record("ledger_row", **row)

            signature = row.get("entry_order_id")
            if not signature:
                raise PilotAbort(
                    "lookup",
                    f"live_trades row {row['live_trade_id']} carries no expected "
                    "signature, so there is nothing to ask the cluster about",
                    exit_code=EXIT_ESCALATE,
                )

            intent = read_persisted_intent(
                self._settings.SOLANA_PILOT_EVIDENCE_DIR, decision_id
            )
            last_valid = (
                None if intent is None else intent.get("last_valid_block_height")
            )
            resolver_url, pinned = resolver_endpoint(self._settings)
            resolution = await resolve_submission(
                expected_signature=str(signature),
                last_valid_block_height=last_valid,
                rpc_client=self._resolver_rpc,
                settings=self._settings,
            )
            evidence.record(
                "resolution",
                **_resolution_summary(resolution),
                last_valid_block_height_source=(
                    "evidence_file" if intent is not None else "unavailable"
                ),
                resolver_rpc_url=resolver_url,
                resolver_endpoint_pinned=pinned,
            )

            retired = False
            if (
                resolution.verdict == "definitively_not_submitted"
                and row["status"] in _BLOCKING_STATUSES
            ):
                if pinned:
                    await retire_row(
                        self._db,
                        row["live_trade_id"],
                        reject_reason="venue_unavailable",
                    )
                    retired = True
                    evidence.record(
                        "row_auto_retired",
                        live_trade_id=row["live_trade_id"],
                        ledger_status="rejected",
                        note="the transaction can never land, so the row asserts "
                        "nothing that is still true",
                    )
                else:
                    # Reporting a verdict is always allowed; ACTING on this one
                    # is not, because a load-balanced read is exactly how a
                    # false 'never submitted' is manufactured. The row stays.
                    evidence.record(
                        "auto_retire_withheld",
                        live_trade_id=row["live_trade_id"],
                        resolver_rpc_url=resolver_url,
                        note="verdict read from a load-balanced endpoint; the "
                        "row is NOT retired on it",
                    )

            lines = [
                f"  decision ID : {decision_id}",
                f"  signature   : {signature}",
                f"  row         : #{row['live_trade_id']} ({row['status']})",
                f"  verdict     : {resolution.verdict}",
                f"  slot        : {resolution.slot}",
                f"  confirmation: {resolution.confirmation_status}",
                f"  on-chain err: {resolution.on_chain_err}",
                f"  lastValidBH : {last_valid}"
                + ("" if intent is not None else "  (evidence file unreadable)"),
                f"  sweeps      : {len(resolution.probes)}",
                f"  resolver RPC: {resolver_url}"
                + ("" if pinned else "   ** NOT PINNED - verdict is advisory **"),
                f"  evidence    : {evidence.path}",
            ]
            lines.extend(_verdict_guidance(resolution.verdict, retired))
            if resolution.verdict == "definitively_not_submitted" and not pinned:
                lines.extend(
                    [
                        "",
                        "  The row was NOT retired. This verdict came from a",
                        "  load-balanced endpoint, where one node ahead on block",
                        "  height and missing the signature is enough to invent",
                        "  it. Set SOLANA_RESOLVER_RPC_URL to a dedicated node",
                        "  and run this again before acting.",
                    ]
                )
            _print_block("SOLANA PILOT RESOLUTION", lines)
            if resolution.verdict == "landed":
                return EXIT_BLOCKED
            if resolution.verdict == "definitively_not_submitted":
                return EXIT_OK
            if resolution.verdict == "failed_on_chain":
                return EXIT_BLOCKED
            return EXIT_ESCALATE

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
            log.error(
                "solana_pilot_unexpected_error",
                decision_id=decision_id,
                command="resolve",
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=True,
            )
            evidence.record(
                "unexpected_error", error_type=type(exc).__name__, error=str(exc)
            )
            print(f"ESCALATE [resolve]: {type(exc).__name__}: {exc}")
            return EXIT_ESCALATE

    # ---------------- status ----------------
    async def status(self) -> int:
        """Read-only lane report. Writes no evidence file — nothing decided."""
        settings = self._settings
        lines: list[str] = [
            f"  SOLANA_PILOT_ENABLED     : {settings.SOLANA_PILOT_ENABLED}",
            f"  pair                     : {PAIR} (mainnet, fixed)",
            f"  per-swap band            : {settings.SOLANA_PILOT_MIN_ORDER_USD} - "
            f"{settings.SOLANA_PILOT_MAX_ORDER_USD} USD on the USDC output",
            f"  slippage ceiling         : {settings.SOLANA_PILOT_SLIPPAGE_BPS} bps",
            f"  jito tip requested       : "
            f"{settings.SOLANA_PILOT_JITO_TIP_LAMPORTS} lamports",
            f"  rpc (read-only)          : {settings.SOLANA_RPC_URL}",
            f"  resolver rpc             : {resolver_endpoint(settings)[0]}"
            + (
                ""
                if resolver_endpoint(settings)[1]
                else "   ** NOT PINNED - place will refuse **"
            ),
            f"  block engine (submit)    : {settings.JITO_BLOCK_ENGINE_URL}",
            f"  database                 : {Path(settings.DB_PATH).resolve()}",
        ]

        kill_state = await self._ks.is_active()
        lines.append(
            "  kill switch              : "
            + (
                "INACTIVE"
                if kill_state is None
                else f"ACTIVE (#{kill_state.kill_event_id}, {kill_state.reason})"
            )
        )

        signer_pubkey: str | None = None
        try:
            keypair = self._load_keypair()
        except SolanaKeypairError as exc:
            lines.append(f"  keypair custody          : REFUSED — {exc}")
        except Exception as exc:  # pragma: no cover - loader contract is typed
            lines.append(
                f"  keypair custody          : FAILED {type(exc).__name__}: {exc}"
            )
        else:
            signer_pubkey = str(keypair.pubkey())
            lines.append(f"  keypair custody          : ok, signer {signer_pubkey}")

        if signer_pubkey is not None:
            try:
                lamports = await self._rpc.get_balance(signer_pubkey)
                lines.append(
                    f"  SOL balance              : "
                    f"{_fmt(sol_from_lamports(lamports))} ({lamports} lamports)"
                )
            except Exception as exc:
                lines.append(
                    f"  SOL balance              : unavailable "
                    f"({type(exc).__name__})"
                )
            try:
                raw = await self._rpc.get_token_balance(signer_pubkey, USDC_MINT)
                lines.append(
                    f"  USDC balance             : {_fmt(usdc_from_raw(raw))} "
                    f"({raw} raw)"
                )
            except Exception as exc:
                lines.append(
                    f"  USDC balance             : unavailable "
                    f"({type(exc).__name__})"
                )

        reconciliation = await self._reconcile_open_rows(auto_retire=False)
        rows = reconciliation["rows"]
        lines.append(f"  outstanding ledger rows  : {len(rows)}")
        for row in rows:
            resolution = row.get("resolution", {})
            lines.append(
                f"    #{row['live_trade_id']} {row['status']} "
                f"cid={row['client_order_id']} sig={row['entry_order_id']} "
                f"size={row['size_usd']} verdict={resolution.get('verdict')}"
                + ("" if row.get("blocking") else "  (place would auto-retire this)")
            )
        lines.append(f"  blocking                 : {reconciliation['blockers']}")

        _print_block("SOLANA PILOT STATUS", lines)
        if reconciliation["blockers"]:
            print(
                "The lane is BLOCKED: `place` will refuse until everything above "
                "is resolved."
            )
        return EXIT_OK

    def _print_blocked(self, reconciliation: dict[str, Any]) -> None:
        lines = [
            "  A restart must not forget a signature, and a landed swap must not",
            "  be traded over. The lane refuses to submit anything new until each",
            "  item below is resolved.",
            "",
        ]
        for row in reconciliation["rows"]:
            if not row.get("blocking"):
                continue
            resolution = row.get("resolution", {})
            lines.append(
                f"    live_trades #{row['live_trade_id']}  status={row['status']}"
            )
            lines.append(f"      decision id     : {row['client_order_id']}")
            lines.append(f"      signature       : {row['entry_order_id']}")
            lines.append(
                f"      size / created  : {row['size_usd']} USD / "
                f"{row['created_at']}"
            )
            lines.append(f"      cluster says    : {resolution.get('verdict')}")
            if resolution.get("detail"):
                lines.append(f"      detail          : {resolution['detail']}")
        lines.extend(
            [
                "",
                "  To clear:",
                "   - landed: you hold USDC. Dispose of it, then set the row's",
                "     status by hand to record how it ended.",
                "   - failed_on_chain: the fee was paid and nothing executed.",
                "     Record the cost, then retire the row by hand.",
                "   - unresolved: re-run the resolver as the blockhash expires:",
                "       python -m scout.live.solana_pilot resolve --decision-id <id>",
                "     It retires the row automatically once the verdict becomes",
                "     definitively_not_submitted. NEVER rebuild in the meantime.",
            ]
        )
        _print_block("SOLANA PILOT LANE BLOCKED", lines)


# ----------------------------------------------------------------------
# Presentation helpers
# ----------------------------------------------------------------------
def _route_summary(quote: SolanaQuote) -> str:
    """Human-readable route, e.g. ``Raydium -> Orca (2 hops)``."""
    labels = [step.label or step.amm_key[:8] for step in quote.route_plan]
    if not labels:
        return "(no route steps reported)"
    return (
        " -> ".join(labels) + f" ({len(labels)} hop{'s' if len(labels) != 1 else ''})"
    )


def _resolution_summary(report: ResolutionReport) -> dict[str, Any]:
    return {
        "verdict": report.verdict,
        "signature": report.signature,
        "last_valid_block_height": report.last_valid_block_height,
        "rebuild_is_safe": report.rebuild_is_safe,
        "checked_at": report.checked_at,
        "slot": report.slot,
        "confirmation_status": report.confirmation_status,
        "on_chain_err": report.on_chain_err,
        "balances": dict(report.balances),
        "probes": [
            {
                "sweep": p.sweep,
                "outcome": p.outcome,
                "block_height": p.block_height,
                "blockhash_expired": p.blockhash_expired,
                "slot": p.slot,
                "confirmation_status": p.confirmation_status,
                "err": p.err,
                "error_type": p.error_type,
                "error": p.error,
            }
            for p in report.probes
        ],
    }


def _verdict_guidance(verdict: str, retired: bool) -> list[str]:
    if verdict == "landed":
        return [
            "",
            "  The swap EXECUTED. You hold the USDC. The row stays blocking",
            "  until you dispose of the position and resolve it by hand.",
        ]
    if verdict == "failed_on_chain":
        return [
            "",
            "  The transaction ran and FAILED. The fee was paid and no position",
            "  exists. Record the cost, then retire the row by hand.",
        ]
    if verdict == "definitively_not_submitted":
        return [
            "",
            "  The transaction can NEVER land: absent from the cluster and its",
            "  blockhash has expired."
            + (" The row has been retired and the lane is clear." if retired else ""),
            "  Rerunning `place` is safe — it builds a new transaction and asks",
            "  for a new authorization.",
        ]
    return [
        "",
        "  UNRESOLVED — we could not tell what happened. *** DO NOT REBUILD. ***",
        "  Run this command again once the blockhash has had time to expire;",
        "  absence only becomes definitive after that.",
    ]


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _sol_arg(raw: str) -> Decimal:
    """argparse converter for ``--sol``.

    Four rejections, all of which would otherwise reach the trading logic:

    - ``Decimal('x')`` raises InvalidOperation, an ArithmeticError, which is
      NOT one of the exceptions argparse converts into a usage error — a
      typo'd amount would surface as a traceback rather than a usage message.
    - ``Decimal('NaN')`` and ``Decimal('Infinity')`` PARSE. NaN then makes
      every comparison in the band checks False, so a NaN size passes every
      bound it is tested against; Infinity poisons the arithmetic instead.
    - Zero and negatives are not swaps.
    - A fractional lamport cannot be expressed in the quote's integer amount,
      so it would be silently truncated into a different swap.
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
    try:
        lamports_from_sol(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
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
        prog="solana_pilot",
        description="Execute ONE supervised SOL->USDC swap on Solana mainnet.",
        epilog="There is no `cancel`: a submitted Solana transaction either "
        "lands before its lastValidBlockHeight or it can never land. Use "
        "`resolve` to find out which.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    place = sub.add_parser("place", help="execute one supervised swap")
    place.add_argument(
        "--sol", type=_sol_arg, required=True, help="exact SOL amount to swap"
    )
    place.add_argument(
        "--simulate-only",
        action="store_true",
        help="full rehearsal including approval and signing; submits nothing",
    )
    place.add_argument(
        "--yes-i-am-rehearsing",
        action="store_true",
        help="required with --simulate-only; refused without it",
    )

    resolve = sub.add_parser(
        "resolve", help="ask the cluster what a persisted signature did"
    )
    resolve.add_argument("--decision-id", type=_decision_id_arg, required=True)

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
        if args.simulate_only and not args.yes_i_am_rehearsing:
            print(
                "REFUSED [args]: --simulate-only requires --yes-i-am-rehearsing, "
                "so a rehearsal is never mistaken for the real run."
            )
            return EXIT_REFUSED
        if args.yes_i_am_rehearsing and not args.simulate_only:
            # Refused rather than ignored: an operator who typed the rehearsal
            # flag believes they are rehearsing, and silently executing a real
            # swap under that belief is the worst outcome available here.
            print(
                "REFUSED [args]: --yes-i-am-rehearsing was passed without "
                "--simulate-only. This would be a REAL swap."
            )
            return EXIT_REFUSED

    settings = load_settings()

    # The DB must already exist. sqlite3 CREATES a database for any path it is
    # handed, so running this from the wrong directory does not fail — it
    # silently succeeds against an empty file, and every safety mechanism that
    # reads state finds nothing to object to: no kill switch, no prior
    # signature to reconcile. The lane would be wide open precisely because it
    # is looking at the wrong database.
    db_path = Path(settings.DB_PATH)
    if not db_path.exists():
        print(
            f"REFUSED [database]: no database at {db_path.resolve()}\n"
            "  DB_PATH resolves relative to the current directory, so this is "
            "almost always\n"
            "  the wrong working directory. Run the pilot from the deployment "
            "root.\n"
            "  Creating one here would disable the kill switch and the startup "
            "reconciliation\n"
            "  at the same time, silently."
        )
        return EXIT_REFUSED

    # The lock guards PLACEMENT only, and is taken for `place` alone.
    #
    # It exists to stop two processes submitting two swaps. `status` is
    # read-only and `resolve` only ever retires a row for a transaction that
    # can never land, so neither can do the thing the lock prevents — and
    # gating them would be actively harmful: a stale lock arises exactly when
    # an earlier run died with a signature possibly in flight, which is the
    # moment the operator most needs to look. A lock that blocks its own
    # recovery path is worse than no lock.
    lock_fd: int | None = None
    lock_path: Path | None = None
    if args.command == "place":
        try:
            lock_fd, lock_path = acquire_pilot_lock(db_path)
        except PilotLockHeld as held:
            print(
                f"REFUSED [lock]: another pilot run holds {held.lock_path}\n"
                f"  holder: {held.holder}\n"
                "  Two runs are two swaps, so only `place` is blocked.\n"
                "  `status` and `resolve` still work — use them to see what is "
                "outstanding.\n"
                "  Once the on-chain state is confirmed and that process is "
                "gone, delete\n"
                "  the lock file by hand."
            )
            return EXIT_BLOCKED

    # Everything past the acquire is inside the finally that releases it.
    # Database construction and initialize() can BOTH raise — a migration can
    # exceed busy_timeout, and a slow one invites Ctrl+C — and a lock leaked
    # there is permanent, blocking every later swap on a machine where nothing
    # is actually outstanding.
    try:
        db = Database(db_path, busy_timeout_ms=settings.SQLITE_BUSY_TIMEOUT_MS)
        await db.initialize()
        # Explicit per-request timeout. aiohttp's default total is five
        # minutes, which on this lane means a hung Jito POST stalls past the
        # blockhash expiry the run is racing — and every client here passes
        # its own timeout anyway, so this only ever binds a call site that
        # forgot to.
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=float(settings.SOLANA_HTTP_TIMEOUT_SEC))
        )
        try:
            # Two RPC clients on one session. The resolver reads from a
            # SINGLE pinned node (see `resolver_endpoint`) because its
            # definitive verdict combines two facts that must come from the
            # same view of the chain; everything else can use the general
            # endpoint. When the two URLs are equal this is the same target
            # twice, which is exactly what a deployment with one dedicated
            # node wants.
            resolver_url, _pinned = resolver_endpoint(settings)
            resolver_settings = settings.model_copy(
                update={"SOLANA_RPC_URL": resolver_url}
            )
            runner = PilotRunner(
                settings=settings,
                db=db,
                jupiter=JupiterClient(settings, session),
                rpc=SolanaRpcClient(settings, session),
                jito=JitoClient(settings, session),
                kill_switch=KillSwitch(db),
                resolver_rpc=SolanaRpcClient(resolver_settings, session),
            )
            if args.command == "place":
                return await runner.place(
                    sol=args.sol, simulate_only=args.simulate_only
                )
            if args.command == "resolve":
                return await runner.resolve(decision_id=args.decision_id)
            return await runner.status()
        finally:
            await session.close()
            await db.close()
    finally:
        if lock_fd is not None and lock_path is not None:
            release_pilot_lock(lock_fd, lock_path)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
